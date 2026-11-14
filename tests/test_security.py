"""Security regression tests. Fully offline: Supabase, Lemon Squeezy, OpenAI and DNS are stubbed.

Run:  python -m pytest tests -q
"""
from __future__ import annotations

import hashlib
import hmac
import io
import itertools
import json
import os
import socket
import sys
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---- environment + stubs BEFORE importing the app ----
os.environ.update({
    "SUPABASE_URL": "https://stub.supabase.co", "SUPABASE_KEY": "stub",
    "API_KEY": "test-api-key", "LEMONSQUEEZY_WEBHOOK_SECRET": "whsec-test",
    "LEMONSQUEEZY_PRODUCT_ID": "111", "LEMONSQUEEZY_ULTIMATE_PRODUCT_ID": "222",
    "ADMIN_EMAILS": "boss@example.com", "ADMIN_USER_IDS": "",
})
for k in ("ENABLE_DOCS", "ENABLE_LEGACY_ENDPOINTS", "OPENAI_API_KEY", "GROQ_API_KEY"):
    os.environ.pop(k, None)

import supabase as _supabase_pkg  # noqa: E402
import fake_supabase  # noqa: E402

_supabase_pkg.create_client = fake_supabase.create_client
DB = fake_supabase.DB

import main  # noqa: E402
import security  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(main.app, raise_server_exceptions=False)
_ip = itertools.count(1)


