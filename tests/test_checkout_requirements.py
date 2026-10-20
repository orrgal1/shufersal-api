"""The checkout's age-confirmation checkbox (cart with alcohol, tobacco or
medicine) and the generic required-field guard. Live evidence: the 2026-10-20
08:45 / 09:16 runs ended outcome_unknown because the site's own validation
(red ".over-18.errorForm" box, unchecked "יהיה בבית אדם בגיל 18 ומעלה לקבל את
המשלוח") blocked the place-order click and nothing was sent."""
import asyncio

import pytest

import main
from shufersal_driver import ShufersalDriver
from tests.test_live_driver import _quote, client, live, site  # noqa: F401
from tests.test_unattended import _submit, ua  # noqa: F401

LABEL = "יהיה בבית אדם בגיל 18 ומעלה לקבל את המשלוח"

# The live markup of section 4, from .runtime/diag/20261020-092231-outcome_unknown.html.
LIVE_OVER18 = (
    '<fieldset data-v-56b4494d="" class="over-18 errorForm"><div data-v-56b4494d="" class="subTitleContainer">'
    '<legend data-v-56b4494d="" class="topTitle"><img data-v-56b4494d="" '
    'src="https://www.shufersal.co.il/online/_ui/responsive/theme-miglog/img/checkout/icon-over18-error.png" loading="lazy">\n'
    '                                אופן קבלת המשלוח\n                              </legend> '
    '<span data-v-56b4494d="">קבלת משלוח המכיל אלכוהול, סיגריות, טבק או תרופות, תתאפשר רק ע"י אדם בגיל 18 ומעלה</span></div> '
    '<div data-v-56b4494d="" class="radioContainer v2 row space"><label data-v-56b4494d="" for="true" '
    'aria-labelledby="אופן קבלת המשלוח" tabindex="-1" class="btn col-xs-6"><div data-v-56b4494d="" class="checkboxContainer">'
    '<label data-v-56b4494d="" class="btn activeCheckbox"><span data-v-56b4494d="" class="checkboxPic"></span></label></div> '
    '<strong data-v-56b4494d="" id="over18text" class="radioText">יהיה בבית אדם בגיל 18 ומעלה לקבל את המשלוח</strong>'
    '</label></div></fieldset>'
)
# The hidden "adult-modal" of the same page (its text must not be matched).
LIVE_ADULT_MODAL = (
    '<div class="modal messagesModal centerModal notHome-modal adult-modal" style="display:none"><div class="modal-dialog">'
    '<h3 class="title">קבלת המשלוח תתאפשר רק ע"י אדם בגיל 18 ומעלה</h3><p class="text">יש לאשר שיהיה בבית אדם בגיל 18 '
    'ומעלה לקבל את המשלוח או להסיר את הפריטים מהסל</p></div></div>'
)
# Another checkbox of the same style on the page: must never be clicked.
OTHER_BOX = ('<div class="radioContainer"><label class="btn other"><div class="checkboxContainer">'
             '<label class="btn activeCheckbox other-box"><span class="checkboxPic"></span></label></div>'
             '<strong class="radioText">שלחו לי עדכונים</strong></label></div>')
# Stand-in for the Vue handler: a click on the box toggles "active" on its label
# and clears the red box; every click target is recorded.
VUE = ('<script>window.__clicks = []; document.addEventListener("click", (e) => {'
       ' window.__clicks.push(e.target.className || e.target.tagName);'
       ' const l = e.target.closest(".over-18 .radioContainer > label"); if (!l) return;'
       ' l.classList.toggle("active"); l.closest(".over-18").classList.toggle("errorForm", !l.classList.contains("active"));'
       '}, true);</script>')


def _page(*parts):
    return '<html><body><div class="orderContainer">' + "".join(parts) + "</div>" + VUE + "</body></html>"


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


def _run(browser_page, html, tick):
    loop, page = browser_page

    async def go():
        await page.set_content(html)
        first = await page.evaluate(ShufersalDriver.UA_ADULT_JS, tick)
        after = await page.evaluate(ShufersalDriver.UA_ADULT_JS, False)
        reqs = await page.evaluate(ShufersalDriver.UA_REQUIREMENTS_JS)
        clicks = await page.evaluate("() => window.__clicks")
        return first, after, reqs, clicks

    return loop.run_until_complete(go())


def test_live_markup_is_found_by_its_label_unchecked_and_flagged(browser_page):
    first, after, reqs, clicks = _run(browser_page, _page(OTHER_BOX, LIVE_OVER18, LIVE_ADULT_MODAL), tick=False)
    assert first["present"] and first["count"] == 1 and first["checked"] is False and first["error"] is True
    assert "אופן קבלת המשלוח" in first["text"] and "18 ומעלה" in first["text"]
    assert clicks == []  # the check never clicks
    assert len(reqs) == 1 and reqs[0].startswith("אופן קבלת המשלוח")


def test_tick_clicks_only_the_age_box_once(browser_page):
    first, after, reqs, clicks = _run(browser_page, _page(OTHER_BOX, LIVE_OVER18, LIVE_ADULT_MODAL), tick=True)
    assert first["clicked"] is True
    assert clicks == ["btn activeCheckbox"]  # the age box's own square, once; not the other checkbox
    assert after["checked"] is True and after["error"] is False
    assert reqs == []


def test_already_ticked_box_is_not_clicked(browser_page):
    html = _page(LIVE_OVER18.replace('class="over-18 errorForm"', 'class="over-18"')
                 .replace('tabindex="-1" class="btn col-xs-6"', 'tabindex="-1" class="btn col-xs-6 active"'))
    first, after, reqs, clicks = _run(browser_page, html, tick=True)
    assert first["checked"] is True and "clicked" not in first and clicks == [] and reqs == []


