"""
ProfitScout — FastAPI Backend
=============================
Run with:  uvicorn main:app --reload
API Docs:  http://127.0.0.1:8000/docs  (only when ENABLE_DOCS=true)
"""
from fastapi.responses import PlainTextResponse
import os
import httpx
import jwt
import time
from fastapi import APIRouter
from fastapi import Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi import FastAPI, HTTPException, Query, Security, Request
from datetime import datetime, timezone, timedelta
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel
from supplier_engine import find_suppliers, find_suppliers_by_platform, initialize_credits
import credits as credits_v2
from db_paging import fetch_all, count_exact
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
from openai import AsyncOpenAI
from supabase import create_client, Client
from dotenv import load_dotenv
from typing import Optional
import hmac
import hashlib
import json
import logging
import tempfile
import threading
from fastapi import Depends
from fastapi.concurrency import run_in_threadpool
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from limits import parse as _parse_limit
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter
from security import (normalize_plan, is_paid, normalize_email, email_hash, parse_uuid,
                      client_ip, safe_fetch, is_http_url)
from supplier_engine import SUPPLIER_PUBLIC_COLUMNS, public_supplier


load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
from openai import OpenAI as _SyncOpenAI
openai_sync_client = _SyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
ADZUNA_ID  = os.environ.get("ADZUNA_APP_ID", "")
ADZUNA_KEY = os.environ.get("ADZUNA_APP_KEY", "")

class GenerateListingRequest(BaseModel):
    product_name: str
    price: float
    category: str = ""
    product_id: Optional[str] = None   # preferred: the server looks up the product's own image
    image_url: str = ""                # legacy: ignored when product_id is given

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("tikprofitpro")


def _env_flag(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in ("1", "true", "yes", "on")


# Interactive docs map every admin/legacy route: off unless ENABLE_DOCS=true.
ENABLE_DOCS = _env_flag("ENABLE_DOCS")
# Old blog / job-board / CV / AI-proxy / autopilot routes: 404 unless ENABLE_LEGACY_ENDPOINTS=true.
ENABLE_LEGACY_ENDPOINTS = _env_flag("ENABLE_LEGACY_ENDPOINTS")

app = FastAPI(
    docs_url="/docs" if ENABLE_DOCS else None,
    redoc_url="/redoc" if ENABLE_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_DOCS else None,
)

GENERIC_ERROR = "Something went wrong on our side. Please try again in a minute."


ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "https://tikprofitpro.shop,https://www.tikprofitpro.shop,http://localhost:8080,http://localhost:5173").split(",") if o.strip()]


@app.exception_handler(Exception)
async def _unhandled_error(request: Request, exc: Exception):
    # Never send exception text to clients (it leaks schema / upstream details). Log it instead.
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    resp = JSONResponse({"detail": GENERIC_ERROR}, status_code=500)
    # Starlette runs this handler OUTSIDE CORSMiddleware, so without these headers the browser
    # hides the 500 and the site shows "Couldn't reach the server" instead of a real error.
    origin = request.headers.get("origin")
    if origin and origin in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
    return resp


def server_error(where: str) -> HTTPException:
    """Log the current exception server-side and return a generic 500 for the client."""
    logger.exception("%s failed", where)
    return HTTPException(status_code=500, detail=GENERIC_ERROR)


def require_legacy():
    """Dependency: legacy routes answer 404 unless ENABLE_LEGACY_ENDPOINTS=true. Runs before auth."""
    if not ENABLE_LEGACY_ENDPOINTS:
        raise HTTPException(status_code=404, detail="Not Found")


# ------------------------------------------------------------------------------
# Rate limiting (in-memory, per process). Keyed by signed-in user id, else client IP.
# ------------------------------------------------------------------------------
def resolve_user(request: Request):
    """Dependency (sync, so it runs in the threadpool): verifies the bearer token once and caches
    the user on request.state for the limiter key, auth checks and the handler."""
    return credits_v2.user_from_request(request)


def user_or_ip_key(request: Request) -> str:
    u = getattr(request.state, "auth_user", None)
    return f"user:{u.id}" if u else f"ip:{client_ip(request)}"


def ip_key(request: Request) -> str:
    return f"ip:{client_ip(request)}"


limiter = Limiter(key_func=user_or_ip_key, storage_uri="memory://", headers_enabled=False)
app.state.limiter = limiter

LIST_LIMIT = os.getenv("RATE_LIMIT_LIST", "60/minute")
SUPPLIER_LIMIT = os.getenv("RATE_LIMIT_SUPPLIER", "10/minute")
AMAZON_LIMIT = os.getenv("RATE_LIMIT_AMAZON", "3/minute")
LISTING_LIMIT = os.getenv("RATE_LIMIT_LISTING", "20/day")
CHECKOUT_LIMIT = os.getenv("RATE_LIMIT_CHECKOUT", "5/minute")
ADMIN_LIMIT = os.getenv("RATE_LIMIT_ADMIN", "30/minute")
POLL_LIMIT = os.getenv("RATE_LIMIT_POLL", "60/minute")

# For limits checked inside a handler (Amazon is one platform of a shared route).
_manual_limits = MovingWindowRateLimiter(MemoryStorage())


def check_limit(limit: str, scope: str, key: str) -> None:
    if not _manual_limits.hit(_parse_limit(limit), scope, key):
        raise HTTPException(status_code=429, detail="Too many requests. Please wait a minute and try again.")


@app.exception_handler(RateLimitExceeded)
async def _rate_limited(request: Request, exc: RateLimitExceeded):
    return JSONResponse({"detail": "Too many requests. Please wait a minute and try again.",
                         "code": "rate_limited"}, status_code=429)


def scheduled_autopilot():
    try:
        from autopilot import run
        print("⏰ Scheduled autopilot starting...")
        run()
    except Exception as e:
        print(f"❌ Autopilot error: {e}")

# Off by default: with more than one web instance the post would be published twice.
# Prefer a Render Cron Job running `python autopilot.py`; set RUN_SCHEDULER=true only on ONE instance.
if os.getenv("RUN_SCHEDULER", "false") == "true":
    scheduler = BackgroundScheduler()
    scheduler.add_job(scheduled_autopilot, 'cron', hour=9, minute=0)
    scheduler.start()
    print("Autopilot scheduler started — runs daily at 9AM UTC")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- API Key Auth ---
API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(request: Request, api_key: str = Security(API_KEY_HEADER)):
    if request.method == "OPTIONS":
        return None
    # Note: this key ships in the public JS bundle. It identifies the app, it is NOT auth.
    expected = os.getenv("API_KEY") or ""
    if not api_key or not expected or not hmac.compare_digest(api_key.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="Forbidden")
    return api_key


ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")