def H(token=None, ip=None, **extra):
    h = {"X-API-Key": "test-api-key", "CF-Connecting-IP": ip or f"198.51.100.{next(_ip) % 250}"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    h.update(extra)
    return h


def fresh_ip():
    return f"203.0.{next(_ip) % 250}.{next(_ip) % 250}"


@pytest.fixture(autouse=True)
def _reset():
    DB.reset()
    main._free_cache.clear()
    main.limiter.reset()
    main._manual_limits.storage.reset()
    yield


def seed_products(n=30):
    rows = []
    for i in range(n):
        rows.append({"id": str(uuid.UUID(int=i + 1)), "product_name": f"Product {i} lipstick" if i % 2 else f"Product {i} mug",
                     "virality_score": 100 - i, "tiktok_price": 5 + i, "category": "Beauty" if i % 2 else "Kitchen",
                     "trend_phase": "rising" if i % 3 else "peak", "is_active": True,
                     "thumbnail_url": f"https://cdn.example.com/{i}.jpg"})
    DB.tables["tiktok_products"] = rows
    return rows


# ---------------------------------------------------------------------------
# L1 / L5: docs and legacy routes off by default
# ---------------------------------------------------------------------------
def test_docs_disabled_by_default():
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


@pytest.mark.parametrize("method,path", [
    ("post", "/api/ai-proxy"), ("post", "/api/parse-cv"), ("get", "/api/extract-email?url=https://x.com"),
    ("get", "/api/jobs"), ("post", "/run-autopilot"), ("post", "/publish-blog-post"),
    ("post", "/generate-blog-post"), ("get", "/get-image"), ("get", "/jobs"),
])
def test_legacy_routes_404_by_default(method, path):
    r = getattr(client, method)(path, headers={**H(), "X-Admin-Secret": "x", "X-Autopilot-Secret": "x"})
    assert r.status_code == 404


def test_api_key_constant_time_and_required():
    seed_products()
    assert client.get("/stats", headers={"X-API-Key": "wrong"}).status_code == 403
    assert client.get("/stats").status_code == 403
    assert client.get("/stats", headers=H()).status_code == 200


# ---------------------------------------------------------------------------
# H2 / L3: free gate
# ---------------------------------------------------------------------------
def _ids(r):
    body = r.json()
    return [p["id"] for p in body["products"]]


def test_free_gate_same_top10_whatever_the_filters():
    rows = seed_products()
    expected = [r["id"] for r in rows[:10]]
    variants = ["/products", "/products?max_score=60", "/products?min_score=0&max_score=75&category=Kitchen",
                "/products?min_price=20&max_price=30&offset=50&limit=100", "/products?trend_phase=peak",
                "/search?q=lipstick", "/search?q=mug&offset=20"]
    for v in variants:
        r = client.get(v, headers=H())
        assert r.status_code == 200, v
        assert _ids(r) == expected, v
    # Tier routes: only the members of the same fixed top 10 that are really in the tier
    # (scores 100..91 here), never rows from outside it.
    for v, want in [("/products/tier/hot", expected), ("/products/tier/watch", []),
                    ("/products/tier/pass?offset=5", [])]:
        r = client.get(v, headers=H())
        assert r.status_code == 200, v
        assert _ids(r) == want, v


def test_free_gate_applies_to_null_and_odd_plans():
    rows = seed_products()
    expected = [r["id"] for r in rows[:10]]
    DB.add_user("t-null", "n@example.com", plan=None)
    DB.add_user("t-weird", "w@example.com", plan="cancelled")
    for tok in ("t-null", "t-weird"):
        r = client.get("/products?max_score=60&limit=100&offset=40", headers=H(tok))
        assert _ids(r) == expected
        assert r.json()["plan"] == "free"


def test_paid_user_gets_filters():
    seed_products(60)
    DB.add_user("t-pro", "pro@example.com", plan="Pro ")   # normalised to pro
    r = client.get("/products?max_score=60&limit=100", headers=H("t-pro"))
    assert r.status_code == 200
    scores = [p["virality_score"] for p in r.json()["products"]]
    assert scores and max(scores) <= 60
    assert r.json()["plan"] == "pro"


def test_free_product_detail_only_inside_top10():
    rows = seed_products()
    assert client.get(f"/products/{rows[3]['id']}", headers=H()).status_code == 200
    r = client.get(f"/products/{rows[20]['id']}", headers=H())
    assert r.status_code == 403 and "Upgrade" in r.json()["detail"]
    DB.add_user("t-ult", "u@example.com", plan="ultimate")
    assert client.get(f"/products/{rows[20]['id']}", headers=H("t-ult")).status_code == 200


def test_free_creators_and_shops_fixed():
    DB.tables["creators"] = [{"tiktok_handle": f"c{i}", "gmv_total": 1000 - i, "creator_type": "video" if i % 2 else "live",
                              "contact_email": f"c{i}@mail.com"} for i in range(25)]
    DB.tables["shops"] = [{"shop_name": f"s{i}", "gmv_total": 500 - i, "follower_count": i} for i in range(25)]
    a = client.get("/creators", headers=H()).json()["creators"]
    b = client.get("/creators?creator_type=live&sort_by=followers&offset=10", headers=H()).json()["creators"]
    assert [c["tiktok_handle"] for c in a] == [c["tiktok_handle"] for c in b] == [f"c{i}" for i in range(10)]
    assert all("contact_email" not in c for c in a)
    s1 = client.get("/shops", headers=H()).json()["shops"]
    s2 = client.get("/shops?sort_by=follower_count&offset=15", headers=H()).json()["shops"]
    assert [x["shop_name"] for x in s1] == [x["shop_name"] for x in s2] == [f"s{i}" for i in range(10)]


# ---------------------------------------------------------------------------
# M5: generic errors, 404 for bad ids
# ---------------------------------------------------------------------------
def test_errors_are_generic():
    seed_products()
    DB.add_user("t-pro", "pro@example.com", plan="pro")
    DB.fail_tables["tiktok_products"] = "invalid input syntax for type uuid: SECRET_SCHEMA_DETAIL"
    for path in ("/products", "/search?q=abc", "/stats", "/products/tier/hot"):
        r = client.get(path, headers=H("t-pro"))
        assert r.status_code == 500, path
        assert "SECRET_SCHEMA_DETAIL" not in r.text and "uuid" not in r.text
    DB.fail_tables.clear()
    assert client.get("/products/abc", headers=H()).status_code == 404
    assert client.get(f"/products/{uuid.uuid4()}", headers=H("t-pro")).status_code == 404


# ---------------------------------------------------------------------------
# C2 / M4: webhook
# ---------------------------------------------------------------------------
def _sign(body: bytes) -> str:
    return hmac.new(b"whsec-test", body, hashlib.sha256).hexdigest()


def send_webhook(event, sub_id="sub1", status="active", variant="111", custom=None, user_email="", updated_at="2026-11-07T10:00:00Z"):
    payload = {"meta": {"event_name": event, "custom_data": custom or {}},
               "data": {"type": "subscriptions", "id": sub_id,
                        "attributes": {"status": status, "variant_id": variant, "user_email": user_email,
                                       "updated_at": updated_at}}}
    body = json.dumps(payload).encode()
    return client.post("/lemonsqueezy-webhook", content=body, headers={"x-signature": _sign(body)})


def plans():
    return {u["user_email"]: u["plan"] for u in DB.tables.get("users", [])}


def test_webhook_wildcard_email_upgrades_nobody():
    DB.add_user("a", "alice@gmail.com")
    DB.add_user("b", "bob@gmail.com")
    DB.add_user("c", "a_b@x.com")
    DB.add_user("d", "axb@x.com")
    assert send_webhook("subscription_created", custom={"email": "%@gmail.com"}).status_code == 200
    assert send_webhook("subscription_created", sub_id="sub2", custom={"email": "%@%"}).status_code == 200
    assert send_webhook("subscription_created", sub_id="sub3", user_email="a_b@x.com").status_code == 200
    p = plans()
    assert p["alice@gmail.com"] == "free" and p["bob@gmail.com"] == "free"
    assert p["a_b@x.com"] == "pro"          # exact match only
    assert p["axb@x.com"] == "free"         # '_' is not a wildcard any more


def test_webhook_signed_user_id_and_mismatch():
    u = DB.add_user("t", "carol@example.com")
    DB.add_user("v", "victim@example.com")
    sig = main._checkout_sig(u.id)
    # good: signed id, matching email
    send_webhook("subscription_created", custom={"user_id": u.id, "email": "carol@example.com", "sig": sig},
                 user_email="Carol@Example.com ")
    assert plans()["carol@example.com"] == "pro"
    # forged (unsigned) user id falls back to exact email only
    u2 = DB.add_user("t2", "dave@example.com")
    send_webhook("subscription_created", sub_id="sub10", custom={"user_id": u2.id, "sig": "bad"},
                 user_email="nobody@example.com")
    assert plans()["dave@example.com"] == "free"
    # signed id but the buyer typed another email at checkout: the signed-in account that paid gets
    # the plan (plans audit 2026-11-14: this used to be ignored and left the payer on Free); the other
    # address gets nothing.
    send_webhook("subscription_created", sub_id="sub9",
                 custom={"user_id": u2.id, "email": "victim@example.com", "sig": main._checkout_sig(u2.id)},
                 user_email="victim@example.com")
    assert plans()["dave@example.com"] == "pro" and plans()["victim@example.com"] == "free"


def test_webhook_out_of_order_event_ignored():
    DB.add_user("t", "erin@example.com")
    send_webhook("subscription_created", user_email="erin@example.com", updated_at="2026-11-01T00:00:00Z")
    assert plans()["erin@example.com"] == "pro"
    send_webhook("subscription_expired", status="expired", user_email="erin@example.com", updated_at="2026-11-05T00:00:00Z")
    assert plans()["erin@example.com"] == "free"
    # a delayed 'active' update from before the expiry must not re-grant Pro
    r = send_webhook("subscription_updated", status="active", user_email="erin@example.com", updated_at="2026-11-03T00:00:00Z")
    assert r.json().get("stale") is True
    assert plans()["erin@example.com"] == "free"


def test_webhook_one_lapsed_sub_does_not_downgrade_another_active():
    DB.add_user("t", "fay@example.com")
    send_webhook("subscription_created", sub_id="A", variant="222", user_email="fay@example.com")
    send_webhook("subscription_created", sub_id="B", variant="111", user_email="fay@example.com")
    send_webhook("subscription_expired", sub_id="B", status="expired", user_email="fay@example.com",
                 updated_at="2026-11-08T00:00:00Z")
    assert plans()["fay@example.com"] == "ultimate"


def test_webhook_missing_users_row_logged_not_crashing(caplog):
    r = send_webhook("subscription_created", user_email="ghost@example.com")
    assert r.status_code == 200
    assert "no users row" in caplog.text and "ghost@example.com" not in caplog.text


def test_webhook_rejects_bad_signature():
    body = b'{"meta":{}}'
    assert client.post("/lemonsqueezy-webhook", content=body, headers={"x-signature": "00"}).status_code == 401


def test_checkout_sends_signed_user_id(monkeypatch):
    monkeypatch.setenv("LEMONSQUEEZY_API_KEY", "k")
    monkeypatch.setenv("LEMONSQUEEZY_STORE_ID", "1")
    u = DB.add_user("t", "hal@example.com")
    sent = {}

    class FakeResp:
        status_code = 201
        def json(self):
            return {"data": {"attributes": {"url": "https://pay.example/checkout"}}}

    class FakeHTTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None, headers=None):
            sent.update(json); return FakeResp()

    monkeypatch.setattr(main.httpx, "Client", FakeHTTP)
    r = client.post("/create-checkout", json={"email": "evil@example.com", "plan": "pro"}, headers=H("t"))
    assert r.status_code == 200
    custom = sent["data"]["attributes"]["checkout_data"]["custom"]
    assert custom["user_id"] == u.id and custom["email"] == "hal@example.com"
    assert custom["sig"] == main._checkout_sig(u.id)
    # 5/minute per user
    codes = [client.post("/create-checkout", json={"plan": "pro"}, headers=H("t")).status_code for _ in range(6)]
    assert 429 in codes


