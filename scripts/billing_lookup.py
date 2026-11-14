"""Read-only: show why an account is (or isn't) on Pro.

    SUPABASE_URL=... SUPABASE_KEY=<service key> python scripts/billing_lookup.py someone@example.com

Prints users.plan (and same-address rows in other letter case), subscriptions, user_credits and the
latest webhook_events. Only SELECTs; prints no secrets (env vars are shown as set / not set).
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402
from supabase import create_client  # noqa: E402

from billing_diag import billing_snapshot  # noqa: E402


def _plan_for_variant(v: str):
    ids = lambda *names: {p.strip() for n in names for p in (os.getenv(n) or "").split(",") if p.strip()}
    if v in ids("LEMONSQUEEZY_ULTIMATE_PRODUCT_ID", "LEMON_ULTIMATE_VARIANT_IDS"):
        return "ultimate"
    if v in ids("LEMONSQUEEZY_PRODUCT_ID", "LEMON_PRO_VARIANT_IDS"):
        return "pro"
    return None


def main() -> int:
    load_dotenv()
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    print(json.dumps(billing_snapshot(client, sys.argv[1], _plan_for_variant), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
