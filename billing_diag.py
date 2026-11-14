"""Read-only billing snapshot for one email: "I paid but I'm still Free".

Used by GET /admin/billing/lookup (admins only) and by scripts/billing_lookup.py.
It only SELECTs. It never prints secrets: env vars are reported as set / not set, and only the
variant / product ids (which are public in every checkout URL) are shown.
"""
from __future__ import annotations

import os
from typing import Any

from security import normalize_email, normalize_plan

ID_VARS = ("LEMONSQUEEZY_PRODUCT_ID", "LEMON_PRO_VARIANT_IDS", "LEMON_PRO_PRODUCT_IDS",
           "LEMONSQUEEZY_ULTIMATE_PRODUCT_ID", "LEMON_ULTIMATE_VARIANT_IDS", "LEMON_ULTIMATE_PRODUCT_IDS")
SECRET_VARS = ("LEMONSQUEEZY_WEBHOOK_SECRET", "LEMONSQUEEZY_API_KEY", "LEMONSQUEEZY_STORE_ID",
               "CHECKOUT_SIGNING_SECRET")
USER_COLS = ("user_email", "plan", "id", "user_id", "created_at", "updated_at")


def _safe(fn) -> Any:
    try:
        return fn()
    except Exception as e:  # report the failure, keep going
        return {"error": type(e).__name__ + ": " + str(e)[:200]}


def billing_snapshot(client, email: str, plan_for_variant=None, events_limit: int = 30) -> dict:
    email = normalize_email(email)
    out: dict = {"email": email}

    def users():
        exact = client.table("users").select("*").eq("user_email", email).execute().data or []
        # Same address in another case / with spaces would be invisible to the API's exact match.
        similar = client.table("users").select("*").ilike("user_email", email.replace("\\", "\\\\")
                                                             .replace("%", "\\%").replace("_", "\\_")).execute().data or []
        pick = lambda r: {k: r.get(k) for k in USER_COLS if k in r}
        return {"exact": [pick(r) for r in exact],
                "case_variants": [pick(r) for r in similar if r.get("user_email") != email]}
    out["users"] = _safe(users)
    rows = out["users"].get("exact") if isinstance(out["users"], dict) else None
    out["plan_api_uses"] = normalize_plan(rows[0].get("plan")) if rows else "free (no users row)"

    def subs():
        rs = client.table("subscriptions").select("*").eq("user_email", email).execute().data or []
        for r in rs:
            if plan_for_variant:
                r["variant_recognised_as"] = plan_for_variant(str(r.get("variant_id") or "")) or "UNKNOWN"
        return rs
    out["subscriptions"] = _safe(subs)
    out["credits"] = _safe(lambda: client.table("user_credits").select("*").eq("user_email", email).execute().data or [])

    sub_ids = [str(r.get("ls_subscription_id")) for r in out["subscriptions"]] if isinstance(out["subscriptions"], list) else []

    def events():
        rs = (client.table("webhook_events").select("*").order("id", desc=True).limit(500).execute().data or [])
        mine = [r for r in rs if any(f":{sid}:" in str(r.get("id") or "") for sid in sub_ids)]
        return {"for_this_users_subscriptions": mine[:events_limit],
                "latest_any_user": rs[:events_limit]}
    out["webhook_events"] = _safe(events)

    out["config"] = {
        "recognised_ids": {n: [p.strip() for p in (os.getenv(n) or "").split(",") if p.strip()] for n in ID_VARS},
        "secrets_set": {n: bool(os.getenv(n)) for n in SECRET_VARS},
    }
    return out