# ---------------------------------------------------------------------------
# H3: admin + confirmed email
# ---------------------------------------------------------------------------
def test_admin_requires_confirmed_email_or_id(monkeypatch):
    DB.add_user("unconf", "boss@example.com", confirmed=False)
    assert client.get("/admin/me", headers=H("unconf")).json() == {"admin": False}
    assert client.get("/admin/import", headers=H("unconf")).status_code == 403
    DB.reset()
    DB.add_user("conf", "boss@example.com", confirmed=True)
    assert client.get("/admin/me", headers=H("conf")).json() == {"admin": True}
    assert client.get("/admin/import", headers=H("conf")).status_code == 200
    DB.reset()
    u = DB.add_user("byid", "someone@else.com", confirmed=False)
    monkeypatch.setattr(main, "ADMIN_USER_IDS", {u.id})
    assert client.get("/admin/import", headers=H("byid")).status_code == 200
    assert client.get("/admin/import", headers=H()).status_code == 401


def test_unconfirmed_email_cannot_spend_credits():
    rows = seed_products()
    DB.tables["suppliers"] = [{"id": 1, "tiktok_product_id": rows[0]["id"], "platform": "alibaba", "similarity": 0.95,
                               "supplier_name": "S", "unit_cost_min": 1.0, "reviewed_by": "admin@example.com",
                               "hidden": None, "match_method": "auto"}]
    DB.add_user("u", "new@example.com", confirmed=False)
    r = client.get(f"/products/{rows[0]['id']}/suppliers/alibaba", headers=H("u"))
    assert r.status_code == 403 and "Confirm your email" in r.text
    assert DB.consumed == []
    DB.add_user("c", "ok@example.com", confirmed=True)
    r = client.get(f"/products/{rows[0]['id']}/suppliers/alibaba", headers=H("c"))
    assert r.status_code == 200 and r.json()["suppliers"]
    assert "reviewed_by" not in r.json()["suppliers"][0]           # L4
    assert ("suppliers", tuple(c.strip() for c in main.SUPPLIER_PUBLIC_COLUMNS.split(","))) in DB.selected_columns


