"""What the unattended checkout does after its single place-order click:
logging, diagnostics, and the post-click states read from the site's checkout
bundle (3-D Secure by SMS or by app approval, a bank window, a rejection, a
client-side stop). It never clicks place-order twice."""
import json
import logging

import pytest

import config
import main
from checkout_diag import safe_url, summarize_place_order
from tests.test_live_driver import _quote, client, live, site  # noqa: F401
from tests.test_unattended import _no_secrets, _submit, ua  # noqa: F401


def _diag_files(diag_dir):
    return sorted(p.name for p in diag_dir.iterdir()) if diag_dir.exists() else []


def _steps(res):
    return res.json()["checks"]["unattended_steps"]


def test_timeout_saves_masked_diagnostics_and_says_no_request_was_sent(ua, client, site, diag_dir, caplog):
    caplog.set_level(logging.INFO)
    site.after_click = "nothing"
    res = _submit(client, _quote(client), "pc-nothing")
    body = res.json()
    assert body["status"] == "outcome_unknown" and site.place_clicks == 1
    files = _diag_files(diag_dir)
    assert any(f.endswith("-outcome_unknown.png") for f in files)
    assert any(f.endswith("-outcome_unknown.html") for f in files)
    assert any(f.endswith("-outcome_unknown.json") for f in files)
    assert "diagnostics saved to" in caplog.text and str(diag_dir) in caplog.text
    assert any("no place-order request was seen" in w for w in body["warnings"])
    assert all((diag_dir / f).stat().st_mode & 0o077 == 0 for f in files)
    _no_secrets(caplog, res, site)


def test_placed_order_saves_no_diagnostics(ua, client, site, diag_dir):
    res = _submit(client, _quote(client), "pc-ok")
    assert res.json()["status"] == "placed"
    assert _diag_files(diag_dir) == []
    assert any("placeOrder request sent: after3ds=false" in s for s in _steps(res))


def test_place_order_jwt_and_bank_tokens_never_logged(ua, client, site, diag_dir, caplog):
    caplog.set_level(logging.INFO)
    site.after_click = "3ds"
    res = _submit(client, _quote(client), "pc-3ds")
    assert res.json()["status"] == "placed"
    assert "SECRETJWT" not in caplog.text and "token=SECRET" not in caplog.text
    assert any('"kind": "three_ds"' in s for s in _steps(res))
    _no_secrets(caplog, res, site)


def test_rejected_place_order_stops_early_with_the_site_codes(ua, client, site, monkeypatch):
    monkeypatch.setattr(config, "UNATTENDED_RESULT_SECONDS", 30)
    site.after_click = "rejected"
    res = _submit(client, _quote(client), "pc-rej")
    body = res.json()
    assert body["status"] == "outcome_unknown" and site.place_clicks == 1
    assert any("TIMESLOT_UNAVAILABLE" in w for w in body["warnings"])
    assert any("המועד שבחרת אינו זמין יותר" in s for s in _steps(res))
    assert main.checkout_ledger.unresolved()[0]["state"] == "outcome_unknown"  # still blocks


def test_client_side_stop_is_reported_without_waiting(ua, client, site, monkeypatch):
    # Grace before calling it a client-side stop: min(10 s, half the wait).
    monkeypatch.setattr(config, "UNATTENDED_RESULT_SECONDS", 2)
    site.after_click = "toast"
    res = _submit(client, _quote(client), "pc-toast")
    body = res.json()
    assert body["status"] == "outcome_unknown" and site.place_clicks == 1
    assert any("stopped the order before sending it" in w and "יש לבחור מועד משלוח" in w for w in body["warnings"])


def test_accepted_without_navigation_is_placed_unconfirmed(ua, client, site):
    site.after_click = "accepted_no_nav"
    res = _submit(client, _quote(client), "pc-acc")
    body = res.json()
    assert body["status"] == "placed_unconfirmed" and body["order_id"] is None
    assert any("accepted the place-order request" in w for w in body["warnings"])


