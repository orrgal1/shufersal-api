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
    res = client.patch("/cart/items/0", json={"qty": 5, "product_code": "522319"}, headers=AUTH)
    assert res.status_code == 502
    assert res.json()["error"]["code"] == "cart_write_unverified"


def test_update_and_delete_require_product_code(live, client, site):
    client.post("/cart/items", json={"product_id": "522319", "qty": 1}, headers=AUTH)
    before = len(write_requests(site))
    assert client.patch("/cart/items/0", json={"qty": 3}, headers=AUTH).json()["error"]["code"] == "product_code_required"
    assert client.delete("/cart/items/0", headers=AUTH).json()["error"]["code"] == "product_code_required"
    # A code that is not in the cart never touches another line, whatever the line id.
    assert client.patch("/cart/items/0", json={"qty": 3, "product_code": "52231"}, headers=AUTH).status_code == 404
    assert client.delete("/cart/items/0?product_code=4131074", headers=AUTH).status_code == 200
    assert len(write_requests(site)) == before
    res = client.patch("/cart/items/0", json={"qty": 3, "product_code": "P_522319"}, headers=AUTH)
    assert res.status_code == 200 and res.json()["items"][0]["qty"] == 3.0
    res = client.delete("/cart/items/0?product_code=522319", headers=AUTH)
    assert res.status_code == 200 and res.json()["items"] == []


def test_stale_line_ids_after_renumbering_hit_the_right_product(live, client, site):
    for code in ("522319", "7296073746423", "4131074"):  # lines 0, 1, 2
        site.cart.append({"entry": len(site.cart), "code": code, "qty": 1.0})
    # Delete A (line 0); the site renumbers: B becomes 0, C becomes 1.
    assert client.delete("/cart/items/0?product_code=522319", headers=AUTH).status_code == 200
    # Delete B with its stale id (1, which now points at C).
    res = client.delete("/cart/items/1?product_code=7296073746423", headers=AUTH)
    assert res.status_code == 200, res.text
    assert [e["code"] for e in site.cart] == ["4131074"]
    # Update C with its stale id (2, which no longer exists).
    res = client.patch("/cart/items/2", json={"qty": 4, "product_code": "4131074"}, headers=AUTH)
    assert res.status_code == 200 and site.cart == [{"entry": 0, "code": "4131074", "qty": 4.0}]


def test_retry_after_uncertain_add_reconciles_instead_of_adding_twice(live, client, site):
    site.fail_reads_after_write = 1  # the add lands, but the verification read fails
    hdr = {**AUTH, "Idempotency-Key": "add-uncertain-1"}
    first = client.post("/cart/items", json={"product_id": "522319", "qty": 2}, headers=hdr)
    assert first.status_code == 502
    assert site.cart == [{"entry": 0, "code": "522319", "qty": 2.0}]
    retry = client.post("/cart/items", json={"product_id": "522319", "qty": 2}, headers=hdr)
    assert retry.status_code == 200, retry.text
    assert site.cart == [{"entry": 0, "code": "522319", "qty": 2.0}]  # not 4
    assert len([r for r in site.requests if r[1] == "/online/he/cart/add"]) == 1


def test_retry_after_failed_add_writes_once(live, client, site):
    hdr = {**AUTH, "Idempotency-Key": "add-failed-1"}
    site.fail_add = True
    assert client.post("/cart/items", json={"product_id": "522319", "qty": 1}, headers=hdr).status_code == 502
    site.fail_add = False
    res = client.post("/cart/items", json={"product_id": "522319", "qty": 1}, headers=hdr)
    assert res.status_code == 200 and site.cart[0]["qty"] == 1.0


