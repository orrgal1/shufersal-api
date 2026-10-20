"""Unattended checkout (watched=false, SHUFERSAL_LIVE_CHECKOUT_ENABLED and
SHUFERSAL_UNATTENDED_CHECKOUT on) against the fake site and a fake sms-api."""
import logging

import pytest

import config
import main
from checkout_ledger import CheckoutLedger
from sms_codes import SmsCodeReader, check_issuer_code
from tests.test_live_driver import AUTH, _fill, _quote, _real, client, live, make_driver, site  # noqa: F401
from tests.submit import submit


@pytest.fixture
def ua(monkeypatch, live, site):
    monkeypatch.setattr(config, "LIVE_CHECKOUT_ENABLED", True)
    monkeypatch.setattr(config, "UNATTENDED_CHECKOUT", True)
    monkeypatch.setattr(config, "SHUFERSAL_CVV", "042")
    monkeypatch.setattr(config, "MAX_ORDER_ILS", 600.0)
    for name, value in (("SMS_WAIT_SECONDS", 0.3), ("SMS_POLL_SECONDS", 0), ("UNATTENDED_POLL_SECONDS", 0),
                        ("UNATTENDED_STEP_SECONDS", 0.5), ("UNATTENDED_RESULT_SECONDS", 0.5)):
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(config, "THREE_DS_MERCHANT_HINTS", ["shufersal", "שופרסל"])
    live.sms_reader = SmsCodeReader(fetch=site.sms.fetch)
    site.checkout_gated = True  # live: /checkout needs the re-auth first
    _fill(site)
    return live


def _submit(client, q, key, **extra):
    return submit(client, "/checkout/submit", json=_real(q, **extra), headers={**AUTH, "Idempotency-Key": key})


def _no_secrets(caplog, res, site):
    text = caplog.text + res.text
    for secret in (site.login_code, site.issuer_code, "042"):
        assert secret not in text


def test_full_unattended_path_with_3ds(ua, client, site, caplog):
    caplog.set_level(logging.INFO)
    site.after_click = "3ds"
    q = _quote(client)
    res = _submit(client, q, "ua-1")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "placed" and body["order_id"] == "987654321"
    assert site.place_clicks == 1 and site.orders_placed == 1
    assert site.three_ds_codes == [site.issuer_code]
    assert site.benefit_continue_adds == 0 and len(site.cart) == 2  # popup closed, nothing added
    assert body["checks"]["site_total_source"] == "checkout"
    steps = body["checks"]["unattended_steps"]
    for expected in ("clicked 'מעבר לקופה'", "re-auth: SMS code requested", "reached the checkout page",
                     "clicking the place-order button (once)", "3-D Secure challenge shown"):
        assert any(expected in s for s in steps), (expected, steps)
    assert "unattended checkout:" in caplog.text
    _no_secrets(caplog, res, site)
    assert main.checkout_ledger.unresolved() == []


def test_unattended_without_3ds_and_with_cvv(ua, client, site, caplog):
    caplog.set_level(logging.INFO)
    site.checkout_cvv_field = True
    q = _quote(client)
    res = _submit(client, q, "ua-2")
    assert res.status_code == 200 and res.json()["status"] == "placed", res.text
    assert site.ui["cvv_value"] == "042"
    assert site.place_clicks == 1
    _no_secrets(caplog, res, site)


def test_benefit_popup_closed_only_by_its_exact_x_and_logged(ua, client, site, caplog):
    caplog.set_level(logging.INFO)
    q = _quote(client)
    res = _submit(client, q, "ua-x")
    assert res.json()["status"] == "placed"
    sel = '#creditBenefitProductModal button.btnClose[aria-label="סגור"]'
    assert site.closed_with == [sel] and sel in caplog.text


@pytest.mark.parametrize("where", ["cartsummary", "checkout"])
def test_any_other_popup_stops_before_the_click_without_burning_placing(ua, client, site, where):
    if where == "cartsummary":
        site.cartsummary_popups = ["orderUpdatableModal"]
    else:
        site.checkout_popups = ["phoneCheckModal"]
    q = _quote(client)
    res = _submit(client, q, f"ua-pop-{where}")
    assert res.status_code == 409 and res.json()["error"]["code"] == "checkout_blocked", res.text
    assert site.place_clicks == 0 and len(site.cart) == 2
    assert main.checkout_ledger.unresolved() == []
    assert main.checkout_ledger.get(f"ua-pop-{where}")["state"] == "failed:checkout_blocked"