def require_admin(request: Request):
    """Internal tools (blog, autopilot, job board). Header X-Admin-Secret must match ADMIN_SECRET."""
    given = request.headers.get("X-Admin-Secret", "")
    if not ADMIN_SECRET or not hmac.compare_digest(given, ADMIN_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")


FREE_PRODUCT_LIMIT = 10
# "Hot" means one thing in the whole app: virality_score >= 80 (/stats.hot, the "Score 80+" tile,
# /products/tier/hot). The life-cycle stage once stored as "hot" is "emerging" (see product_stage).
HOT_MIN_SCORE = 80
FREE_CACHE_SECONDS = 300
MIN_SIMILARITY = float(os.getenv("SOURCING_MIN_SIMILARITY", "0.80"))
# Matching off (default since 2026-11-11): unscored "auto" rows from sourcing are shown, top 3.
SOURCING_MATCHING = os.getenv("SOURCING_MATCHING", "off").strip().lower() == "on"
SUPPLIER_KEEP = int(os.getenv("SOURCING_KEEP_TOP", "3"))
UPGRADE_MESSAGE = "This product is part of the Pro catalogue. Upgrade to Pro to see it."


def gate_list(plan: str, limit: int, offset: int) -> tuple[int, int]:
    """Kept for compatibility. Unpaid callers no longer reach a filtered query at all (see free_top_*)."""
    if not is_paid(plan):
        return min(limit, FREE_PRODUCT_LIMIT), 0
    return limit, offset


ADMIN_PLAN = "ultimate"


def access_plan(user, db_plan: Optional[str]) -> str:
    """The plan that decides what a caller may SEE (catalogue, search, shops, creators, listing tool).
    Admins (ADMIN_USER_IDS, or a confirmed address in ADMIN_EMAILS) see everything, like Ultimate.
    This is never written back to users.plan: credits (plan_allowance() in SQL) and Lemon Squeezy
    keep using the stored plan, so a webhook can't be confused and nothing about billing changes."""
    if user is not None and is_admin(user):
        return ADMIN_PLAN
    return normalize_plan(db_plan)


def _db_plan(email: str) -> str:
    """users.plan for this email; 'free' when there is no row or the lookup fails (fail closed)."""
    try:
        rows = supabase.table("users").select("plan").eq("user_email", normalize_email(email)).limit(1).execute().data
        return normalize_plan(rows[0].get("plan") if rows else None)
    except Exception:
        logger.exception("plan lookup failed")
        return "free"


def get_user_plan(request: Request) -> str:
    """Plan of the signed-in caller: 'free' (also for signed-out), 'pro' or 'ultimate'. Admins count as
    'ultimate' (see access_plan). Sync on purpose: callers are plain `def` routes, so the GoTrue + DB
    round-trips run in the threadpool."""
    user = credits_v2.user_from_request(request)
    if not user:
        return "free"
    if is_admin(user):
        return ADMIN_PLAN               # no DB round-trip needed
    # No ensure_user_credits() here any more: get_credit_balance() takes a FOR UPDATE row lock and
    # ran on every list page (5 parallel /products calls per Products load). /credits and
    # consume_credit() both create / roll over the credit row themselves.
    return access_plan(user, _db_plan(user.email))


def ensure_user_credits(user_email: str, plan: str):
    """Create/roll over the credit row. Plan comes from users.plan inside the DB function."""
    try:
        credits_v2.get_balance(user_email)
    except Exception:
        logger.warning("ensure_user_credits failed for user %s", email_hash(user_email))


# ------------------------------------------------------------------------------
# Free tier: ONE fixed top-10 per list, whatever filters / sort / offset / q the caller sends.
# Otherwise sliding filter windows enumerate the whole paid catalogue (audit H2).
# ------------------------------------------------------------------------------
_free_cache: dict = {}
_free_cache_lock = threading.Lock()


def _cached_top(name: str, loader) -> list:
    now = time.time()
    with _free_cache_lock:
        hit = _free_cache.get(name)
        if hit and now - hit[0] < FREE_CACHE_SECONDS:
            return [dict(r) for r in hit[1]]
    rows = loader() or []
    with _free_cache_lock:
        _free_cache[name] = (now, rows)
    return [dict(r) for r in rows]


# The prod table still has the old trend_phase check, so import_kalodata.py writes v1 names
# (emerging->hot, peaking/unknown->watch, saturated->pass). The UI filters and labels by v2 names,
# so "Emerging" / "Peaking" / "Saturated" matched nothing and "pass" showed as "New data".
# v2 rows (data_end_date set) are mapped back exactly: stage() only returns "unknown" when growth
# is unknown, and only "peaking" when it is known.
_V1_TO_V2 = {"hot": "emerging", "pass": "saturated", "rising": "rising"}
V2_STAGES = ("emerging", "rising", "peaking", "saturated", "unknown")


def product_stage(p: dict) -> Optional[str]:
    phase = (p.get("trend_phase") or "").strip().lower() or None
    if not p.get("data_end_date") or phase in V2_STAGES:
        return phase
    if phase == "watch":
        return "unknown" if p.get("gmv_growth_pct") is None else "peaking"
    return _V1_TO_V2.get(phase, phase)


def present_product(p: dict) -> dict:
    """API view of a tiktok_products row: v2 stage name, plus the real per-day revenue so the UI
    does not have to show a 1-day window multiplied by 7 as "7-day" revenue."""
    out = dict(p)
    out["trend_phase"] = product_stage(p)
    w = p.get("window_days")
    rev = p.get("revenue_window")
    try:
        out["revenue_per_day"] = round(float(rev) / float(w), 2) if rev is not None and w else None
    except (TypeError, ValueError):
        out["revenue_per_day"] = None
    out["gmv_7d_estimated"] = bool(w) and isinstance(w, (int, float)) and 0 < w < 7
    return out


def present_products(rows) -> list:
    return [present_product(r) for r in rows or []]


def _stage_query_values(stage: str) -> list:
    """DB values that can hold this (v2 or v1) stage name."""
    stage = stage.strip().lower()
    back = {"emerging": ["emerging", "hot"], "saturated": ["saturated", "pass"],
            "peaking": ["peaking", "watch"], "unknown": ["unknown", "watch"],
            "hot": ["hot", "emerging"], "pass": ["pass", "saturated"],
            "watch": ["watch", "peaking", "unknown"]}
    return back.get(stage, [stage])


def _stage_filter(query, stage: str):
    """DB filter for a stage name (v2 or v1). v1 "watch" rows hold both peaking (growth known) and
    unknown (growth unknown), so those two are told apart by gmv_growth_pct, inside the query."""
    st = stage.strip().lower()
    if st == "peaking":
        return query.or_("trend_phase.eq.peaking,and(trend_phase.eq.watch,gmv_growth_pct.not.is.null)")
    if st == "unknown":
        return query.or_("trend_phase.eq.unknown,and(trend_phase.eq.watch,gmv_growth_pct.is.null)")
    return query.in_("trend_phase", _stage_query_values(st))


def _like_exact(text: str) -> str:
    """ILIKE pattern that matches `text` exactly (case-insensitive): % and _ are escaped."""
    return text.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Paid catalogue pages and /stats are the same for every caller and only change on an import:
# cache them briefly per process (cleared after an admin import).
LIST_CACHE_SECONDS = int(os.getenv("LIST_CACHE_SECONDS", "120"))
_LIST_CACHE_MAX = 512


def _cached(key, loader, ttl: int):
    now = time.time()
    with _free_cache_lock:
        hit = _free_cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = loader()
    with _free_cache_lock:
        if len(_free_cache) >= _LIST_CACHE_MAX:
            _free_cache.clear()
        _free_cache[key] = (now, val)
    return val


def clear_list_caches() -> None:
    with _free_cache_lock:
        _free_cache.clear()


def _order_products(q):
    # Ties are common (FastMoss scores top out at 70, nine products share it): break them by
    # revenue, then id, so pages are stable and the free top 10 is the 10 biggest sellers.
    return (q.order("virality_score", desc=True)
             .order("gmv_7d", desc=True, nullsfirst=False)
             .order("id"))


def free_top_products() -> list:
    return present_products(_cached_top("products", lambda: (
        _order_products(supabase.table("tiktok_products").select("*")
                        .eq("is_active", True))
        .limit(FREE_PRODUCT_LIMIT).execute().data)))


def free_top_product_ids() -> set:
    return {str(p.get("id")) for p in free_top_products()}


def free_top_creators() -> list:
    rows = _cached_top("creators", lambda: (
        supabase.table("creators").select("*")
        .order("gmv_total", desc=True, nullsfirst=False).order("tiktok_handle")
        .limit(FREE_PRODUCT_LIMIT).execute().data))
    for r in rows:                      # contact details are a paid feature
        r.pop("contact_email", None)
        r["contact_locked"] = True
    return rows


def free_top_shops() -> tuple[list, Optional[int]]:
    rows = _cached_top("shops", lambda: (
        supabase.table("shops").select("*")
        .order("gmv_total", desc=True, nullsfirst=False).order("shop_name")
        .limit(FREE_PRODUCT_LIMIT).execute().data))
    return rows, None

# ==============================================================================
# ENDPOINT 1 — Health Check
# ==============================================================================
@app.get("/")
def root():
    out = {
        "status": "running",
        "app": "ProfitScout API",
        "version": "2.0.0",
    }
    if ENABLE_DOCS:
        out["docs"] = "/docs"
    return out


@app.get("/ping")
def ping():
    return {"status": "ok", "message": "FastAPI is working!"}


# ==============================================================================
# ENDPOINT 2 — Get All Products (with filters + plan gating)
# ==============================================================================
@app.get("/products")
@limiter.limit(LIST_LIMIT, key_func=ip_key)
def get_products(
    request:     Request,
    min_score:   int            = Query(default=0,   ge=0, le=100),
    max_score:   int            = Query(default=100, ge=0, le=100),
    trend_phase: Optional[str]  = Query(default=None),
    category:    Optional[str]  = Query(default=None),
    min_price:   Optional[float]= Query(default=None),
    max_price:   Optional[float]= Query(default=None),
    shop_name:   Optional[str]  = Query(default=None, max_length=300, description="Exact shop name (case-insensitive)"),
    limit:       int            = Query(default=20,  ge=1, le=100),
    offset:      int            = Query(default=0,   ge=0),
    api_key:     str            = Security(verify_api_key),
):
    plan = get_user_plan(request)
    try:
        if not is_paid(plan):
            rows = free_top_products()
            return {"success": True, "count": len(rows), "offset": 0, "limit": FREE_PRODUCT_LIMIT,
                    "plan": plan, "products": rows}

        def load():
            query = (
                supabase.table("tiktok_products")
                .select("*")
                .eq("is_active", True)
                .gte("virality_score", min_score)
                .lte("virality_score", max_score)
            )
            if trend_phase:
                query = _stage_filter(query, trend_phase)
            if category:
                query = query.ilike("category", f"%{category}%")
            if min_price is not None:
                query = query.gte("tiktok_price", min_price)
            if max_price is not None:
                query = query.lte("tiktok_price", max_price)
            if shop_name:
                query = query.ilike("shop_name", _like_exact(shop_name))
            # The stage filter runs in the DB (see _stage_filter): filtering after .range() made
            # pages shorter than `limit`, which a client reads as "last page".
            return present_products(_order_products(query).range(offset, offset + limit - 1).execute().data)

        key = ("paid-products", min_score, max_score, trend_phase, category, min_price, max_price, shop_name,
               limit, offset)
        rows = _cached(key, load, LIST_CACHE_SECONDS)

        return {
            "success":  True,
            "count":    len(rows),
            "offset":   offset,
            "limit":    limit,
            "plan":     plan,
            "products": rows
        }

    except Exception:
        raise server_error("/products")


# ==============================================================================
# ENDPOINT 3 — Get Single Product Detail
# ==============================================================================
@app.get("/products/{product_id}")
@limiter.limit(LIST_LIMIT, key_func=ip_key)
def get_product(product_id: str, request: Request, api_key: str = Security(verify_api_key)):
    pid = parse_uuid(product_id)
    if not pid:
        raise HTTPException(status_code=404, detail="Product not found")
    plan = get_user_plan(request)
    try:
        if not is_paid(plan) and pid not in free_top_product_ids():
            raise HTTPException(status_code=403, detail=UPGRADE_MESSAGE)
        rows = (
            supabase.table("tiktok_products")
            .select("*")
            .eq("id", pid)
            .eq("is_active", True)            # retired (hidden March) rows are not in the catalogue
            .limit(1)
            .execute()
        ).data
        if not rows:
            raise HTTPException(status_code=404, detail="Product not found")
        # Suppliers are NOT returned here: they cost a credit via /products/{id}/suppliers/{platform}.
        return {
            "success": True,
            "product": present_product(rows[0])
        }

    except HTTPException:
        raise
    except Exception:
        raise server_error("/products/{id}")


# ==============================================================================
# ENDPOINT 4 — Get Products by Virality Tier
# ==============================================================================
@app.get("/products/tier/{tier}")
@limiter.limit(LIST_LIMIT, key_func=ip_key)
def get_products_by_tier(
    tier:    str,
    request: Request,
    limit:   int = Query(default=20, ge=1, le=100),
    offset:  int = Query(default=0,  ge=0),
    api_key: str = Security(verify_api_key),
):
    tier_ranges = {           # (min inclusive, max inclusive); queried as >= min and < next floor
        "hot":    (HOT_MIN_SCORE, 100),
        "rising": (65, HOT_MIN_SCORE - 1),
        "watch":  (50, 64),
        "pass":   (0,  49),
    }

    if tier not in tier_ranges:
        raise HTTPException(
            status_code=400,
            detail="Invalid tier. Use: hot, rising, watch, or pass"
        )

    min_score, max_score = tier_ranges[tier]
    plan = get_user_plan(request)

    try:
        upper = max_score + 1            # exclusive bound, so a 79.5 score is not lost between tiers
        if not is_paid(plan):
            # Still only the fixed top 10, but only the ones really in this tier (was: the whole
            # top 10 returned under every tier label, e.g. score-70 rows as "hot 80-100").
            rows = [r for r in free_top_products()
                    if min_score <= float(r.get("virality_score") or 0) < upper]
        else:
            rows = _cached(("tier", tier, limit, offset), lambda: present_products(
                _order_products(
                    supabase.table("tiktok_products")
                    .select("*")
                    .eq("is_active", True)
                    .gte("virality_score", min_score)
                    .lt("virality_score", upper))
                .range(offset, offset + limit - 1)
                .execute().data), LIST_CACHE_SECONDS)

        return {
            "success":  True,
            "tier":     tier,
            "range":    f"{min_score}-{max_score}",
            "count":    len(rows),
            "products": rows
        }

    except Exception:
        raise server_error("/products/tier")


# ==============================================================================
# ENDPOINT 5 — Dashboard Stats
# ==============================================================================
@app.get("/stats")
@limiter.limit(LIST_LIMIT, key_func=ip_key)
def get_stats(request: Request, api_key: str = Security(verify_api_key)):
    try:
        return _cached("stats", _load_stats, LIST_CACHE_SECONDS)
    except Exception:
        raise server_error("/stats")


def _load_stats() -> dict:
    # Exact counts (PostgREST caps a plain select at max-rows, default 1000).
    def n(lo=None, below=None):
        def build():
            q = supabase.table("tiktok_products").select("id", count="exact").eq("is_active", True)
            if lo is not None:
                q = q.gte("virality_score", lo)
            if below is not None:
                q = q.lt("virality_score", below)
            return q
        return count_exact(build)

    total  = n()
    hot    = n(HOT_MIN_SCORE)
    rising = n(65, HOT_MIN_SCORE)
    watch  = n(50, 65)
    low    = n(None, 50)

    rows = fetch_all(lambda: supabase.table("tiktok_products")
                     .select("id, category, trend_phase, gmv_growth_pct, data_end_date, window_days")
                     .eq("is_active", True).order("id"))
    categories = {}
    stages = {s: 0 for s in V2_STAGES}
    end_dates, windows = set(), set()
    for p in rows:
        cat = p.get("category") or "Other"
        categories[cat] = categories.get(cat, 0) + 1
        st = product_stage(p) or "unknown"
        stages[st] = stages.get(st, 0) + 1
        if p.get("data_end_date"):
            end_dates.add(str(p["data_end_date"])[:10])
        if p.get("window_days"):
            windows.add(p["window_days"])
    top_categories = sorted(categories.items(), key=lambda x: x[1], reverse=True)[:5]

    return {
        "success": True,
        "stats": {
            "total_products": total,
            # Score buckets, labelled "Score 80+" etc. in the UI. These legacy keys are the SAME
            # numbers as score_tiers below and as /products/tier/{hot|rising|watch|pass}.
            "hot":            hot,
            "rising":         rising,
            "watch":          watch,
            # One definition everywhere: "hot" = virality_score >= 80. Life-cycle stages
            # (emerging / rising / peaking / saturated / unknown) are a separate axis, never "hot".
            "score_tiers": {
                "score_80_plus": hot,
                "score_65_79":   rising,
                "score_50_64":   watch,
                "score_below_50": low,
            },
            "hot_definition": f"virality_score >= {HOT_MIN_SCORE}",
            # life-cycle stages, v2 names, active rows only
            "stages":         stages,
            "data_end_date":  max(end_dates) if end_dates else None,
            "window_days":    sorted(windows)[0] if len(windows) == 1 else (sorted(windows) or None),
            "top_categories": [{"name": k, "count": v} for k, v in top_categories]
        }
    }


# ==============================================================================
# ENDPOINT 6 — Profit Calculator
# ==============================================================================
class ProfitRequest(BaseModel):
    product_id:                str = "manual"
    supplier_id:               Optional[str] = None
    sale_price:                Optional[float] = None
    selling_price:             Optional[float] = None
    cogs:                      Optional[float] = None
    product_cost:              Optional[float] = None
    shipping_cost:             Optional[float] = 0.0
    quantity:                  int = 1
    affiliate_commission_rate: float = 15.0
    fbt_enabled:               bool  = False
    product_weight_oz:         float = 8.0
    inbound_shipping:          float = 0.0
    return_rate_pct:           float = 8.0
    ad_spend_per_unit:         float = 0.0
    is_new_seller:             bool  = False


@app.post("/calculate-profit")
@limiter.limit(LIST_LIMIT, key_func=ip_key)
def calculate_profit(req: ProfitRequest, request: Request, api_key: str = Security(verify_api_key)):
    try:
        # Normalize field names from frontend
        sale_price = req.sale_price or req.selling_price or 0
        cogs = req.cogs or ((req.product_cost or 0) + (req.shipping_cost or 0))

        referral_rate   = 0.03 if req.is_new_seller else 0.06
        referral_fee    = round(sale_price * referral_rate, 2)
        transaction_fee = round(sale_price * 0.029, 2)
        affiliate_amt   = round(sale_price * (req.affiliate_commission_rate / 100), 2)

        if req.fbt_enabled:
            if req.product_weight_oz <= 4:    fbt_fee = 2.68
            elif req.product_weight_oz <= 8:  fbt_fee = 3.58
            elif req.product_weight_oz <= 12: fbt_fee = 4.20
            elif req.product_weight_oz <= 16: fbt_fee = 4.88
            else:                             fbt_fee = 5.50
        else:
            fbt_fee = 0.0

        refund_admin_fee = min(referral_fee * 0.20, 5.0)
        return_buffer    = round((req.return_rate_pct / 100) * refund_admin_fee, 2)

        total_costs = round(
            cogs + referral_fee + transaction_fee + affiliate_amt
            + fbt_fee + req.inbound_shipping + return_buffer + req.ad_spend_per_unit,
            2
        )

        net_profit = round(sale_price - total_costs, 2)
        margin_pct = round((net_profit / sale_price) * 100, 1) if sale_price > 0 else 0

        if margin_pct >= 35:   rating = "green"
        elif margin_pct >= 20: rating = "amber"
        else:                  rating = "red"

        warnings = []
        total_fees_pct = ((referral_fee + transaction_fee + affiliate_amt) / sale_price) * 100 if sale_price > 0 else 0
        if total_fees_pct > 35:
            warnings.append(f"TikTok fees + affiliate = {total_fees_pct:.1f}% of sale price. Very tight margins.")
        if req.affiliate_commission_rate > 15:
            warnings.append(f"Affiliate rate of {req.affiliate_commission_rate}% is high. Consider reducing.")
        if req.return_rate_pct > 20:
            warnings.append(f"Return rate of {req.return_rate_pct}% will significantly erode profit.")
        if margin_pct < 0:
            warnings.append("This product is LOSING money at this price. Raise sale price or find cheaper supplier.")

        return {
            "success": True,
            "rating":  rating,
            "summary": {
                "sale_price":  sale_price,
                "total_costs": total_costs,
                "net_profit":  net_profit,
                "margin_pct":  margin_pct,
            },
            "breakdown": {
                "cost_of_goods":        cogs,
                "referral_fee":         referral_fee,
                "transaction_fee":      transaction_fee,
                "affiliate_commission": affiliate_amt,
                "fbt_fulfillment":      fbt_fee,
                "inbound_shipping":     req.inbound_shipping,
                "return_buffer":        return_buffer,
                "ad_spend":             req.ad_spend_per_unit,
            },
            "warnings": warnings
        }

    except Exception:
        logger.exception("/calculate-profit failed")
        raise HTTPException(status_code=400, detail="Could not calculate profit for these numbers.")


# ==============================================================================
# ENDPOINT 7 — Search Products
# ==============================================================================
@app.get("/search")
@limiter.limit(LIST_LIMIT, key_func=ip_key)
def search_products(
    request: Request,
    q:       str = Query(..., min_length=2, max_length=200, description="Search keyword"),
    limit:   int = Query(default=20, ge=1, le=100),
    offset:  int = Query(default=0,  ge=0),
    api_key: str = Security(verify_api_key),
):
    plan = get_user_plan(request)
    try:
        if not is_paid(plan):
            # Free / signed-out: the same fixed top 10 as /products. q is ignored on purpose:
            # searching would let anyone page through the paid catalogue 10 rows at a time.
            rows = free_top_products()
            return {"success": True, "query": q, "count": len(rows), "plan": plan, "products": rows}

        result = (
            _order_products(
                supabase.table("tiktok_products")
                .select("*")
                .eq("is_active", True)
                .ilike("product_name", f"%{q}%"))
            .range(offset, offset + limit - 1)
            .execute()
        )
        rows = present_products(result.data)

        return {
            "success":  True,
            "query":    q,
            "count":    len(rows),
            "plan":     plan,
            "products": rows
        }

    except Exception:
        raise server_error("/search")


    # ==============================================================================
# ==============================================================================
# ENDPOINT 8/9 — Find Suppliers (credits v2 + photo search)
# GET /products/{product_id}/suppliers/{platform}?photo=true
# platform = alibaba | aliexpress | amazon
#
# Credit rules (see credits.py):
#   - products you already unlocked are FREE (cached results still cost 1 credit the first time)
#   - otherwise 1 credit is reserved before the search and REFUNDED if nothing usable is found
#   - the engine never charges; this endpoint is the only place credits move
# ==============================================================================
def _dedupe_key(r: dict):
    ext = r.get("external_product_id")
    if ext:
        return ("ext", str(ext))
    return ("name", (r.get("supplier_name") or "").strip().lower(), round(float(r.get("unit_cost_min") or 0), 2))


def supplier_visible(r: dict) -> bool:
    """Same rule as verified_suppliers' filter: not hidden, and hand-approved, a verified photo
    match, or (matching off) an unscored 'auto' search-order row. Admin review uses it so auto
    rows users DO see are listed as live, not 'below threshold' (audit S7)."""
    if r.get("hidden"):
        return False
    method = r.get("match_method")
    if method == "manual":
        return True
    if not SOURCING_MATCHING and method == "auto":
        return True
    return r.get("similarity") is not None and float(r.get("similarity") or 0) >= MIN_SIMILARITY


def verified_suppliers(product: dict, platform: str, keep: int | None = None) -> list[dict]:
    """Saved suppliers that passed the photo check, best match first, no duplicates.
    Rows without a similarity score (old keyword-only results) are never shown or charged for."""
    from supplier_engine import calculate_profit
    rows = (
        supabase.table("suppliers").select(SUPPLIER_PUBLIC_COLUMNS)
        .eq("tiktok_product_id", product["id"]).eq("platform", platform)
        .or_(f"similarity.gte.{MIN_SIMILARITY},match_method.eq.manual"
             + ("" if SOURCING_MATCHING else ",match_method.eq.auto"))   # verified, hand-approved, or (matching off) auto
        .not_.is_("hidden", "true")
        .execute().data or []
    )

    def score(r):  # a hand-approved row ranks as a verified match
        sim = float(r.get("similarity") or 0)
        return max(sim, MIN_SIMILARITY) if r.get("match_method") == "manual" else sim

    def order(r):  # scored rows by match then price; unscored auto rows keep the search order (insert id)
        if r.get("match_method") == "auto" and r.get("similarity") is None:
            # Supplier ids are UUIDs, so int(id) crashed here (500 on every auto row). Keep the
            # search order instead: oldest batch first; inside a batch the stable sort keeps the
            # order the rows came back in.
            return (1, 0, str(r.get("sourced_at") or ""))
        return (0, -round(score(r), 2), float(r.get("unit_cost_min") or 1e9))

    keep = keep or SUPPLIER_KEEP
    seen, out = set(), []
    for r in sorted(rows, key=order):
        k = _dedupe_key(r)
        if k in seen:
            continue
        seen.add(k)
        cogs = r.get("unit_cost_min") or r.get("unit_cost_max") or 0
        r["_profit"] = calculate_profit(sale_price=product.get("tiktok_price") or 0, cogs=cogs)
        r["_image_url"] = r.get("image_url", "")
        out.append(r)
    return out[:keep]


def _balance(email: str) -> Optional[dict]:
    try:
        return credits_v2.get_balance(email)
    except credits_v2.CreditError:
        return None


def _charge_or_402(user_email: str, product_id: str, platform: str, mode: str) -> bool:
    try:
        credit = credits_v2.consume(user_email, product_id, platform, mode)
    except credits_v2.CreditError:
        logger.exception("credit system error")
        raise HTTPException(status_code=503, detail="Credits are temporarily unavailable. Please try again in a minute.")
    if not credit.get("allowed"):
        raise HTTPException(status_code=402, detail={
            "code": "no_credits",
            "message": f"You've used all {credit.get('credits_total', 0)} credits for this period.",
            "credits": credit,
        })
    return bool(credit.get("charged"))


# ==============================================================================
# Supplier lookup — credit rules (credits.py + SQL functions do the accounting):
#   - Alibaba / AliExpress are PRE-SOURCED in a batch (sourcing/presource.py).
#   - A user pays 1 credit the first time they open a product+platform that has at least one
#     photo-verified supplier (similarity >= SOURCING_MIN_SIMILARITY). Reopening is free.
#   - No verified supplier yet (not sourced, no match, only old unscored rows) -> nothing shown, nothing charged.
#   - Amazon keeps the legacy live search: charged up front, refunded if nothing usable is found.
# ==============================================================================
CONFIRM_EMAIL_MESSAGE = "Confirm your email to use credits. Check your inbox for the confirmation link."

# One live Amazon search per user at a time (per process). Each one runs Apify + GPT for minutes.
_amazon_inflight: set = set()
_amazon_inflight_lock = threading.Lock()


def _plan_for_email(email: str) -> str:
    """Stored users.plan (kept for callers that only have an email; access checks use access_plan)."""
    return _db_plan(email)


def _safe_is_unlocked(email: str, product_id: str, platform: str) -> bool:
    try:
        return bool(credits_v2.is_unlocked(email, product_id, platform))
    except Exception:
        logger.exception("is_unlocked failed")
        return False


def _supplier_search(product_id: str, platform: str, request: Request, photo: bool):
    if platform not in ("alibaba", "aliexpress", "amazon"):
        raise HTTPException(status_code=400, detail="Platform must be: alibaba, aliexpress, or amazon")

    user = credits_v2.user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in to search suppliers")
    user_email = user.email

    pid = parse_uuid(product_id)
    if not pid:
        raise HTTPException(status_code=404, detail="Product not found")
    rows = (
        supabase.table("tiktok_products")
        .select("id, product_name, tiktok_price, thumbnail_url, image_storage_url, is_active")
        .eq("id", pid).limit(1).execute().data
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Product not found")
    product = rows[0]
    product_id = pid
    # Retired products (the 728 hidden March rows still have ~3.9k supplier rows) are out of the
    # catalogue: nobody starts paying for them. A user who already unlocked one keeps it, free.
    if product.get("is_active") is False and not _safe_is_unlocked(user_email, product_id, platform):
        raise HTTPException(status_code=404, detail="Product not found")

    # Free plan: suppliers only for the fixed top 10 (audit S3). Checked before any charge.
    # A product the user already unlocked (e.g. it left the top 10 since) stays free to reopen.
    if not is_paid(get_user_plan(request)) and pid not in free_top_product_ids():
        if not _safe_is_unlocked(user_email, product_id, platform):
            raise HTTPException(status_code=403, detail={"code": "upgrade_required", "message": UPGRADE_MESSAGE})

    def charge(mode: str) -> bool:
        # Unconfirmed email = 0 usable credits (stops throwaway-account credit farming, audit H3).
        if not user.email_confirmed:
            raise HTTPException(status_code=403, detail={"code": "email_unconfirmed", "message": CONFIRM_EMAIL_MESSAGE})
        return _charge_or_402(user_email, product_id, platform, mode)

    if platform in ("alibaba", "aliexpress"):
        rows = verified_suppliers(product, platform)
        if rows:
            charged = False
            if not credits_v2.is_unlocked(user_email, product_id, platform):
                charged = charge("image")
            return {"success": True, "cached": True, "platform": platform, "match_method": "photo",
                    "suppliers": [public_supplier(r) for r in rows], "credit_charged": charged,
                    "credits": _balance(user_email)}

        last = (
            supabase.table("sourcing_jobs").select("status")
            .eq("product_id", str(product_id)).eq("platform", platform)
            .order("id", desc=True).limit(1).execute().data
        )
        state = last[0]["status"] if last else "pending"
        msg = (f"No {platform.title()} suppliers found for this product. No credit used."
               if state == "no_match" else
               f"{platform.title()} suppliers for this product are being prepared. Check back soon. No credit used.")
        return {"success": False, "status": "no_match" if state == "no_match" else "not_sourced",
                "platform": platform, "suppliers": [], "credit_charged": False, "message": msg,
                "credits": _balance(user_email)}

    # Amazon: legacy live search (Apify). Charge first, refund if nothing usable.
    check_limit(AMAZON_LIMIT, "amazon", f"user:{user.id}")
    with _amazon_inflight_lock:
        if user.id in _amazon_inflight:
            raise HTTPException(status_code=429, detail="An Amazon search is already running for you. Wait for it to finish.")
        _amazon_inflight.add(user.id)
    try:
        image_url = product.get("image_storage_url") or product.get("thumbnail_url") or ""
        charged = False
        if not credits_v2.is_unlocked(user_email, product_id, platform):
            charged = charge("image" if photo else "text")
        try:
            result = find_suppliers_by_platform(
                product_id=product["id"], product_title=product["product_name"],
                tiktok_price=product["tiktok_price"] or 0, platform=platform,
                user_email=user_email, product_image_url=image_url, use_photo=photo,
            )
        except Exception:
            logger.exception("amazon search error")
            if charged:
                credits_v2.refund(user_email, product_id, platform)
            raise HTTPException(status_code=500, detail="Supplier search failed. Your credit was not used.")
        if charged and not result.get("success"):
            credits_v2.refund(user_email, product_id, platform)
            charged = False
        result["suppliers"] = [public_supplier(r) for r in result.get("suppliers") or []]
        result["credit_charged"] = charged
        result["credits"] = _balance(user_email)
        return result
    finally:
        with _amazon_inflight_lock:
            _amazon_inflight.discard(user.id)


# Plain `def` routes: FastAPI runs them in its threadpool, so a slow Apify/GPT search no longer
# freezes the event loop for every other user (audit H1).
@app.get("/products/{product_id}/suppliers/{platform}")
@limiter.limit(SUPPLIER_LIMIT)
def get_suppliers_by_platform(
    product_id: str,
    platform:   str,
    request:    Request,
    photo:      bool = Query(default=True, description="Use the product photo to match suppliers"),
    user=Depends(resolve_user),
    api_key:    str = Security(verify_api_key),
):
    return _supplier_search(product_id, platform, request, photo)


@app.get("/products/{product_id}/suppliers")
@limiter.limit(SUPPLIER_LIMIT)
def get_suppliers(product_id: str, request: Request, user=Depends(resolve_user),
                  api_key: str = Security(verify_api_key)):
    """Legacy route. Used to give unlimited free Alibaba searches; now same rules as above."""
    return _supplier_search(product_id, "alibaba", request, True)


@app.get("/sourcing/jobs/{job_id}")
@limiter.limit(POLL_LIMIT)
def get_sourcing_job(job_id: int, request: Request, user=Depends(resolve_user),
                     api_key: str = Security(verify_api_key)):
    """Frontend polls this every 3s after a photo search was queued (worker mode only)."""
    email = user.email if user else None
    if not email:
        raise HTTPException(status_code=401, detail="Sign in required")
    jobs = supabase.table("sourcing_jobs").select("*").eq("id", job_id).limit(1).execute().data
    job = jobs[0] if jobs else None
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    out = {"job_id": job_id, "status": job["status"]}
    # Suppliers only for users who actually unlocked this product+platform.
    if job["status"] == "done" and credits_v2.is_unlocked(email, job["product_id"], job["platform"]):
        prows = supabase.table("tiktok_products").select("id, tiktok_price").eq("id", job["product_id"]).limit(1).execute().data
        out["suppliers"] = [public_supplier(r) for r in verified_suppliers(prows[0], job["platform"])] if prows else []
    if job["status"] in ("done", "no_match", "failed"):
        out["credits"] = _balance(email)
    return out


# ==============================================================================
# ENDPOINT 10 — Get User Credits (single source of truth for the UI)
# ==============================================================================
@app.get("/credits")
@limiter.limit(POLL_LIMIT)
def get_credits(request: Request, user=Depends(resolve_user), api_key: str = Security(verify_api_key)):
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        out = {"success": True, "credits": credits_v2.get_balance(user.email)}
    except credits_v2.CreditError:
        logger.exception("credit system error")
        raise HTTPException(status_code=503, detail="Credits are temporarily unavailable")
    if not user.email_confirmed:
        out["email_confirmed"] = False
        out["message"] = CONFIRM_EMAIL_MESSAGE
    return out


# ==============================================================================
# ENDPOINT 11 — LemonSqueezy Webhook
# ==============================================================================
LEMONSQUEEZY_WEBHOOK_SECRET = os.getenv("LEMONSQUEEZY_WEBHOOK_SECRET")
def _env_ids(*names: str) -> list[str]:
    """Comma-separated ids from several env vars, in order, without duplicates or blanks."""
    out: list[str] = []
    for n in names:
        for part in (os.getenv(n) or "").split(","):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


def pro_variant_ids() -> list[str]:
    # LEMON_PRO_VARIANT_IDS: every Pro variant (monthly, yearly, discount...). The old single
    # LEMONSQUEEZY_PRODUCT_ID (a variant id despite the name) keeps working and comes first.
    return _env_ids("LEMONSQUEEZY_PRODUCT_ID", "LEMON_PRO_VARIANT_IDS")


def ultimate_variant_ids() -> list[str]:
    return _env_ids("LEMONSQUEEZY_ULTIMATE_PRODUCT_ID", "LEMON_ULTIMATE_VARIANT_IDS")


# Lemon Squeezy subscription statuses -> do they keep the paid plan?
#   active, on_trial            paid
#   cancelled                   paid until ends_at (LS sends subscription_expired when it really ends)
#   past_due                    paid (grace while LS retries the card)
#   unpaid, expired, paused     free
PAID_STATUSES = {"active", "on_trial", "cancelled", "past_due"}


def _plan_for_variant(variant_id: str) -> Optional[str]:
    variant_id = str(variant_id or "").strip()
    if not variant_id:
        return None
    if variant_id in ultimate_variant_ids():
        return "ultimate"
    if variant_id in pro_variant_ids():
        return "pro"
    return None


_PLAN_RANK = {"free": 0, "pro": 1, "ultimate": 2}


def _checkout_signing_key() -> bytes:
    return (os.getenv("CHECKOUT_SIGNING_SECRET") or LEMONSQUEEZY_WEBHOOK_SECRET or "").encode()


def _checkout_sig(user_id: str) -> str:
    """Signs the user id we put in checkout custom data. Lemon Squeezy hosted buy links let anyone
    add their own custom data, so an unsigned user_id is never trusted."""
    key = _checkout_signing_key()
    if not key:
        return ""
    return hmac.new(key, f"tpp-checkout:{user_id}".encode(), hashlib.sha256).hexdigest()[:40]


def _clean_email(raw) -> str:
    email = normalize_email(raw)
    if not email or "@" not in email or any(c.isspace() for c in email) or len(email) > 320:
        return ""
    return email


def _resolve_webhook_user(meta: dict, attrs: dict) -> tuple[str, Optional[str]]:
    """(email, user_id) the event belongs to, or ("", None).
    1. custom_data.user_id (signed by /create-checkout): the Supabase user with that id, and only if
       its email matches attrs.user_email or custom_data.email when those are present.
    2. otherwise the exact (lowercased) email. Never a pattern match."""
    custom = meta.get("custom_data") or {}
    if not isinstance(custom, dict):
        custom = {}
    attrs_email = _clean_email(attrs.get("user_email"))
    custom_email = _clean_email(custom.get("email") or custom.get("user_email"))
    uid = parse_uuid(custom.get("user_id"))
    if uid:
        sig = str(custom.get("sig") or "")
        expected = _checkout_sig(uid)
        if expected and hmac.compare_digest(sig, expected):
            try:
                auth_user = supabase.auth.admin.get_user_by_id(uid).user
            except Exception:
                logger.exception("webhook: user lookup by id failed")
                auth_user = None
            if auth_user and auth_user.email:
                auth_email = normalize_email(auth_user.email)
                claimed = {e for e in (attrs_email, custom_email) if e}
                if claimed and auth_email not in claimed:
                    logger.warning("webhook: user_id %s email does not match the order email; event ignored", uid)
                    return "", None
                return auth_email, uid
            logger.warning("webhook: user_id %s not found; falling back to exact email", uid)
        else:
            logger.warning("webhook: unsigned or badly signed custom user_id; falling back to exact email")
    # Unsigned checkout: the order email (what the buyer paid with) wins over custom data.
    return (attrs_email or custom_email), None


def _record_subscription(sub_id: str, email: str, user_id: Optional[str], variant_id: str,
                         status: str, updated_at: Optional[str]) -> Optional[bool]:
    """Upsert the subscription row unless we already hold a NEWER state for it (LS can deliver
    out of order). True = applied, False = stale event ignored, None = table unavailable."""
    try:
        res = supabase.rpc("upsert_subscription", {
            "p_id": sub_id, "p_user_email": email, "p_user_id": user_id, "p_variant_id": variant_id,
            "p_status": status, "p_updated_at": updated_at,
        }).execute()
        return bool(res.data)
    except Exception:
        logger.exception("webhook: upsert_subscription failed (run migration 20261107_security_fixes.sql)")
        return None


def _effective_plan(email: str, user_id: Optional[str], fallback: str) -> str:
    """Best plan over all of this user's subscriptions that still pay. One lapsed subscription no
    longer downgrades someone who holds another active one (audit M4)."""
    try:
        rows = (supabase.table("subscriptions").select("variant_id, status")
                .eq("user_email", email).execute().data or [])
        if user_id:
            rows += (supabase.table("subscriptions").select("variant_id, status")
                     .eq("user_id", user_id).execute().data or [])
    except Exception:
        logger.exception("webhook: subscriptions read failed; using this event's plan")
        return fallback
    best = "free"
    for r in rows:
        if (r.get("status") or "").lower() in PAID_STATUSES:
            p = _plan_for_variant(str(r.get("variant_id") or "")) or "free"
            if _PLAN_RANK[p] > _PLAN_RANK[best]:
                best = p
    return best


class WebhookRetry(Exception):
    """Processing failed in a way a Lemon Squeezy retry can fix: answer 500, don't mark processed."""


def _set_plan(email: str, plan: str, new_period: bool) -> None:
    email = normalize_email(email)
    res = supabase.table("users").update({"plan": plan}).eq("user_email", email).execute()
    if not res.data:
        # No users row (signup trigger missing or late). Create it so the payment isn't lost.
        logger.error("Webhook: no users row for user %s; creating it with plan %s", email_hash(email), plan)
        try:
            supabase.table("users").insert({"user_email": email, "plan": plan}).execute()
        except Exception as e:
            if "duplicate" in str(e).lower() or "23505" in str(e):   # created concurrently
                supabase.table("users").update({"plan": plan}).eq("user_email", email).execute()
            else:
                logger.exception("Webhook: could not create users row for user %s", email_hash(email))
                raise WebhookRetry("users row insert failed")
    try:
        if new_period:
            credits_v2.start_period(email)      # fresh 30-day allowance on each successful payment
        else:
            credits_v2.get_balance(email)       # applies the new allowance, keeps usage
    except credits_v2.CreditError:
        logger.exception("Webhook credit update failed for user %s", email_hash(email))
        if new_period:
            raise WebhookRetry("start_credit_period failed")   # the plan is set; retry restarts the period


def _event_key(payload: dict) -> str:
    meta = payload.get("meta", {}) or {}
    data = payload.get("data", {}) or {}
    attrs = data.get("attributes", {}) or {}
    return (f"{meta.get('event_name') or ''}:{data.get('type')}:{data.get('id')}:"
            f"{attrs.get('updated_at') or attrs.get('created_at')}")


def _already_processed(event_key: str) -> bool:
    try:
        return bool(supabase.table("webhook_events").select("id").eq("id", event_key).limit(1).execute().data)
    except Exception:
        logger.exception("webhook_events read failed (processing anyway; handling is idempotent)")
        return False


def _mark_processed(event_key: str, event_name: str) -> None:
    try:
        supabase.table("webhook_events").insert({"id": event_key, "event_name": event_name}).execute()
    except Exception as e:
        if "duplicate" in str(e).lower() or "23505" in str(e):
            return                               # a concurrent delivery finished first
        logger.exception("webhook_events insert failed (event was processed)")


def _handle_webhook(payload: dict) -> dict:
    """Idempotency: LS retries deliveries; each (event, object, updated_at) is recorded as processed
    only AFTER it was handled. A failure raises (-> 500) so LS retries it."""
    event_key = _event_key(payload)
    if _already_processed(event_key):
        return {"status": "ok", "duplicate": True}
    result = _process_webhook(payload)
    _mark_processed(event_key, (payload.get("meta", {}) or {}).get("event_name") or "")
    return result


def _process_webhook(payload: dict) -> dict:
    meta = payload.get("meta", {}) or {}
    event_name = meta.get("event_name") or ""
    data = payload.get("data", {}) or {}
    attrs = data.get("attributes", {}) or {}

    email, user_id = _resolve_webhook_user(meta, attrs)
    if not email:
        logger.warning("Webhook %s: no usable user", event_name)
        return {"status": "ok"}

    if event_name.startswith("subscription_payment_"):
        # Payment events carry an invoice; only a success starts a new credit period.
        if event_name == "subscription_payment_success":
            rows = supabase.table("users").select("plan").eq("user_email", email).limit(1).execute().data or []
            if rows and is_paid(rows[0].get("plan")):
                try:
                    credits_v2.start_period(email)
                except credits_v2.CreditError:
                    logger.exception("Webhook: start_credit_period failed for user %s", email_hash(email))
                    raise WebhookRetry("start_credit_period failed")
        return {"status": "ok"}

    if not event_name.startswith("subscription_"):
        return {"status": "ok"}

    variant_id = str(attrs.get("variant_id") or "")
    plan = _plan_for_variant(variant_id)
    if plan is None:
        logger.info("Webhook %s: unknown variant %s; ignored", event_name, variant_id)
        return {"status": "ok"}

    status = "expired" if event_name == "subscription_expired" else (attrs.get("status") or "").lower()
    applied = _record_subscription(str(data.get("id") or ""), email, user_id, variant_id, status,
                                   attrs.get("updated_at") or attrs.get("created_at"))
    if applied is False:
        logger.info("Webhook %s for subscription %s is older than what we hold; ignored", event_name, data.get("id"))
        return {"status": "ok", "stale": True}

    event_plan = plan if status in PAID_STATUSES else "free"
    new_plan = event_plan if applied is None else _effective_plan(email, user_id, event_plan)
    _set_plan(email, new_plan, new_period=(event_name == "subscription_created" and new_plan != "free"))
    logger.info("user %s -> %s (%s, status=%s)", email_hash(email), new_plan, event_name, status)
    return {"status": "ok"}


def _handle_webhook_or_500(payload: dict) -> dict:
    try:
        return _handle_webhook(payload)
    except WebhookRetry as e:
        logger.error("webhook not processed (%s); answering 500 so Lemon Squeezy retries", e)
    except Exception:
        logger.exception("webhook processing failed; answering 500 so Lemon Squeezy retries")
    raise HTTPException(status_code=500, detail="Webhook not processed; please retry")


@app.post("/lemonsqueezy-webhook")
async def lemonsqueezy_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("x-signature") or ""
    if not LEMONSQUEEZY_WEBHOOK_SECRET or not signature:
        raise HTTPException(status_code=401, detail="No signature")
    digest = hmac.new(LEMONSQUEEZY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    # DB + GoTrue calls are synchronous: keep them off the event loop.
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")
    return await run_in_threadpool(_handle_webhook_or_500, payload)


# ==============================================================================
# ENDPOINT 12 — Create Checkout (supports Pro + Ultimate)
# ==============================================================================
class CheckoutRequest(BaseModel):
    email: str = ""          # ignored: the signed-in user's email is used
    plan:  str = "pro"


@app.post("/create-checkout")
@limiter.limit(CHECKOUT_LIMIT)
def create_checkout(checkout_data: CheckoutRequest, request: Request, user=Depends(resolve_user),
                    api_key: str = Security(verify_api_key)):
    if not user:
        raise HTTPException(status_code=401, detail="Sign in to subscribe")
    checkout_data.email = user.email   # never trust the email in the body
    if checkout_data.plan not in ("pro", "ultimate"):
        raise HTTPException(status_code=400, detail="Unknown plan")
    # Checkout uses the plan's first configured variant (the old single var, else the list's first).
    ids = ultimate_variant_ids() if checkout_data.plan == "ultimate" else pro_variant_ids()
    variant_id = ids[0] if ids else None
    if not variant_id:
        logger.error("checkout: no variant configured for plan %s", checkout_data.plan)
        raise HTTPException(status_code=503, detail="Checkout is not available right now. Please try again later.")

    logger.info("checkout: plan=%s user=%s", checkout_data.plan, user.id)

    headers = {
        "Authorization": f"Bearer {os.getenv('LEMONSQUEEZY_API_KEY')}",
        "Content-Type": "application/vnd.api+json",
        "Accept": "application/vnd.api+json",
    }

    payload = {
        "data": {
            "type": "checkouts",
            "attributes": {
                "checkout_data": {
                    "email": checkout_data.email,
                    "custom": {
                        "user_id": user.id,
                        "email": checkout_data.email,
                        "sig": _checkout_sig(user.id),
                    }
                }
            },
            "relationships": {
                "store": {
                    "data": {
                        "type": "stores",
                        "id": str(os.getenv("LEMONSQUEEZY_STORE_ID"))
                    }
                },
                "variant": {
                    "data": {
                        "type": "variants",
                        "id": str(variant_id)
                    }
                }
            }
        }
    }

    try:
        with httpx.Client(timeout=20) as client:
            response = client.post(
                "https://api.lemonsqueezy.com/v1/checkouts",
                json=payload,
                headers=headers
            )
    except httpx.HTTPError:
        logger.exception("LemonSqueezy checkout request failed")
        raise HTTPException(status_code=502, detail="Checkout creation failed")

    if response.status_code not in (200, 201):
        logger.error("LemonSqueezy checkout failed with HTTP %s", response.status_code)
        raise HTTPException(status_code=400, detail="Checkout creation failed")

    data = response.json()
    checkout_url = data.get("data", {}).get("attributes", {}).get("url")
    return {"success": True, "checkout_url": checkout_url}

# ==============================================================================
# LEGACY (blog tooling, job board, CV parser, AI proxy, autopilot).
# All of these answer 404 unless ENABLE_LEGACY_ENDPOINTS=true, on top of their own secret header.
# ==============================================================================
@app.post("/publish-blog-post", dependencies=[Depends(require_legacy)])
async def publish_blog_post(request: Request):
    require_admin(request)
    import httpx, jwt, time, os, json

    try:
           body = await request.json()
    except Exception:
           body = {}

    ghost_url     = os.environ.get("GHOST_URL", "https://blog.tikprofitpro.shop")
    admin_api_key = os.environ.get("GHOST_ADMIN_API_KEY", "")

    if not admin_api_key or ":" not in admin_api_key:
        return {"errors": [{"message": "GHOST_ADMIN_API_KEY missing or invalid"}]}

    title         = body.get("title", "Untitled")  # ← change request → body
    slug          = body.get("slug", "")
    meta          = body.get("meta", "")
    content       = body.get("content", "")
    feature_image = body.get("feature_image", None)

    # Build Ghost JWT
    key_id, key_secret = admin_api_key.split(":")
    iat = int(time.time())
    payload = {"iat": iat, "exp": iat + 300, "aud": "/admin/"}
    token = jwt.encode(
        payload,
        bytes.fromhex(key_secret),
        algorithm="HS256",
        headers={"kid": key_id}
    )

    # Build mobiledoc
    mobiledoc = json.dumps({
        "version": "0.3.1",
        "atoms": [],
        "cards": [["markdown", {"markdown": content}]],
        "markups": [],
        "sections": [[10, 0]]
    })

    post_data = {
        "posts": [{
            "title":          title,
            "slug":           slug,
            "custom_excerpt": meta,
            "mobiledoc":      mobiledoc,
            "status":         "published",
        }]
    }

    if feature_image:
        post_data["posts"][0]["feature_image"] = feature_image

    async with httpx.AsyncClient() as client:
        res = await client.post(
            f"{ghost_url}/ghost/api/admin/posts/",
            headers={
                "Authorization":  f"Ghost {token}",
                "Content-Type":   "application/json",
                "Accept-Version": "v5.0",
            },
            json=post_data,
            timeout=30.0,
        )

    try:
        data = res.json()
    except ValueError:
        logger.error("Ghost publish returned HTTP %s with a non-JSON body", res.status_code)
        return JSONResponse({"errors": [{"message": "Blog publish failed"}]}, status_code=502)

    # ── Auto-ping Google & Bing after successful publish ──
    if data.get("posts"):
        published_slug = data["posts"][0].get("slug", slug)
        published_url  = f"https://blog.tikprofitpro.shop/{published_slug}/"
        await ping_google(published_url)
        logger.info("Published and pinged: %s", published_url)

    return data

# ─── Blog Post Generator via OpenAI ──────────────────────────────
# ─── Blog Post Generator via Groq ──────────────────────────────
@app.post("/generate-blog-post", dependencies=[Depends(require_legacy)])
async def generate_blog_post(raw: Request):
    require_admin(raw)
    request = await raw.json()
    import httpx, os
    api_key = os.environ.get("GROQ_API_KEY", "")
    prompt  = request.get("prompt", "")

    if not api_key:
        return {"content": None, "error": "GROQ_API_KEY not set in Render"}

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":      "llama-3.3-70b-versatile",
                    "max_tokens": 2000,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=120.0,
            )
        data = res.json()

        if "choices" not in data:
            logger.error("Groq blog generation returned no choices (HTTP %s)", res.status_code)
            return {"content": None, "error": "The AI provider returned an error"}

        content = data["choices"][0]["message"]["content"]
        return data

    except Exception:
        logger.exception("generate-blog-post failed")
        return {"content": None, "error": "Blog post generation failed"}


@app.get("/get-image", dependencies=[Depends(require_legacy)])
async def get_image(request: Request, query: str = "ecommerce"):
    require_admin(request)
    import httpx, os

    access_key = os.environ.get("UNSPLASH_ACCESS_KEY", "")

    if not access_key:
        return {"url": None, "error": "UNSPLASH_ACCESS_KEY not set in Railway"}

    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(
                "https://api.unsplash.com/search/photos",
                params={"query": query, "per_page": 1, "orientation": "landscape"},
                headers={"Authorization": f"Client-ID {access_key}"},
                timeout=15.0,
            )
        data = res.json()
        results = data.get("results", [])
        if results:
            return {
                "url":         results[0]["urls"]["regular"],
                "thumb":       results[0]["urls"]["thumb"],
                "author":      results[0]["user"]["name"],
                "description": results[0].get("alt_description", ""),
            }
        return {"url": None, "error": f"No results from Unsplash. Total: {data.get('total', 0)}"}
    except Exception:
        logger.exception("get-image failed")
        return {"url": None, "error": "Image search failed"}
    
    # ─── Google Search Console Auto-Ping ─────────────────────────────
async def ping_google(url: str):
    """Ping Google to index a new URL immediately after publishing"""
    import httpx
    try:
        # Method 1: Google Ping (IndexNow)
        async with httpx.AsyncClient() as client:
            # Ping Google Search via IndexNow protocol
            await client.get(
                f"https://www.google.com/ping?sitemap=https://blog.tikprofitpro.shop/sitemap.xml",
                timeout=10.0,
            )
            # Also ping Bing (IndexNow) — they share signals with Google
            await client.post(
                "https://api.indexnow.org/indexnow",
                json={
                    "host":    "blog.tikprofitpro.shop",
                    "key":     os.environ.get("INDEXNOW_KEY", ""),
                    "urlList": [url],
                },
                headers={"Content-Type": "application/json"},
                timeout=10.0,
            )
    except Exception as e:
        logger.warning("Ping error (non-fatal): %s", type(e).__name__)


@app.get("/indexnow")
async def indexnow_verify():
    indexnow_key = os.environ.get("INDEXNOW_KEY", "")
    return PlainTextResponse(indexnow_key)

@app.post("/generate-listing")
@limiter.limit(LISTING_LIMIT)
def generate_listing(req: GenerateListingRequest, request: Request, user=Depends(resolve_user)):
    """
    Draft a TikTok Shop listing. Rewritten to stop generic "AI slop":
    - looks at the product photo, so it describes the real item
    - banned-word list (no "elevate", "game-changer", "must-have", emojis...)
    - concrete specs only; unknown facts become [fill in] placeholders instead of invented claims
    - respects TikTok Shop rules: no medical claims, no "best/#1", no competitor brands
    Requires a signed-in user (it spends OpenAI money). Plain `def`: runs in the threadpool.
    Image: looked up server-side from product_id. A client-sent image_url is only used when no
    product_id is given, and then only through safe_fetch (public hosts only, no internal network).
    """
    if not user:
        raise HTTPException(status_code=401, detail="Sign in to generate listings")
    email = user.email
    if not is_paid(access_plan(user, credits_v2.plan_for(email))):
        raise HTTPException(status_code=402, detail="The listing generator is a Pro feature")
    if not (GROQ_API_KEY or openai_sync_client):
        raise HTTPException(status_code=503, detail="Listing generator is not configured")

    image_url = ""
    if req.product_id:
        pid = parse_uuid(req.product_id)
        rows = (supabase.table("tiktok_products").select("image_storage_url, thumbnail_url")
                .eq("id", pid).limit(1).execute().data) if pid else []
        if not rows:
            raise HTTPException(status_code=404, detail="Product not found")
        image_url = rows[0].get("image_storage_url") or rows[0].get("thumbnail_url") or ""
    elif req.image_url:
        image_url = req.image_url

    image_part = []
    if image_url:
        from image_search import fetch_image_bytes, _data_url
        img = fetch_image_bytes(image_url)   # safe_fetch inside
        if img:
            image_part = [{"type": "image_url", "image_url": {"url": _data_url(img), "detail": "low"}}]

    system = """You write TikTok Shop product listings for a US seller who will sell their OWN white-label version of this product.

Write like a careful human merchandiser, not a marketer:
- Plain, specific, short sentences. Say what it is, what it's made of, size, what's in the box, who it's for.
- Only state facts you can see in the photo or read in the input. If a spec is unknown, write [fill in: size], [fill in: material] etc. Never invent numbers, certifications, ingredients or results.
- BANNED words/phrases: elevate, game-changer, must-have, revolutionary, transform, unleash, ultimate, perfect, effortless, seamless, luxurious, premium quality, take it to the next level, say goodbye, look no further, whether you're, in today's world, viral, best, #1, guaranteed. No emojis. No exclamation marks.
- TikTok Shop policy: no medical or cure claims, no before/after promises, no other brands' names.
- Title: max 120 characters, format "<Product type> - <key spec/material> - <key use>", no brand, no ALL CAPS.

Respond ONLY with JSON:
{"title": "...",
 "description": "3-4 short factual sentences",
 "sellingPoints": "5 lines separated by \\n, each starting with the feature, then the concrete benefit",
 "category": "one of: Beauty & Personal Care, Health, Fashion Accessories, Womenswear & Underwear, Home Supplies, Kitchenware, Phones & Electronics, Pet Supplies, Sports & Outdoor, Tools & Hardware, Toys & Hobbies",
 "missing_info": ["specs the seller must fill in before publishing"]}"""

    user_text = f"""Reference product title (from a competitor, may contain their brand; do NOT copy it): {req.product_name}
Selling price: ${req.price}
Category hint: {req.category}"""

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": [{"type": "text", "text": user_text}] + image_part},
    ]
    try:
        if GROQ_API_KEY:
            # Groq (OpenAI-compatible, much cheaper). Llama 4 Scout reads images.
            with httpx.Client(timeout=60) as client:
                r = client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    json={"model": os.getenv("LISTING_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),
                          "temperature": 0.3, "max_tokens": 700,
                          "response_format": {"type": "json_object"}, "messages": messages},
                )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
        else:
            resp = openai_sync_client.chat.completions.create(
                model="gpt-4o-mini", temperature=0.3, max_tokens=700,
                response_format={"type": "json_object"}, messages=messages,
            )
            content = resp.choices[0].message.content
        return json.loads(content or "{}")
    except Exception:
        logger.exception("generate-listing failed")
        raise HTTPException(status_code=502, detail="The listing generator couldn't finish. Try again in a minute.")


# ─── Get Shops ───────────────────────────────────────────────────
@app.get("/shops")
@limiter.limit(LIST_LIMIT, key_func=ip_key)
def get_shops(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    sort_by: str = "gmv_total",
    api_key: str = Security(verify_api_key),
):
    plan = get_user_plan(request)
    try:
        if not is_paid(plan):
            rows, _ = free_top_shops()
            return {"shops": rows, "total": len(rows), "limit": FREE_PRODUCT_LIMIT, "offset": 0, "plan": plan}

        # Valid sort columns
        allowed_sorts = ["gmv_total", "gmv_growth_pct", "created_at", "follower_count"]
        order_col = sort_by if sort_by in allowed_sorts else "gmv_total"

        res = supabase.table("shops")\
                .select("*", count="exact")\
                .order(order_col, desc=True, nullsfirst=False)\
                .order("id")\
                .range(offset, offset + limit - 1)\
                .execute()
        return {
            "shops": res.data,
            "total": res.count,
            "limit": limit,
            "offset": offset,
            "plan": plan,
            # The shops table is a separate (older) Kalodata shop export: it has no data date.
            # /shops/catalogue is built from the current product list instead.
            "source": "shops_table",
        }
    except Exception:
        # Was a 200 with an empty list, so the page said "no shops" during an outage.
        raise server_error("/shops")


def _shop_key(name) -> str:
    return " ".join(str(name or "").split()).lower()


def _load_catalogue_shops() -> dict:
    """Shops behind the ACTIVE products, from tiktok_products.shop_name (filled by every import).
    Real numbers only: product count, revenue in the export window, share of the list's revenue,
    revenue-weighted growth, and the shop's top products. The older shops table is joined by name
    for profile fields (followers, link) where a row exists."""
    rows = fetch_all(lambda: supabase.table("tiktok_products")
                     .select("id, product_name, shop_name, revenue_window, window_days, gmv_growth_pct, "
                             "virality_score, data_end_date, thumbnail_url, image_storage_url")
                     .eq("is_active", True).order("id"))
    total_rev = sum(float(r.get("revenue_window") or 0) for r in rows)
    groups: dict = {}
    no_shop = 0
    for r in rows:
        key = _shop_key(r.get("shop_name"))
        if not key:
            no_shop += 1
            continue
        g = groups.setdefault(key, {"shop_name": " ".join(str(r["shop_name"]).split()), "products": []})
        g["products"].append(r)
    profiles = {}
    try:
        for sh in fetch_all(lambda: supabase.table("shops")
                            .select("shop_name, follower_count, shop_url, seller_type, image_url")
                            .order("id")):
            profiles.setdefault(_shop_key(sh.get("shop_name")), sh)
    except Exception:
        logger.warning("shops profile join failed; catalogue shops are shown without profiles")
    out = []
    for key, g in groups.items():
        prods = g["products"]
        rev = sum(float(p.get("revenue_window") or 0) for p in prods)
        weighted = [(float(p["gmv_growth_pct"]), float(p.get("revenue_window") or 0)) for p in prods
                    if p.get("gmv_growth_pct") is not None]
        wsum = sum(w for _, w in weighted)
        growth = (round(sum(g_ * w for g_, w in weighted) / wsum, 1) if wsum
                  else (round(sum(g_ for g_, _ in weighted) / len(weighted), 1) if weighted else None))
        windows = {p.get("window_days") for p in prods if p.get("window_days")}
        top = sorted(prods, key=lambda p: (-(float(p.get("revenue_window") or 0)), str(p.get("id"))))[:3]
        prof = profiles.get(key) or {}
        out.append({
            "shop_name": g["shop_name"],
            "product_count": len(prods),
            "revenue_window": round(rev, 2),
            "window_days": next(iter(windows)) if len(windows) == 1 else None,
            "revenue_share_pct": round(rev / total_rev * 100, 2) if total_rev else None,
            "growth_pct": growth,
            "best_score": max((p.get("virality_score") or 0) for p in prods),
            "data_end_date": max((str(p["data_end_date"])[:10] for p in prods if p.get("data_end_date")), default=None),
            "top_products": [{"id": p["id"], "product_name": p.get("product_name"),
                              "virality_score": p.get("virality_score"),
                              "revenue_window": p.get("revenue_window"),
                              "image": p.get("image_storage_url") or p.get("thumbnail_url")} for p in top],
            "follower_count": prof.get("follower_count"),
            "shop_url": prof.get("shop_url"),
            "seller_type": prof.get("seller_type"),
            "image_url": prof.get("image_url"),
            "has_shop_profile": bool(prof),
        })
    out.sort(key=lambda x: (-x["revenue_window"], x["shop_name"].lower()))
    return {"shops": out, "catalogue_revenue": round(total_rev, 2), "products_without_shop": no_shop,
            "active_products": len(rows)}


@app.get("/shops/catalogue")
@limiter.limit(LIST_LIMIT, key_func=ip_key)
def get_catalogue_shops(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    api_key: str = Security(verify_api_key),
):
    """Shops ranked by revenue in the current product list, with product count and revenue share.
    Free: the top 10 shops, and only top products that are in the free top 10."""
    plan = get_user_plan(request)
    try:
        data = _cached("catalogue-shops", _load_catalogue_shops, LIST_CACHE_SECONDS)
        shops = data["shops"]
        if not is_paid(plan):
            free_ids = free_top_product_ids()
            page = [dict(s, top_products=[p for p in s["top_products"] if str(p["id"]) in free_ids])
                    for s in shops[:FREE_PRODUCT_LIMIT]]
            return {"shops": page, "total": len(page), "limit": FREE_PRODUCT_LIMIT, "offset": 0, "plan": plan,
                    "catalogue_revenue": data["catalogue_revenue"], "source": "catalogue"}
        return {"shops": shops[offset:offset + limit], "total": len(shops), "limit": limit, "offset": offset,
                "plan": plan, "catalogue_revenue": data["catalogue_revenue"],
                "products_without_shop": data["products_without_shop"], "source": "catalogue"}
    except Exception:
        raise server_error("/shops/catalogue")

# ─── Get Creators ─────────────────────────────────────────────────
@app.get("/creators")
@limiter.limit(LIST_LIMIT, key_func=ip_key)
def get_creators(
    request: Request,
    creator_type: str = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    sort_by: str = "gmv_total",
    api_key: str = Security(verify_api_key),
):
    plan = get_user_plan(request)
    try:
        if not is_paid(plan):
            # Fixed top 10: creator_type / sort_by / offset are ignored for unpaid callers.
            return {"creators": free_top_creators(), "plan": plan}
        order_col = sort_by if sort_by in ("gmv_total", "followers", "engagement_rate", "video_gmv", "live_gmv") else "gmv_total"
        query = supabase.table("creators").select("*")
        if creator_type:
            query = query.eq("creator_type", creator_type)
        res = query.order(order_col, desc=True, nullsfirst=False).range(offset, offset + limit - 1).execute()
        return {"creators": res.data or [], "plan": plan}
    except Exception:
        raise server_error("/creators")

# ================================================================
# /api/jobs — real jobs from Remotive + Adzuna
# ================================================================
@app.get("/api/jobs", dependencies=[Depends(require_legacy)])
async def get_jobs(request: Request, niches: str = "", location: str = "remote", job_type: str = "Any"):
    require_admin(request)
    niche_list = [n.strip().lower() for n in niches.split(",") if n.strip()]
    all_jobs = []
 
    # 1. REMOTIVE — free, no key needed
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for niche in niche_list[:3]:
                r = await client.get(
                    f"https://remotive.com/api/remote-jobs?search={niche.replace(' ','+')}&limit=20"
                )
                if r.status_code == 200:
                    for j in r.json().get("jobs", []):
                        all_jobs.append({
                            "id":          str(j.get("id")),
                            "title":       j.get("title", ""),
                            "company":     j.get("company_name", ""),
                            "source":      "Remotive",
                            "location":    j.get("candidate_required_location", "Remote"),
                            "type":        j.get("job_type", "full_time").replace("_", "-").title(),
                            "salary":      j.get("salary") or "Not listed",
                            "description": (j.get("description", "")[:300] + "...").strip(),
                            "url":         j.get("url", ""),
                            "applyEmail":  "",
                            "postedDate":  j.get("publication_date", "")[:10],
                            "skills":      j.get("tags", [])[:6],
                            "badge":       "new",
                            "real":        True,
                        })
    except Exception as e:
        print(f"[Remotive error] {e}")
 
    # 2. ADZUNA — free key from developer.adzuna.com
    if ADZUNA_ID and ADZUNA_KEY:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                for niche in niche_list[:2]:
                    r = await client.get(
                        f"https://api.adzuna.com/v1/api/jobs/us/search/1"
                        f"?app_id={ADZUNA_ID}&app_key={ADZUNA_KEY}"
                        f"&results_per_page=10&what={niche.replace(' ','%20')}"
                        f"&content-type=application/json"
                    )
                    if r.status_code == 200:
                        for j in r.json().get("results", []):
                            sal = ""
                            if j.get("salary_min") and j.get("salary_max"):
                                sal = f"${int(j['salary_min']):,} – ${int(j['salary_max']):,}"
                            all_jobs.append({
                                "id":          str(j.get("id")),
                                "title":       j.get("title", ""),
                                "company":     j.get("company", {}).get("display_name", ""),
                                "source":      "Adzuna",
                                "location":    j.get("location", {}).get("display_name", "Remote"),
                                "type":        "Full-time",
                                "salary":      sal or "Not listed",
                                "description": (j.get("description", "")[:300] + "...").strip(),
                                "url":         j.get("redirect_url", ""),
                                "applyEmail":  "",
                                "postedDate":  j.get("created", "")[:10],
                                "skills":      [],
                                "badge":       "new",
                                "real":        True,
                            })
        except Exception as e:
            print(f"[Adzuna error] {e}")
 
    # Deduplicate
    seen, unique = set(), []
    for j in all_jobs:
        key = f"{j['title'].lower()}|{j['company'].lower()}"
        if key not in seen:
            seen.add(key)
            unique.append(j)
 
    # Score against niches
    def score(job):
        text = (job["title"] + " " + job["description"] + " " + " ".join(job.get("skills",[]))).lower()
        s = 50
        for n in niche_list:
            words = n.split()
            if all(w in text for w in words): s += 30
            elif any(w in text for w in words): s += 15
        if job.get("salary") and job["salary"] != "Not listed": s += 5
        if job.get("postedDate", "") >= "2026-04": s += 8
        return min(s, 99)
 
    for j in unique:
        j["match"] = score(j)
        j["badge"] = "hot" if j["match"] >= 85 else "featured" if j["match"] >= 70 else "new"
 
    unique.sort(key=lambda x: x["match"], reverse=True)
 
    return JSONResponse({
        "jobs": unique[:15],
        "total": len(unique),
        "sources": {
            "remotive": sum(1 for j in unique[:15] if j["source"] == "Remotive"),
            "adzuna":   sum(1 for j in unique[:15] if j["source"] == "Adzuna"),
        }
    })
 
 
# ================================================================
# /api/parse-cv — upload PDF, GPT extracts structured profile
# ================================================================
@app.post("/api/parse-cv", dependencies=[Depends(require_legacy)])
async def parse_cv(request: Request):
    # Auth + size checks run BEFORE the body is read (no File() param, so FastAPI doesn't parse it).
    require_admin(request)
    _require_content_length(request, _MAX_UPLOAD_BYTES)
    form = await request.form(max_files=1, max_fields=5)
    tmp_path = None
    try:
        file = form.get("file")
        if not file or not hasattr(file, "read"):
            return JSONResponse({"error": "Upload a PDF as 'file'"}, status_code=400)
        if not (file.filename or "").lower().endswith(".pdf"):
            return JSONResponse({"error": "Only PDF files supported"}, status_code=400)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = tmp.name
            await _copy_upload(file, tmp, _MAX_UPLOAD_BYTES)

        def _extract(path: str) -> str:
            import pdfplumber
            out = ""
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    out += (page.extract_text() or "") + "\n"
            return out

        try:
            text = await run_in_threadpool(_extract, tmp_path)
        except Exception:
            logger.exception("parse-cv: PDF read error")
            return JSONResponse({"error": "Could not read this PDF"}, status_code=400)
    finally:
        await form.close()
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    if not text.strip():
        return JSONResponse({"error": "Could not extract text from PDF"}, status_code=400)
 
    prompt = f"""Extract structured info from this CV. Return ONLY valid JSON, no markdown:
{{
  "name": "full name",
  "email": "email or empty string",
  "role": "current or most recent job title",
  "linkedin": "linkedin url or empty string",
  "skills": "comma separated top 10 technical skills",
  "bio": "2-sentence professional summary",
  "projects": "comma separated key projects max 5",
  "experience_years": "number as string",
  "education": "degree and institution"
}}
CV:
{text[:4000]}"""
 
    try:
        resp = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            temperature=0.2,
        )
        raw = resp.choices[0].message.content or ""
        s, e = raw.find("{"), raw.rfind("}")
        return JSONResponse({"profile": json.loads(raw[s:e+1]), "cv_text": text[:2000]})
    except Exception:
        logger.exception("parse-cv: AI parse failed")
        return JSONResponse({"error": "Could not parse this CV"}, status_code=500)
 
 
