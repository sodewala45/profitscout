"""
Turn whatever the result page gives us (XHR JSON, embedded window.* JSON, or plain DOM)
into one normalised offer shape:

  {platform, product_id, title, price_min, price_max, moq, url, image_url,
   supplier_name, supplier_years, verified, orders, rating, welcome_deal}

Strategy: walk every JSON blob we saw and pick out objects that look like product cards.
That survives most front-end redesigns because the underlying data keys rarely change.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable


def _f(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"\d+(?:[.,]\d+)?", str(v).replace(",", ""))
    return float(m.group(0)) if m else None


def _abs(url: str | None) -> str | None:
    if not url:
        return None
    url = str(url)
    if url.startswith("//"):
        return "https:" + url
    return url


def _walk(obj: Any) -> Iterable[dict]:
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def json_blobs_from_html(html: str) -> list:
    """Embedded JSON: window._dida_config_, window.runParams, __INIT_DATA__, etc."""
    blobs = []
    # AliExpress search: the result cards live in "itemList":{"content":[...]}
    for m in re.finditer(r'"itemList"\s*:\s*\{', html):
        chunk = _balanced(html, m.end() - 1)
        if chunk:
            try:
                blobs.append(json.loads(chunk))
            except Exception:
                pass
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S | re.I):
        text = m.group(1)
        if len(text) < 2000 or ("productId" not in text and "offerId" not in text and "subject" not in text):
            continue
        for start in [i.start() for i in re.finditer(r"[\[{]", text)][:40]:
            chunk = _balanced(text, start)
            if chunk and len(chunk) > 1000:
                try:
                    blobs.append(json.loads(chunk))
                    break
                except Exception:
                    continue
    return blobs


def _balanced(s: str, start: int) -> str | None:
    open_c = s[start]
    close_c = "}" if open_c == "{" else "]"
    depth, in_str, esc = 0, False, False
    for i in range(start, min(len(s), start + 5_000_000)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


# ─── AliExpress ──────────────────────────────────────────────────────────────
def aliexpress_offers(blobs: list) -> list[dict]:
    out, seen = [], set()
    for blob in blobs:
        for d in _walk(blob):
            pid = d.get("productId") or d.get("product_id")
            if not pid or str(pid) in seen:
                continue
            prices = d.get("prices") if isinstance(d.get("prices"), dict) else {}
            sale = prices.get("salePrice") or {}
            orig = prices.get("originalPrice") or {}
            if not (prices or d.get("price") or d.get("salePrice") or d.get("target_sale_price")):
                continue
            # 85%+ off = new-user welcome deal (not a real cost). Coupon pricing: prefer the pre-coupon price.
            welcome = (_f(sale.get("discount")) or 0) >= 85
            if welcome or prices.get("builderType") == "skuCoupon":
                pmin = _f(orig.get("minPrice")) or (None if welcome else _f(sale.get("minPrice")))
            else:
                pmin = _f(sale.get("minPrice")) or _f(orig.get("minPrice"))
            pmin = pmin or _f(d.get("salePrice")) or _f(d.get("price")) or _f(d.get("target_sale_price"))
            if not pmin:
                continue
            title = d.get("title")
            title = title.get("displayTitle") if isinstance(title, dict) else (title or d.get("productTitle") or d.get("subject"))
            img = d.get("image")
            img = img.get("imgUrl") if isinstance(img, dict) else (img or d.get("imageUrl") or d.get("productImage"))
            store = d.get("store") or d.get("storeInfo") or {}
            trade = d.get("trade") or {}
            ev = d.get("evaluation") or {}
            seen.add(str(pid))
            item_id = int(pid) - 2**51 if str(pid).startswith("3256") else pid   # search ids are offset by 2^51
            out.append(
                {
                    "platform": "aliexpress",
                    "product_id": str(item_id),
                    "title": (title or "")[:300],
                    "price_min": round(pmin, 2),
                    "price_max": round(_f(orig.get("maxPrice")) or _f(orig.get("minPrice")) or pmin, 2),
                    "moq": 1,
                    "url": f"https://www.aliexpress.com/item/{item_id}.html",
                    "image_url": _abs(img),
                    "supplier_name": (store.get("storeName") if isinstance(store, dict) else None) or "AliExpress store",
                    "supplier_years": None,
                    "verified": False,
                    "orders": int(_f(trade.get("tradeDesc") if isinstance(trade, dict) else trade) or 0) or None,
                    "rating": _f(ev.get("starRating") if isinstance(ev, dict) else ev),
                    "welcome_deal": welcome,
                }
            )
    return out


# ─── Alibaba ─────────────────────────────────────────────────────────────────
def alibaba_offers(blobs: list) -> list[dict]:
    out, seen = [], set()
    for blob in blobs:
        for d in _walk(blob):
            pid = d.get("offerId") or d.get("productId") or d.get("id")
            title = d.get("subject") or d.get("title") or d.get("productTitle")
            if not pid or not title or str(pid) in seen or not isinstance(title, str):
                continue
            price_obj = d.get("price") if isinstance(d.get("price"), dict) else {}
            ladder = price_obj.get("productLadderPrices") or d.get("ladderPrice") or []
            prices = [_f(t.get("dollarPrice") or t.get("price")) for t in ladder if isinstance(t, dict)]
            prices = [p for p in prices if p]
            rng = price_obj.get("productRangePrices") or {}
            pmin = min(prices) if prices else (_f(rng.get("dollarPriceRangeLow")) or _f(d.get("priceMin")) or _f(d.get("price")) or _f(d.get("promotionPrice")))
            if not pmin:
                continue
            pmax = max(prices) if prices else (_f(rng.get("dollarPriceRangeHigh")) or pmin)
            url = d.get("productUrl") or d.get("detailUrl") or d.get("url") or f"https://www.alibaba.com/product-detail/_{pid}.html"
            img = d.get("mainImage") or d.get("imageUrl") or d.get("image") or d.get("picUrl")
            if isinstance(img, dict):
                img = img.get("normal") or img.get("big") or img.get("url")
            if not img and isinstance(d.get("mediaItems"), list) and d["mediaItems"]:
                m0 = d["mediaItems"][0]
                img = (m0.get("imageUrl") or {}).get("normal") if isinstance(m0.get("imageUrl"), dict) else m0.get("url")
            sup = d.get("supplierInfo") or d.get("company") or {}
            seen.add(str(pid))
            out.append(
                {
                    "platform": "alibaba",
                    "product_id": str(pid),
                    "title": re.sub(r"<[^>]+>", "", title)[:300],
                    "price_min": round(pmin, 2),
                    "price_max": round(pmax, 2),
                    "moq": int(_f(d.get("moq") or d.get("minOrderQuantity") or d.get("moqV2")) or 1),
                    "url": _abs(url),
                    "image_url": _abs(img),
                    "supplier_name": (sup.get("name") or sup.get("companyName") if isinstance(sup, dict) else None) or d.get("companyName") or "Alibaba supplier",
                    "supplier_years": _f((sup.get("joinYears") if isinstance(sup, dict) else None) or d.get("goldSupplierYears")),
                    "verified": bool((sup.get("verifiedSupplier") if isinstance(sup, dict) else None) or d.get("isVerified")),
                    "orders": None,
                    "rating": _f(d.get("reviewScore")),
                    "welcome_deal": False,
                }
            )
    return out


def alibaba_offers_from_dom(cards: list[dict]) -> list[dict]:
    """cards come from page.evaluate (anchor href, img src, text) when no JSON was found."""
    out, seen = [], set()
    for c in cards:
        m = re.search(r"_(\d{8,})\.html", c.get("href", "")) or re.search(r"/(\d{8,})", c.get("href", ""))
        if not m or m.group(1) in seen:
            continue
        prices = [float(x) for x in re.findall(r"\$\s?(\d+(?:\.\d+)?)", c.get("text", ""))]
        if not prices:
            continue
        moq = re.search(r"Min\.?\s*order:?\s*([\d,]+)", c.get("text", ""), re.I)
        seen.add(m.group(1))
        out.append(
            {
                "platform": "alibaba",
                "product_id": m.group(1),
                "title": (c.get("title") or c.get("text", "").split("\n")[0])[:300],
                "price_min": min(prices),
                "price_max": max(prices),
                "moq": int(moq.group(1).replace(",", "")) if moq else 1,
                "url": _abs(c.get("href")),
                "image_url": _abs(c.get("img")),
                "supplier_name": c.get("company") or "Alibaba supplier",
                "supplier_years": None,
                "verified": "verified" in c.get("text", "").lower(),
                "orders": None,
                "rating": None,
                "welcome_deal": False,
            }
        )
    return out
