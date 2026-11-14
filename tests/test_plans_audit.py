"""Plans audit (2026-11-14): "I paid for Pro and I'm still Free".

Covers: signed checkout user wins over a different order email, product-id / int variant matching,
order_created upgrades, status mapping, users row created via tpp_ensure_user_row, /me 503 instead of
a fake "free", checkout redirect back to the app + logged LS error, and the admin billing lookup."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging

import pytest

import test_security as ts          # sets env + patches supabase before importing main
from test_security import DB, H, client, main, plans, send_webhook


@pytest.fixture(autouse=True)
def _reset():
    DB.reset()
    main._free_cache.clear()
    main.limiter.reset()
    main._manual_limits.storage.reset()
    yield


def _post(payload: dict):
    body = json.dumps(payload).encode()
    sig = hmac.new(b"whsec-test", body, hashlib.sha256).hexdigest()
    return client.post("/lemonsqueezy-webhook", content=body, headers={"x-signature": sig})


def sub_event(event, *, variant=111, product=None, status="active", sub_id="s1", email="", custom=None,
              updated_at="2026-11-14T10:00:00Z", test_mode=False):
    attrs = {"status": status, "variant_id": variant, "user_email": email, "updated_at": updated_at}
    if product is not None:
        attrs["product_id"] = product
    return _post({"meta": {"event_name": event, "custom_data": custom or {}, "test_mode": test_mode},
                  "data": {"type": "subscriptions", "id": sub_id, "attributes": attrs}})


def order_event(*, variant=111, product=None, status="paid", order_id="o1", email="", custom=None):
    item = {"variant_id": variant}
    if product is not None:
        item["product_id"] = product
    return _post({"meta": {"event_name": "order_created", "custom_data": custom or {}},
                  "data": {"type": "orders", "id": order_id,
                           "attributes": {"status": status, "user_email": email, "first_order_item": item,
                                          "created_at": "2026-11-14T10:00:00Z"}}})


def signed(u):
    return {"user_id": u.id, "email": u.email, "sig": main._checkout_sig(u.id)}


# ---------------------------------------------------------------- who gets the plan
def test_signed_checkout_with_different_buyer_email_upgrades_the_account():
    u = DB.add_user("t", "hassan@example.com")
    DB.add_user("o", "paypal@example.com")
    r = sub_event("subscription_created", email="PayPal@Example.com", custom=signed(u))
    assert r.status_code == 200
    assert plans()["hassan@example.com"] == "pro"
    assert plans()["paypal@example.com"] == "free"
    # later lifecycle events keep following the account
    sub_event("subscription_updated", status="cancelled", email="paypal@example.com", custom=signed(u),
              updated_at="2026-11-15T10:00:00Z")
    assert plans()["hassan@example.com"] == "pro"          # cancelled = paid until ends_at
    sub_event("subscription_expired", status="expired", email="paypal@example.com", custom=signed(u),
              updated_at="2026-12-15T10:00:00Z")
    assert plans()["hassan@example.com"] == "free"


def test_signed_user_without_users_row_is_created_via_db_helper():
    u = DB.add_user("t", "norow@example.com", users_row=False)
    called = []

    def ensure(p_id, p_email):
        called.append((p_id, p_email))
        DB.tables.setdefault("users", []).append({"user_email": p_email, "plan": "free"})
    DB.rpc_tpp_ensure_user_row = ensure
    try:
        sub_event("subscription_created", email="norow@example.com", custom=signed(u))
    finally:
        del DB.rpc_tpp_ensure_user_row
    assert called == [(u.id, "norow@example.com")]
    assert plans()["norow@example.com"] == "pro"


# ---------------------------------------------------------------- variant / product matching
def test_int_variant_id_matches():
    DB.add_user("t", "int@example.com")
    sub_event("subscription_created", variant=111, email="int@example.com")
    assert plans()["int@example.com"] == "pro"


def test_product_id_matches_and_survives_later_events(monkeypatch):
    monkeypatch.setenv("LEMON_PRO_PRODUCT_IDS", "555")
    DB.add_user("t", "prod@example.com")
    sub_event("subscription_created", variant=999, product=555, email="prod@example.com")
    assert plans()["prod@example.com"] == "pro"
    # the stored variant (999) is unknown; the update must not re-derive "free" from it
    sub_event("subscription_updated", variant=999, product=555, status="past_due", email="prod@example.com",
              updated_at="2026-11-20T10:00:00Z")
    assert plans()["prod@example.com"] == "pro"


def test_legacy_product_id_var_also_matches_a_product_id():
    # LEMONSQUEEZY_PRODUCT_ID=111 in the test env: someone pasted the PRODUCT id there
    DB.add_user("t", "legacy@example.com")
    sub_event("subscription_created", variant=424242, product=111, email="legacy@example.com")
    assert plans()["legacy@example.com"] == "pro"


def test_unknown_variant_is_a_warning_that_names_the_ids(caplog):
    DB.add_user("t", "unk@example.com")
    with caplog.at_level(logging.WARNING):
        sub_event("subscription_created", variant=777, product=888, email="unk@example.com", test_mode=True)
    assert plans()["unk@example.com"] == "free"
    assert "unknown variant 777 / product 888 (test_mode=True)" in caplog.text
    assert "unk@example.com" not in caplog.text


@pytest.mark.parametrize("status,expected", [("on_trial", "pro"), ("active", "pro"), ("past_due", "pro"),
                                             ("cancelled", "pro"), ("unpaid", "free"), ("paused", "free"),
                                             ("expired", "free")])
def test_status_mapping(status, expected):
    DB.add_user("t", "st@example.com")
    sub_event("subscription_updated", status=status, email="st@example.com")
    assert plans()["st@example.com"] == expected


# ---------------------------------------------------------------- order_created
def test_paid_order_upgrades_without_subscription_created():
    u = DB.add_user("t", "order@example.com")
    assert order_event(email="order@example.com", custom=signed(u)).status_code == 200
    assert plans()["order@example.com"] == "pro"
    assert ("start_credit_period", {"p_email": "order@example.com"}) in DB.rpc_calls


def test_unpaid_order_and_unknown_variant_do_nothing():
    DB.add_user("t", "pend@example.com")
    order_event(email="pend@example.com", status="pending")
    order_event(email="pend@example.com", order_id="o2", variant=31337)
    assert plans()["pend@example.com"] == "free"


def test_order_never_downgrades():
    DB.add_user("t", "ult@example.com", plan="ultimate")
    order_event(email="ult@example.com", variant=111)
    assert plans()["ult@example.com"] == "ultimate"


def test_upgrade_shows_on_me_and_products():
    ts.seed_products(30)
    u = DB.add_user("t", "flow@example.com")
    assert client.get("/me", headers=H("t")).json()["plan"] == "free"
    assert client.get("/products?limit=100", headers=H("t")).json()["count"] == main.FREE_PRODUCT_LIMIT
    sub_event("subscription_created", email="flow@example.com", custom=signed(u))
    me = client.get("/me", headers=H("t")).json()
    assert me["plan"] == "pro" and me["billing_plan"] == "pro"
    assert client.get("/products?limit=100", headers=H("t")).json()["count"] == 30


# ---------------------------------------------------------------- /me
def test_me_answers_503_instead_of_free_when_the_lookup_fails():
    DB.add_user("t", "down@example.com", plan="pro")
    DB.fail_tables["users"] = "connection reset"
    assert client.get("/me", headers=H("t")).status_code == 503
    DB.fail_tables.clear()
    assert client.get("/me", headers=H("t")).json()["plan"] == "pro"


# ---------------------------------------------------------------- checkout
class _Resp:
    def __init__(self, code, body):
        self.status_code, self._body = code, body

    def json(self):
        return self._body


def _fake_http(monkeypatch, resp, sent):
    class FakeHTTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None, headers=None):
            sent.update(json); return resp
    monkeypatch.setattr(main.httpx, "Client", FakeHTTP)


def test_checkout_redirects_back_to_the_app(monkeypatch):
    monkeypatch.setenv("LEMONSQUEEZY_API_KEY", "k")
    monkeypatch.setenv("LEMONSQUEEZY_STORE_ID", "1")
    DB.add_user("t", "ret@example.com")
    sent = {}
    _fake_http(monkeypatch, _Resp(201, {"data": {"attributes": {"url": "https://pay.example/c"}}}), sent)
    r = client.post("/create-checkout", json={"plan": "pro"}, headers=H("t"))
    assert r.status_code == 200
    redirect = sent["data"]["attributes"]["product_options"]["redirect_url"]
    assert redirect == main.CHECKOUT_REDIRECT_URL and "checkout=success" in redirect


def test_checkout_failure_logs_lemon_error_detail(monkeypatch, caplog):
    monkeypatch.setenv("LEMONSQUEEZY_API_KEY", "secret-key-never-logged")
    monkeypatch.setenv("LEMONSQUEEZY_STORE_ID", "1")
    DB.add_user("t", "err@example.com")
    _fake_http(monkeypatch, _Resp(404, {"errors": [{"title": "Not Found", "detail": "variant 111 not found"}]}), {})
    with caplog.at_level(logging.ERROR):
        assert client.post("/create-checkout", json={"plan": "pro"}, headers=H("t")).status_code == 400
    assert "variant 111 not found" in caplog.text and "secret-key-never-logged" not in caplog.text


def test_checkout_unconfigured_is_503_and_says_which_var(monkeypatch, caplog):
    monkeypatch.delenv("LEMONSQUEEZY_API_KEY", raising=False)
    DB.add_user("t", "cfg@example.com")
    with caplog.at_level(logging.ERROR):
        assert client.post("/create-checkout", json={"plan": "pro"}, headers=H("t")).status_code == 503
    assert "LEMONSQUEEZY_API_KEY set=False" in caplog.text


# ---------------------------------------------------------------- admin diagnostic
def test_billing_lookup_is_admin_only_and_read_only(monkeypatch):
    monkeypatch.setenv("LEMONSQUEEZY_API_KEY", "super-secret")
    u = DB.add_user("t", "cust@example.com")
    sub_event("subscription_created", email="cust@example.com", custom=signed(u), sub_id="S77")
    assert client.get("/admin/billing/lookup?email=cust@example.com", headers=H("t")).status_code == 403
    assert client.get("/admin/billing/lookup?email=cust@example.com", headers=H()).status_code == 401
    DB.add_user("boss", "boss@example.com")
    snap = lambda: json.dumps({k: v for k, v in DB.tables.items() if v}, default=str, sort_keys=True)
    before = snap()
    r = client.get("/admin/billing/lookup?email=Cust@Example.com", headers=H("boss"))
    assert r.status_code == 200
    j = r.json()
    assert j["plan_api_uses"] == "pro"
    assert j["subscriptions"][0]["variant_recognised_as"] == "pro"
    assert any(":S77:" in e["id"] for e in j["webhook_events"]["for_this_users_subscriptions"])
    assert j["config"]["secrets_set"]["LEMONSQUEEZY_API_KEY"] is True
    assert "super-secret" not in r.text
    assert snap() == before      # nothing written
