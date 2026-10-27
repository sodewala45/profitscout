"""
ProfitScout — FastAPI Backend
=============================
Run with:  uvicorn main:app --reload
API Docs:  http://127.0.0.1:8000/docs
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
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
from openai import AsyncOpenAI
from supabase import create_client, Client
from dotenv import load_dotenv
from typing import Optional
import hmac
import hashlib
import json
import pdfplumber
import tempfile


load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
ADZUNA_ID  = os.environ.get("ADZUNA_APP_ID", "")
ADZUNA_KEY = os.environ.get("ADZUNA_APP_KEY", "")

class GenerateListingRequest(BaseModel):
    product_name: str
    price: float
    category: str = ""
    image_url: str = ""

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI()

def scheduled_autopilot():
    try:
        from autopilot import run
        print("⏰ Scheduled autopilot starting...")
        run()
    except Exception as e:
        print(f"❌ Autopilot error: {e}")

scheduler = BackgroundScheduler()
scheduler.add_job(scheduled_autopilot, 'cron', hour=9, minute=0)
scheduler.start()
print("✅ Autopilot scheduler started — runs daily at 9AM UTC")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
# --- API Key Auth ---
API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(request: Request, api_key: str = Security(API_KEY_HEADER)):
    if request.method == "OPTIONS":
        return None
    if not api_key or api_key != os.getenv("API_KEY"):
        raise HTTPException(status_code=403, detail="Forbidden")
    return api_key


async def get_user_plan(request: Request):
    """Extract user email from Supabase auth token and return their plan."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        print("get_user_plan: No auth header found")
        return "free"

    token = auth_header.replace("Bearer ", "")
    try:
        user_response = supabase.auth.get_user(token)
        email = user_response.user.email
        print(f"get_user_plan: email={email}")
        result = supabase.table("users").select("plan").eq("user_email", email).execute()
        print(f"get_user_plan: result={result.data}")
        plan = result.data[0].get("plan", "free") if result.data else "free"
        ensure_user_credits(email, plan)  # Auto-create credits if not exist
        return plan
    except Exception as e:
        print(f"get_user_plan ERROR: {e}")
        return "free"

def ensure_user_credits(user_email: str, plan: str):
    """Create/roll over the credit row. Plan comes from users.plan inside the DB function."""
    try:
        credits_v2.get_balance(user_email)
    except Exception as e:
        print(f"ensure_user_credits error: {e}")

# ==============================================================================
# ENDPOINT 1 — Health Check
# ==============================================================================
@app.get("/")
def root():
    return {
        "status": "running",
        "app": "ProfitScout API",
        "version": "2.0.0",
        "docs": "/docs"
    }


@app.get("/ping")
def ping():
    return {"status": "ok", "message": "FastAPI is working!"}


