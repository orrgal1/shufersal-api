"""Out-of-stock cart lines: flagged by GET /cart and the quote, and the
checkout's missing-products popup stops an unattended order with
409 products_unavailable, without clicking anything in it."""
import json

import pytest

import main
from shufersal_parsers import parse_checkout_recommendations, parse_product_page_stock
from tests.test_live_driver import AUTH, _fill, _quote, client, live, site  # noqa: F401
from tests.test_unattended import _submit, ua  # noqa: F401

# Trimmed from the live answer on 2026-10-20 (slot 148340771, 19-line cart).
LIVE_RECOMMENDATIONS = [{"type": "OUT_OF_STOCK", "count": 1, "products": [{"product": {
    "code": "P_749", "name": "פלפל שישקה מתוק", "purchasable": True,
    "stock": {"stockLevelStatus": {"code": "outOfStock", "type": "StockLevelStatus"}},
    "sellingMethod": {"code": "BY_WEIGHT", "type": "SellingMethodEnum"},
    "cartStatus": {"inCart": True, "qty": 0.5, "sellingMethod": "BY_WEIGHT", "comment": None, "cartEntryNumber": 3}},
    "replaceable": True}]}]


def _add_pepper(site):
    site.cart.append({"entry": len(site.cart), "code": "749", "qty": 0.5})
    site.out_of_stock = {"749"}


# ==================== Parsers ====================

def test_parse_live_recommendations():
    assert parse_checkout_recommendations(LIVE_RECOMMENDATIONS) == [{
        "product_id": "749", "name": "פלפל שישקה מתוק", "reason": "out_of_stock", "qty": 0.5,
        "line_id": "3", "replaceable": True}]
    assert parse_checkout_recommendations([]) == []
    promo_only = [{"type": "EXPIRING_PROMOTION", "count": 1, "promotions": [{"code": "x"}], "products": []}]
    assert parse_checkout_recommendations(promo_only) == []
    with pytest.raises(ValueError):
        parse_checkout_recommendations({"error": "x"})


def test_parse_product_page_stock():
    page = '<div class="productDetails nonFood miglog-prod-{} notOverlay"></div><div class="miglog-prod-outOfStock-msg">חסר במלאי</div>'
    assert parse_product_page_stock(page.format("outOfStock")) is False
    assert parse_product_page_stock(page.format("inStock")) is True
    assert parse_product_page_stock(page.format("lowStock")) is True
    # The "חסר במלאי" text alone is in every page: no class, no answer.
    assert parse_product_page_stock('<div class="miglog-prod-outOfStock-msg">חסר במלאי</div>') is None
    assert parse_product_page_stock(None) is None


# ==================== GET /cart and the quote ====================

def test_cart_flags_out_of_stock_lines(live, client, site):
    _fill(site)
    _add_pepper(site)
    res = client.get("/cart", headers=AUTH)
    assert res.status_code == 200, res.text
    body = res.json()
    by_code = {i["product_id"]: i for i in body["items"]}
    assert by_code["749"]["available"] is False
    assert by_code["522319"]["available"] is True and by_code["7296073746423"]["available"] is True
    assert body["unavailable_items"] == [{"line_id": "2", "product_id": "749", "name": "פלפל שישקה מתוק",
                                          "qty": 0.5, "reason": "out_of_stock"}]
    assert body["availability_complete"] is True
    # Read-only: no writes, and the out-of-stock product's page was read.
    assert not [r for r in site.requests if r[0] != "GET"]
    assert any(r[1] == "/online/he/p/P_749" for r in site.requests)


def test_cart_all_in_stock(live, client, site):
    _fill(site)
    body = client.get("/cart", headers=AUTH).json()
    assert body["unavailable_items"] == [] and body["availability_complete"] is True
    assert all(i["available"] is True for i in body["items"])


def test_unreadable_stock_is_unknown_not_available(live, client, site):
    _fill(site)
    _add_pepper(site)
    site.product_page_status = 500
    body = client.get("/cart", headers=AUTH).json()
    pepper = next(i for i in body["items"] if i["product_id"] == "749")
    assert pepper["available"] is None
    assert body["availability_complete"] is False and body["unavailable_items"] == []


def test_quote_lists_unavailable_items(live, client, site):
    _fill(site)
    _add_pepper(site)
    q = _quote(client)
    assert [u["product_id"] for u in q["unavailable_items"]] == ["749"]
    assert next(i for i in q["items"] if i["product_id"] == "749")["available"] is False
    assert q["availability_complete"] is True


# ==================== Unattended checkout ====================

def _assert_stopped_untouched(site, before):
    assert site.place_clicks == 0 and site.orders_placed == 0
    assert site.cart == before
    assert not [c for c in site.ui_clicks if "missingProducts" in c or "btnConfirm" in c or "proceed" in c]
    assert main.checkout_ledger.unresolved() == []


@pytest.mark.parametrize("late", [False, True])
def test_missing_products_popup_returns_products_unavailable(ua, client, site, late):
    _add_pepper(site)
    site.missing_popup_late = late
    q = _quote(client)
    before = [dict(e) for e in site.cart]
    key = f"ua-missing-{late}"
    res = _submit(client, q, key)
    assert res.status_code == 409, res.text
    err = res.json()["error"]
    assert err["code"] == "products_unavailable"
    assert "פלפל שישקה מתוק (749)" in err["message"]
    items = err["details"]["unavailable_items"]
    assert [(i["product_id"], i["name"], i["qty"]) for i in items] == [("749", "פלפל שישקה מתוק", 0.5)]
    assert items[0]["replaceable"] is True
    assert err["details"]["popup_options"] == ["להחלפה", "לא תודה", "המשך לתשלום"]
    _assert_stopped_untouched(site, before)
    assert main.checkout_ledger.get(key)["state"] == "failed:products_unavailable"


def test_missing_products_wins_over_other_checkout_popups(ua, client, site):
    _add_pepper(site)
    site.checkout_popups = ["modal messageCreditCardModal messagesModal centerModal in"]
    q = _quote(client)
    before = [dict(e) for e in site.cart]
    res = _submit(client, q, "ua-missing-both")
    assert res.json()["error"]["code"] == "products_unavailable", res.text
    _assert_stopped_untouched(site, before)


def test_missing_products_falls_back_to_the_popup_markup(ua, client, site):
    _add_pepper(site)
    site.recommendation_status = 500
    q = _quote(client)
    before = [dict(e) for e in site.cart]
    res = _submit(client, q, "ua-missing-dom")
    err = res.json()["error"]
    assert err["code"] == "products_unavailable", res.text
    assert [(i["product_id"], i["name"]) for i in err["details"]["unavailable_items"]] == [("749", "פלפל שישקה מתוק")]
    _assert_stopped_untouched(site, before)


def test_other_checkout_popup_is_still_checkout_blocked(ua, client, site):
    site.checkout_popups = ["phoneCheckModal"]
    q = _quote(client)
    res = _submit(client, q, "ua-other-pop")
    err = res.json()["error"]
    assert err["code"] == "checkout_blocked" and "details" not in err
    assert site.place_clicks == 0


def test_recommendation_endpoint_is_never_called_outside_checkout(live, client, site):
    _fill(site)
    _add_pepper(site)
    client.get("/cart", headers=AUTH)
    _quote(client)
    assert not [r for r in site.requests if r[1].endswith("/checkout/recommendation/direct")]
    json.dumps(LIVE_RECOMMENDATIONS)  # fixture stays JSON-serialisable
