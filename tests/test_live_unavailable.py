"""Live mode (MOCK_MODE off) must never serve fixture data.

Regression for issue #4: when the Playwright browser binary went missing, the
driver silently fell back to its mock store, so /search returned [], /product
404'd, /health reported the fixture cart (cart_size 2, logged_in true) and
/payment/method and /delivery/slots kept returning hard-coded fixtures.
"""
import asyncio
import os
import sys
import types

os.environ.setdefault("SHUFERSAL_API_TOKEN", "test-secret-token-xyz-123")
os.environ.setdefault("JWT_SECRET", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")

import pytest
from fastapi.testclient import TestClient

import config
import main
import shufersal_driver as driver_module
from security import APIException, generate_jwt_token
from shufersal_driver import ShufersalDriver

AUTH = {"Authorization": f"Bearer {generate_jwt_token(subject='live-unavailable-test', expires_days=1)}"}
MISSING_BROWSER = (
    "BrowserType.launch_persistent_context: Executable doesn't exist at "
    "/tmp/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell"
)


class FakePage:
    def __init__(self, evaluate_result=None):
        self.url = "https://www.shufersal.co.il/online/he/"
        self.gotos = []
        self.evaluates = 0
        self.evaluate_result = evaluate_result

    async def goto(self, url, **kwargs):
        self.gotos.append(url)

    async def wait_for_timeout(self, ms):
        return None

    async def evaluate(self, script, *args):
        self.evaluates += 1
        return self.evaluate_result


def _install_failing_playwright(monkeypatch):
    class Chromium:
        async def launch_persistent_context(self, **kwargs):
            raise Exception(MISSING_BROWSER + "\n<install banner>")

    class PW:
        chromium = Chromium()

        async def stop(self):
            return None

    class Starter:
        async def start(self):
            return PW()

    fake = types.ModuleType("playwright.async_api")
    fake.async_playwright = lambda: Starter()
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake)


@pytest.fixture
def live_driver(monkeypatch):
    monkeypatch.setattr(config, "MOCK_MODE", False)
    _install_failing_playwright(monkeypatch)
    drv = ShufersalDriver()
    asyncio.run(drv.start())
    monkeypatch.setattr(main, "shufersal_driver", drv)
    return drv


def test_failed_launch_records_cause_and_does_not_fake_live(live_driver):
    assert live_driver.page is None
    assert "Executable doesn't exist" in live_driver._live_error
    assert "playwright install" in live_driver._live_error
    assert "<install banner>" not in live_driver._live_error


@pytest.mark.parametrize("path", ["/health", "/search?q=milk", "/product/7296073746423", "/cart"])
def test_reads_return_503_instead_of_fixture_or_empty(live_driver, path):
    res = TestClient(main.app, raise_server_exceptions=False).get(path, headers=AUTH)
    assert res.status_code == 503, res.text
    assert res.json()["error"]["code"] == "browser_unavailable"
    assert "playwright install" in res.json()["error"]["message"]


def test_health_does_not_report_fixture_cart(live_driver):
    res = TestClient(main.app, raise_server_exceptions=False).get("/health", headers=AUTH)
    assert res.status_code != 200
    assert "cart_size" not in res.text


def test_cart_writes_are_refused_when_browser_unavailable(live_driver):
    res = TestClient(main.app, raise_server_exceptions=False).post(
        "/cart/items", json={"product_id": "7296073746423", "qty": 1}, headers=AUTH
    )
    assert res.status_code == 503
    assert live_driver._mock_cart[0]["product_id"] == "P12345"  # mock store untouched
    assert len(live_driver._mock_cart) == 1


@pytest.mark.parametrize("path", ["/payment/method", "/delivery/slots"])
def test_fixture_only_endpoints_are_501_in_live_mode(live_driver, path):
    live_driver.page = FakePage()
    res = TestClient(main.app, raise_server_exceptions=False).get(path, headers=AUTH)
    assert res.status_code == 501, res.text
    assert res.json()["error"]["code"] == "not_implemented"
    assert "4242" not in res.text


def test_submit_order_never_runs_against_live_account(live_driver):
    page = FakePage(evaluate_result=0)
    live_driver.page = page
    quote = {"total_ils": 0.0, "delivery_slot_id": "slot_2026-10-20_morning"}
    with pytest.raises(APIException) as exc:
        asyncio.run(live_driver.submit_order("q1", 0.0, quote, cvv="123"))
    assert exc.value.status_code == 501
    assert page.evaluates == 0  # no cart read and no cart clearing


def test_retry_recovers_once_browser_is_installed(live_driver, monkeypatch):
    live_driver._last_live_attempt -= ShufersalDriver.LIVE_RETRY_SECONDS + 1

    async def ok_start(**kwargs):
        live_driver.page = FakePage(evaluate_result=[])
        live_driver._live_error = None
        return True

    monkeypatch.setattr(live_driver, "_start_live", ok_start)
    res = TestClient(main.app, raise_server_exceptions=False).get("/search?q=milk", headers=AUTH)
    assert res.status_code == 200
    assert res.json() == {"results": []}


def test_retry_is_throttled(live_driver, monkeypatch):
    calls = []

    async def counting_start(**kwargs):
        calls.append(1)
        return False

    monkeypatch.setattr(live_driver, "_start_live", counting_start)
    client = TestClient(main.app, raise_server_exceptions=False)
    for _ in range(3):
        assert client.get("/search?q=milk", headers=AUTH).status_code == 503
    assert calls == []  # last attempt was just now, so no relaunch storm


def test_search_query_is_url_encoded(live_driver):
    page = FakePage(evaluate_result=[])
    live_driver.page = page
    asyncio.run(live_driver.search_products("קוטג 3%&x", limit=3))
    assert page.gotos[0].endswith("/online/he/search?text=%D7%A7%D7%95%D7%98%D7%92%203%25%26x")


def test_product_requires_exact_code_match(live_driver):
    other = {"id": "522319", "name": "חלב", "brand": "x", "price_ils": 7.35,
             "unit": "יחידה", "image_url": "", "in_stock": True}
    live_driver.page = FakePage(evaluate_result=[other])
    assert asyncio.run(live_driver.get_product("7296073746423")) is None
    hit = dict(other, id="7296073746423")
    live_driver.page = FakePage(evaluate_result=[other, hit])
    assert asyncio.run(live_driver.get_product("P_7296073746423"))["id"] == "7296073746423"