def test_retry_refuses_when_cart_moved_on(live, client, site):
    site.fail_reads_after_write = 1
    hdr = {**AUTH, "Idempotency-Key": "add-moved-1"}
    client.post("/cart/items", json={"product_id": "522319", "qty": 2}, headers=hdr)
    site.cart[0]["qty"] = 5.0  # someone changed it in between
    res = client.post("/cart/items", json={"product_id": "522319", "qty": 2}, headers=hdr)
    assert res.status_code == 409 and res.json()["error"]["code"] == "cart_state_changed"
    assert site.cart[0]["qty"] == 5.0


def test_batch_retry_reconciles(live, client, site):
    site.fail_reads_after_write = 1
    hdr = {**AUTH, "Idempotency-Key": "batch-1"}
    body = {"items": [{"product_id": "522319", "qty": 2}, {"product_id": "4131074", "qty": 1}], "clear_first": False}
    client.post("/cart/batch", json=body, headers=hdr)
    res = client.post("/cart/batch", json=body, headers=hdr)
    assert res.status_code == 200, res.text
    assert sorted((e["code"], e["qty"]) for e in site.cart) == [("4131074", 1.0), ("522319", 2.0)]


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


# ==================== Checkout ====================

def _fill(site):
    site.cart.append({"entry": 0, "code": "522319", "qty": 2.0})
    site.cart.append({"entry": 1, "code": "7296073746423", "qty": 1.0})


def _quote(client, slot="148340770"):
    res = client.post(f"/checkout/quote?delivery_slot_id={slot}", headers=AUTH)
    assert res.status_code == 200, res.text
    return res.json()


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setattr(config, "LIVE_CHECKOUT_ENABLED", True)
    monkeypatch.setattr(config, "SHUFERSAL_CVV", "042")


def test_quote_is_read_only_and_uses_live_fee(live, client, site):
    _fill(site)
    q = _quote(client)
    assert q["delivery_fee_ils"] == 35.9
    assert q["total_ils"] == round(7.35 * 2 + 39.9 + 35.9, 2)
    assert q["payment"] == {"brand": "VISA", "last4": "1111", "expiry_month": "07", "expiry_year": "2029", "name_on_card": "T. H."}
    assert write_requests(site) == []
    assert client.post("/checkout/quote?delivery_slot_id=148340772", headers=AUTH).json()["error"]["code"] == "slot_unavailable"


def test_dry_run_walks_checkout_and_stops_before_placing(live, client, site):
    _fill(site)
    q = _quote(client)
    cart_before = [dict(e) for e in site.cart]
    res = client.post("/checkout/submit", json={"quote_id": q["quote_id"], "confirm_total_ils": q["total_ils"],
                                                "expected_cart_hash": q["cart_hash"]}, headers=AUTH)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "dry_run" and body["dry_run"] is True and body["order_id"] is None
    assert body["site_total_ils"] == q["total_ils"]
    assert body["blockers"] == [] and body["checks"]["reached_checkout"] is True
    assert body["summary"]["slot"]["window"] == "12:00 - 14:00"
    assert site.selected_slot == "148340770"
    assert site.orders_placed == 0
    assert site.cart == cart_before  # never modifies the cart
    assert not [r for r in site.requests if r[1].startswith("/online/he/cart/") and r[0] == "POST"]


def test_quote_refuses_when_page_hides_lines(live, client, site):
    site.cart.append({"entry": 0, "code": "522319", "qty": 3.0})
    site.cart.append({"entry": 1, "code": "7296073746423", "qty": 1.0})
    site.hidden_entries = {1}  # site counts 4 units; the page shows 1 line of 3 units
    assert client.get("/cart", headers=AUTH).json()["lines_complete"] is False
    res = client.post("/checkout/quote?delivery_slot_id=148340770", headers=AUTH)
    assert res.status_code == 409 and res.json()["error"]["code"] == "cart_incomplete"


def test_dry_run_reports_blockers(live, client, site):
    _fill(site)
    q = _quote(client)
    site.checkout_state.update(reauth=True, cvv=True)
    site.cost_summary = {"unrelated": 1}
    body = client.post("/checkout/submit", json={"quote_id": q["quote_id"], "confirm_total_ils": q["total_ils"],
                                                 "expected_cart_hash": q["cart_hash"]}, headers=AUTH).json()
    assert body["status"] == "dry_run"
    assert len(body["blockers"]) == 3
    assert site.orders_placed == 0


