# Pre-sourcing suppliers (replaces the on-demand photo worker)

Suppliers are found once per product, in a batch, after each Kalodata import.
Users only read saved results: no waiting, no live searches, no credit spent on a product with no suppliers.

## Why not the Agent Browser worker
Agent Browser is billed per GB of browser traffic. The Web Scraper API is billed per successful
page (about $1.15 to $1.35 per 1,000), and the old pipelines already use it.
Alibaba's photo upload is a plain web request, so no browser is needed at all.

## How each product is sourced
| Platform | Step 1 | Step 2 (only if step 1 finds no look-alike) |
|---|---|---|
| Alibaba | Photo search: upload the photo to Alibaba (free), fetch results via Oxylabs `alibaba` source | Text search via `alibaba_search` |
| AliExpress | Text search via `aliexpress_search`, keyword cut from the title (no AI) | none |

Every candidate photo is compared with the TikTok photo (CLIP + pHash, runs on your PC, free).
Only results at or above `SOURCING_MIN_SIMILARITY` (default 0.80) are saved, top 5 per platform,
and anything priced at 70% or more of the TikTok price is dropped (no margin).

Cost: 1 to 3 Oxylabs results per product for both platforms, roughly $0.003 to $0.005.
The free trial's 2,000 results cover roughly 600 to 900 products.

## Setup (once)
1. Oxylabs dashboard: Web Scraper API, then create an API user. Put it in `.env`:
   `OXYLABS_USER=...` and `OXYLABS_PASS=...`
2. `pip install -r requirements-presource.txt` (downloads CLIP weights on first run, about 600 MB)
3. Railway API service: leave `PRESOURCE_ONLY` unset (defaults to true). `PHOTO_WORKER_ENABLED` stays false.

## Every week (after the Kalodata import)
```
python import_kalodata.py products "data/2026-11-02/*.csv" --rehost
python -m sourcing.presource --new --dry-run          # see what will be sourced
python -m sourcing.presource --new --limit 20         # first time: small test
python -m sourcing.presource --new                    # everything new
python -m sourcing.presource --stale-days 30          # monthly: refresh prices
```
`--max-requests` (default 300) is a hard cap on paid Oxylabs results per run.
Each run logs one row per product/platform in `sourcing_jobs` (done / no_match / failed), so
`--new` never pays twice for the same product. Failed rows are retried on the next run.

## What users see
- Suppliers saved: shown instantly (credits work as before).
- Not sourced yet: "suppliers for this product are being prepared", no credit used.
- Sourced but nothing matched: "No supplier matched this product's photo", no credit used.
