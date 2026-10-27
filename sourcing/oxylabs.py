"""
Oxylabs Web Scraper API client (the cheap one, billed per successful result, about $1.15-1.35 per 1,000).
No Agent Browser, no Playwright. Same account the old pipelines use.

Env: OXYLABS_USER / OXYLABS_PASS   (OXYLABS_USERNAME / OXYLABS_PASSWORD also accepted)
"""

from __future__ import annotations

import os
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

USER = os.getenv("OXYLABS_USER") or os.getenv("OXYLABS_USERNAME")
PASS = os.getenv("OXYLABS_PASS") or os.getenv("OXYLABS_PASSWORD")
ENDPOINT = "https://realtime.oxylabs.io/v1/queries"


class OxylabsError(Exception):
    pass


class Budget:
    """Hard cap on paid requests per run so a bug can never burn the account."""

    def __init__(self, max_requests: int):
        self.max = max_requests
        self.used = 0

    def take(self) -> None:
        if self.used >= self.max:
            raise OxylabsError(f"request budget of {self.max} reached")
        self.used += 1


def scrape(payload: dict, budget: Budget, retries: int = 2) -> str:
    """Returns page HTML. Raises OxylabsError on auth problems or repeated failure."""
    if not (USER and PASS):
        raise OxylabsError("OXYLABS_USER / OXYLABS_PASS are not set")
    last = "unknown"
    for attempt in range(retries + 1):
        budget.take()
        try:
            r = httpx.post(ENDPOINT, auth=(USER, PASS), json=payload, timeout=180)
        except httpx.HTTPError as e:
            last = f"network: {e}"
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 401:
            raise OxylabsError("Oxylabs rejected the username/password (401). Create a Web Scraper API user in the Oxylabs dashboard.")
        if r.status_code in (402, 403):
            raise OxylabsError(f"Oxylabs account has no traffic left ({r.status_code}). Top up in the dashboard.")
        if r.status_code != 200:
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            time.sleep(3 * (attempt + 1))
            continue
        res = (r.json().get("results") or [{}])[0]
        content = res.get("content") or ""
        if res.get("status_code", 200) >= 400 or not content:
            last = f"target returned {res.get('status_code')}"
            continue
        return content if isinstance(content, str) else str(content)
    raise OxylabsError(last)