def test_benefit_popup_without_a_safe_x_stops(ua, client, site):
    site.benefit_close_safe = False
    q = _quote(client)
    res = _submit(client, q, "ua-unsafe-x")
    assert res.json()["error"]["code"] == "checkout_blocked"
    assert site.ui["sms_sent"] is False and site.place_clicks == 0


def test_checkout_slot_must_match_the_quote(ua, client, site):
    q = _quote(client)
    site.checkout_selected_slot = "148340771"
    res = _submit(client, q, "ua-slot")
    assert res.status_code == 409 and res.json()["error"]["code"] == "checkout_blocked"
    assert "slot" in res.json()["error"]["message"] and site.place_clicks == 0


def test_checkout_cart_check_does_not_need_the_login_marker(ua, client, site):
    # The fake checkout page has no window.miglog.account; the check must not reload home.
    q = _quote(client)
    loads = site.page_loads
    res = _submit(client, q, "ua-m5")
    assert res.json()["status"] == "placed"
    assert any("cart re-verified" in s for s in res.json()["checks"]["unattended_steps"])
    assert site.page_loads - loads <= 3  # /checkout probe, cart summary; no home reload on checkout


def test_session_change_during_checkout_cart_read_stops(ua, client, site):
    q = _quote(client)
    site.rotate_session_on_checkout_cart_read = True
    res = _submit(client, q, "ua-sess")
    assert res.json()["error"]["code"] == "checkout_blocked" and site.place_clicks == 0


def test_second_queued_order_does_not_click_after_first_became_unknown(ua, client, site):
    """H2: B claimed while A was still running (no block yet); A ends unknown;
    B must not click."""
    ledger = main.checkout_ledger
    q_a = _quote(client)
    assert ledger.claim("A", q_a["quote_id"]) is None
    ledger.finish("A", "outcome_unknown", 202, {})  # A finished ambiguous while B waited
    q_b = _quote(client)
    assert ledger.get("B") is None
    # Simulate B having passed claim() before A's outcome: claim directly bypassing the block.
    ledger._db().execute("INSERT INTO submissions (idempotency_key, quote_id, state, created_at, updated_at)"
                         " VALUES ('B', ?, 'in_progress', 'x', 'x')", (q_b["quote_id"],))
    res = run_driver_checkout(ua, q_b, "B")
    assert res == "order_outcome_unresolved" and site.place_clicks == 0


def run_driver_checkout(drv, q, key):
    import asyncio
    from security import APIException
    try:
        asyncio.run(drv.checkout(slot_id=q["slot"]["id"], expected_cart_hash=q["cart_hash"],
                                 confirm_total_ils=q["total_ils"], place_order=True, unattended=True,
                                 on_place_order=lambda: main._begin_placing(key)))
    except APIException as e:
        return e.code
    return "no-error"


def test_guard_rechecks_checkout_active_after_lock(live):
    import asyncio
    from security import APIException

    async def scenario():
        await live._lock.acquire()          # a checkout holds the lock...
        waiter = asyncio.ensure_future(live.get_cart())
        await asyncio.sleep(0)
        live._checkout_active = True         # ...and is marked active after the waiter queued
        live._lock.release()
        try:
            await waiter
        except APIException as e:
            return e.code
        finally:
            live._checkout_active = False
    assert asyncio.run(scenario()) == "checkout_in_progress"


@pytest.mark.parametrize("mode", ["no_form", "two_submits"])
def test_3ds_submit_only_inside_the_form_with_one_submit(ua, client, site, mode):
    site.after_click = "3ds"
    site.three_ds_form = mode
    q = _quote(client)
    res = _submit(client, q, f"ua-3dsform-{mode}")
    assert res.json()["status"] == "outcome_unknown" and site.three_ds_codes == []


def test_handover_timeout_blocks_further_orders(ua, client, site):
    main.checkout_ledger.claim("w", "qw")
    main.checkout_ledger.finish("w", "handover_timeout", 202, {})
    res = _submit(client, _quote(client), "ua-after-timeout")
    assert res.json()["error"]["code"] == "order_outcome_unresolved"


def test_ledger_default_path_is_absolute(monkeypatch):
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if k != "SHUFERSAL_CHECKOUT_LEDGER"}
    out = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); import config; "
                          "print(config.CHECKOUT_LEDGER)" % os.getcwd()], cwd="/", env=env,
                         capture_output=True, text=True)
    path = out.stdout.strip()
    assert os.path.isabs(path) and path == os.path.join(os.getcwd(), ".runtime", "checkout_ledger.sqlite3")


