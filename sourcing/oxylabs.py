"""
Oxylabs Web API client (https://developers.oxylabs.io/products/web-api). One credential:

  OXYLABS_WEB_API_KEY   the API key of the Web API instance, sent as  Authorization: Bearer <key>

Endpoint POST https://webapi.oxylabs.io/v1/scrape, body {"url", "output": ["html"], "location", "run_js"}.
It fetches one page URL and returns its HTML. Billed per successful result.

The old username/password "Web Scraper API" (realtime.oxylabs.io + OXYLABS_USER / OXYLABS_PASS) is gone:
the account only has an API key, and the 2026-11-14 run fell back to a stale username/password and
got 401 on every paid call while the run still reported success.

Errors:
  OxylabsError and its subclasses   STOP the whole run (bad key, no quota, request rejected, budget used).
  RuntimeError                      one page failed; the caller treats that product as failed and goes on.
"""

from __future__ import annotations

import os
import threading
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

WEB_API_KEY = (os.getenv("OXYLABS_WEB_API_KEY") or "").strip()
WEB_API_ENDPOINT = "https://webapi.oxylabs.io/v1/scrape"
TIMEOUT = 150            # docs: at least 150 s when the page is rendered with JavaScript


class OxylabsError(Exception):
    """Fatal for the run."""


class OxylabsAuthError(OxylabsError):
    """401 / 403, or no key at all."""


class OxylabsQuotaError(OxylabsError):
    """402, or 429 that still fails after the retries (plan quota / traffic used up)."""


class OxylabsRequestError(OxylabsError):
    """400 / 422: Oxylabs rejected the request itself, so every later call would fail too."""


class BudgetExhausted(OxylabsError):
    """The run's own --max-requests cap. Not an error in the account: the next run continues."""


class Budget:
    """Hard cap on paid requests per run so a bug can never burn the account."""

    def __init__(self, max_requests: int):
        self.max = max_requests
        self.used = 0
        self._lock = threading.Lock()   # shared by parallel workers

    def take(self) -> None:
        with self._lock:
            if self.used >= self.max:
                raise BudgetExhausted(f"request budget of {self.max} Oxylabs results reached")
            self.used += 1


def configured() -> bool:
    return bool(WEB_API_KEY)


def redact(text) -> str:
    """Never let the key reach a log line, even if a response echoes the header back."""
    t = str(text or "")
    return t.replace(WEB_API_KEY, "***") if WEB_API_KEY else t


def _snippet(r: httpx.Response) -> str:
    try:
        body = r.text
    except Exception:
        body = ""
    return redact(" ".join(body[:200].split()))


def _target_status(res: dict):
    try:
        return int(((res.get("metadata") or {}).get("statuses") or {}).get("http_request", {}).get("code"))
    except Exception:
        return None


def scrape(payload: dict, budget: Budget, retries: int = 2) -> str:
    """HTML of payload["url"] through the Web API. payload["render"] (any value) turns on run_js."""
    if not WEB_API_KEY:
        raise OxylabsAuthError("OXYLABS_WEB_API_KEY is not set (Oxylabs dashboard > Web API instance > API key)")
    url = payload.get("url")
    if not url:
        raise OxylabsRequestError(f"Oxylabs Web API only fetches page URLs; got source={payload.get('source')!r}")
    body = {"url": url, "output": ["html"], "location": "US"}
    if payload.get("render"):
        body["run_js"] = True
    headers = {"Authorization": f"Bearer {WEB_API_KEY}", "Content-Type": "application/json"}
    last = "unknown"
    for attempt in range(retries + 1):
        budget.take()
        try:
            r = httpx.post(WEB_API_ENDPOINT, json=body, headers=headers, timeout=TIMEOUT)
        except httpx.HTTPError as e:
            last = f"network: {type(e).__name__}"
            time.sleep(3 * (attempt + 1))
            continue
        code = r.status_code
        if code in (401, 403):
            raise OxylabsAuthError(f"Oxylabs rejected OXYLABS_WEB_API_KEY (HTTP {code}): {_snippet(r)}. "
                                   "Copy the key again from the Web API instance in the Oxylabs dashboard.")
        if code == 402:
            raise OxylabsQuotaError(f"Oxylabs: no traffic left on the plan (HTTP 402): {_snippet(r)}")
        if code in (400, 422):
            raise OxylabsRequestError(f"Oxylabs refused the request (HTTP {code}): {_snippet(r)}")
        if code == 429:
            text = _snippet(r)
            if "quota" in text.lower() or attempt == retries:
                raise OxylabsQuotaError(f"Oxylabs: rate limit or plan quota reached (HTTP 429): {text}")
            last = "HTTP 429"
            time.sleep(5 * (attempt + 1))
            continue
        if code == 408 or code >= 500:
            last = f"HTTP {code}: {_snippet(r)}"
            time.sleep(3 * (attempt + 1))
            continue
        if not (200 <= code < 300):
            raise RuntimeError(f"Oxylabs Web API HTTP {code}: {_snippet(r)}")
        try:
            data = r.json()
        except ValueError:
            last = f"HTTP {code} but the body is not JSON: {_snippet(r)}"
            print(f"  oxylabs: {last}")
            time.sleep(2 * (attempt + 1))
            continue
        state = (data or {}).get("state")
        if state and state != "done":                      # pending / faulted
            last = f"job state {state}"
            time.sleep(3 * (attempt + 1))
            continue
        res = ((data or {}).get("results") or [{}])[0] or {}
        html = res.get("html") or res.get("content") or ""
        if isinstance(html, list):
            html = "".join(map(str, html))
        target = _target_status(res)
        if target is not None and target >= 400 and not html:
            last = f"target answered HTTP {target}"
            continue
        if html:
            return html if isinstance(html, str) else str(html)
        last = "empty page"
        body["run_js"] = True          # docs: an empty shell means the page needs JavaScript
    raise RuntimeError(f"Oxylabs Web API failed for this page: {last}")
