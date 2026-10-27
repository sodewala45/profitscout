"""
Pre-sourcing: find suppliers for every product ONCE, in a batch, right after a Kalodata import.
Users never trigger a search; the app only reads the saved suppliers.

  python -m sourcing.presource --new                 # products never sourced (normal weekly run)
  python -m sourcing.presource --new --limit 20      # small test first
  python -m sourcing.presource --stale-days 30       # also redo products sourced > 30 days ago
  python -m sourcing.presource --product <id>        # one product, prints what it found
  python -m sourcing.presource --new --dry-run       # list what would be sourced, spend nothing

Per product and platform (Oxylabs Web Scraper API, ~1 paid result per step):
  Alibaba:    photo search -> if no photo match, text search
  AliExpress: text search (keyword from the title, no AI)
Every candidate is checked against the TikTok photo with CLIP + pHash; only real look-alikes are saved.

Cost guard: --max-requests (default 300) stops the run before it can overspend.
Typical cost: 1-2 results per product per platform, about $0.003-0.005 per product for both.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv
from supabase import create_client

from . import config, match
from .http_sources import aliexpress_by_image, aliexpress_by_text, alibaba_by_image, alibaba_by_text, keyword_from_title
from .oxylabs import Budget, OxylabsError
from .store import save_suppliers

load_dotenv()
sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
IMG_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
PLATFORMS = ("alibaba", "aliexpress")


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


def _check(offers: list[dict], ref: bytes, price: float, client: httpx.Client) -> tuple[list[dict], float]:
    offers = offers[:40]
    imgs = [_download(client, o.get("image_url")) for o in offers]
    scores = match.similarities(ref, imgs)
    best = match.rank(offers, scores, price, config.MIN_SIMILARITY, config.KEEP_TOP)
    return best, (max(scores) if scores else 0.0)


def source_one(product: dict, platform: str, budget: Budget, client: httpx.Client) -> dict:
    ref = _download(client, product.get("image_storage_url") or product.get("thumbnail_url"))
    if not ref:
        return {"status": "failed", "error": "product photo missing or expired (import with --rehost)"}
    price = product.get("tiktok_price") or 0
    kw = keyword_from_title(product.get("product_name") or "")
    tried, top = [], 0.0

    steps = [("photo", lambda: alibaba_by_image(ref, budget)), ("text", lambda: alibaba_by_text(kw, budget))] \
        if platform == "alibaba" else [("photo", lambda: aliexpress_by_image(ref, budget)), ("text", lambda: aliexpress_by_text(kw, budget))]

    for method, run in steps:
        try:
            offers = run()
        except OxylabsError:
            raise
        except Exception as e:
            tried.append(f"{method}: {type(e).__name__}: {e}"[:160])
            continue
        best, s = _check(offers, ref, price, client)
        top = max(top, s)
        tried.append(f"{method}: {len(offers)} results, best look-alike {s:.2f}")
        if best:
            save_suppliers(sb, product["id"], platform, best, method)
            return {"status": "done", "method": method, "count": len(best), "tried": tried}
    return {"status": "no_match", "error": "; ".join(tried) or "no results", "best": round(top, 2)}


def _record(product_id: str, platform: str, res: dict) -> None:
    sb.table("sourcing_jobs").insert({
        "product_id": str(product_id), "platform": platform, "status": res["status"], "charged": False,
        "error": (res.get("error") or "; ".join(res.get("tried", [])))[:500],
        "result_count": res.get("count", 0), "finished_at": datetime.now(timezone.utc).isoformat(),
    }).execute()


def _targets(new: bool, stale_days: int | None, limit: int, platforms: tuple) -> list[tuple[dict, str]]:
    prods = (
        sb.table("tiktok_products")
        .select("id, product_name, tiktok_price, thumbnail_url, image_storage_url, virality_score")
        .eq("is_active", True).order("virality_score", desc=True).limit(5000).execute().data
    )
    since = (datetime.now(timezone.utc) - timedelta(days=stale_days)).isoformat() if stale_days else None
    out = []
    for platform in platforms:
        q = sb.table("sourcing_jobs").select("product_id, finished_at").eq("platform", platform).in_("status", ["done", "no_match"])
        done = {}
        for row in q.limit(20000).execute().data:
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
    a = ap.parse_args()
    platforms = PLATFORMS if a.platform == "both" else (a.platform,)
    budget = Budget(a.max_requests)

    if a.product:
        p = (sb.table("tiktok_products").select("id, product_name, tiktok_price, thumbnail_url, image_storage_url")
             .eq("id", a.product).single().execute().data)
        targets = [(p, pl) for pl in platforms]
    elif a.new or a.stale_days:
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
    with httpx.Client(headers=IMG_UA, follow_redirects=True) as client:
        for i, (p, pl) in enumerate(targets, 1):
            try:
                res = source_one(p, pl, budget, client)
            except OxylabsError as e:
                print(f"\nSTOPPED: {e}")
                break
            stats[res["status"]] += 1
            if not a.product:
                _record(p["id"], pl, res)
            print(f"[{i}/{len(targets)}] {pl:10} {res['status']:8} {(p.get('product_name') or '')[:50]}  {res.get('error') or res.get('method', '')}")
            if a.product:
                print(json.dumps(res, indent=2, default=str))
    print(f"\n{stats} · {budget.used} Oxylabs results used · {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
