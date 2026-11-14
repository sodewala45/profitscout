# Weekly data refresh (Kalodata -> Supabase -> suppliers)

Run on your PC every week, same day. Takes ~10 minutes plus sourcing time.

## 1. Export from Kalodata
- Products: open each saved category view, date range **Last 7 days** (always 7, growth compares like with like), Export CSV.
  Save as `data/YYYY-MM-DD/products_<category>.csv`.
- Creators (optional): Creators ranking, Last 7 days, Export -> `data/YYYY-MM-DD/creators.csv`.
- Shops (optional): Shops ranking (New shops), Last 7 days, Export -> `data/YYYY-MM-DD/shops.csv`.

## 2. Run
```
python scripts/weekly_refresh.py data/2026-11-09 --dry-run   # check columns, nothing written, nothing spent
python scripts/weekly_refresh.py data/2026-11-09             # import + retire stale rows + source new products
```
That runs, in order:
1. `import_kalodata.py products ... --rehost --deactivate` — upserts products, keeps weekly snapshots,
   copies images to Supabase Storage (TikTok image links expire), hides v1 rows and products not seen for 3+ weeks.
2. `import_kalodata.py creators` / `shops` if those files exist. Unknown columns are printed, not lost silently.
3. `python -m sourcing.presource --new` — photo-matches every product that has never been sourced
   (Alibaba photo search + text search, AliExpress text search, CLIP + pHash check, >= 0.80 similarity, top 5, deduped).

## First run after this update
- Run the SQL in `supabase/migrations/20261106_launch_hardening.sql` once.
- Re-source everything once so every supplier row has a photo-match score:
  `python -m sourcing.presource --stale-days 0 --max-requests 3000`
  (old keyword-only rows have no score and are no longer shown or charged for).

## Monthly
`python -m sourcing.presource --stale-days 30` refreshes supplier prices.

## Rules
- Never show the Kalodata URL in the app.
- The UI shows "Sales data to <data_end_date>".
- Week 1 after the switch: growth is blank and stages show Emerging/Unknown (no history yet). From week 2 they're real.

## Weekly update without a PC (admin page)
1. Sign in with an `ADMIN_EMAILS` email and open **tikprofitpro.shop/admin/upload**.
2. Drop the Kalodata CSVs (products, creators, shops). Click **Check files**, read the counts, then **Import**.
3. Click **Find suppliers** (New products only). It runs `.github/workflows/presource.yml` on GitHub Actions.
4. Review near-misses at **/admin/review**.

One-time setup:
- GitHub repo `sodewala45/profitscout` > Settings > Secrets and variables > Actions > New repository secret:
  `SUPABASE_URL`, `SUPABASE_KEY` (service_role), `OXYLABS_WEB_API_KEY` (Oxylabs Web API key; username/password is no longer used).
- A GitHub token saved on Render as `GITHUB_ACTIONS_TOKEN`. A fine-grained token must be created **by the repo owner's account**
  (resource owner `sodewala45`, repository `profitscout`, permission Actions: Read and write). A fine-grained token made by a
  collaborator account can't reach a repo owned by another account, which gives the 403 on "Find suppliers".
  Alternative: a classic token with the `repo` and `workflow` scopes from an account with write access.