def test_spinner_and_in_flight_request_are_logged(ua, client, site):
    site.after_click = "spinner_forever"
    res = _submit(client, _quote(client), "pc-spin")
    assert res.json()["status"] == "outcome_unknown"
    steps = _steps(res)
    assert any("spinner is shown" in s for s in steps)
    assert any("placeOrder request sent" in s for s in steps)


def test_app_approval_is_waited_for_and_completes(ua, client, site, monkeypatch):
    monkeypatch.setattr(config, "UNATTENDED_RESULT_SECONDS", 5)
    site.after_click = "3ds_app"
    site.app_approve_reads = 8
    res = _submit(client, _quote(client), "pc-app")
    body = res.json()
    assert body["status"] == "placed" and body["order_id"] == "987654321"
    assert site.three_ds_codes == [] and site.place_clicks == 1
    assert any("approve the payment in its app" in s for s in _steps(res))


def test_app_approval_never_given_abandons_the_checkout_page(ua, client, site, diag_dir):
    site.after_click = "3ds_app"
    res = _submit(client, _quote(client), "pc-app-never")
    body = res.json()
    assert body["status"] == "outcome_unknown" and site.place_clicks == 1
    assert ua.page.gotos[-1].endswith("/cart/cartsummary")  # left the page; no click
    assert any("late approval cannot place the order" in w for w in body["warnings"])
    assert any("timeout-bank-pending" in f for f in _diag_files(diag_dir))
    assert site.orders_placed == 0


def test_failed_3ds_leaves_the_page(ua, client, site, diag_dir):
    site.after_click = "3ds"
    site.send_issuer_sms = False
    res = _submit(client, _quote(client), "pc-3ds-nosms")
    assert res.json()["status"] == "outcome_unknown"
    assert ua.page.gotos[-1].endswith("/cart/cartsummary")
    assert any("three-ds-failed" in f for f in _diag_files(diag_dir))


def test_bank_page_in_a_popup_window_is_found(ua, client, site, diag_dir):
    site.after_click = "popup_3ds"
    res = _submit(client, _quote(client), "pc-popup")
    body = res.json()
    assert body["status"] == "placed", body
    assert site.three_ds_codes == [site.issuer_code] and site.place_clicks == 1
    steps = _steps(res)
    assert any("popup window opened: https://bank.example/acs" in s for s in steps)
    assert all("SECRET" not in s for s in steps)


def test_url_changes_after_the_click_are_logged(ua, client, site):
    res = _submit(client, _quote(client), "pc-url")
    assert any("page URL: https://www.shufersal.co.il/online/he/checkout/orderConfirmation/987654321" in s
               for s in _steps(res))


def test_never_clicks_place_order_twice_in_any_mode(ua, client, site):
    for i, mode in enumerate(["nothing", "toast", "rejected", "3ds_app", "spinner_forever", "accepted_no_nav"]):
        site.after_click = mode
        before = site.place_clicks
        _submit(client, _quote(client), f"pc-once-{i}")
        assert site.place_clicks - before == 1, mode
        assert site.ui_clicks.count("a.btnConfirm") == i + 1
        main.checkout_ledger.resolve(f"pc-once-{i}", "not_placed")


@pytest.mark.parametrize("text,kind,extra", [
    ('{"success": true, "redirect": "/checkout/orderConfirmation/1?x=y"}', "success",
     {"redirect": "/checkout/orderConfirmation/1"}),
    ('{"allocationTimeout": true}', "allocation_timeout", {}),
    ('{"directCreditCard3DSLink": "https://acs/x?t=1"}', "three_ds", {}),
    ('[{"type": "X", "errorCode": "invalidCvv"}]', "errors", {"errors": ["invalidCvv"]}),
    ('{"foo": 1}', "server_payment_error", {"keys": ["foo"]}),
    ("<html>", "unparsed", {}),
])
def test_summarize_place_order(text, kind, extra):
    out = summarize_place_order(200, text)
    assert out["kind"] == kind
    for k, v in extra.items():
        assert out[k] == v
    assert "x=y" not in json.dumps(out) and "t=1" not in json.dumps(out)


def test_safe_url_drops_queries():
    assert safe_url("https://a.b/c/d?jwtPayme=abc#f") == "https://a.b/c/d"
    assert safe_url("/x?y=1") == "/x"