def test_unattended_needs_both_flags(monkeypatch, ua, client, site):
    monkeypatch.setattr(config, "UNATTENDED_CHECKOUT", False)
    q = _quote(client)
    res = _submit(client, q, "ua-3")
    assert res.status_code == 409 and res.json()["error"]["code"] == "checkout_blocked"
    assert site.place_clicks == 0 and site.ui["sms_sent"] is False
    monkeypatch.setattr(config, "UNATTENDED_CHECKOUT", True)
    monkeypatch.setattr(config, "LIVE_CHECKOUT_ENABLED", False)
    q = _quote(client)
    res = _submit(client, q, "ua-4")
    assert res.status_code == 403 and res.json()["error"]["code"] == "live_checkout_disabled"


def test_existing_confirm_key_and_hash_checks_still_apply(ua, client, site):
    q = _quote(client)
    res = submit(client, "/checkout/submit", json=_real(q, confirm=False), headers={**AUTH, "Idempotency-Key": "k"})
    assert res.json()["error"]["code"] == "confirmation_required"
    res = submit(client, "/checkout/submit", json=_real(q), headers=AUTH)
    assert res.json()["error"]["code"] == "idempotency_key_required"
    res = _submit(client, q, "ua-5", expected_cart_hash="0" * 16)
    assert res.json()["error"]["code"] == "cart_mismatch"
    assert site.place_clicks == 0


def test_cart_summary_total_drift_stops_before_anything(ua, client, site):
    q = _quote(client)
    site.summary_total_text = "999.99 ₪"
    res = _submit(client, q, "ua-6")
    assert res.status_code == 409 and res.json()["error"]["code"] == "total_drift"
    assert site.ui["sms_sent"] is False and site.place_clicks == 0


def test_checkout_page_total_is_reverified(ua, client, site):
    q = _quote(client)
    site.checkout_total_delta = 0.05
    site.summary_total_text = "%.2f ₪" % q["total_ils"]
    res = _submit(client, q, "ua-7")
    assert res.status_code == 409 and res.json()["error"]["code"] == "total_drift"
    assert "checkout page" in res.json()["error"]["message"]
    assert site.place_clicks == 0
    assert main.checkout_ledger.unresolved() == []  # nothing was placed: no block


def test_cart_change_on_the_way_is_refused(ua, client, site):
    q = _quote(client)
    site.cart_change_on_checkout = True
    res = _submit(client, q, "ua-8")
    assert res.json()["error"]["code"] == "cart_mismatch" and site.place_clicks == 0


def test_order_cap(monkeypatch, ua, client, site):
    monkeypatch.setattr(config, "MAX_ORDER_ILS", 50.0)
    q = _quote(client)
    res = _submit(client, q, "ua-9")
    assert res.status_code == 409 and res.json()["error"]["code"] == "order_over_cap"
    assert site.ui["sms_sent"] is False and site.place_clicks == 0


def test_default_cap_is_600_and_unattended_off(tmp_path):
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if k not in ("SHUFERSAL_MAX_ORDER_ILS", "SHUFERSAL_UNATTENDED_CHECKOUT")}
    out = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); import config; "
                          "print(config.MAX_ORDER_ILS, config.UNATTENDED_CHECKOUT)" % os.getcwd()],
                         cwd=tmp_path, env=env, capture_output=True, text=True)
    assert out.stdout.split() == ["600.0", "False"], out.stderr


@pytest.mark.parametrize("case", ["old", "wrong_sender", "none", "sms_down"])
def test_login_code_must_be_fresh_and_from_shufersal(ua, client, site, case):
    if case == "old":
        site.send_login_sms = False
        site.sms.add("SHUFERSAL", "קוד האימות שלך: 111111", age_seconds=30)
    elif case == "wrong_sender":
        site.login_sms_sender = "Dominos"
    elif case == "none":
        site.send_login_sms = False
    elif case == "sms_down":
        site.sms.fail = True
    q = _quote(client)
    res = _submit(client, q, f"ua-login-{case}")
    assert res.status_code == 409, res.text
    expected = "sms_unavailable" if case == "sms_down" else "sms_code_timeout"
    assert res.json()["error"]["code"] == expected
    assert site.ui["sms_value"] == "" and site.place_clicks == 0
    assert main.checkout_ledger.unresolved() == []