# ---------------------------------------------------------------------------
# M3 / H1: rate limits + Amazon guard
# ---------------------------------------------------------------------------
def test_list_rate_limit_429():
    seed_products()
    ip = fresh_ip()
    codes = [client.get("/products", headers=H(ip=ip)).status_code for _ in range(61)]
    assert codes[:60] == [200] * 60 and codes[60] == 429
    assert "Too many requests" in client.get("/products", headers=H(ip=ip)).json()["detail"]
    assert client.get("/products", headers=H(ip=fresh_ip())).status_code == 200


def test_supplier_rate_limit_per_user():
    rows = seed_products()
    DB.add_user("u", "s@example.com")
    codes = [client.get(f"/products/{rows[0]['id']}/suppliers/alibaba", headers=H("u", ip=fresh_ip())).status_code
             for _ in range(11)]
    assert codes[-1] == 429 and codes[0] == 200       # keyed by user, not IP


def test_amazon_limit_and_inflight(monkeypatch):
    rows = seed_products()
    u = DB.add_user("u", "amz@example.com")
    monkeypatch.setattr(main, "find_suppliers_by_platform",
                        lambda **k: {"success": True, "suppliers": [{"id": 9, "reviewed_by": "x", "supplier_name": "A"}]})
    r = client.get(f"/products/{rows[1]['id']}/suppliers/amazon", headers=H("u"))
    assert r.status_code == 200 and "reviewed_by" not in r.json()["suppliers"][0]
    main._amazon_inflight.add(u.id)            # simulate a search already running
    assert client.get(f"/products/{rows[1]['id']}/suppliers/amazon", headers=H("u")).status_code == 429
    main._amazon_inflight.discard(u.id)
    codes = [client.get(f"/products/{rows[1]['id']}/suppliers/amazon", headers=H("u")).status_code for _ in range(3)]
    assert codes[-1] == 429                     # 3/minute