# ==============================================================================
# ENDPOINT 2 — Get All Products (with filters + plan gating)
# ==============================================================================
@app.get("/products")
async def get_products(
    request:     Request,
    min_score:   int            = Query(default=0,   ge=0, le=100),
    max_score:   int            = Query(default=100, ge=0, le=100),
    trend_phase: Optional[str]  = Query(default=None),
    category:    Optional[str]  = Query(default=None),
    min_price:   Optional[float]= Query(default=None),
    max_price:   Optional[float]= Query(default=None),
    limit:       int            = Query(default=20,  ge=1, le=100),
    offset:      int            = Query(default=0,   ge=0),
    api_key:     str            = Security(verify_api_key),
):
    try:
        plan = await get_user_plan(request)

        # Free users: max 10 products, no offset
        if plan == "free":
            limit = min(limit, 10)
            offset = 0

        query = (
            supabase.table("tiktok_products")
            .select("*")
            .eq("is_active", True)
            .gte("virality_score", min_score)
            .lte("virality_score", max_score)
            .order("virality_score", desc=True)
            .range(offset, offset + limit - 1)
        )

        if trend_phase:
            query = query.eq("trend_phase", trend_phase)
        if category:
            query = query.ilike("category", f"%{category}%")
        if min_price is not None:
            query = query.gte("tiktok_price", min_price)
        if max_price is not None:
            query = query.lte("tiktok_price", max_price)

        result = query.execute()

        return {
            "success":  True,
            "count":    len(result.data),
            "offset":   offset,
            "limit":    limit,
            "plan":     plan,
            "products": result.data
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==============================================================================
# ENDPOINT 3 — Get Single Product Detail
# ==============================================================================
@app.get("/products/{product_id}")
def get_product(product_id: str, api_key: str = Security(verify_api_key)):
    try:
        product_result = (
            supabase.table("tiktok_products")
            .select("*")
            .eq("id", product_id)
            .single()
            .execute()
        )

        if not product_result.data:
            raise HTTPException(status_code=404, detail="Product not found")

        product = product_result.data

        suppliers_result = (
            supabase.table("suppliers")
            .select("*")
            .eq("tiktok_product_id", product_id)
            .order("supplier_trust_score", desc=True)
            .execute()
        )

        product["suppliers"] = suppliers_result.data or []

        return {
            "success": True,
            "product": product
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==============================================================================
# ENDPOINT 4 — Get Products by Virality Tier
# ==============================================================================
@app.get("/products/tier/{tier}")
def get_products_by_tier(
    tier:    str,
    limit:   int = Query(default=20, ge=1, le=100),
    offset:  int = Query(default=0,  ge=0),
    api_key: str = Security(verify_api_key),
):
    tier_ranges = {
        "hot":    (80, 100),
        "rising": (65, 79),
        "watch":  (50, 64),
        "pass":   (0,  49),
    }

    if tier not in tier_ranges:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid tier '{tier}'. Use: hot, rising, watch, or pass"
        )

    min_score, max_score = tier_ranges[tier]

    try:
        result = (
            supabase.table("tiktok_products")
            .select("*")
            .eq("is_active", True)
            .gte("virality_score", min_score)
            .lte("virality_score", max_score)
            .order("virality_score", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )

        return {
            "success":  True,
            "tier":     tier,
            "range":    f"{min_score}-{max_score}",
            "count":    len(result.data),
            "products": result.data
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==============================================================================
# ENDPOINT 5 — Dashboard Stats
# ==============================================================================
@app.get("/stats")
def get_stats(api_key: str = Security(verify_api_key)):
    try:
        all_products = (
            supabase.table("tiktok_products")
            .select("virality_score, trend_phase, category")
            .eq("is_active", True)
            .execute()
        )

        products = all_products.data or []

        hot    = len([p for p in products if p["virality_score"] >= 80])
        rising = len([p for p in products if 65 <= p["virality_score"] < 80])
        watch  = len([p for p in products if 50 <= p["virality_score"] < 65])
        total  = len(products)

        categories = {}
        for p in products:
            cat = p.get("category") or "Other"
            categories[cat] = categories.get(cat, 0) + 1
        top_categories = sorted(categories.items(), key=lambda x: x[1], reverse=True)[:5]

        return {
            "success": True,
            "stats": {
                "total_products": total,
                "hot":            hot,
                "rising":         rising,
                "watch":          watch,
                "top_categories": [{"name": k, "count": v} for k, v in top_categories]
            }
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
def calculate_profit(req: ProfitRequest, api_key: str = Security(verify_api_key)):
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

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==============================================================================
# ENDPOINT 7 — Search Products
# ==============================================================================
@app.get("/search")
async def search_products(
    q:       str = Query(..., min_length=2, description="Search keyword"),
    limit:   int = Query(default=20, ge=1, le=100),
    offset:  int = Query(default=0,  ge=0),
    api_key: str = Security(verify_api_key),
):
    try:
        result = (
            supabase.table("tiktok_products")
            .select("*")
            .eq("is_active", True)
            .ilike("product_name", f"%{q}%")
            .order("virality_score", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )

        return {
            "success":  True,
            "query":    q,
            "count":    len(result.data),
            "products": result.data
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


    # ==============================================================================
# ==============================================================================
# ENDPOINT 8/9 — Find Suppliers (credits v2 + photo search)
# GET /products/{product_id}/suppliers/{platform}?photo=true
# platform = alibaba | aliexpress | amazon
#
# Credit rules (see credits.py):
#   - cached results and products you already unlocked are FREE
#   - otherwise 1 credit is reserved before the search and REFUNDED if nothing usable is found
#   - the engine never charges; this endpoint is the only place credits move
# ==============================================================================
def _supplier_search(product_id: str, platform: str, request: Request, photo: bool):
    if platform not in ("alibaba", "aliexpress", "amazon"):
        raise HTTPException(status_code=400, detail="Platform must be: alibaba, aliexpress, or amazon")

    user_email = credits_v2.email_from_request(request)
    if not user_email:
        raise HTTPException(status_code=401, detail="Sign in to search suppliers")

    product_result = (
        supabase.table("tiktok_products")
        .select("id, product_name, tiktok_price, thumbnail_url, image_storage_url")
        .eq("id", product_id)
        .single()
        .execute()
    )
    if not product_result.data:
        raise HTTPException(status_code=404, detail="Product not found")
    product = product_result.data
    image_url = product.get("image_storage_url") or product.get("thumbnail_url") or ""

    # Free paths first: shared cache, or this user already unlocked it
    has_cache = bool(
        supabase.table("suppliers").select("id").eq("tiktok_product_id", product_id)
        .eq("platform", platform).limit(1).execute().data
    )
    already = credits_v2.is_unlocked(user_email, product_id, platform)

    # Pre-sourced mode (default): suppliers are found in a batch after each Kalodata import
    # (python -m sourcing.presource --new). Users never trigger a live search and are never
    # charged for a product that has no suppliers yet.
    if platform in ("alibaba", "aliexpress") and not has_cache and os.getenv("PRESOURCE_ONLY", "true") == "true":
        last = (
            supabase.table("sourcing_jobs").select("status, finished_at")
            .eq("product_id", str(product_id)).eq("platform", platform)
            .order("id", desc=True).limit(1).execute().data
        )
        state = last[0]["status"] if last else "pending"
        msg = (
            f"No {platform.title()} supplier matched this product's photo."
            if state == "no_match"
            else f"{platform.title()} suppliers for this product are being prepared. Check back soon."
        )
        out = {"success": False, "status": "not_sourced" if state != "no_match" else "no_match",
               "platform": platform, "suppliers": [], "credit_charged": False, "message": msg}
        try:
            out["credits"] = credits_v2.get_balance(user_email)
        except credits_v2.CreditError:
            pass
        return out

    charged = False
    if not has_cache and not already:
        try:
            credit = credits_v2.consume(user_email, product_id, platform, "image" if photo else "text")
        except credits_v2.CreditError as e:
            print(f"credit system error: {e}")
            raise HTTPException(status_code=503, detail="Credits are temporarily unavailable. Please try again in a minute.")
        if not credit.get("allowed"):
            raise HTTPException(
                status_code=402,
                detail={
                    "code": "no_credits",
                    "message": f"You've used all {credit.get('credits_total', 0)} credits for this period.",
                    "credits": credit,
                },
            )
        charged = bool(credit.get("charged"))

    # Alibaba / AliExpress: real photo search runs in the sourcing worker (Playwright + Oxylabs).
    if platform in ("alibaba", "aliexpress") and photo and not has_cache and os.getenv("PHOTO_WORKER_ENABLED", "false") == "true":
        live = (
            supabase.table("sourcing_jobs").select("id")
            .eq("product_id", str(product_id)).eq("platform", platform)
            .in_("status", ["queued", "running"]).limit(1).execute().data
        )
        if live:
            job_id = live[0]["id"]
            if charged:  # another user's job already covers this product: don't bill twice
                credits_v2.refund(user_email, product_id, platform)
                charged = False
        else:
            job_id = supabase.table("sourcing_jobs").insert({
                "product_id": str(product_id), "platform": platform, "user_email": user_email,
                "charged": charged, "status": "queued",
            }).execute().data[0]["id"]
        return {"success": True, "status": "queued", "job_id": job_id, "platform": platform,
                "credit_charged": charged, "message": "Searching by photo. Usually 30 to 90 seconds."}

    try:
        result = find_suppliers_by_platform(
            product_id=product["id"],
            product_title=product["product_name"],
            tiktok_price=product["tiktok_price"] or 0,
            platform=platform,
            user_email=user_email,
            product_image_url=image_url,
            use_photo=photo,
        )
    except Exception as e:
        if charged:
            credits_v2.refund(user_email, product_id, platform)
        raise HTTPException(status_code=500, detail=f"Supplier search failed. Your credit was not used. ({e})")

    if charged and not result.get("success"):
        credits_v2.refund(user_email, product_id, platform)
        charged = False

    try:
        result["credits"] = credits_v2.get_balance(user_email)
    except credits_v2.CreditError:
        pass
    result["credit_charged"] = charged
    return result


@app.get("/products/{product_id}/suppliers/{platform}")
async def get_suppliers_by_platform(
    product_id: str,
    platform:   str,
    request:    Request,
    photo:      bool = Query(default=True, description="Use the product photo to match suppliers"),
    api_key:    str = Security(verify_api_key),
):
    return _supplier_search(product_id, platform, request, photo)


@app.get("/products/{product_id}/suppliers")
async def get_suppliers(product_id: str, request: Request, api_key: str = Security(verify_api_key)):
    """Legacy route. Used to give unlimited free Alibaba searches; now same rules as above."""
    return _supplier_search(product_id, "alibaba", request, True)


@app.get("/sourcing/jobs/{job_id}")
async def get_sourcing_job(job_id: int, request: Request, api_key: str = Security(verify_api_key)):
    """Frontend polls this every 3s after a photo search was queued."""
    job = supabase.table("sourcing_jobs").select("*").eq("id", job_id).single().execute().data
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    out = {"job_id": job_id, "status": job["status"], "error": job.get("error")}
    if job["status"] == "done":
        product = supabase.table("tiktok_products").select("id, tiktok_price").eq("id", job["product_id"]).single().execute().data
        res = find_suppliers_by_platform(product_id=product["id"], product_title="", tiktok_price=product["tiktok_price"] or 0,
                                         platform=job["platform"], use_photo=False)  # served from cache
        out["suppliers"] = res.get("suppliers", [])
    if job["status"] in ("done", "no_match", "failed"):
        email = credits_v2.email_from_request(request)
        if email:
            try:
                out["credits"] = credits_v2.get_balance(email)
            except credits_v2.CreditError:
                pass
    return out


# ==============================================================================
# ENDPOINT 10 — Get User Credits (single source of truth for the UI)
# ==============================================================================
@app.get("/credits")
async def get_credits(request: Request, api_key: str = Security(verify_api_key)):
    user_email = credits_v2.email_from_request(request)
    if not user_email:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        return {"success": True, "credits": credits_v2.get_balance(user_email)}
    except credits_v2.CreditError as e:
        print(f"credit system error: {e}")
        raise HTTPException(status_code=503, detail="Credits are temporarily unavailable")


# ==============================================================================
# ENDPOINT 11 — LemonSqueezy Webhook
# ==============================================================================
LEMONSQUEEZY_WEBHOOK_SECRET = os.getenv("LEMONSQUEEZY_WEBHOOK_SECRET")
PRO_VARIANT_ID = os.getenv("LEMONSQUEEZY_PRODUCT_ID")
ULTIMATE_VARIANT_ID = os.getenv("LEMONSQUEEZY_ULTIMATE_PRODUCT_ID")


@app.post("/lemonsqueezy-webhook")
async def lemonsqueezy_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("x-signature")

    if not signature:
        raise HTTPException(status_code=401, detail="No signature")

    digest = hmac.new(
        LEMONSQUEEZY_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(digest, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = json.loads(body)
    event_name = payload.get("meta", {}).get("event_name")
    custom_data = payload.get("meta", {}).get("custom_data", {})
    user_email = custom_data.get("email") or custom_data.get("user_email")

    if not user_email:
        user_email = payload.get("data", {}).get("attributes", {}).get("user_email")

    if not user_email:
        print(f"Webhook {event_name}: no email found")
        return {"status": "ok"}

    # Determine plan from variant ID
    variant_id = str(payload.get("data", {}).get("attributes", {}).get("variant_id") or "")

    if variant_id == ULTIMATE_VARIANT_ID:
        plan = "ultimate"
    elif variant_id == PRO_VARIANT_ID:
        plan = "pro"
    else:
        plan = "pro"

    if event_name == "subscription_created":
        supabase.table("users").update({"plan": plan}).eq("user_email", user_email).execute()
        initialize_credits(user_email, plan)
        print(f"Upgraded {user_email} to {plan}")

    elif event_name in ["subscription_cancelled", "subscription_expired"]:
        supabase.table("users").update({"plan": "free"}).eq("user_email", user_email).execute()
        initialize_credits(user_email, "free")
        print(f"Downgraded {user_email} to free")

    elif event_name == "subscription_updated":
        status = payload.get("data", {}).get("attributes", {}).get("status")
        if status == "active":
            supabase.table("users").update({"plan": plan}).eq("user_email", user_email).execute()
            initialize_credits(user_email, plan)
        elif status in ["cancelled", "expired", "past_due"]:
            supabase.table("users").update({"plan": "free"}).eq("user_email", user_email).execute()
            initialize_credits(user_email, "free")
        print(f"Updated {user_email}: status={status}, plan={plan}")

    return {"status": "ok"}


# ==============================================================================
# ENDPOINT 12 — Create Checkout (supports Pro + Ultimate)
# ==============================================================================
class CheckoutRequest(BaseModel):
    email: str
    plan:  str = "pro"


@app.post("/create-checkout")
async def create_checkout(checkout_data: CheckoutRequest, api_key: str = Security(verify_api_key)):
    if checkout_data.plan == "ultimate":
        variant_id = os.getenv("LEMONSQUEEZY_ULTIMATE_PRODUCT_ID")
    else:
        variant_id = os.getenv("LEMONSQUEEZY_PRODUCT_ID")

    # Debug — print what we're sending
    print(f"Checkout: plan={checkout_data.plan}, email={checkout_data.email}")
    print(f"Variant ID: {variant_id}")
    print(f"Store ID: {os.getenv('LEMONSQUEEZY_STORE_ID')}")
    print(f"API Key exists: {bool(os.getenv('LEMONSQUEEZY_API_KEY'))}")

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
                        "email": checkout_data.email
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

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.lemonsqueezy.com/v1/checkouts",
            json=payload,
            headers=headers
        )

    # Print full LemonSqueezy response for debugging
    print(f"LemonSqueezy status: {response.status_code}")
    print(f"LemonSqueezy response: {response.text}")

    if response.status_code != 200 and response.status_code != 201:
        raise HTTPException(status_code=400, detail="Checkout creation failed")

    data = response.json()
    checkout_url = data.get("data", {}).get("attributes", {}).get("url")
    return {"success": True, "checkout_url": checkout_url}

# ==============================================================================
# ENDPOINT 13 — Create Checkout (supports Pro + Ultimate)
# ==============================================================================
from fastapi import Request
@app.post("/publish-blog-post")
async def publish_blog_post(request: Request):
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

    data = res.json()

    # ── Auto-ping Google & Bing after successful publish ──
    if data.get("posts"):
        published_slug = data["posts"][0].get("slug", slug)
        published_url  = f"https://blog.tikprofitpro.shop/{published_slug}/"
        await ping_google(published_url)
        print(f"✅ Published and pinged: {published_url}")

    return data

# ─── Blog Post Generator via OpenAI ──────────────────────────────
# ─── Blog Post Generator via Groq ──────────────────────────────
@app.post("/generate-blog-post")
async def generate_blog_post(request: dict):
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
            return {"content": None, "error": f"Groq error: {data}"}

        content = data["choices"][0]["message"]["content"]
        return data
        
    except Exception as e:
        return {"content": None, "error": str(e)}


@app.get("/get-image")
async def get_image(query: str = "ecommerce"):
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
    except Exception as e:
        return {"url": None, "error": str(e)}
    
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
        print(f"Ping error (non-fatal): {e}")


@app.get("/indexnow")
async def indexnow_verify():
    indexnow_key = os.environ.get("INDEXNOW_KEY", "")
    return PlainTextResponse(indexnow_key)

@app.post("/generate-listing")
async def generate_listing(req: GenerateListingRequest, request: Request):
    """
    Draft a TikTok Shop listing. Rewritten to stop generic "AI slop":
    - looks at the product photo, so it describes the real item
    - banned-word list (no "elevate", "game-changer", "must-have", emojis...)
    - concrete specs only; unknown facts become [fill in] placeholders instead of invented claims
    - respects TikTok Shop rules: no medical claims, no "best/#1", no competitor brands
    Requires a signed-in user (it spends OpenAI money).
    """
    if not credits_v2.email_from_request(request):
        raise HTTPException(status_code=401, detail="Sign in to generate listings")
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OpenAI API key not configured")

    image_part = []
    if req.image_url:
        from image_search import fetch_image_bytes, _data_url
        img = fetch_image_bytes(req.image_url)
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

    try:
        resp = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.3,
            max_tokens=700,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": [{"type": "text", "text": user_text}] + image_part},
            ],
        )
        return json.loads(resp.choices[0].message.content or "{}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Get Shops ───────────────────────────────────────────────────
@app.get("/shops")
async def get_shops(
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "gmv_total"
):
    try:
        # Valid sort columns
        allowed_sorts = ["gmv_total", "gmv_growth_pct", "created_at", "follower_count"]
        order_col = sort_by if sort_by in allowed_sorts else "gmv_total"

        res = supabase.table("shops")\
                .select("*")\
                .order(order_col, desc=True)\
                .range(offset, offset + limit - 1)\
                .execute()
        return {
            "shops": res.data,
            "total": 4502,
            "limit": limit,
            "offset": offset
        }
    except Exception as e:
        return {"shops": [], "error": str(e)}

# ─── Get Creators ─────────────────────────────────────────────────
@app.get("/creators")
async def get_creators(creator_type: str = None, limit: int = 50):
    try:
        query = supabase.table("creators").select("*").limit(limit)
        if creator_type:
            query = query.eq("creator_type", creator_type)
        res = query.order("followers", desc=True).execute()
        return {"creators": res.data}
    except Exception as e:
        return {"creators": [], "error": str(e)}
    
# ================================================================
# /api/jobs — real jobs from Remotive + Adzuna
# ================================================================
@app.get("/api/jobs")
async def get_jobs(niches: str = "", location: str = "remote", job_type: str = "Any"):
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
@app.post("/api/parse-cv")
async def parse_cv(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        return JSONResponse({"error": "Only PDF files supported"}, status_code=400)
 
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
 
    try:
        text = ""
        with pdfplumber.open(tmp_path) as pdf:
            for page in pdf.pages:
                text += (page.extract_text() or "") + "\n"
        os.unlink(tmp_path)
    except Exception as e:
        return JSONResponse({"error": f"PDF read error: {str(e)}"}, status_code=500)
 
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
    except Exception as ex:
        return JSONResponse({"error": f"Parse error: {str(ex)}"}, status_code=500)
 
 
# ================================================================
# /api/extract-email — fetch job page, GPT extracts apply email
# ================================================================
@app.get("/api/extract-email")
async def extract_email(url: str = ""):
    """
    Fetches the job posting page and uses GPT to extract
    any apply/contact email address from the HTML content.
    Returns {"email": "found@email.com"} or {"email": ""}
    """
    if not url:
        return JSONResponse({"email": ""})
 
    # Fetch the job page
    try:
        async with httpx.AsyncClient(
            timeout=12,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        ) as client:
            r = await client.get(url)
            html = r.text
    except Exception as e:
        print(f"[extract-email fetch error] {e}")
        return JSONResponse({"email": "", "error": str(e)})
 
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
    except Exception as ex:
        return JSONResponse({"email": "", "error": str(ex)})
 
 
# ================================================================
# /api/ai-proxy — cover email + CV summary generation (unchanged)
# ================================================================
@app.post("/api/ai-proxy")
async def ai_proxy(request: Request, api_key: str = Security(verify_api_key)):
    # Was open to the whole internet: anyone could spend the OpenAI balance.
    try:
        body = await request.json()
        resp = await openai_client.chat.completions.create(
            model       = body.get("model", "gpt-4o-mini"),
            messages    = body.get("messages", []),
            max_tokens  = body.get("max_tokens", 2000),
            temperature = body.get("temperature", 0.7),
        )
        return JSONResponse({"content": resp.choices[0].message.content or ""})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
 
 
# ================================================================
# /jobs — serves job_hunt.html
# ================================================================
@app.get("/jobs", response_class=HTMLResponse)
async def job_hunt_ui():
    path = r"F:\profitscout\job_hunt.html"
    if not os.path.exists(path):
        return HTMLResponse(f"<h2>job_hunt.html not found at {path}</h2>", status_code=404)
    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())
 

 # ─── Daily Blog Autopilot Endpoint ───────────────────────────────
@app.post("/run-autopilot")
async def run_autopilot(secret: str = ""):
    if secret != os.environ.get("AUTOPILOT_SECRET") or object():
        return {"error": "unauthorized"}
    try:
        from autopilot import run
        import threading
        thread = threading.Thread(target=run)
        thread.start()
        return {"status": "started", "message": "Autopilot running in background"}
    except Exception as e:
        return {"error": str(e)}
