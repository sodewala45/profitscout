"""
Supplier sources over plain HTTP + Oxylabs Web Scraper API. No browser.

  alibaba_by_image(photo)   upload the photo to Alibaba's image-search storage and call Alibaba's own
                            image-search JSON API. Both are plain web requests: free. Oxylabs is only
                            used as a fallback if Alibaba rate-limits us.
  alibaba_by_text(title)    Oxylabs source alibaba_search (1 result)
  aliexpress_by_text(title) Oxylabs source aliexpress_search (1 result)

Every list is then photo-checked against the TikTok picture with CLIP + pHash (match.py),
so text search is only a way to get candidates; the photo decides what is a match.
"""

from __future__ import annotations

import json
import random
import re
import string
import time
from urllib.parse import quote

import httpx

from . import parse
from .oxylabs import Budget, scrape

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Origin": "https://www.alibaba.com",
    "Referer": "https://www.alibaba.com/",
}
SIGN_URL = "https://open-s.alibaba.com/openservice/ossUploadSecretKeyDataService?appKey=a5m1ismomeptugvfmkkjnwwqnwyrhpb1&appName=magellan"


def _rand(k: int) -> str:
    return "".join(random.choices(string.ascii_letters, k=k))


def alibaba_upload(photo: bytes) -> str:
    """Upload exactly like alibaba.com's camera button does. Returns the imageAddress key."""
    sign = httpx.get(SIGN_URL, headers=UA, timeout=20).json().get("data") or {}
    for k in ("host", "policy", "accessid", "signature", "imagePath"):
        if not sign.get(k):
            raise RuntimeError(f"alibaba upload signature missing {k}")
    name = _rand(5) + ".jpg"
    key = f"{sign['imagePath']}/{_rand(10)}{int(time.time() * 1000)}.jpg"
    files = {
        "name": (None, name), "key": (None, key), "policy": (None, sign["policy"]),
        "OSSAccessKeyId": (None, sign["accessid"]), "success_action_status": (None, "200"),
        "callback": (None, ""), "signature": (None, sign["signature"]), "file": (name, photo, "image/jpeg"),
    }
    r = httpx.post(sign["host"], files=files, headers=UA, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"alibaba upload failed: HTTP {r.status_code}")
    return key


def _alibaba_from_html(html: str) -> list[dict]:
    offers = parse.alibaba_offers(parse.json_blobs_from_html(html))
    if offers:
        return offers
    # DOM fallback: product cards as plain HTML
    cards = []
    for m in re.finditer(r'<a[^>]+href="([^"]*(?:product-detail|/offer/)[^"]*)"[^>]*>(.*?)</a>', html, re.S | re.I):
        href, inner = m.group(1), m.group(2)
        start = max(0, m.start() - 1500)
        block = html[start : m.end() + 1500]
        img = re.search(r'<img[^>]+(?:data-src|src)="([^"]+)"', block)
        text = re.sub(r"<[^>]+>", " ", block)
        cards.append({"href": href if href.startswith("http") else "https:" + href,
                      "title": re.sub(r"<[^>]+>", "", inner).strip()[:200],
                      "img": img.group(1) if img else None, "company": None, "text": text})
    return parse.alibaba_offers_from_dom(cards)


IMG_SEARCH_API = "https://open-s.alibaba.com/openservice/imageSearchViewService"


def _as_jpeg(photo: bytes) -> bytes:
    """TikTok/AliExpress CDNs often serve WebP/AVIF; Alibaba's image search only reads real JPEGs."""
    import io
    from PIL import Image
    try:
        buf = io.BytesIO()
        Image.open(io.BytesIO(photo)).convert("RGB").save(buf, "JPEG", quality=90)
        return buf.getvalue()
    except Exception:
        return photo


def alibaba_by_image(photo: bytes, budget: Budget) -> list[dict]:
    """Alibaba's own image-search JSON API (the one its results page calls). Free; Oxylabs only if blocked."""
    key = alibaba_upload(_as_jpeg(photo))
    params = {"appName": "magellan", "appKey": "a5m1ismomeptugvfmkkjnwwqnwyrhpb1", "imageType": "oss",
              "imageAddress": "/" + key, "beginPage": 1, "pageSize": 40, "language": "en_US"}
    data = None
    try:
        r = httpx.get(IMG_SEARCH_API, params=params, headers=UA, timeout=30)
        j = r.json()
        if "SUCCESS" in " ".join(j.get("ret") or []):
            data = j.get("data") or {}
    except Exception:
        pass
    if data is None:  # rate-limited / challenge: same request through Oxylabs (1 paid result)
        url = str(httpx.URL(IMG_SEARCH_API, params=params))
        raw = scrape({"source": "universal", "url": url, "geo_location": "United States"}, budget)
        try:
            data = json.loads(raw).get("data") or {}
        except Exception:
            data = {}
    return parse.alibaba_offers([data.get("offers") or []])


def alibaba_by_text(keyword: str, budget: Budget) -> list[dict]:
    html = scrape({"source": "alibaba_search", "query": keyword, "render": "html"}, budget)
    return _alibaba_from_html(html)


def aliexpress_by_text(keyword: str, budget: Budget) -> list[dict]:
    html = scrape({"source": "aliexpress_search", "query": keyword, "render": "html",
                   "geo_location": "United States"}, budget)
    return parse.aliexpress_offers(parse.json_blobs_from_html(html))


# ─── keyword from a TikTok title, no AI ──────────────────────────────────────
_STOP = set("""
a an the and or for with of to in on by from your you my our new hot best top sale free shipping pack set pcs pc piece pieces
2024 2025 2026 tiktok viral trending gift gifts premium upgraded original official authentic genuine women men womens mens
""".split())


def keyword_from_title(title: str, words: int = 6) -> str:
    t = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", title or "")          # drop (...) [...] notes
    t = re.split(r"[|,;:/]| - ", t)[0] if len(t) > 60 else t       # keep the head of long titles
    t = re.sub(r"\b\d+(\.\d+)?\s*(ml|oz|g|kg|lb|lbs|cm|mm|inch|in|pcs|pack|count|ct|x)\b", " ", t, flags=re.I)
    toks = [w for w in re.findall(r"[A-Za-z][A-Za-z\-']+", t) if w.lower() not in _STOP and len(w) > 1]
    # brand names are usually the first CamelCase / ALLCAPS token: skip it when the title is long enough
    if len(toks) > words and (toks[0].isupper() or (toks[0][0].isupper() and any(c.isupper() for c in toks[0][1:]))):
        toks = toks[1:]
    return " ".join(toks[:words]) or (title or "")[:60]