def test_cart_change_after_quote_aborts(live, client, site, armed):
    _fill(site)
    q = _quote(client)
    site.cart[0]["qty"] = 3.0
    res = client.post("/checkout/submit", json={"quote_id": q["quote_id"], "confirm_total_ils": q["total_ils"],
                                                "expected_cart_hash": q["cart_hash"], "dry_run": False, "confirm": True},
                      headers={**AUTH, "Idempotency-Key": "change-1"})
    assert res.status_code == 409 and res.json()["error"]["code"] == "cart_mismatch"
    assert site.orders_placed == 0


def test_real_order_returns_site_order_number_once(live, client, site, armed):
    _fill(site)
    q = _quote(client)
    req = {"quote_id": q["quote_id"], "confirm_total_ils": q["total_ils"], "expected_cart_hash": q["cart_hash"],
           "dry_run": False, "confirm": True}
    res = client.post("/checkout/submit", json=req, headers={**AUTH, "Idempotency-Key": "order-1"})
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "placed" and res.json()["order_id"] == "123456789"
    assert site.place_order_query["jwtPayme"] == ["payme-jwt"]
    again = client.post("/checkout/submit", json=req, headers={**AUTH, "Idempotency-Key": "order-1"})
    assert again.json() == res.json()
    assert site.orders_placed == 1
    assert len(site.cart) == 2  # checkout does not clear the cart itself


def test_real_order_without_parsable_number_is_not_invented(live, client, site, armed):
    _fill(site)
    q = _quote(client)
    site.place_order_response = {"success": True, "redirect": "/checkout/thanks"}
    site.confirmation_html = "<p>תודה!</p>"
    res = client.post("/checkout/submit", json={"quote_id": q["quote_id"], "confirm_total_ils": q["total_ils"],
                                                "expected_cart_hash": q["cart_hash"], "dry_run": False, "confirm": True},
                      headers={**AUTH, "Idempotency-Key": "order-2"})
    assert res.status_code == 202
    assert res.json()["status"] == "placed_unconfirmed" and res.json()["order_id"] is None


@pytest.mark.parametrize("response,status,code", [
    ({"otherCreditCard3DSLink": "https://3ds"}, 202, "three_ds_required"),
    ([{"code": "OUT_OF_STOCK"}], 409, "order_rejected"),
    ({"weird": True}, 202, "outcome_unknown"),
])
def test_place_order_outcomes(live, client, site, armed, response, status, code):
    _fill(site)
    q = _quote(client)
    site.place_order_response = response
    key = f"order-{code}"
    req = {"quote_id": q["quote_id"], "confirm_total_ils": q["total_ils"], "expected_cart_hash": q["cart_hash"],
           "dry_run": False, "confirm": True}
    res = client.post("/checkout/submit", json=req, headers={**AUTH, "Idempotency-Key": key})
    assert res.status_code == status, res.text
    body = res.json()
    assert (body.get("status") or body["error"]["code"]) == code
    assert body.get("order_id") is None
    # Whatever happened, neither the key nor the quote can place a second order.
    client.post("/checkout/submit", json=req, headers={**AUTH, "Idempotency-Key": key})
    other = client.post("/checkout/submit", json=req, headers={**AUTH, "Idempotency-Key": key + "-new"})
    assert other.status_code == 409 and other.json()["error"]["code"] == "quote_consumed"
    assert site.orders_placed == 1