# ================================================================
# /api/extract-email — fetch job page, GPT extracts apply email
# ================================================================
@app.get("/api/extract-email", dependencies=[Depends(require_legacy)])
async def extract_email(request: Request, url: str = ""):
    require_admin(request)
    if openai_client is None:
        return JSONResponse({"email": "", "error": "AI not configured"})
    if not url.startswith(("http://", "https://")):
        return JSONResponse({"email": ""})
    """
    Fetches the job posting page and uses GPT to extract
    any apply/contact email address from the HTML content.
    Returns {"email": "found@email.com"} or {"email": ""}
    """
    if not url:
        return JSONResponse({"email": ""})
 
    # Fetch the job page (SSRF-safe: public hosts only, redirects re-checked, 2 MB cap)
    r = await run_in_threadpool(safe_fetch, url, 2_000_000, 12.0, 3, None,
                                {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    if r is None:
        return JSONResponse({"email": "", "error": "Could not fetch that page"})
    html = r.text
 
    # Strip HTML tags — keep only visible text
    import re
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'\s+', ' ', text).strip()
 
    # Quick regex pass first — fast and free
    email_pattern = re.findall(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', text)
    # Filter out common non-apply emails
    exclude = ['sentry', 'noreply', 'no-reply', 'example', 'placeholder', 'wixpress',
               '.png', '.jpg', 'support@', 'info@lever', 'info@greenhouse']
    candidates = [e for e in email_pattern if not any(x in e.lower() for x in exclude)]
 
    if candidates:
        # Return the first clean candidate directly — no GPT needed
        return JSONResponse({"email": candidates[0], "all": candidates[:5], "method": "regex"})
 
    # No email found via regex — try GPT on page text
    try:
        prompt = f"""Look at this job posting page text and find any email address for applying or contacting about the job.
Return ONLY valid JSON: {{"email": "found@email.com"}} or {{"email": ""}} if none found.
Do not invent or guess emails. Only return real emails visible in the text.
 
Page text (first 2000 chars):
{text[:2000]}"""
 
        resp = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=80,
            temperature=0,
        )
        raw = resp.choices[0].message.content or ""
        s, e = raw.find("{"), raw.rfind("}")
        result = json.loads(raw[s:e+1])
        result["method"] = "gpt"
        return JSONResponse(result)
    except Exception:
        logger.exception("extract-email: AI step failed")
        return JSONResponse({"email": "", "error": "Could not read an email from that page"})
 
 
# ================================================================
# /api/ai-proxy — cover email + CV summary generation (unchanged)
# ================================================================
AI_PROXY_MODELS = {m.strip() for m in os.getenv("AI_PROXY_MODELS", "gpt-4o-mini").split(",") if m.strip()}
AI_PROXY_MAX_TOKENS = 2000


@app.post("/api/ai-proxy", dependencies=[Depends(require_legacy)])
async def ai_proxy(request: Request, api_key: str = Security(verify_api_key)):
    require_admin(request)
    if openai_client is None:
        return JSONResponse({"error": "AI not configured"}, status_code=503)
    # Was open to the whole internet: anyone could spend the OpenAI balance.
    try:
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "Send a JSON object"}, status_code=400)
        model = body.get("model") or "gpt-4o-mini"
        if model not in AI_PROXY_MODELS:          # caller can't pick an expensive model
            model = "gpt-4o-mini" if "gpt-4o-mini" in AI_PROXY_MODELS or not AI_PROXY_MODELS else sorted(AI_PROXY_MODELS)[0]
        try:
            max_tokens = int(body.get("max_tokens") or AI_PROXY_MAX_TOKENS)
        except (TypeError, ValueError):
            max_tokens = AI_PROXY_MAX_TOKENS
        max_tokens = max(1, min(max_tokens, AI_PROXY_MAX_TOKENS))
        try:
            temperature = float(body.get("temperature", 0.7))
        except (TypeError, ValueError):
            temperature = 0.7
        messages = body.get("messages", [])
        if not isinstance(messages, list):
            return JSONResponse({"error": "messages must be a list"}, status_code=400)
        resp = await openai_client.chat.completions.create(
            model       = model,
            messages    = messages,
            max_tokens  = max_tokens,
            temperature = max(0.0, min(temperature, 1.5)),
        )
        return JSONResponse({"content": resp.choices[0].message.content or ""})
    except Exception:
        logger.exception("ai-proxy failed")
        return JSONResponse({"error": "AI request failed"}, status_code=500)
 
 
