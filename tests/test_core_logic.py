"""Core logic pass (2026-11-14): admin = ultimate for access, consistent "hot", DB-side stage filter,
shops from the current catalogue, import jobs that survive a restart, clear GitHub dispatch errors."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import test_security as ts          # sets env + patches supabase before importing main
from test_security import DB, H, client, main


@pytest.fixture(autouse=True)
def _reset():
    DB.reset()
    main._free_cache.clear()
    main.limiter.reset()
    main._manual_limits.storage.reset()
    main._IMPORT_JOBS.clear()
    main._job_store["ok"] = True
    yield


def pid(i):
    return str(uuid.UUID(int=i + 1))


def row(i, score, phase="rising", shop=None, rev=100.0, growth=10.0, active=True):
    return {"id": pid(i), "product_name": f"P{i}", "virality_score": score, "tiktok_price": 10.0,
            "category": "Home", "trend_phase": phase, "is_active": active, "gmv_growth_pct": growth,
            "data_end_date": "2026-11-10", "window_days": 1, "revenue_window": rev, "gmv_7d": rev * 7,
            "shop_name": shop}


def seed(n=25):
    DB.tables["tiktok_products"] = [row(i, 100 - i) for i in range(n)]


# --------------------------------------------------------------------------- admins
def admin(token="boss"):
    DB.add_user(token, "boss@example.com", plan="free")      # stored plan stays free
    return H(token)


def test_admin_with_free_billing_plan_sees_the_whole_catalogue():
    seed(25)
    r = client.get("/products?limit=100", headers=admin()).json()
    assert r["plan"] == "ultimate" and r["count"] == 25
    assert client.get("/products?limit=100", headers=H()).json()["count"] == main.FREE_PRODUCT_LIMIT


def test_admin_paid_on_search_shops_creators_tiers():
    seed(25)
    DB.tables["shops"] = [{"id": i, "shop_name": f"S{i}", "gmv_total": 100 - i} for i in range(15)]
    DB.tables["creators"] = [{"id": i, "tiktok_handle": f"c{i}", "gmv_total": 100 - i} for i in range(15)]
    h = admin()
    assert client.get("/search?q=P1&limit=100", headers=h).json()["plan"] == "ultimate"
    assert len(client.get("/shops", headers=h).json()["shops"]) == 15
    assert len(client.get("/creators", headers=h).json()["creators"]) == 15
    assert len(client.get("/products/tier/hot?limit=100", headers=h).json()["products"]) == 21   # 100..80


def test_unconfirmed_admin_email_is_not_admin():
    seed(25)
    DB.add_user("fake", "boss@example.com", confirmed=False, plan="free")
    assert client.get("/products?limit=100", headers=H("fake")).json()["plan"] == "free"


def test_admin_plan_never_touches_billing():
    DB.add_user("boss", "boss@example.com", plan="free")
    me = client.get("/me", headers=H("boss")).json()
    assert me == {"signed_in": True, "plan": "ultimate", "billing_plan": "free", "admin": True}
    assert DB.tables["users"][0]["plan"] == "free"           # nothing written back
    assert not any(c for c in DB.rpc_calls if "start_credit_period" in str(c))


def test_me_for_normal_and_signed_out_users():
    DB.add_user("p", "pro@example.com", plan="Pro ")
    assert client.get("/me", headers=H("p")).json() == {"signed_in": True, "plan": "pro", "billing_plan": "pro", "admin": False}
    assert client.get("/me", headers=H()).json()["plan"] == "free"


# --------------------------------------------------------------------------- hot / stats
def test_stats_hot_is_score_80_plus_and_matches_tier_route():
    DB.tables["tiktok_products"] = [row(0, 95, "emerging"), row(1, 80, "saturated"), row(2, 79, "emerging"),
                                    row(3, 66, "rising"), row(4, 55), row(5, 10), row(6, 99, active=False)]
    st = client.get("/stats", headers=H()).json()["stats"]
    assert st["hot"] == 2 and st["score_tiers"] == {"score_80_plus": 2, "score_65_79": 2, "score_50_64": 1, "score_below_50": 1}
    assert st["hot_definition"] == "virality_score >= 80"
    assert st["stages"]["emerging"] == 2                       # stage is a separate axis
    DB.add_user("p", "pro@example.com", plan="pro")
    tier = client.get("/products/tier/hot", headers=H("p")).json()["products"]
    assert len(tier) == st["hot"]


# --------------------------------------------------------------------------- stage filter paging
def test_stage_filter_runs_in_the_db_so_pages_are_full():
    # v1 "watch" rows hold both peaking (growth known) and unknown (growth unknown)
    DB.tables["tiktok_products"] = (
        [row(i, 100 - i, "watch", growth=None) for i in range(10)] +
        [row(10 + i, 80 - i, "watch", growth=5.0) for i in range(10)])
    DB.add_user("p", "pro@example.com", plan="pro")
    r = client.get("/products?trend_phase=peaking&limit=5&offset=0", headers=H("p")).json()
    assert r["count"] == 5 and all(p["trend_phase"] == "peaking" for p in r["products"])
    r2 = client.get("/products?trend_phase=peaking&limit=5&offset=5", headers=H("p")).json()
    assert r2["count"] == 5
    assert client.get("/products?trend_phase=unknown&limit=100", headers=H("p")).json()["count"] == 10


# --------------------------------------------------------------------------- shops
def test_products_of_one_shop_paid_only():
    DB.tables["tiktok_products"] = [row(0, 90, shop="Medicube US Store"), row(1, 80, shop="medicube us store"),
                                    row(2, 70, shop="Other"), row(3, 60, shop="Medi_cube")]
    DB.add_user("p", "pro@example.com", plan="pro")
    got = client.get("/products?shop_name=MEDICUBE US STORE", headers=H("p")).json()["products"]
    assert [p["id"] for p in got] == [pid(0), pid(1)]
    assert [p["id"] for p in client.get("/products?shop_name=Medi_cube", headers=H("p")).json()["products"]] == [pid(3)]


def test_catalogue_shops_counts_share_growth_and_profile_join():
    DB.tables["tiktok_products"] = [
        row(0, 90, shop="Medicube US Store", rev=300, growth=100.0),
        row(1, 80, shop="medicube us store", rev=100, growth=-20.0),
        row(2, 70, shop="Other", rev=600, growth=None),
        row(3, 60, shop=None, rev=1000),
        row(4, 99, shop="Medicube US Store", rev=5000, active=False),          # hidden: ignored
    ]
    DB.tables["shops"] = [{"id": 1, "shop_name": "MEDICUBE US STORE", "follower_count": 1234,
                           "shop_url": "https://shop.example/m", "seller_type": "Brand", "image_url": None}]
    DB.add_user("p", "pro@example.com", plan="pro")
    r = client.get("/shops/catalogue", headers=H("p")).json()
    assert r["catalogue_revenue"] == 2000 and r["products_without_shop"] == 1 and r["total"] == 2
    other, medi = r["shops"]
    assert other["shop_name"] == "Other" and other["revenue_share_pct"] == 30.0 and other["growth_pct"] is None
    assert medi["product_count"] == 2 and medi["revenue_window"] == 400 and medi["revenue_share_pct"] == 20.0
    assert medi["growth_pct"] == 70.0                           # (300*100 + 100*-20) / 400
    assert [p["id"] for p in medi["top_products"]] == [pid(0), pid(1)]
    assert medi["follower_count"] == 1234 and medi["has_shop_profile"] is True


def test_catalogue_shops_free_tier_hides_paid_products():
    DB.tables["tiktok_products"] = [row(i, 100 - i, shop=f"S{i % 12}", rev=1000 - i) for i in range(24)]
    r = client.get("/shops/catalogue", headers=H()).json()
    assert r["plan"] == "free" and len(r["shops"]) == main.FREE_PRODUCT_LIMIT
    free_ids = {pid(i) for i in range(10)}
    assert all(p["id"] in free_ids for s in r["shops"] for p in s["top_products"])


# --------------------------------------------------------------------------- import jobs
def _job(status="running", age_s=0, jid="abc123"):
    t = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
    return {"id": jid, "status": status, "started_by": "boss@example.com", "started_at": t, "updated_at": t,
            "finished_at": None, "files": [], "summary": None, "warnings": [], "error": None, "log": ["== Products"]}


def test_import_job_survives_restart_from_the_table():
    DB.add_user("boss", "boss@example.com")
    DB.tables["import_jobs"] = [_job("done")]
    r = client.get("/admin/import/abc123", headers=H("boss"))
    assert r.status_code == 200 and r.json()["status"] == "done" and r.json()["by"] == "boss@example.com"


def test_running_job_from_a_dead_process_is_reported_as_interrupted():
    DB.add_user("boss", "boss@example.com")
    DB.tables["import_jobs"] = [_job("running", age_s=600)]
    j = client.get("/admin/import/abc123", headers=H("boss")).json()
    assert j["status"] == "failed" and "restarted" in j["error"] and j["log"][-1].startswith("Stopped:")
    assert DB.tables["import_jobs"][0]["status"] == "failed"


def test_recent_running_job_stays_running():
    DB.add_user("boss", "boss@example.com")
    DB.tables["import_jobs"] = [_job("running", age_s=10)]
    assert client.get("/admin/import/abc123", headers=H("boss")).json()["status"] == "running"


def test_persist_job_writes_row_and_turns_off_without_table():
    job = {"id": "j1", "status": "running", "by": "a", "started_at": "x", "log": ["l"], "dir": "/tmp/x"}
    main._persist_job(job, force=True)
    stored = DB.tables["import_jobs"][0]
    assert stored["id"] == "j1" and "dir" not in stored and stored["started_by"] == "a"
    DB.fail_tables["import_jobs"] = "PGRST205 table not found"
    main._persist_job(dict(job, id="j2"), force=True)
    assert main._job_store["ok"] is False                      # memory only from now on, no crash
    main._persist_job(dict(job, id="j3", dry_run=True), force=True)


def test_import_list_merges_memory_and_table():
    DB.add_user("boss", "boss@example.com")
    DB.tables["import_jobs"] = [_job("done", jid="old1")]
    main._IMPORT_JOBS["mem1"] = dict(_job("running", jid="mem1"), by="boss@example.com", dir="/tmp/x")
    ids = {j["id"] for j in client.get("/admin/import", headers=H("boss")).json()["imports"]}
    assert ids == {"old1", "mem1"}


# --------------------------------------------------------------------------- GitHub dispatch
@pytest.mark.parametrize("code,needle", [(403, "fine-grained token from the sodewala45"), (401, "wrong or expired"),
                                         (404, "presource.yml"), (422, "must contain")])
def test_github_errors_are_specific_and_short(code, needle):
    r = httpx.Response(code, json={"message": "Resource not accessible by personal access token"})
    msg = main._gh_error(r)
    assert needle in msg and len(msg) <= 240