def test_blockers_stop_a_real_order(live, client, site, armed):
    _fill(site)
    q = _quote(client)
    site.checkout_state.update(cvv=True)
    res = client.post("/checkout/submit", json={"quote_id": q["quote_id"], "confirm_total_ils": q["total_ils"],
                                                "expected_cart_hash": q["cart_hash"], "dry_run": False, "confirm": True},
                      headers={**AUTH, "Idempotency-Key": "blocked-1"})
    assert res.status_code == 409 and res.json()["error"]["code"] == "checkout_blocked"
    assert "CVV" in res.json()["error"]["message"]
    assert site.orders_placed == 0


def test_live_order_placement_is_off_by_default(live, client, site, monkeypatch):
    monkeypatch.setattr(config, "SHUFERSAL_CVV", "042")
    _fill(site)
    q = _quote(client)
    res = client.post("/checkout/submit", json={"quote_id": q["quote_id"], "confirm_total_ils": q["total_ils"],
                                                "expected_cart_hash": q["cart_hash"], "dry_run": False, "confirm": True},
                      headers={**AUTH, "Idempotency-Key": "off-1"})
    assert res.status_code == 403 and res.json()["error"]["code"] == "live_checkout_disabled"
    assert site.orders_placed == 0 and site.selected_slot is None


def test_ledger_survives_restart(tmp_path):
    from checkout_ledger import CheckoutLedger
    path = str(tmp_path / "ledger.sqlite3")
    a = CheckoutLedger(path)
    assert a.claim("k", "q1") is None
    a.finish("k", "placed", 200, {"order_id": "1"})
    b = CheckoutLedger(path)  # a fresh process
    prev = b.claim("k", "q2")
    assert prev["state"] == "placed" and prev["result"] == {"order_id": "1"}


# ==================== Watched checkout ====================

@pytest.fixture
def watch_fast(monkeypatch):
    monkeypatch.setattr(config, "HEADLESS", True)
    monkeypatch.setattr(config, "WATCH_POLL_SECONDS", 0)
    monkeypatch.setattr(config, "WATCH_TIMEOUT_SECONDS", 5)


def _real(q, **extra):
    return {"quote_id": q["quote_id"], "confirm_total_ils": q["total_ils"], "expected_cart_hash": q["cart_hash"],
            "dry_run": False, "confirm": True, **extra}


