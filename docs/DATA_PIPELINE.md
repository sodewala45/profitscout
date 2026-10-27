# Weekly data refresh (Kalodata → Supabase)

1. In Kalodata, open the same saved views every week (one per category) with the
   **same date-range length** (use 7 days; growth compares like with like).
2. Export Products CSV per category, and Creators CSV.
3. Put the files in `data/YYYY-MM-DD/`.
4. Check first, then import:
   ```
   python import_kalodata.py products "data/2026-11-02/*.csv" --dry-run
   python import_kalodata.py products "data/2026-11-02/*.csv" --rehost
   python import_kalodata.py creators data/2026-11-02/creators.csv
   ```
5. Find suppliers for the new products (see docs/PRESOURCING.md):
   ```
   python -m sourcing.presource --new
   ```
6. Week 1: stages show "Unknown/Emerging" and growth is blank (honest: no history yet).
   From week 2 on, growth and Emerging/Rising/Peaking/Saturated come from real snapshots.

Rules:
- Never show the Kalodata URL in the app.
- The UI must show "Data as of <data_end_date>" (column now exists).
- Products missing from the newest export for 3+ weeks should be set `is_active=false`.
