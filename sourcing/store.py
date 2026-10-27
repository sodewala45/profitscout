"""Writes matched suppliers to the shared `suppliers` table (one set per product + platform)."""

from __future__ import annotations

from datetime import datetime, timezone


def save_suppliers(sb, product_id: str, platform: str, offers: list[dict], method: str = "photo") -> None:
    rows = [
        {
            "tiktok_product_id": product_id,
            "platform": platform,
            "supplier_name": o["supplier_name"],
            "product_title": o["title"],
            "unit_cost_min": o["price_min"],
            "unit_cost_max": o["price_max"],
            "moq": o["moq"],
            "shipping_estimate_days": 20 if platform == "alibaba" else 10,
            "shipping_cost_estimate": None,
            "years_on_platform": o.get("supplier_years"),
            "supplier_trust_score": min((o.get("supplier_years") or 0) / 2, 5.0),
            "whitelabel_available": platform == "alibaba",
            "supplier_url": o["url"],
            "image_url": o.get("image_url"),
            "similarity": o["similarity"],
            "match_method": method,
            "external_product_id": o["product_id"],
            "orders": o.get("orders"),
            "rating": o.get("rating"),
            "verified_supplier": o.get("verified"),
            "sourced_at": datetime.now(timezone.utc).isoformat(),
        }
        for o in offers
    ]
    sb.table("suppliers").delete().eq("tiktok_product_id", product_id).eq("platform", platform).execute()
    sb.table("suppliers").insert(rows).execute()