def test_watched_run_opens_visible_browser_and_returns_or_s_order(live, client, site, armed, watch_fast):
    _fill(site)
    q = _quote(client)
    site.checkout_state_sequence = [{"reauth": True}, {"reauth": True}]  # Or types the SMS code
    site.checkout_state.update(cvv=True)  # PayMe card field: Or handles it
    site.confirm_on_url_reads = 3          # ...then clicks place-order
    res = client.post("/checkout/submit", json=_real(q, watched=True), headers={**AUTH, "Idempotency-Key": "watch-1"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "placed" and body["order_id"] == "987654321"
    assert body["blockers"] == [] and any("CVV" in w for w in body["warnings"])
    assert site.orders_placed == 0  # the service did not place the order itself
    assert [a["headless"] for a in live.attempts] == [True, False, True]  # visible for checkout, then back
    assert live._headless is True


def test_watched_run_timeout_burns_key_and_quote(live, client, site, armed, watch_fast, monkeypatch):
    monkeypatch.setattr(config, "WATCH_TIMEOUT_SECONDS", 0.05)
    _fill(site)
    q = _quote(client)
    res = client.post("/checkout/submit", json=_real(q, watched=True), headers={**AUTH, "Idempotency-Key": "watch-2"})
    assert res.status_code == 202 and res.json()["status"] == "handover_timeout" and res.json()["order_id"] is None
    again = client.post("/checkout/submit", json=_real(q, watched=True), headers={**AUTH, "Idempotency-Key": "watch-3"})
    assert again.json()["error"]["code"] == "quote_consumed"
    assert live._headless is True


def test_watched_dry_run_does_not_wait_or_place(live, client, site, watch_fast):
    _fill(site)
    q = _quote(client)
    site.confirm_on_url_reads = 0
    res = client.post("/checkout/submit", json={**_real(q, watched=True), "dry_run": True, "confirm": False}, headers=AUTH)
    assert res.status_code == 200 and res.json()["status"] == "dry_run"
    assert site.orders_placed == 0


def test_site_total_reads_cart_level_key_only():
    from shufersal_parsers import extract_site_total
    assert extract_site_total({"totalPrice": {"value": 90.5}, "entries": [{"totalPrice": {"value": 7.35}}]}) == 90.5
    assert extract_site_total({"entries": [{"totalPrice": {"value": 7.35}}], "subTotal": {"value": 50}}) is None
    assert extract_site_total({"totalPriceWithDelivery": {"value": 126.4}, "totalPrice": {"value": 90.5}}) == 126.4
    assert extract_site_total([{"totalPrice": 1}]) is None


def test_completeness_uses_units_not_lines(live, client, site):
    site.cart.append({"entry": 0, "code": "522319", "qty": 2.0})
    assert client.get("/cart", headers=AUTH).json()["lines_complete"] is True  # count 2 == 2 units
    original = site._minicart
    site._minicart = lambda: original().replace('"miniCartCount": 2', '"miniCartCount": 1')  # a line count
    assert client.get("/cart", headers=AUTH).json()["lines_complete"] is False


def test_calls_fail_fast_while_a_watched_checkout_is_open(live, client, site, armed, watch_fast):
    _fill(site)
    q = _quote(client)
    seen = {}

    async def scenario():
        import main as m
        task = asyncio.ensure_future(live.checkout(slot_id="148340770", expected_cart_hash=q["cart_hash"],
                                                   confirm_total_ils=q["total_ils"], place_order=True, watched=True))
        await asyncio.sleep(0)  # let it take the lock and start waiting for Or
        while not live._checkout_active:
            await asyncio.sleep(0)
        for name, call in (("add", live.add_to_cart("4131074", 1)), ("cart", live.get_cart()),
                           ("health", live.check_login_status()), ("clear", live.clear_cart())):
            try:
                await asyncio.wait_for(call, timeout=1)
                seen[name] = "ok"
            except APIException as e:
                seen[name] = e.code
        site.confirm_on_url_reads = 0  # Or places the order
        return await task

    result = run(scenario())
    assert seen == {"add": "checkout_in_progress", "cart": "checkout_in_progress",
                    "health": "checkout_in_progress", "clear": "checkout_in_progress"}
    assert result["status"] == "placed"
    assert live._checkout_active is False
    assert client.get("/cart", headers=AUTH).status_code == 200
    assert len(site.cart) == 2  # nothing slipped in during the handover


def test_failed_visible_relaunch_restores_headless_and_clears_flag(live, client, site, armed, watch_fast, monkeypatch):
    _fill(site)
    q = _quote(client)
    real_start = live._start_live

    async def start(headless=None, user_data_dir=None):
        if headless is False:
            return False
        return await real_start(headless=headless, user_data_dir=user_data_dir)

    monkeypatch.setattr(live, "_start_live", start)
    res = client.post("/checkout/submit", json=_real(q, watched=True), headers={**AUTH, "Idempotency-Key": "watch-fail"})
    assert res.status_code == 503
    assert live._checkout_active is False and live._is_alive()
    assert client.get("/cart", headers=AUTH).status_code == 200


def test_watched_run_blocks_when_total_is_unreadable(live, client, site, armed, watch_fast):
    _fill(site)
    q = _quote(client)
    site.cost_summary = {"entries": [{"totalPrice": {"value": 1}}]}
    site.confirm_on_url_reads = 0
    dry = client.post("/checkout/submit", json={**_real(q, watched=True), "dry_run": True, "confirm": False}, headers=AUTH)
    assert any("order total" in b for b in dry.json()["blockers"])
    res = client.post("/checkout/submit", json=_real(q, watched=True), headers={**AUTH, "Idempotency-Key": "watch-total"})
    assert res.status_code == 409 and res.json()["error"]["code"] == "checkout_blocked"