def test_code_received_before_trigger_is_ignored_even_if_recent(ua, client, site):
    # Arrived (higher rowid) but stamped before the trigger: not accepted.
    site.send_login_sms = False
    site.sms.add("SHUFERSAL", "קוד: 222222", age_seconds=20)
    q = _quote(client)
    res = _submit(client, q, "ua-10")
    assert res.json()["error"]["code"] == "sms_code_timeout"


def test_wrong_login_code_is_a_clean_failure(ua, client, site):
    # Every SMS carries the same wrong code: the resent one is not typed again.
    site.login_sms_text = "קוד: 000000"
    q = _quote(client)
    res = _submit(client, q, "ua-11")
    assert res.status_code == 409 and res.json()["error"]["code"] == "reauth_failed"
    assert "same code" in res.json()["error"]["message"]
    assert site.login_codes_submitted == ["000000"]
    assert site.place_clicks == 0


RESEND = "#forgottenPwdModal .resend-otp-link.passwordModal"


def test_expired_first_code_gets_one_new_code_then_succeeds(ua, client, site, caplog):
    # Or, 2026-10-20: the first code was "expired"; a new one worked. No 3-D
    # Secure for the saved card: the click goes straight to the confirmation page.
    caplog.set_level(logging.INFO)
    site.login_sms_codes = ["135790"]  # the site rejects it; the resend delivers login_code
    q = _quote(client)
    res = _submit(client, q, "ua-resend-ok")
    assert res.status_code == 200 and res.json()["status"] == "placed", res.text
    assert site.login_codes_submitted == ["135790", site.login_code]
    assert site.ui_clicks.count(RESEND) == 1 and site.ui_clicks.count("#sendSmsBtn") == 1
    assert site.place_clicks == 1 and site.three_ds_codes == []
    steps = res.json()["checks"]["unattended_steps"]
    for expected in ("the site rejected the SMS code", "fresh SMS baseline taken before the resend",
                     "clicked the modal's resend control", "new SMS code requested",
                     "new SMS code entered and confirmed", "reached the checkout page"):
        assert any(expected in s for s in steps), (expected, steps)
    _no_secrets(caplog, res, site)
    assert "135790" not in caplog.text + res.text


def test_two_rejected_codes_fail_after_a_single_resend(ua, client, site):
    site.login_sms_codes = ["111111", "222222"]
    q = _quote(client)
    res = _submit(client, q, "ua-resend-fail")
    assert res.status_code == 409 and res.json()["error"]["code"] == "reauth_failed", res.text
    assert "rejected too" in res.json()["error"]["message"]
    assert site.login_codes_submitted == ["111111", "222222"]
    assert site.login_codes_sent == ["111111", "222222"]
    assert site.ui_clicks.count(RESEND) == 1
    assert site.place_clicks == 0
    assert main.checkout_ledger.unresolved() == []


def test_old_code_is_never_reentered_when_no_new_sms_arrives(ua, client, site):
    # The rejected code's SMS is still fresh in the inbox, but it is at or below
    # the baseline taken before the resend.
    site.login_sms_codes = ["135790"]
    site.resend_sends_sms = False
    q = _quote(client)
    res = _submit(client, q, "ua-resend-none")
    assert res.status_code == 409 and res.json()["error"]["code"] == "sms_code_timeout", res.text
    assert site.login_codes_submitted == ["135790"]
    assert site.ui_clicks.count(RESEND) == 1 and site.place_clicks == 0


def test_resend_falls_back_to_rechoosing_sms(ua, client, site):
    site.login_sms_codes = ["135790"]
    site.resend_link = False
    q = _quote(client)
    res = _submit(client, q, "ua-resend-tab")
    assert res.status_code == 200 and res.json()["status"] == "placed", res.text
    assert site.login_codes_submitted == ["135790", site.login_code]
    assert '#forgottenPwdModal a.tab[href="#passwordTab"]' in site.ui_clicks
    assert site.ui_clicks.count("#sendSmsBtn") == 2


@pytest.mark.parametrize("field,value", [("issuer_sender", "Cal"), ("issuer_amount", 1.0),
                                         ("issuer_last4", "9999"), ("issuer_merchant", "BugMultisystem"),
                                         ("send_issuer_sms", False)])
