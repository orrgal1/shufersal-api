"""The checkout's informational card notice (messageCreditCardModal): closed
by its only "הבנתי" button, and only when it is exactly the known notice; the
checkout total is re-verified after it closes and before the place-order click."""
import asyncio

import pytest

import main
from shufersal_driver import ShufersalDriver
from tests.fake_site import CREDIT_NOTICE
from tests.test_live_driver import _quote, client, live, site  # noqa: F401
from tests.test_unattended import _submit, ua  # noqa: F401


def _last(events, name):
    return max(i for i, e in enumerate(events) if e == name)


@pytest.mark.parametrize("late", [False, True])
def test_plain_notice_is_closed_and_the_total_is_rechecked_after_it(ua, client, site, late):
    site.credit_notice = "ok"
    site.credit_notice_late = late
    q = _quote(client)
    res = _submit(client, q, f"ua-notice-{late}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "placed" and site.place_clicks == 1
    assert site.credit_notice_clicks == 1
    ev = site.events
    dismissed, place = ev.index("credit_notice_dismissed"), ev.index("place_click")
    # A total read (compared with confirm_total_ils) happens after the dismissal and before the click.
    assert any(e == "cost_summary" for e in ev[dismissed + 1:place]), ev
    if late:  # it opened during the first verification: that one ran before it, then all again
        assert ev[:ev.index("credit_notice_dismissed")].count("cost_summary") == 1
    steps = body["checks"]["unattended_steps"]
    i_closed = next(i for i, s in enumerate(steps) if "credit-card notice" in s)
    i_total = max(i for i, s in enumerate(steps) if s.startswith("checkout total verified"))
    i_click = next(i for i, s in enumerate(steps) if s.startswith("clicking the place-order"))
    assert i_closed < i_total < i_click, steps


def test_total_change_behind_the_notice_stops_before_the_click(ua, client, site):
    site.credit_notice = "ok"
    site.credit_notice_late = True
    site.credit_notice_total_delta = 5.0  # e.g. the card benefits dropped once it was acknowledged
    q = _quote(client)
    res = _submit(client, q, "ua-notice-drift")
    assert res.status_code == 409 and res.json()["error"]["code"] == "total_drift", res.text
    assert site.credit_notice_clicks == 1 and site.place_clicks == 0 and site.orders_placed == 0
    assert main.checkout_ledger.unresolved() == []


@pytest.mark.parametrize("variant", ["extra_button", "two_buttons", "other_text"])
@pytest.mark.parametrize("late", [False, True])
def test_notice_variants_are_not_clicked(ua, client, site, variant, late):
    site.credit_notice = variant
    site.credit_notice_late = late
    q = _quote(client)
    res = _submit(client, q, f"ua-notice-{variant}-{late}")
    assert res.status_code == 409 and res.json()["error"]["code"] == "checkout_blocked", res.text
    assert site.credit_notice_clicks == 0 and site.place_clicks == 0
    assert main.checkout_ledger.get(f"ua-notice-{variant}-{late}")["state"] == "failed:checkout_blocked"


def test_notice_together_with_another_popup_is_blocked(ua, client, site):
    site.credit_notice = "ok"
    site.checkout_popups = ["phoneCheckModal"]
    q = _quote(client)
    res = _submit(client, q, "ua-notice-plus")
    assert res.json()["error"]["code"] == "checkout_blocked", res.text
    assert site.credit_notice_clicks == 0 and site.place_clicks == 0


def test_is_credit_notice_needs_exactly_that_popup():
    f = ShufersalDriver._is_credit_notice
    assert f([CREDIT_NOTICE])
    assert not f([CREDIT_NOTICE, "phoneCheckModal"])
    assert not f(["messageCreditCardModalX"]) and not f([])


# ==================== The real JS in a real (offline) browser ====================

LIVE_TEXT = ('<div class="modal-body"><h3>לתשומת ליבך,</h3><p>כרטיס אשראי שופרסל שלך לא מסומן</p>'
             '<p>המבצעים למחזיקי כרטיס אשראי שופרסל - לא יכובדו</p>'
             '<p>בכדי לממש את המבצעים יש להחליף לתשלום באשראי שופרסל</p></div>')
OK_BUTTON = '<button type="button" class="btn" onclick="window.__clicked=(window.__clicked||0)+1"><span>הבנתי</span></button>'


def _modal(inner, extra_modal=""):
    return ('<html><body><div class="modal messageCreditCardModal messagesModal centerModal in" '
            f'style="display:block">{inner}</div>{extra_modal}</body></html>')


CASES = {
    "ok": (_modal(LIVE_TEXT + OK_BUTTON), True),
    "extra_button": (_modal(LIVE_TEXT + OK_BUTTON + '<button type="button">להחלפה לאשראי שופרסל</button>'), False),
    "close_x": (_modal('<button class="btnClose" aria-label="סגור"></button>' + LIVE_TEXT + OK_BUTTON), False),
    "two_ok_buttons": (_modal(LIVE_TEXT + OK_BUTTON + OK_BUTTON), False),
    "link": (_modal(LIVE_TEXT + LIVE_TEXT.replace("<h3>", '<a href="/change-card">שינוי</a><h3>') + OK_BUTTON), False),
    "other_text": (_modal(LIVE_TEXT + "<p>יש לאשר את החלפת אמצעי התשלום</p>" + OK_BUTTON), False),
    "missing_text": (_modal("<p>לתשומת ליבך,</p>" + OK_BUTTON), False),
    "other_label": (_modal(LIVE_TEXT + OK_BUTTON.replace("הבנתי", "אישור")), False),
    "in_form": (_modal(LIVE_TEXT + "<form>" + OK_BUTTON + "</form>"), False),
    "submit": (_modal(LIVE_TEXT + OK_BUTTON.replace('type="button"', 'type="submit"')), False),
    "two_modals": (_modal(LIVE_TEXT + OK_BUTTON, _modal(LIVE_TEXT + OK_BUTTON)[12:-14]), False),
}


@pytest.fixture(scope="module")
def browser_page():
    pw_api = pytest.importorskip("playwright.async_api")
    loop = asyncio.new_event_loop()

    async def open_():
        pw = await pw_api.async_playwright().start()
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # no browser installed here
            await pw.stop()
            pytest.skip(f"chromium unavailable: {e}")
        return pw, browser, await browser.new_page()

    pw, browser, page = loop.run_until_complete(open_())
    yield loop, page
    loop.run_until_complete(browser.close())
    loop.run_until_complete(pw.stop())
    loop.close()


@pytest.mark.parametrize("case", sorted(CASES))
def test_credit_notice_js_in_a_browser(browser_page, case):
    loop, page = browser_page
    html, expect_ok = CASES[case]

    async def run():
        await page.set_content(html)
        await page.evaluate("() => { window.__clicked = 0; }")
        check = await page.evaluate(ShufersalDriver.UA_CREDIT_NOTICE_JS, False)
        clicked_on_check = await page.evaluate("() => window.__clicked || 0")
        result = await page.evaluate(ShufersalDriver.UA_CREDIT_NOTICE_JS, True)
        return check, clicked_on_check, result, await page.evaluate("() => window.__clicked || 0")

    check, clicked_on_check, result, clicked = loop.run_until_complete(run())
    assert clicked_on_check == 0  # the check never clicks
    assert check["ok"] is expect_ok and result["ok"] is expect_ok, (check, result)
    assert clicked == (1 if expect_ok else 0)
