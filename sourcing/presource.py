"""
Pre-sourcing: find suppliers for every product ONCE, in a batch, right after a Kalodata import.
Users never trigger a search; the app only reads the saved suppliers.

  python -m sourcing.presource --new                 # products never sourced (normal weekly run)
  python -m sourcing.presource --new --limit 20      # small test first
  python -m sourcing.presource --stale-days 30       # also redo products sourced > 30 days ago
  python -m sourcing.presource --product <id>        # one product, prints what it found
  python -m sourcing.presource --new --dry-run       # list what would be sourced, spend nothing

Per product and platform (matching off, the default): photo search only, the same list a person
gets from the camera button on alibaba.com / aliexpress.us. Top 3 in the site's order are saved.
Oxylabs is only used when a site blocks the direct request (about 1 paid result then).

Cost guard: --max-requests (default 300) stops the run before it can overspend.
Typical cost: 1-2 results per product per platform, about $0.003-0.005 per product for both.

Needs OXYLABS_WEB_API_KEY (the only Oxylabs credential). --allow-no-oxylabs runs the free direct
searches only (blocked pages are then 'failed', never paid for).

Exit codes (GitHub Actions marks the run failed on anything but 0):
  0  finished (or stopped at --max-requests; the next run carries on)
  2  OXYLABS_WEB_API_KEY missing
  3  stopped: Oxylabs refused the key / quota used up / request rejected
  4  nothing worked: every attempted product failed (marketplaces blocked the runner)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv
from supabase import create_client

from . import config, match
from .http_sources import aliexpress_by_image, aliexpress_by_text, alibaba_by_image, alibaba_by_text, keyword_from_title
from . import oxylabs
from .oxylabs import Budget, BudgetExhausted, OxylabsError
from .store import clear_auto_suppliers, save_candidates, save_suppliers

from db_paging import fetch_all  # repo root (run as python -m sourcing.presource)

load_dotenv()
class _LazyClient:
    """Supabase client made on first use, so importing this module (tests, --help) needs no env."""
    _client = None

    def __getattr__(self, name):
        if _LazyClient._client is None:
            _LazyClient._client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
        return getattr(_LazyClient._client, name)


sb = _LazyClient()
IMG_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
PLATFORMS = ("alibaba", "aliexpress")
# One Supabase client is shared by all workers; its HTTP/2 connection is not thread-safe
# (on Windows: "WinError 10035 ... non-blocking socket"). Every database call takes this lock.
DB_LOCK = threading.Lock()


def _download(client: httpx.Client, url: str | None) -> bytes | None:
    if not url:
        return None
    try:
        r = client.get(url, timeout=15)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
            return r.content
    except Exception:
        pass
    return None


def _check(offers: list[dict], ref: bytes, price: float, client: httpx.Client) -> tuple[list[dict], list[dict], float]:
    offers = offers[:40]
    imgs = [_download(client, o.get("image_url")) for o in offers]
    scores = match.similarities(ref, imgs)
    best = match.rank(offers, scores, price, config.MIN_SIMILARITY, config.KEEP_TOP)
    near = match.rank(offers, scores, price, config.REVIEW_MIN_SIMILARITY, config.REVIEW_KEEP, max_sim=config.MIN_SIMILARITY)
    return best, near, (max(scores) if scores else 0.0)


def source_one(product: dict, platform: str, budget: Budget, client: httpx.Client) -> dict:
    ref = _download(client, product.get("image_storage_url") or product.get("thumbnail_url"))
    if not ref:
        return {"status": "failed", "error": "product photo missing or expired (import with --rehost)"}
    price = product.get("tiktok_price") or 0
    kw = keyword_from_title(product.get("product_name") or "")
    tried, top, near_all = [], 0.0, []
    answered = False          # did the marketplace actually return a result list?

    photo = (lambda: alibaba_by_image(ref, budget)) if platform == "alibaba" else (lambda: aliexpress_by_image(ref, budget))
    text = (lambda: alibaba_by_text(kw, budget)) if platform == "alibaba" else (lambda: aliexpress_by_text(kw, budget))
    if config.MATCHING:
        steps = [("photo", photo), ("text", text)]
    else:
        # No photo check: a keyword search returns look-alike categories, not the product, and
        # nothing would catch it. Photo search only (tried twice); otherwise no_match, so the
        # product shows "no suppliers" instead of wrong ones.
        steps = [("photo", photo), ("photo-retry", photo)]

    for method, run in steps:
        try:
            offers = run()
        except OxylabsError:
            raise
        except Exception as e:
            tried.append(f"{method}: {type(e).__name__}: {e}"[:160])
            continue
        answered = True
        if not config.MATCHING:
            if not offers and method == "photo":
                tried.append("photo: 0 results")
                continue
            best = match.top_raw(offers, price, config.KEEP_TOP)
            tried.append(f"{method}: {len(offers)} results, kept {len(best)}")
            if best:
                with DB_LOCK:
                    save_suppliers(sb, product["id"], platform, best, "auto")
                return {"status": "done", "method": method, "count": len(best), "tried": tried, "review": 0}
            break   # photo search answered, nothing usable: retrying gives the same list
        best, near, s = _check(offers, ref, price, client)
        near_all += [dict(o, _method=method) for o in near]
        top = max(top, s)
        tried.append(f"{method}: {len(offers)} results, best look-alike {s:.2f}")
        if best:
            with DB_LOCK:
                save_suppliers(sb, product["id"], platform, best, method)
            _save_review(product["id"], platform, near_all)
            return {"status": "done", "method": method, "count": len(best), "tried": tried, "review": len(near_all)}
    if not answered:
        # Blocked / challenge page / no Oxylabs: NOT a "no match". Old rows stay, and the
        # product is picked up again by the next run (failed jobs are not counted as sourced).
        return {"status": "failed", "error": "; ".join(tried) or "search did not answer"}
    if config.MATCHING:
        _save_review(product["id"], platform, near_all)
    elif answered:            # only when the search really ran; a block keeps the old rows
        try:
            with DB_LOCK:
                clear_auto_suppliers(sb, product["id"], platform)
        except Exception as e:
            print(f"  old suppliers not cleared: {e}")
    return {"status": "no_match", "error": "; ".join(tried) or "no results", "best": round(top, 2), "review": len(near_all)}


def _save_review(product_id, platform: str, near: list[dict]) -> None:
    try:
        with DB_LOCK:
            save_candidates(sb, product_id, platform, near)
    except Exception as e:                      # the review queue must never break sourcing
        print(f"  review queue not saved: {e}")


def _record(product_id: str, platform: str, res: dict) -> None:
    with DB_LOCK:
        _record_unlocked(product_id, platform, res)


def _record_unlocked(product_id: str, platform: str, res: dict) -> None:
    sb.table("sourcing_jobs").insert({
        "product_id": str(product_id), "platform": platform, "status": res["status"], "charged": False,
        "error": (res.get("error") or "; ".join(res.get("tried", [])))[:500],
        "result_count": res.get("count", 0), "finished_at": datetime.now(timezone.utc).isoformat(),
    }).execute()


def _targets(new: bool, stale_days: int | None, limit: int, platforms: tuple) -> list[tuple[dict, str]]:
    # Page through everything: PostgREST silently caps one response at max-rows (default 1000),
    # which made already-sourced products look new and re-sourced them (paid Oxylabs calls).
    prods = fetch_all(lambda: (
        sb.table("tiktok_products")
        .select("id, product_name, tiktok_price, thumbnail_url, image_storage_url, virality_score")
        .eq("is_active", True).order("virality_score", desc=True).order("id")
    ), max_rows=20000)
    # NB: --stale-days 0 means "redo everything", so test for None, not falsiness.
    since = (datetime.now(timezone.utc) - timedelta(days=stale_days)).isoformat() if stale_days is not None else None
    out = []
    for platform in platforms:
        rows = fetch_all(lambda: (
            sb.table("sourcing_jobs").select("id, product_id, finished_at")
            .eq("platform", platform).in_("status", ["done", "no_match"]).order("id")
        ))
        done = {}
        for row in rows:
            done[str(row["product_id"])] = max(done.get(str(row["product_id"]), ""), row.get("finished_at") or "")
        for p in prods:
            last = done.get(str(p["id"]))
            if last and (not since or last >= since):
                continue
            if new and last and not since:
                continue
            out.append((p, platform))
    # interleave platforms so a limited run covers both
    out.sort(key=lambda t: -(t[0].get("virality_score") or 0))
    return out[:limit] if limit else out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--new", action="store_true", help="products never sourced")
    ap.add_argument("--stale-days", type=int, help="also redo products sourced more than N days ago")
    ap.add_argument("--product", help="one product id")
    ap.add_argument("--platform", choices=["both", *PLATFORMS], default="both")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-requests", type=int, default=300, help="hard cap on paid Oxylabs results this run")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=int(os.getenv("SOURCING_WORKERS", "1")),
                    help="products sourced at the same time (default 1: one at a time)")
    ap.add_argument("--allow-no-oxylabs", action="store_true",
                    help="run without OXYLABS_WEB_API_KEY (free direct searches only)")
    a = ap.parse_args()
    if not oxylabs.configured() and not (a.allow_no_oxylabs or a.dry_run):
        print("::error::OXYLABS_WEB_API_KEY is empty. Add it as a repo secret (Settings > Secrets and variables > "
              "Actions), or pass --allow-no-oxylabs for a free-only run.")
        sys.exit(2)
    platforms = PLATFORMS if a.platform == "both" else (a.platform,)
    budget = Budget(a.max_requests)

    if a.product:
        p = (sb.table("tiktok_products").select("id, product_name, tiktok_price, thumbnail_url, image_storage_url")
             .eq("id", a.product).single().execute().data)
        targets = [(p, pl) for pl in platforms]
    elif a.new or a.stale_days is not None:
        targets = _targets(a.new, a.stale_days, a.limit, platforms)
    else:
        ap.error("use --new, --stale-days N, or --product ID")

    print(f"{len(targets)} product/platform pairs to source · budget {a.max_requests} Oxylabs results")
    if a.dry_run:
        for p, pl in targets[:50]:
            print(f"  {pl:10} {p['id']}  kw='{keyword_from_title(p.get('product_name') or '')}'")
        return

    stats = {"done": 0, "no_match": 0, "failed": 0}
    t0 = time.time()
    stop = threading.Event()
    stopped: dict = {}                              # the first fatal Oxylabs error

    def work(item):
        i, (p, pl) = item
        if stop.is_set():
            return
        try:
            with httpx.Client(headers=IMG_UA, follow_redirects=True) as client:
                res = source_one(p, pl, budget, client)
        except OxylabsError as e:
            with lock:
                if not stopped:
                    stopped["error"] = e
                    print(f"\nSTOPPED: {oxylabs.redact(e)}")
            stop.set()
            return
        except Exception as e:                      # one bad product must not stop the run
            res = {"status": "failed", "error": oxylabs.redact(f"{type(e).__name__}: {e}")[:300]}
        with lock:
            stats[res["status"]] += 1
        if not a.product:
            try:
                _record(p["id"], pl, res)
            except Exception as e:
                print(f"  job not recorded: {type(e).__name__}: {e}"[:200])
        print(f"[{i}/{len(targets)}] {pl:10} {res['status']:8} {(p.get('product_name') or '')[:50]}  {res.get('error') or res.get('method', '')}")
        if a.product:
            print(json.dumps(res, indent=2, default=str))

    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as pool:
        list(pool.map(work, enumerate(targets, 1)))
    print(f"\n{stats} · {budget.used} Oxylabs results used · {time.time() - t0:.0f}s")
    sys.exit(exit_code(stats, stopped.get("error"), len(targets)))


def exit_code(stats: dict, stopped_by: Exception | None, n_targets: int) -> int:
    """Turn the run's outcome into an exit code and a GitHub annotation (see the module docstring)."""
    attempted = sum(stats.values())
    left = max(0, n_targets - attempted)
    if isinstance(stopped_by, BudgetExhausted):
        print(f"::warning::Stopped at the Oxylabs budget ({stopped_by}). {left} product/platform pairs left; "
              "run Find suppliers again (mode new) to continue.")
        return 0
    if stopped_by is not None:
        print(f"::error::Find suppliers stopped: {oxylabs.redact(stopped_by)}. {left} pairs were not sourced.")
        return 3
    if attempted and stats["done"] + stats["no_match"] == 0:
        print(f"::error::All {attempted} attempted pairs failed (marketplace blocked or broken). See the log lines above.")
        return 4
    if stats["failed"]:
        print(f"::warning::{stats['failed']} of {attempted} pairs failed; they are retried on the next run.")
    return 0


if __name__ == "__main__":
    main()