def test_blocking_routes_are_sync():
    import inspect
    for fn in (main.get_products, main.get_suppliers_by_platform, main.get_suppliers, main.generate_listing,
               main.get_creators, main.get_shops, main.search_products, main.create_checkout,
               main.admin_review_queue, main.admin_sourcing_run):
        target = inspect.unwrap(fn)
        assert not inspect.iscoroutinefunction(target), fn.__name__


# ---------------------------------------------------------------------------
# M1: safe_fetch
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x", "http://169.254.169.254/latest/meta-data/", "http://10.0.0.5/", "http://[::1]/",
    "http://localhost:8000/", "file:///etc/passwd", "gopher://x", "http://0.0.0.0/", "http://192.168.1.1/",
    "http://[::ffff:169.254.169.254]/", "http://user:pw@example.com/",
])
def test_safe_fetch_rejects_internal(url):
    assert security.safe_fetch(url) is None


def test_safe_fetch_rechecks_redirects(monkeypatch):
    import httpx
    real_getaddrinfo = socket.getaddrinfo
    addr = {"public.example": "93.184.216.34", "evil.example": "169.254.169.254"}

    def fake_getaddrinfo(host, port, *a, **k):
        if host in addr:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr[host], port))]
        return real_getaddrinfo(host, port, *a, **k)

    def handler(request):
        p = request.url.path
        if p == "/to-metadata":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        if p == "/to-evil":
            return httpx.Response(302, headers={"location": "http://evil.example/x"})
        if p == "/loop":
            return httpx.Response(302, headers={"location": "/loop"})
        if p == "/big":
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"x" * 5000)
        if p == "/img":
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"\xff\xd8ok")
        if p == "/html":
            return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>")
        raise AssertionError(f"unexpected fetch {request.url}")

    real_client = httpx.Client
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(security.httpx, "Client", lambda **k: real_client(transport=httpx.MockTransport(handler), **k))
    assert security.safe_fetch("http://public.example/to-metadata") is None
    assert security.safe_fetch("http://public.example/to-evil") is None
    assert security.safe_fetch("http://public.example/loop") is None
    assert security.safe_fetch("http://public.example/big", max_bytes=1000) is None
    assert security.safe_fetch("http://public.example/html", content_type_prefix="image") is None
    r = security.safe_fetch("http://public.example/img", content_type_prefix="image")
    assert r is not None and r.content == b"\xff\xd8ok"
    import image_search
    assert image_search.fetch_image_bytes("http://public.example/img") == b"\xff\xd8ok"
    assert image_search.fetch_image_bytes("http://169.254.169.254/x") is None


