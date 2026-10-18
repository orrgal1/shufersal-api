"""Live-mode driver behaviour (MOCK_MODE off) against an in-memory fake site.

The driver's real _start_live runs against a fake Playwright module, so the
browser launch, relaunch and in-page fetch paths are exercised for real.
"""
import asyncio
import os

os.environ.setdefault("SHUFERSAL_API_TOKEN", "test-secret-token-xyz-123")
os.environ.setdefault("JWT_SECRET", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")

import pytest
from fastapi.testclient import TestClient

import config
import main
from security import APIException, generate_jwt_token
from shufersal_driver import ShufersalDriver
from tests.fake_site import FakeSite, install_fake_playwright

AUTH = {"Authorization": f"Bearer {generate_jwt_token(subject='live-driver-test', expires_days=1)}"}


def run(coro):
    return asyncio.run(coro)


def make_driver(monkeypatch, site, fail_times=0):
    monkeypatch.setattr(config, "MOCK_MODE", False)
    attempts = install_fake_playwright(monkeypatch, site, fail_times=fail_times)
    drv = ShufersalDriver()
    monkeypatch.setattr(drv, "_extract_chrome_session_cookies", lambda: [])
    run(drv.start())
    monkeypatch.setattr(main, "shufersal_driver", drv)
    return drv, attempts


@pytest.fixture
def site():
    return FakeSite()


@pytest.fixture
def live(monkeypatch, site):
    drv, attempts = make_driver(monkeypatch, site)
    drv.attempts = attempts
    return drv


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def write_requests(site):
    return [r for r in site.requests if r[0] == "POST"]


# ==================== Browser unavailable / relaunch ====================

@pytest.fixture
def dead(monkeypatch, site):
    drv, attempts = make_driver(monkeypatch, site, fail_times=99)
    drv.attempts = attempts
    return drv


def test_failed_launch_records_cause_and_does_not_fake_live(dead):
    assert dead.page is None
    assert "Executable doesn't exist" in dead._live_error
    assert "playwright install" in dead._live_error
    assert "<banner>" not in dead._live_error


@pytest.mark.parametrize("path", ["/health", "/search?q=milk", "/product/7296073746423", "/cart",
                                  "/delivery/slots", "/payment/method"])
def test_reads_return_503_when_browser_unavailable(dead, client, path):
    res = client.get(path, headers=AUTH)
    assert res.status_code == 503, res.text
    assert res.json()["error"]["code"] == "browser_unavailable"
    assert "cart_size" not in res.text


def test_cart_writes_refused_when_browser_unavailable(dead, client, site):
    res = client.post("/cart/items", json={"product_id": "522319", "qty": 1}, headers=AUTH)
    assert res.status_code == 503
    assert site.requests == []


def test_failed_launch_is_retried_after_throttle_window(monkeypatch, site, client):
    drv, attempts = make_driver(monkeypatch, site, fail_times=1)
    assert drv.page is None and len(attempts) == 1
    assert client.get("/search?q=522319", headers=AUTH).status_code == 503
    assert len(attempts) == 1  # still inside the retry window
    drv._last_live_attempt -= ShufersalDriver.LIVE_RETRY_SECONDS + 1
    res = client.get("/search?q=522319", headers=AUTH)
    assert res.status_code == 200, res.text
    assert len(attempts) == 2
    assert res.json()["results"][0]["id"] == "522319"


def test_browser_that_dies_mid_run_is_relaunched(live, client, site):
    assert client.get("/cart", headers=AUTH).status_code == 200
    old_page = live.page
    old_page.close_unexpectedly()
    res = client.get("/cart", headers=AUTH)
    assert res.status_code == 200, res.text
    assert len(live.attempts) == 2
    assert live.page is not old_page and not live.page.is_closed()


def test_close_event_from_replaced_context_does_not_kill_new_one(live):
    old_ctx = live.context
    run(live._start_live())  # replaces the context; old one fires "close"
    assert live._is_alive()
    for fn in old_ctx.handlers.get("close", []):
        fn(old_ctx)
    assert live._is_alive()


# ==================== Catalog ====================

def test_search_query_is_url_encoded(live, site):
    run(live.search_products("קוטג 3%&x", limit=3))
    method, path, query, _ = site.requests[-1]
    assert path == "/online/he/search/results"
    assert "q=%D7%A7%D7%95%D7%98%D7%92%203%25%26x" in query


def test_product_requires_exact_code_match(live, client):
    assert client.get("/product/4131", headers=AUTH).status_code == 404
    res = client.get("/product/P_7296073746423", headers=AUTH)
    assert res.status_code == 200
    assert res.json()["id"] == "7296073746423"
    assert res.json()["price_ils"] == 39.9


# ==================== Cart ====================

def test_add_new_product_posts_exact_code_and_verifies(live, client, site):
    res = client.post("/cart/items", json={"product_id": "522319", "qty": 2}, headers=AUTH)
    assert res.status_code == 200, res.text
    cart = res.json()
    assert [(i["product_id"], i["qty"]) for i in cart["items"]] == [("522319", 2.0)]
    assert cart["subtotal_ils"] == 14.7
    adds = [r for r in site.requests if r[1] == "/online/he/cart/add"]
    assert len(adds) == 1 and '"productCodePost": "P_522319"' in adds[0][3]


def test_add_unknown_or_fuzzy_product_never_writes(live, client, site):
    for pid in ("4131", "999999", "52231 9", "../x"):
        res = client.post("/cart/items", json={"product_id": pid, "qty": 1}, headers=AUTH)
        assert res.status_code == 404, (pid, res.text)
    assert write_requests(site) == []


def test_add_existing_product_updates_to_sum(live, client, site):
    client.post("/cart/items", json={"product_id": "522319", "qty": 1}, headers=AUTH)
    res = client.post("/cart/items", json={"product_id": "522319", "qty": 2}, headers=AUTH)
    assert res.status_code == 200
    assert res.json()["items"][0]["qty"] == 3.0
    assert len(res.json()["items"]) == 1


def test_add_ignored_by_site_is_an_error(live, client, site):
    site.fail_add = True
    res = client.post("/cart/items", json={"product_id": "522319", "qty": 1}, headers=AUTH)
    assert res.status_code == 502
    assert res.json()["error"]["code"] == "cart_write_unverified"


def test_update_ignored_by_site_is_an_error(live, client, site):
    client.post("/cart/items", json={"product_id": "522319", "qty": 1}, headers=AUTH)
    site.fail_update = True
    res = client.patch("/cart/items/0", json={"qty": 5}, headers=AUTH)
    assert res.status_code == 502
    assert res.json()["error"]["code"] == "cart_write_unverified"


def test_update_and_delete_need_exact_line_id(live, client, site):
    client.post("/cart/items", json={"product_id": "522319", "qty": 1}, headers=AUTH)
    before = len(write_requests(site))
    for line in ("1", "522319", "5", "00", "P_522319"):
        assert client.patch(f"/cart/items/{line}", json={"qty": 3}, headers=AUTH).status_code == 404, line
        assert client.delete(f"/cart/items/{line}", headers=AUTH).status_code == 404, line
    assert len(write_requests(site)) == before
    res = client.patch("/cart/items/0", json={"qty": 3}, headers=AUTH)
    assert res.status_code == 200 and res.json()["items"][0]["qty"] == 3.0
    res = client.delete("/cart/items/0", headers=AUTH)
    assert res.status_code == 200 and res.json()["items"] == []


def test_clear_loops_until_site_cart_is_empty(live, client, site):
    for i in range(25):
        site.cart.append({"entry": i, "code": "522319" if i % 2 else "4131074", "qty": 1.0})
    site.ignore_first_removals = 3
    res = client.delete("/cart", headers=AUTH)
    assert res.status_code == 200, res.text
    assert site.cart == []
    assert res.json()["items"] == [] and res.json()["item_count"] == 0


def test_clear_that_never_empties_is_an_error(live, client, site):
    site.cart.append({"entry": 0, "code": "522319", "qty": 1.0})
    site.fail_update = True
    res = client.delete("/cart", headers=AUTH)
    assert res.status_code == 502
    assert res.json()["error"]["code"] == "cart_clear_incomplete"


def test_cart_count_failure_is_an_error_not_empty(live, client, site):
    site.cart.append({"entry": 0, "code": "522319", "qty": 1.0})
    site.minicart_error = True
    res = client.get("/cart", headers=AUTH)
    assert res.status_code == 502


def test_cart_uses_site_totals_and_flags_unknown_prices(live, client, site, monkeypatch):
    site.cart.append({"entry": 0, "code": "522319", "qty": 2.0})
    site.cart.append({"entry": 1, "code": "8000001", "qty": 1.5})
    import tests.fake_site as fs
    monkeypatch.setitem(fs.CATALOG, "8000001", dict(fs.CATALOG["8000001"], name="hidden"))
    original = site.handle

    def no_search_for_tomato(url, method, body, ct):
        if "search/results" in url and "8000001" in url:
            return 200, url, '{"results": []}'
        return original(url, method, body, ct)

    site.handle = no_search_for_tomato
    cart = client.get("/cart", headers=AUTH).json()
    assert cart["subtotal_ils"] == round(7.35 * 2 + 9.9 * 1.5, 2)  # the site's own figure
    assert cart["delivery_fee_ils"] is None and cart["total_includes_delivery"] is False
    tomato = next(i for i in cart["items"] if i["product_id"] == "8000001")
    assert tomato["unit_price_ils"] is None and tomato["line_total_ils"] is None
    assert cart["prices_complete"] is False
    milk = next(i for i in cart["items"] if i["product_id"] == "522319")
    assert milk["unit_price_ils"] == 7.35 and milk["line_total_ils"] == 14.7
    assert all(i["line_id"] != "" for i in cart["items"])  # recommendation tiles excluded


# ==================== Slots & payment ====================

def test_live_delivery_slots(live, client):
    slots = client.get("/delivery/slots", headers=AUTH).json()["slots"]
    assert slots[0] == {"id": "148340770", "date": "2026-10-20", "window": "12:00 - 14:00", "fee_ils": 35.9,
                        "available": True, "start": "2026-10-20T12:00:00+03:00", "end": "2026-10-20T14:00:00+03:00"}
    assert slots[1]["available"] is False


def test_live_payment_method_is_masked(live, client):
    res = client.get("/payment/method", headers=AUTH)
    assert res.status_code == 200
    assert res.json() == {"brand": "VISA", "last4": "1111", "expiry_month": "07", "expiry_year": "2029",
                          "name_on_card": "T. H."}
    assert "411111" not in res.text and "secret-token" not in res.text and "Holder" not in res.text