@pytest.mark.parametrize("html", [
    _page(OTHER_BOX, LIVE_ADULT_MODAL),                                                  # no age box
    _page(LIVE_OVER18.replace('class="over-18 errorForm"', 'class="over-18 errorForm" style="display:none"')),
    _page(OTHER_BOX.replace("שלחו לי עדכונים", "יהיה בבית אדם בגיל 18 ומעלה")),         # other label text
])
def test_no_visible_age_box(browser_page, html):
    first, after, reqs, clicks = _run(browser_page, html, tick=True)
    assert first == {"present": False} and clicks == [] and reqs == []


def test_two_age_boxes_are_ambiguous_and_not_clicked(browser_page):
    first, after, reqs, clicks = _run(browser_page, _page(LIVE_OVER18, LIVE_OVER18), tick=True)
    assert first["ambiguous"] is True and clicks == []


def test_requirement_selectors_ignore_hidden_and_empty_and_spinner(browser_page):
    html = _page(
        '<div class="spinner-container" role="alert"><span class="sr-only">התוכן נטען כעת</span></div>',
        '<div class="error-info"></div><div class="errors"></div>',
        '<div class="whiteErrorBg" style="display:none"><span class="error">מוסתר</span></div>',
        '<div class="whiteErrorBg"><span class="error">יש לבחור אמצעי תשלום</span></div>',
        '<span role="alert" class="colorError">שדה חובה</span>',
    )
    _, _, reqs, _ = _run(browser_page, html, tick=False)
    assert reqs == ["יש לבחור אמצעי תשלום", "שדה חובה"]


# ---------- the unattended flow (fake site) ----------

def _no_order(res, site, key, code):
    assert res.status_code == 409 and res.json()["error"]["code"] == code, res.text
    assert site.orders_placed == 0
    assert main.checkout_ledger.unresolved() == []
    assert main.checkout_ledger.get(key)["state"] == f"failed:{code}"


def test_age_box_without_confirmation_stops_before_the_click(ua, client, site):
    site.adult_box = "unchecked"
    res = _submit(client, _quote(client), "adult-no")
    _no_order(res, site, "adult-no", "adult_recipient_required")
    assert site.place_clicks == 0 and site.adult_ticks == 0
    msg = res.json()["error"]["message"]
    assert LABEL in msg and "adult_recipient_confirmed=true" in msg and "Or" in msg


def test_age_box_with_confirmation_is_ticked_once_then_placed(ua, client, site):
    site.adult_box = "unchecked"
    res = _submit(client, _quote(client), "adult-yes", adult_recipient_confirmed=True)
    assert res.status_code == 200 and res.json()["status"] == "placed", res.text
    assert site.adult_ticks == 1 and site.place_clicks == 1
    assert site.events.index("adult_tick") < site.events.index("place_click")
    steps = res.json()["checks"]["unattended_steps"]
    i_tick = next(i for i, s in enumerate(steps) if s.startswith("ticked the age-confirmation checkbox"))
    i_total = max(i for i, s in enumerate(steps) if s.startswith("checkout total verified"))
    assert i_tick < i_total  # ticked before the pre-click verifications
    assert LABEL in steps[i_tick]


def test_already_ticked_box_is_left_alone(ua, client, site):
    site.adult_box = "checked"
    res = _submit(client, _quote(client), "adult-pre")
    assert res.json()["status"] == "placed" and site.adult_ticks == 0


def test_tick_that_does_not_take_is_not_retried(ua, client, site):
    site.adult_box = "unchecked"
    site.adult_tick_works = False
    res = _submit(client, _quote(client), "adult-stuck", adult_recipient_confirmed=True)
    _no_order(res, site, "adult-stuck", "checkout_requirement")
    assert site.adult_ticks == 1 and site.place_clicks == 0


def test_two_age_boxes_stop_without_ticking(ua, client, site):
    site.adult_box = "unchecked"
    site.adult_ambiguous = True
    res = _submit(client, _quote(client), "adult-two", adult_recipient_confirmed=True)
    _no_order(res, site, "adult-two", "checkout_requirement")
    assert site.adult_ticks == 0 and site.place_clicks == 0


def test_visible_required_field_error_stops_before_the_click(ua, client, site):
    site.requirements_before_click = ["יש לבחור אמצעי תשלום"]
    res = _submit(client, _quote(client), "req-pre")
    _no_order(res, site, "req-pre", "checkout_requirement")
    assert site.place_clicks == 0
    assert "יש לבחור אמצעי תשלום" in res.json()["error"]["message"]
    assert res.json()["error"]["details"]["requirements"] == ["יש לבחור אמצעי תשלום"]


def test_validation_after_the_click_fails_fast_when_nothing_was_sent(ua, client, site, diag_dir):
    site.after_click = "validation"
    q = _quote(client)
    res = _submit(client, q, "req-post")
    _no_order(res, site, "req-post", "checkout_requirement")
    assert site.place_clicks == 1
    err = res.json()["error"]
    assert err["details"]["place_order_sent"] is False
    assert "אופן קבלת המשלוח" in err["message"] and "no order request was sent" in err["message"]
    assert any(p.name.endswith("-not_sent.png") for p in diag_dir.iterdir())
    # A retry with the same key replays the failure; it never clicks again.
    again = _submit(client, q, "req-post")
    assert site.place_clicks == 1 and again.json()["error"]["code"] == "checkout_requirement"


def test_schema_field_defaults_to_false():
    from schemas import CheckoutSubmitRequest
    req = CheckoutSubmitRequest(quote_id="q", confirm_total_ils=1.0, expected_cart_hash="h")
    assert req.adult_recipient_confirmed is False
    assert "adult 18+" in CheckoutSubmitRequest.model_fields["adult_recipient_confirmed"].description
