"""Oxylabs Web API key is the only credential; auth/quota errors stop the run; non-JSON is handled."""
import httpx
import pytest

from sourcing import http_sources, oxylabs, presource


KEY = "dummy-test-key-123"


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(oxylabs, "WEB_API_KEY", KEY)
    monkeypatch.setattr(oxylabs.time, "sleep", lambda *_: None)


def test_web_api_key_sent_as_bearer(monkeypatch, key):
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, body=json, auth=headers["Authorization"])
        return httpx.Response(200, json={"state": "done", "results": [{"html": "<html>ok</html>"}]})

    monkeypatch.setattr(oxylabs.httpx, "post", fake_post)
    b = oxylabs.Budget(5)
    out = oxylabs.scrape({"source": "universal", "url": "https://x.test/p", "render": "html"}, b)
    assert out == "<html>ok</html>" and b.used == 1
    assert seen["url"] == oxylabs.WEB_API_ENDPOINT and seen["auth"] == f"Bearer {KEY}"
    assert seen["body"] == {"url": "https://x.test/p", "output": ["html"], "location": "US", "run_js": True}


@pytest.mark.parametrize("code", [401, 403])
def test_auth_errors_stop_the_run(monkeypatch, key, code):
    monkeypatch.setattr(oxylabs.httpx, "post", lambda *a, **k: httpx.Response(code, text=f"denied {KEY}"))
    with pytest.raises(oxylabs.OxylabsAuthError) as e:
        oxylabs.scrape({"url": "https://x.test/p"}, oxylabs.Budget(5))
    assert KEY not in str(e.value)                       # never echo the key


def test_no_key_is_fatal_in_oxylabs_but_a_block_in_sources(monkeypatch):
    monkeypatch.setattr(oxylabs, "WEB_API_KEY", "")
    with pytest.raises(oxylabs.OxylabsAuthError):
        oxylabs.scrape({"url": "https://x.test/p"}, oxylabs.Budget(5))
    with pytest.raises(http_sources.MarketplaceBlocked):
        http_sources.scrape({"url": "https://x.test/p"}, oxylabs.Budget(5))


def test_quota_429_stops(monkeypatch, key):
    monkeypatch.setattr(oxylabs.httpx, "post", lambda *a, **k: httpx.Response(429, json={"message": "Your current plan's quota has been reached."}))
    with pytest.raises(oxylabs.OxylabsQuotaError):
        oxylabs.scrape({"url": "https://x.test/p"}, oxylabs.Budget(5))


def test_non_json_200_is_retried_then_a_page_failure(monkeypatch, key, capsys):
    monkeypatch.setattr(oxylabs.httpx, "post", lambda *a, **k: httpx.Response(200, text="<html>gateway</html>"))
    b = oxylabs.Budget(10)
    with pytest.raises(RuntimeError) as e:
        oxylabs.scrape({"url": "https://x.test/p"}, b, retries=2)
    assert not isinstance(e.value, oxylabs.OxylabsError)  # one page, not a run stopper
    assert "not JSON" in str(e.value) and "gateway" in str(e.value) and b.used == 3
    assert "HTTP 200" in capsys.readouterr().out


def test_budget_exhausted_is_its_own_error():
    b = oxylabs.Budget(1)
    b.take()
    with pytest.raises(oxylabs.BudgetExhausted):
        b.take()


def test_aliexpress_non_json_upload_is_a_block_with_status_and_snippet(monkeypatch):
    class C:
        cookies = httpx.Cookies()

        def post(self, *a, **k):
            return httpx.Response(403, text="<html><title>Access denied</title> punish</html>")

    with pytest.raises(http_sources.MarketplaceBlocked) as e:
        http_sources._ae_mtop(C(), "{}")
    msg = str(e.value)
    assert "HTTP 403" in msg and "non-JSON" in msg and "Access denied" in msg


def test_aliexpress_does_not_hide_a_rejected_key(monkeypatch, key):
    monkeypatch.setattr(http_sources, "aliexpress_upload", lambda photo: "file-1")
    monkeypatch.setattr(http_sources.httpx, "get", lambda *a, **k: httpx.Response(200, text="<html>_____tmd_____</html>"))
    monkeypatch.setattr(http_sources, "_AE_GAP_SECONDS", 0)
    monkeypatch.setattr(oxylabs.httpx, "post", lambda *a, **k: httpx.Response(401, json={}))
    with pytest.raises(oxylabs.OxylabsAuthError):
        http_sources.aliexpress_by_image(b"img", oxylabs.Budget(5))


@pytest.mark.parametrize("stats,err,n,code", [
    ({"done": 9, "no_match": 0, "failed": 2}, oxylabs.OxylabsAuthError("401"), 400, 3),   # the 2026-11-14 run
    ({"done": 0, "no_match": 0, "failed": 5}, None, 5, 4),
    ({"done": 50, "no_match": 1, "failed": 0}, oxylabs.BudgetExhausted("cap"), 400, 0),
    ({"done": 3, "no_match": 1, "failed": 1}, None, 5, 0),
    ({"done": 0, "no_match": 0, "failed": 0}, None, 0, 0),
])
def test_exit_codes(stats, err, n, code):
    assert presource.exit_code(stats, err, n) == code