def test_generate_listing_uses_product_image_not_client_url(monkeypatch):
    rows = seed_products()
    DB.add_user("p", "pro2@example.com", plan="pro")
    fetched = []
    import image_search
    monkeypatch.setattr(image_search, "fetch_image_bytes", lambda url, *a, **k: fetched.append(url) or None)
    monkeypatch.setattr(main, "GROQ_API_KEY", "")

    class FakeOpenAI:
        class chat:
            class completions:
                @staticmethod
                def create(**k):
                    from types import SimpleNamespace as N
                    return N(choices=[N(message=N(content='{"title": "t"}'))])
    monkeypatch.setattr(main, "openai_sync_client", FakeOpenAI)
    body = {"product_name": "x", "price": 1, "product_id": rows[2]["id"], "image_url": "http://169.254.169.254/"}
    r = client.post("/generate-listing", json=body, headers=H("p"))
    assert r.status_code == 200 and fetched == [rows[2]["thumbnail_url"]]
    # free users can't use it
    DB.add_user("f", "free2@example.com")
    assert client.post("/generate-listing", json=body, headers=H("f")).status_code == 402


# ---------------------------------------------------------------------------
# M6: uploads are authorised before the body is read
# ---------------------------------------------------------------------------
def test_admin_import_auth_before_body_read():
    consumed = {"n": 0}

    def body():
        for _ in range(4):
            consumed["n"] += 1
            yield b"x" * 1024

    r = client.post("/admin/import", content=body(),
                    headers={**H(), "Content-Type": "multipart/form-data; boundary=zzz", "Content-Length": "4096"})
    assert r.status_code == 401
    print("chunks consumed:", consumed["n"])
    assert consumed["n"] <= 1   # the transport may pre-read one chunk; the app never parses it


def test_admin_import_rejects_oversize_by_header():
    DB.add_user("adm", "boss@example.com", confirmed=True)
    r = client.post("/admin/import", content=b"--zzz--\r\n",
                    headers={**H("adm"), "Content-Type": "multipart/form-data; boundary=zzz",
                             "Content-Length": str(40 * 1024 * 1024)})
    assert r.status_code == 413


def test_admin_import_dry_run_works(tmp_path):
    DB.add_user("adm", "boss@example.com", confirmed=True)
    csv_text = open(os.path.join(ROOT, "data", "kitchen_export.csv"), "rb").read()
    files = [("files", ("kitchen_export.csv", io.BytesIO(csv_text), "text/csv"))]
    r = client.post("/admin/import", files=files, data={"dry_run": "true"}, headers=H("adm"))
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["status"] == "done" and j["files"][0]["kind"] == "products"
    r = client.post("/admin/import", files=[("files", ("x.txt", io.BytesIO(b"a"), "text/plain"))],
                    data={"dry_run": "true"}, headers=H("adm"))
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# L5 ai-proxy clamp (when legacy routes are switched on)
# ---------------------------------------------------------------------------
def test_ai_proxy_clamps_model_and_tokens(monkeypatch):
    monkeypatch.setattr(main, "ENABLE_LEGACY_ENDPOINTS", True)
    monkeypatch.setattr(main, "ADMIN_SECRET", "adm-secret")
    seen = {}

    class FakeAsync:
        class chat:
            class completions:
                @staticmethod
                async def create(**k):
                    seen.update(k)
                    from types import SimpleNamespace as N
                    return N(choices=[N(message=N(content="hi"))])
    monkeypatch.setattr(main, "openai_client", FakeAsync)
    r = client.post("/api/ai-proxy", json={"model": "o1-pro", "max_tokens": 100000, "messages": []},
                    headers={**H(), "X-Admin-Secret": "adm-secret"})
    assert r.status_code == 200
    assert seen["model"] == "gpt-4o-mini" and seen["max_tokens"] == 2000


# ---------------------------------------------------------------------------
# L6: CSV import
# ---------------------------------------------------------------------------
def test_csv_future_date_and_bad_urls():
    import import_kalodata as k
    row = {"TikTokUrl": "https://www.tiktok.com/shop/pdp/123", "Product Name": "Mug",
           "Date Range": "2030-01-01~2030-01-30", "img_url": "javascript:alert(1)"}
    out, err = k.parse_product(row)
    assert out is None and "future" in err
    row["Date Range"] = "2026-10-01~2026-10-30"
    (p, _), err = k.parse_product(row)
    assert p["thumbnail_url"] == ""
    assert k.http_url("https://ok.example/a.png") == "https://ok.example/a.png"
