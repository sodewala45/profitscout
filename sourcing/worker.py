"""
LEGACY on-demand browser worker (Oxylabs Agent Browser). Replaced by sourcing/presource.py,
which is cheaper and needs no extra Railway service. Kept for reference.

Sourcing worker. Runs as its own Railway service (Dockerfile.worker).

  python -m sourcing.worker                       # process queued jobs forever (Railway start command)
  python -m sourcing.worker --test <product_id> --platform aliexpress   # one product, prints results
  python -m sourcing.worker --batch 100 --platform alibaba              # pre-source the top 100 products

Reliability rules ("zero error" in practice = every job ends in a clear state):
  - each attempt uses a fresh Oxylabs session/IP; up to SOURCING_MAX_ATTEMPTS attempts
  - challenge page → Blocked → retry with new IP after backoff
  - every failure saves a screenshot + HTML to SOURCING_DEBUG_DIR
  - job always finishes as done / no_match / failed, and failed or no_match jobs refund the credit
  - results cached per product+platform, so each product is sourced once for everybody
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import time
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from supabase import create_client

from . import config, match
from .browser import open_page
from .sites import SITE_FLOWS, Blocked, NoResults
from .store import save_suppliers

load_dotenv()
sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}


# ─── image download ─────────────────────────────────────────────────────────
async def _get(client: httpx.AsyncClient, url: str | None) -> bytes | None:
    if not url:
        return None
    try:
        r = await client.get(url, timeout=15)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
            return r.content
    except Exception:
        pass
    return None


def _thumb(url: str | None) -> str | None:
    return url


# ─── core ────────────────────────────────────────────────────────────────────
async def source_product(product: dict, platform: str) -> dict:
    flow = SITE_FLOWS[platform]
    ref_url = product.get("image_storage_url") or product.get("thumbnail_url")
    async with httpx.AsyncClient(headers=UA, follow_redirects=True) as client:
        ref = await _get(client, ref_url)
        if not ref:
            return {"status": "failed", "error": "product photo could not be downloaded (TikTok link expired: run importer with --rehost)"}

        last_err = "unknown"
        for attempt in range(1, config.MAX_ATTEMPTS + 1):
            t0 = time.time()
            try:
                async with open_page() as page:
                    offers = await flow(page, ref)
                print(f"  [{platform}] attempt {attempt}: {len(offers)} offers in {time.time()-t0:.0f}s")
                break
            except NoResults:
                return {"status": "no_match", "error": f"{platform} image search returned no products"}
            except Blocked:
                last_err = "challenge page"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"[:300]
            print(f"  [{platform}] attempt {attempt} failed: {last_err}")
            await asyncio.sleep(3 * attempt + random.random() * 3)
        else:
            return {"status": "failed", "error": last_err}

        offers = offers[:40]
        imgs = await asyncio.gather(*[_get(client, _thumb(o.get("image_url"))) for o in offers])

    scores = match.similarities(ref, list(imgs))
    best = match.rank(offers, scores, product.get("tiktok_price") or 0, config.MIN_SIMILARITY, config.KEEP_TOP)
    if not best:
        top = max(scores) if scores else 0
        return {"status": "no_match", "error": f"no {platform} result looked like the product (best similarity {top:.2f})"}

    _save(product["id"], platform, best)
    return {"status": "done", "count": len(best), "suppliers": best}


def _save(product_id: str, platform: str, offers: list[dict]) -> None:
    save_suppliers(sb, product_id, platform, offers, "photo")


def _product(product_id: str) -> dict:
    return (
        sb.table("tiktok_products")
        .select("id, product_name, tiktok_price, thumbnail_url, image_storage_url")
        .eq("id", product_id).single().execute().data
    )


# ─── job queue ───────────────────────────────────────────────────────────────
async def _run_job(job: dict) -> None:
    jid = job["id"]
    try:
        res = await source_product(_product(job["product_id"]), job["platform"])
    except Exception as e:
        res = {"status": "failed", "error": f"{type(e).__name__}: {e}"[:300]}
    status = res["status"]
    sb.table("sourcing_jobs").update(
        {"status": status, "error": res.get("error"), "result_count": res.get("count", 0),
         "finished_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", jid).execute()
    if status != "done" and job.get("charged") and job.get("user_email"):
        sb.rpc("refund_credit", {"p_email": job["user_email"], "p_product_id": str(job["product_id"]),
                                 "p_platform": job["platform"]}).execute()
    print(f"job {jid} → {status} {res.get('error') or ''}")


async def run_forever() -> None:
    print(f"sourcing worker up · concurrency {config.CONCURRENCY}")
    running: set[asyncio.Task] = set()
    while True:
        while len(running) < config.CONCURRENCY:
            claimed = sb.rpc("claim_sourcing_job", {}).execute().data
            job = claimed[0] if isinstance(claimed, list) and claimed else (claimed if isinstance(claimed, dict) and claimed.get("id") else None)
            if not job:
                break
            t = asyncio.create_task(_run_job(job))
            running.add(t)
            t.add_done_callback(running.discard)
        await asyncio.sleep(config.POLL_SECONDS)


async def run_batch(limit: int, platform: str) -> None:
    prods = (
        sb.table("tiktok_products").select("id").eq("is_active", True)
        .order("virality_score", desc=True).limit(limit).execute().data
    )
    for p in prods:
        if sb.table("suppliers").select("id").eq("tiktok_product_id", p["id"]).eq("platform", platform).limit(1).execute().data:
            continue
        sb.table("sourcing_jobs").insert({"product_id": p["id"], "platform": platform, "status": "queued", "charged": False}).execute()
    print(f"queued up to {len(prods)} {platform} jobs; the worker will process them")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--test")
    ap.add_argument("--batch", type=int)
    ap.add_argument("--platform", default="aliexpress", choices=list(SITE_FLOWS))
    a = ap.parse_args()
    if a.test:
        out = asyncio.run(source_product(_product(a.test), a.platform))
        print(json.dumps(out, indent=2, default=str))
    elif a.batch:
        asyncio.run(run_batch(a.batch, a.platform))
    else:
        asyncio.run(run_forever())