def test_3ds_code_guardrails_make_the_outcome_unknown_and_block(ua, client, site, field, value):
    site.after_click = "3ds"
    setattr(site, field, value)
    q = _quote(client)
    res = _submit(client, q, f"ua-3ds-{field}")
    assert res.status_code == 202 and res.json()["status"] == "outcome_unknown", res.text
    assert site.three_ds_codes == [] and site.place_clicks == 1
    assert [u["idempotency_key"] for u in main.checkout_ledger.unresolved()] == [f"ua-3ds-{field}"]
    # Any further order is blocked until Or resolves it.
    site.after_click = "confirm"
    q2 = _quote(client)
    blocked = _submit(client, q2, "ua-next")
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "order_outcome_unresolved"
    assert site.place_clicks == 1
    assert main.checkout_ledger.resolve(f"ua-3ds-{field}", "not_placed")
    ok = _submit(client, _quote(client), "ua-after-resolve")
    assert ok.status_code == 200 and ok.json()["status"] == "placed"


@pytest.mark.parametrize("after,status", [("error", "outcome_unknown"), ("nothing", "outcome_unknown"),
                                          ("confirm_no_number", "placed_unconfirmed")])
def test_ambiguous_outcomes_after_the_click(ua, client, site, after, status):
    site.after_click = after
    q = _quote(client)
    res = _submit(client, q, f"ua-amb-{after}")
    assert res.status_code == 202 and res.json()["status"] == status and res.json()["order_id"] is None
    assert site.place_clicks == 1
    assert main.checkout_ledger.unresolved()[0]["state"] == status
    # Replaying the same key returns the stored result and never clicks again.
    again = _submit(client, q, f"ua-amb-{after}")
    assert again.json() == res.json() and site.place_clicks == 1


def test_crash_after_click_leaves_a_blocking_placing_record(ua, client, site, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("process died")
    monkeypatch.setattr(ua, "_ua_await_outcome", boom)
    q = _quote(client)
    res = _submit(client, q, "ua-12")
    assert res.json()["status"] == "outcome_unknown" and site.place_clicks == 1
    assert main.checkout_ledger.unresolved()[0]["idempotency_key"] == "ua-12"


def test_placing_state_blocks_even_without_finish(tmp_path):
    ledger = CheckoutLedger(str(tmp_path / "l.sqlite3"))
    assert ledger.claim("a", "q1") is None
    ledger.mark("a", "placing")  # the process died right after the click
    again = CheckoutLedger(str(tmp_path / "l.sqlite3"))
    assert again.claim("b", "q2")["state"] == "blocked_unresolved"
    assert again.resolve("a", "placed")
    assert again.claim("b", "q2") is None


def test_ambiguous_3ds_frame_is_not_guessed(ua, client, site):
    site.after_click = "3ds"
    site.three_ds_inputs = 2
    q = _quote(client)
    res = _submit(client, q, "ua-13")
    assert res.json()["status"] == "outcome_unknown" and site.three_ds_codes == []


def test_watched_mode_still_hands_over(ua, client, site, monkeypatch):
    monkeypatch.setattr(config, "HEADLESS", True)
    monkeypatch.setattr(config, "WATCH_POLL_SECONDS", 0)
    monkeypatch.setattr(config, "WATCH_TIMEOUT_SECONDS", 5)
    site.checkout_gated = True
    site.confirm_on_url_reads = 2
    q = _quote(client)
    res = _submit(client, q, "ua-w", watched=True)
    assert res.status_code == 200 and res.json()["status"] == "placed"
    assert site.place_clicks == 0 and site.ui["sms_sent"] is False  # Or did it


def test_issuer_text_parsing_matches_real_format():
    text = ("חשוב מאוד, לתשומת ליבך-\nזיהינו כעת עסקה בסכום 447.06 ILS ב שופרסל אונליין בכרטיסך 8277,\n"
            "הקוד שלך להשלמת העסקה הינו 123456\nתמיד לשירותך, ישראכרט\n\n"
            "Код для завершения оплаты 123456.")
    assert check_issuer_code(text, 447.06, "8277") == {"ok": True, "code": "123456"}
    # All three of amount, card digits and merchant must be present.
    assert not check_issuer_code(text.replace("בסכום 447.06 ILS", "בסכום ILS"), 447.06, "8277")["ok"]
    assert not check_issuer_code(text.replace("בכרטיסך 8277", "בכרטיס"), 447.06, "8277")["ok"]
    no_merchant = "הקוד שלך להשלמת העסקה הינו 123456"
    assert not check_issuer_code(no_merchant, 447.06, "8277")["ok"]
    assert not check_issuer_code(text, 447.00, "8277")["ok"]
    assert not check_issuer_code(text, 447.06, "1111")["ok"]