# ================================================================
# /jobs — serves job_hunt.html
# ================================================================
@app.get("/jobs", response_class=HTMLResponse, dependencies=[Depends(require_legacy)])
async def job_hunt_ui(request: Request):
    require_admin(request)
    path = os.getenv("JOB_HUNT_HTML", r"F:\profitscout\job_hunt.html")
    if not os.path.exists(path):
        return HTMLResponse("<h2>job_hunt.html not found</h2>", status_code=404)
    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())
 

 # ─── Daily Blog Autopilot Endpoint ───────────────────────────────
@app.post("/run-autopilot", dependencies=[Depends(require_legacy)])
async def run_autopilot(request: Request):
    expected = os.environ.get("AUTOPILOT_SECRET", "")
    given = request.headers.get("X-Autopilot-Secret", "")
    if not expected or not hmac.compare_digest(given, expected):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        from autopilot import run
        import threading
        thread = threading.Thread(target=run)
        thread.start()
        return {"status": "started", "message": "Autopilot running in background"}
    except Exception:
        logger.exception("run-autopilot failed to start")
        return {"error": "Autopilot could not start"}


# ==============================================================================
# ADMIN — supplier review queue
# Admins: signed-in users whose Supabase user id is in ADMIN_USER_IDS (comma separated UUIDs),
# or whose email is in ADMIN_EMAILS AND confirmed (email_confirmed_at set). Prefer ids.
#   Pending   = near-miss photo matches (default 0.60-0.79): same product, different bottle/label/angle
#   Live      = what users see (auto-verified >= 0.80, or hand-approved)
#   Rejected  = never queued again for that product
# ==============================================================================
ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}
ADMIN_USER_IDS = {u for u in (parse_uuid(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()) if u}


def is_admin(user) -> bool:
    if not user:
        return False
    if user.id in ADMIN_USER_IDS:
        return True
    # Email alone proves nothing while signup auto-confirms; require a confirmed address.
    return bool(user.email_confirmed and user.email in ADMIN_EMAILS)


def require_admin_user(request: Request) -> str:
    """Returns the admin's email (used for reviewed_by). Uses the user cached on request.state."""
    user = credits_v2.user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Admins only")
    return user.email


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@app.get("/admin/me")
@limiter.limit(ADMIN_LIMIT)
def admin_me(request: Request, user=Depends(resolve_user), api_key: str = Security(verify_api_key)):
    return {"admin": is_admin(user)}


@app.get("/me")
@limiter.limit(POLL_LIMIT)
def me(request: Request, user=Depends(resolve_user), api_key: str = Security(verify_api_key)):
    """What the UI needs to show the right plan: `plan` is what the caller can see (admins = ultimate),
    `billing_plan` is the stored users.plan that credits and Lemon Squeezy use."""
    if not user:
        return {"signed_in": False, "plan": "free", "billing_plan": "free", "admin": False}
    admin = is_admin(user)
    billing = _db_plan(user.email)
    return {"signed_in": True, "plan": access_plan(user, billing), "billing_plan": billing, "admin": admin}


@app.get("/admin/review/queue")
@limiter.limit(ADMIN_LIMIT)
def admin_review_queue(
    request: Request,
    view: str = Query(default="pending", pattern="^(pending|no_match|live)$"),
    platform: Optional[str] = Query(default=None, pattern="^(alibaba|aliexpress)$"),
    limit: int = Query(default=50, ge=1, le=200),
    user=Depends(resolve_user),
    api_key: str = Security(verify_api_key),
):
    """Products to look at. pending: has near-misses waiting. no_match: last sourcing found nothing
    verified. live: has suppliers users can see (to spot wrong auto-matches)."""
    require_admin_user(request)
    counts: dict = {}

    def bump(pid, plat, key):
        c = counts.setdefault((str(pid), plat), {"pending": 0, "live": 0, "status": None})
        c[key] = c.get(key, 0) + 1

    if view in ("pending", "no_match"):
        def cand_q():
            q = (supabase.table("supplier_candidates").select("id, tiktok_product_id, platform")
                 .eq("status", "pending"))
            return (q.eq("platform", platform) if platform else q).order("id")
        for r in fetch_all(cand_q, max_rows=20000):
            bump(r["tiktok_product_id"], r["platform"], "pending")
    if view in ("no_match", "live"):
        def jobs_q():
            q = (supabase.table("sourcing_jobs").select("product_id, platform, status, error, id")
                 .in_("status", ["done", "no_match"]))
            return (q.eq("platform", platform) if platform else q).order("id", desc=True)
        last = {}
        for r in fetch_all(jobs_q, max_rows=50000):
            last.setdefault((str(r["product_id"]), r["platform"]), r)
        for key, r in last.items():
            if (view == "no_match" and r["status"] == "no_match") or (view == "live" and r["status"] == "done"):
                c = counts.setdefault(key, {"pending": 0, "live": 0, "status": None})
                c["status"], c["note"] = r["status"], (r.get("error") or "")[:200]
        if view == "no_match":
            counts = {k: v for k, v in counts.items() if v["status"] == "no_match"}
    if view == "live":
        counts = {k: v for k, v in counts.items() if v["status"] == "done"}

    ids = list({pid for pid, _ in counts})[:1000]
    products = {}
    for i in range(0, len(ids), 200):
        for p in supabase.table("tiktok_products").select(
            "id, product_name, tiktok_price, thumbnail_url, image_storage_url, virality_score"
        ).in_("id", ids[i:i + 200]).execute().data or []:
            products[str(p["id"])] = p
    items = [
        {**products[pid], "platform": plat, **c}
        for (pid, plat), c in counts.items() if pid in products
    ]
    items.sort(key=lambda x: (-(x.get("pending") or 0), -(x.get("virality_score") or 0)))
    return {"view": view, "count": len(items), "items": items[:limit]}


@app.get("/admin/review/products/{product_id}")
@limiter.limit(ADMIN_LIMIT)
def admin_review_product(product_id: str, request: Request, user=Depends(resolve_user),
                         api_key: str = Security(verify_api_key)):
    """Everything about one product's sourcing: live suppliers, near-misses, rejected, last runs."""
    require_admin_user(request)
    pid = parse_uuid(product_id)
    rows = (supabase.table("tiktok_products").select(
        "id, product_name, tiktok_price, thumbnail_url, image_storage_url, category, virality_score"
    ).eq("id", pid).limit(1).execute().data) if pid else []
    if not rows:
        raise HTTPException(status_code=404, detail="Product not found")
    product, product_id = rows[0], pid
    suppliers = (supabase.table("suppliers").select("*").eq("tiktok_product_id", product_id)
                 .order("similarity", desc=True, nullsfirst=False).execute().data or [])
    for s in suppliers:
        s["visible"] = supplier_visible(s)
    candidates = (supabase.table("supplier_candidates").select("*").eq("tiktok_product_id", product_id)
                  .in_("status", ["pending", "rejected"]).order("similarity", desc=True).execute().data or [])
    jobs = (supabase.table("sourcing_jobs").select("platform, status, error, result_count, finished_at")
            .eq("product_id", product_id).order("id", desc=True).limit(6).execute().data or [])
    return {"product": product, "suppliers": suppliers, "candidates": candidates, "jobs": jobs,
            "min_similarity": MIN_SIMILARITY}


@app.post("/admin/candidates/{candidate_id}/approve")
@limiter.limit(ADMIN_LIMIT)
def admin_approve_candidate(candidate_id: int, request: Request, user=Depends(resolve_user),
                            api_key: str = Security(verify_api_key)):
    admin = require_admin_user(request)
    found = supabase.table("supplier_candidates").select("*").eq("id", candidate_id).limit(1).execute().data
    c = found[0] if found else None
    if not c:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if c["status"] == "approved":
        return {"success": True, "supplier_id": c.get("supplier_id")}
    row = {
        "tiktok_product_id": c["tiktok_product_id"], "platform": c["platform"],
        "supplier_name": c.get("supplier_name"), "product_title": c.get("product_title"),
        "unit_cost_min": c.get("unit_cost_min"), "unit_cost_max": c.get("unit_cost_max"),
        "moq": c.get("moq"), "supplier_url": c.get("supplier_url"), "image_url": c.get("image_url"),
        "similarity": c.get("similarity"), "match_method": "manual",
        "external_product_id": c.get("external_product_id"), "orders": c.get("orders"),
        "rating": c.get("rating"), "years_on_platform": c.get("years_on_platform"),
        "supplier_trust_score": min((c.get("years_on_platform") or 0) / 2, 5.0),
        "verified_supplier": c.get("verified_supplier"),
        "shipping_estimate_days": 20 if c["platform"] == "alibaba" else 10,
        "whitelabel_available": c["platform"] == "alibaba",
        "sourced_at": _now(), "reviewed_by": admin, "reviewed_at": _now(), "hidden": False,
    }
    saved = supabase.table("suppliers").insert(row).execute().data
    sid = saved[0]["id"] if saved else None
    supabase.table("supplier_candidates").update(
        {"status": "approved", "reviewed_by": admin, "reviewed_at": _now(), "supplier_id": sid}
    ).eq("id", candidate_id).execute()
    return {"success": True, "supplier_id": sid}


@app.post("/admin/candidates/{candidate_id}/reject")
@limiter.limit(ADMIN_LIMIT)
def admin_reject_candidate(candidate_id: int, request: Request, user=Depends(resolve_user),
                           api_key: str = Security(verify_api_key)):
    admin = require_admin_user(request)
    supabase.table("supplier_candidates").update(
        {"status": "rejected", "reviewed_by": admin, "reviewed_at": _now()}
    ).eq("id", candidate_id).execute()
    return {"success": True}


@app.post("/admin/candidates/{candidate_id}/reopen")
@limiter.limit(ADMIN_LIMIT)
def admin_reopen_candidate(candidate_id: int, request: Request, user=Depends(resolve_user),
                           api_key: str = Security(verify_api_key)):
    """Undo a reject (or an approve: the supplier row it created is hidden)."""
    admin = require_admin_user(request)
    found = supabase.table("supplier_candidates").select("status, supplier_id").eq("id", candidate_id).limit(1).execute().data
    c = found[0] if found else None
    if not c:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if c.get("supplier_id"):
        supabase.table("suppliers").update({"hidden": True, "reviewed_by": admin, "reviewed_at": _now()}).eq("id", c["supplier_id"]).execute()
    supabase.table("supplier_candidates").update(
        {"status": "pending", "reviewed_by": None, "reviewed_at": None, "supplier_id": None}
    ).eq("id", candidate_id).execute()
    return {"success": True}


class SupplierVisibility(BaseModel):
    hidden: bool


@app.post("/admin/suppliers/{supplier_id}/visibility")
@limiter.limit(ADMIN_LIMIT)
def admin_supplier_visibility(supplier_id: int, body: SupplierVisibility, request: Request,
                              user=Depends(resolve_user), api_key: str = Security(verify_api_key)):
    """Hide a wrong auto-match from users (or show it again). Re-sourcing keeps it hidden."""
    admin = require_admin_user(request)
    supabase.table("suppliers").update(
        {"hidden": body.hidden, "reviewed_by": admin, "reviewed_at": _now()}
    ).eq("id", supplier_id).execute()
    return {"success": True}


class ManualSupplier(BaseModel):
    product_id:    str
    platform:      str
    supplier_url:  str
    supplier_name: str
    unit_cost_min: float
    unit_cost_max: Optional[float] = None
    moq:           Optional[int] = None
    image_url:     Optional[str] = None
    product_title: Optional[str] = None


@app.post("/admin/suppliers")
@limiter.limit(ADMIN_LIMIT)
def admin_add_supplier(body: ManualSupplier, request: Request, user=Depends(resolve_user),
                       api_key: str = Security(verify_api_key)):
    """Add a supplier the search missed (paste the listing link)."""
    admin = require_admin_user(request)
    if body.platform not in ("alibaba", "aliexpress", "amazon"):
        raise HTTPException(status_code=400, detail="Platform must be alibaba, aliexpress or amazon")
    if not body.supplier_url.strip().startswith("https://") or not is_http_url(body.supplier_url.strip()):
        raise HTTPException(status_code=400, detail="Paste the full https:// link")
    if not parse_uuid(body.product_id):
        raise HTTPException(status_code=400, detail="Unknown product")
    if body.image_url and not is_http_url(body.image_url.strip()):
        raise HTTPException(status_code=400, detail="Image link must start with https://")
    row = {
        "tiktok_product_id": body.product_id, "platform": body.platform,
        "supplier_name": body.supplier_name.strip(), "product_title": body.product_title,
        "unit_cost_min": body.unit_cost_min, "unit_cost_max": body.unit_cost_max or body.unit_cost_min,
        "moq": body.moq, "supplier_url": body.supplier_url.strip(), "image_url": body.image_url,
        "similarity": None, "match_method": "manual", "hidden": False,
        "shipping_estimate_days": 20 if body.platform == "alibaba" else 10,
        "whitelabel_available": body.platform == "alibaba",
        "sourced_at": _now(), "reviewed_by": admin, "reviewed_at": _now(),
    }
    saved = supabase.table("suppliers").insert(row).execute().data
    return {"success": True, "supplier": saved[0] if saved else row}


# ==============================================================================
# ADMIN: weekly data upload + supplier sourcing (no PC needed)
#   /admin/upload page in the app uses these.
#   1) POST /admin/import          upload Kalodata CSVs; dry_run=true previews, false imports (background)
#   2) GET  /admin/import/{job}    progress + log of an import
#   3) POST /admin/sourcing/run    starts the "Find suppliers" GitHub Actions workflow
#   4) GET  /admin/sourcing/runs   last runs of that workflow
# Render env for 3/4: GITHUB_ACTIONS_TOKEN (fine-grained token, Actions: read and write on the
# backend repo), optional GITHUB_REPO (default sodewala45/profitscout), GITHUB_REF (default main).
# ==============================================================================
import threading as _threading
import uuid as _uuid
import shutil as _shutil

import import_kalodata as _kalo

_IMPORT_JOBS: dict = {}
_IMPORT_LOCK = _threading.Lock()
_MAX_UPLOAD_BYTES = 30 * 1024 * 1024
_GH_REPO = os.getenv("GITHUB_REPO", "sodewala45/profitscout")
_GH_REF = os.getenv("GITHUB_REF", "main")
_GH_WORKFLOW = os.getenv("GITHUB_SOURCING_WORKFLOW", "presource.yml")


# Import jobs used to live only in _IMPORT_JOBS (this process's memory): a Render restart or deploy
# mid-import lost the job, the page polled into a 404 and nobody knew how far it got. Real imports
# are now also saved to the import_jobs table (migration 20261114_core_logic.sql). Without that
# table the API keeps working from memory only and logs one warning.
_JOB_TABLE = "import_jobs"
_JOB_HEARTBEAT_SECONDS = 15
_JOB_STALE_SECONDS = 120          # a "running" row not updated for this long belongs to a dead process
_job_store = {"ok": True}
_JOB_PRIVATE = ("dir", "_saved")


def _job_view(job: dict) -> dict:
    return {k: v for k, v in job.items() if k not in _JOB_PRIVATE}


def _persist_job(job: dict, force: bool = False) -> None:
    if job.get("dry_run") or not _job_store["ok"] or not job.get("id"):
        return
    now = time.time()
    if not force and now - job.get("_saved", 0) < 5:
        return
    job["_saved"] = now
    row = {"id": job["id"], "status": job["status"], "started_by": job.get("by"),
           "started_at": job.get("started_at"), "finished_at": job.get("finished_at"),
           "files": job.get("files") or [], "summary": job.get("summary"),
           "warnings": job.get("warnings") or [], "error": job.get("error"),
           "log": (job.get("log") or [])[-400:], "updated_at": _now()}
    try:
        supabase.table(_JOB_TABLE).upsert(row, on_conflict="id").execute()
    except Exception as e:
        _job_store["ok"] = False
        logger.warning("import job not saved to %s (%s); jobs are kept in memory only. Run "
                       "supabase/migrations/20261114_core_logic.sql", _JOB_TABLE, type(e).__name__)


def _stored_job(job_id: str) -> Optional[dict]:
    if not _job_store["ok"]:
        return None
    try:
        rows = supabase.table(_JOB_TABLE).select("*").eq("id", job_id).limit(1).execute().data
    except Exception:
        return None
    if not rows:
        return None
    job = dict(rows[0])
    job["by"] = job.pop("started_by", None)
    job["dry_run"] = False
    return _mark_if_interrupted(job)


def _mark_if_interrupted(job: dict) -> dict:
    """A stored 'running' job that this process isn't running and that stopped sending heartbeats
    was cut off by a restart. Say so instead of showing 'running' forever."""
    if job.get("status") != "running" or job.get("id") in _IMPORT_JOBS:
        return job
    try:
        seen = datetime.fromisoformat(str(job.get("updated_at") or job.get("started_at")).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - seen).total_seconds()
    except Exception:
        age = _JOB_STALE_SECONDS + 1
    if age <= _JOB_STALE_SECONDS:
        return job
    job.update(status="failed", finished_at=job.get("finished_at") or _now(),
               error="The server restarted during this import, so it stopped part-way. Run the same files "
                     "again; it's safe to repeat.")
    job["log"] = list(job.get("log") or []) + [f"Stopped: {job['error']}"]
    try:
        supabase.table(_JOB_TABLE).update({"status": "failed", "error": job["error"], "log": job["log"][-400:],
                                           "finished_at": job["finished_at"], "updated_at": _now()}) \
            .eq("id", job["id"]).eq("status", "running").execute()
    except Exception:
        pass
    return job


def _run_import(job: dict, files: list, dry_run: bool, rehost: bool, deactivate: bool):
    def log(msg=""):
        job["log"].append(str(msg))
        job["log"] = job["log"][-400:]
        _persist_job(job)
    beat = _threading.Event()

    def heartbeat():                       # image copying can run minutes without a log line
        while not beat.wait(_JOB_HEARTBEAT_SECONDS):
            _persist_job(job, force=True)
    if not dry_run:
        _threading.Thread(target=heartbeat, daemon=True).start()
    try:
        groups = {"products": [], "creators": [], "shops": []}
        for f in files:
            if f["kind"] in groups:
                groups[f["kind"]].append(f["path"])
        summary = {}
        if groups["products"]:
            log(f"== Products ({len(groups['products'])} file(s))")
            summary["products"] = _kalo.import_products(groups["products"], dry_run, rehost, deactivate, log=log)
        if groups["creators"]:
            log(f"== Creators ({len(groups['creators'])} file(s))")
            summary["creators"] = _kalo.import_creators(groups["creators"], dry_run, log=log)
        if groups["shops"]:
            log(f"== Shops ({len(groups['shops'])} file(s))")
            summary["shops"] = _kalo.import_shops(groups["shops"], dry_run, log=log)
        job["summary"] = summary
        # Skipped columns, v1 stage fallback, images not copied...: the import worked but the UI
        # must not show a plain green "done" (2026-11-12/13 drift was only visible in the log).
        job["warnings"] = [w for part in summary.values() if isinstance(part, dict)
                           for w in (part.get("warnings") or [])]
        job["status"] = "done"
    except Exception as e:
        job["status"] = "failed"
        # the reason, not just "APIError" (admin-only endpoint; the full text is in the log too)
        job["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        log(f"Stopped: {type(e).__name__}: {str(e)[:300]}")
        if not dry_run:
            log("Some rows may already be written (snapshots are saved first). Fix the error and run it again; it's safe to repeat.")
    finally:
        beat.set()
        if not dry_run:
            clear_list_caches()          # also after a failure: snapshots / some batches may be written
        job["finished_at"] = _now()
        _persist_job(job, force=True)
        _shutil.rmtree(job["dir"], ignore_errors=True)
        if not dry_run and _IMPORT_LOCK.locked():
            _IMPORT_LOCK.release()


_UPLOAD_OVERHEAD = 1024 * 1024   # multipart boundaries + form fields on top of the file bytes


def _require_content_length(request: Request, max_bytes: int) -> None:
    """Reject before reading a single body byte. uvicorn/h11 never deliver more than the declared
    Content-Length, so checking the header caps the whole body."""
    raw = request.headers.get("content-length")
    if raw is None or not raw.isdigit():
        raise HTTPException(status_code=411, detail="Upload size unknown (Content-Length required)")
    if int(raw) > max_bytes + _UPLOAD_OVERHEAD:
        raise HTTPException(status_code=413, detail=f"Files are larger than {max_bytes // (1024 * 1024)} MB in total")


async def _copy_upload(up, fh, budget: int) -> int:
    """Copy an UploadFile to fh in 1 MB chunks; stop with 413 once `budget` bytes are exceeded."""
    n = 0
    while True:
        chunk = await up.read(1024 * 1024)
        if not chunk:
            return n
        n += len(chunk)
        if n > budget:
            raise HTTPException(status_code=413, detail=f"Files are larger than {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB in total")
        fh.write(chunk)


def _form_bool(v, default: bool) -> bool:
    if v is None or isinstance(v, (bytes,)) or hasattr(v, "read"):
        return default
    v = str(v).strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


@app.post("/admin/import")
@limiter.limit(ADMIN_LIMIT)
async def admin_import(
    request: Request,
    user=Depends(resolve_user),
    api_key: str = Security(verify_api_key),
):
    """Upload Kalodata CSV exports (multipart: files[], dry_run, deactivate, rehost).
    Each file's type (products / creators / shops) is read from its columns.
    The admin check and the size check run BEFORE the body is read: this route has no File()/Form()
    params, so FastAPI doesn't parse the multipart body up front (audit M6)."""
    admin = require_admin_user(request)          # user already resolved (in the threadpool) by resolve_user
    _require_content_length(request, _MAX_UPLOAD_BYTES)
    form = await request.form(max_files=50, max_fields=20)
    folder = tempfile.mkdtemp(prefix="kalo_")
    handed_off = False
    try:
        files = [f for f in form.getlist("files") if hasattr(f, "read")]
        dry_run = _form_bool(form.get("dry_run"), True)
        deactivate = _form_bool(form.get("deactivate"), True)
        rehost = _form_bool(form.get("rehost"), True)
        if not files:
            raise HTTPException(status_code=400, detail="Add at least one CSV file")
        saved, total = [], 0
        for up in files:
            name = os.path.basename(up.filename or "file.csv")
            if not name.lower().endswith((".csv", ".xlsx")):
                raise HTTPException(status_code=400, detail=f"{name} is not a .csv or .xlsx file")
            path = os.path.join(folder, f"{len(saved)}_{name}")
            with open(path, "wb") as fh:
                size = await _copy_upload(up, fh, _MAX_UPLOAD_BYTES - total)
            total += size
            saved.append({"name": name, "path": path, "kind": None, "bytes": size})
        for f in saved:
            f["kind"] = await run_in_threadpool(_kalo.detect_kind, f["path"])

        unknown = [f["name"] for f in saved if not f["kind"]]
        job = {"id": _uuid.uuid4().hex[:12], "status": "running", "dry_run": dry_run, "by": admin,
               "started_at": _now(), "finished_at": None, "log": [], "dir": folder,
               "files": [{k: f[k] for k in ("name", "kind", "bytes")} for f in saved]}
        if unknown:
            job["log"].append(f"Not recognised as a Kalodata or FastMoss product export, skipped: {', '.join(unknown)}")
        if not any(f["kind"] for f in saved):
            raise HTTPException(status_code=400, detail="None of these files look like Kalodata or FastMoss product exports")

        if dry_run:  # writes nothing; parsed in the threadpool so big files don't stall the API
            handed_off = True                    # _run_import removes the folder
            await run_in_threadpool(_run_import, job, saved, True, False, False)
            return _job_view(job)

        if not _IMPORT_LOCK.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="An import is already running. Wait for it to finish.")
        _IMPORT_JOBS[job["id"]] = job
        for old in sorted(_IMPORT_JOBS, key=lambda j: _IMPORT_JOBS[j]["started_at"])[:-10]:
            _IMPORT_JOBS.pop(old, None)
        await run_in_threadpool(_persist_job, job, True)
        try:
            _threading.Thread(target=_run_import, args=(job, saved, False, rehost, deactivate), daemon=True).start()
        except BaseException:
            _IMPORT_JOBS.pop(job["id"], None)
            _IMPORT_LOCK.release()           # else every later import is 409 until a restart
            raise
        handed_off = True
        return _job_view(job)
    finally:
        await form.close()
        if not handed_off:
            _shutil.rmtree(folder, ignore_errors=True)


@app.get("/admin/import/{job_id}")
@limiter.limit(ADMIN_LIMIT)
def admin_import_status(job_id: str, request: Request, user=Depends(resolve_user),
                        api_key: str = Security(verify_api_key)):
    require_admin_user(request)
    job = _IMPORT_JOBS.get(job_id) or _stored_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Import not found (the server may have restarted). Run it again; it's safe to repeat.")
    return _job_view(job)


@app.get("/admin/import")
@limiter.limit(ADMIN_LIMIT)
def admin_import_list(request: Request, user=Depends(resolve_user), api_key: str = Security(verify_api_key)):
    require_admin_user(request)
    jobs = {j["id"]: j for j in _IMPORT_JOBS.values()}
    if _job_store["ok"]:
        try:
            for row in (supabase.table(_JOB_TABLE).select("*").order("started_at", desc=True)
                        .limit(10).execute().data or []):
                if row.get("id") not in jobs:
                    row = dict(row)
                    row["by"] = row.pop("started_by", None)
                    jobs[row["id"]] = _mark_if_interrupted(row)
        except Exception:
            pass
    ordered = sorted(jobs.values(), key=lambda j: str(j.get("started_at") or ""), reverse=True)[:10]
    return {"imports": [{k: v for k, v in _job_view(j).items() if k != "log"} for j in ordered]}


def _gh_headers():
    token = os.getenv("GITHUB_ACTIONS_TOKEN", "")
    if not token:
        raise HTTPException(status_code=503, detail="Supplier search isn't set up yet: add GITHUB_ACTIONS_TOKEN on Render")
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def _gh_error(r) -> str:
    """Admin-facing reason for a refused GitHub call. GitHub's own message is included (it never
    contains the token); the 403 case says exactly which token works, because that was the 2026-11
    failure: a fine-grained token made by a collaborator can't reach a repo owned by another account."""
    try:
        gh_msg = str((r.json() or {}).get("message") or "")[:160]
    except Exception:
        gh_msg = ""
    owner, repo = (_GH_REPO.split("/", 1) + [""])[:2]
    base = {
        401: "GitHub token on Render (GITHUB_ACTIONS_TOKEN) is wrong or expired.",
        # Kept under 240 characters: the admin page only shows short server messages.
        403: (f"GitHub token can't run Actions on {_GH_REPO}. Make a fine-grained token from the {owner} "
              f"account: repo {repo}, Actions read and write."),
        404: f"Workflow {_GH_WORKFLOW} not found on {_GH_REPO}@{_GH_REF}, or the token can't see the repo.",
        422: f"GitHub refused the run: branch {_GH_REF} must contain {_GH_WORKFLOW}.",
    }.get(r.status_code, f"GitHub error {r.status_code}.")
    out = f"{base} GitHub said: {gh_msg}" if gh_msg else base
    return out if len(out) <= 235 else base


class SourcingRun(BaseModel):
    mode: str = "new"          # new = products never sourced; all = every active product again
    max_requests: int = 1500   # cap on paid Oxylabs results this run
    limit: int = 0             # 0 = no limit


@app.post("/admin/sourcing/run")
@limiter.limit(ADMIN_LIMIT)
def admin_sourcing_run(body: SourcingRun, request: Request, user=Depends(resolve_user),
                       api_key: str = Security(verify_api_key)):
    admin = require_admin_user(request)
    if body.mode not in ("new", "all"):
        raise HTTPException(status_code=400, detail="mode must be new or all")
    if _IMPORT_LOCK.locked():
        # "new" picks products that have no suppliers yet: started mid-import it misses the rest
        raise HTTPException(status_code=409, detail="An import is still running. Start Find suppliers when it's done.")
    max_req = max(10, min(int(body.max_requests), 5000))
    url = f"https://api.github.com/repos/{_GH_REPO}/actions/workflows/{_GH_WORKFLOW}/dispatches"
    payload = {"ref": _GH_REF, "inputs": {"mode": body.mode, "max_requests": str(max_req),
                                          "limit": str(max(0, int(body.limit))), "started_by": admin}}
    with httpx.Client(timeout=20) as c:
        r = c.post(url, headers=_gh_headers(), json=payload)
    if r.status_code != 204:
        raise HTTPException(status_code=502, detail=_gh_error(r))
    return {"success": True, "message": "Supplier search started. It takes about 1 to 3 hours for a full run."}


@app.get("/admin/sourcing/runs")
@limiter.limit(ADMIN_LIMIT)
def admin_sourcing_runs(request: Request, user=Depends(resolve_user), api_key: str = Security(verify_api_key)):
    require_admin_user(request)
    url = f"https://api.github.com/repos/{_GH_REPO}/actions/workflows/{_GH_WORKFLOW}/runs?per_page=5"
    with httpx.Client(timeout=20) as c:
        r = c.get(url, headers=_gh_headers())
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=_gh_error(r))
    runs = [{"id": x["id"], "status": x["status"], "conclusion": x.get("conclusion"),
             "created_at": x["created_at"], "updated_at": x["updated_at"], "url": x["html_url"],
             "title": x.get("display_title")} for x in r.json().get("workflow_runs", [])]
    return {"runs": runs}
