"""
Weekly data refresh: Kalodata exports -> Supabase -> supplier pre-sourcing.

  python scripts/weekly_refresh.py data/2026-11-09            # full run
  python scripts/weekly_refresh.py data/2026-11-09 --dry-run  # check files, write nothing, spend nothing

Folder layout (one Kalodata export per file, same 7-day date range every week):
  data/2026-11-09/products_*.csv   one per category view
  data/2026-11-09/creators.csv     optional
  data/2026-11-09/shops.csv        optional

Runs on your PC (CLIP needs ~600 MB and is free locally). Needs .env with SUPABASE_URL,
SUPABASE_KEY (service role) and OXYLABS_WEB_API_KEY.
"""
import glob
import os
import subprocess
import sys


def run(args):
    print("\n$ " + " ".join(args))
    r = subprocess.run([sys.executable, *args])
    if r.returncode != 0:
        sys.exit(f"step failed: {' '.join(args)}")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    folder = sys.argv[1].rstrip("/")
    dry = "--dry-run" in sys.argv
    products = sorted(glob.glob(f"{folder}/products_*.csv"))
    if not products:
        sys.exit(f"no products_*.csv in {folder}")

    run(["import_kalodata.py", "products", *products, "--dry-run"])
    if not dry:
        run(["import_kalodata.py", "products", *products, "--rehost", "--deactivate"])
    for kind in ("creators", "shops"):
        f = f"{folder}/{kind}.csv"
        if os.path.exists(f):
            run(["import_kalodata.py", kind, f] + (["--dry-run"] if dry else []))
    run(["-m", "sourcing.presource", "--new"] + (["--dry-run"] if dry else ["--max-requests", os.getenv("PRESOURCE_MAX_REQUESTS", "1500")]))
    print("\nDone. Open the app: new products show 'Sales data to <date>' and suppliers as they finish.")


if __name__ == "__main__":
    main()
