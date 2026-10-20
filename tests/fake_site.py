"""In-memory stand-in for shufersal.co.il, served through a fake Playwright page.

It answers the in-page fetch() the driver runs (page.evaluate(script, [url,
method, body, contentType])) with the response shapes captured from the live
site, and keeps a cart so write paths can be verified end to end.
"""
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import parse_qs, urlparse

BASE = "https://www.shufersal.co.il"

CATALOG = {
    "522319": {"name": "חלב טרי 3%", "price": 7.35, "selling": "BY_UNIT"},
    "7296073746423": {"name": "טבליות למדיח", "price": 39.9, "selling": "BY_UNIT"},
    "4131074": {"name": "חלב 1%", "price": 6.2, "selling": "BY_UNIT"},
    "8000001": {"name": "עגבניות", "price": 9.9, "selling": "BY_WEIGHT"},
    "749": {"name": "פלפל שישקה מתוק", "price": 14.9, "selling": "BY_WEIGHT"},
    # Sold per package; the site counts the package's grams (live 2026-10-19: 1.3 kg carrots).
    "7296073754848": {"name": "גזר ארוז", "price": 4.9, "selling": "BY_PACKAGE", "package_g": 1300},
}

# Two real-shaped slots (epoch ms, Asia/Jerusalem = UTC+3 in October).
SLOTS = {
    "20261020": [
        {"code": "148340770", "fromHour": 1792486800000, "toHour": 1792494000000, "selectable": True,
         "price": {"value": 35.9}, "fromHourString": "2026/10/20 12:00", "available": 202, "reserved": 0, "locked": 1},
        {"code": "148340772", "fromHour": 1792504800000, "toHour": 1792512000000, "selectable": False,
         "price": {"value": 35.9}, "fromHourString": "2026/10/20 17:00", "available": 0, "reserved": 0, "locked": 6},
    ]
}

PAYMENT_INFOS = [
    {"id": "8800001", "saved": True, "defaultPaymentInfo": True, "blackList": False, "brand": "VISA",
     "cardNumber": "1111", "ccNumber": "411111******1111", "expiryMonth": "7", "expiryYear": "2029",
     "accountHolderName": "Test Holder", "hybrisPaymentInfoToken": "secret-token",
     "paymentModeData": {"type": "CREDIT_CARD"}},
]


class FakeSite:
    def __init__(self):
        self.cart = []  # [{"entry": int, "code": str, "qty": float}]
        self.requests = []
        self.fail_add = False          # add returns 200 but changes nothing
        self.fail_update = False       # update returns 200 but changes nothing
        self.minicart_error = False
        self.ignore_first_removals = 0
        # Checkout behaviour (shapes partly assumed: see shufersal_parsers).
        self.selected_slot = None
        self.checkout_state = {"path": "/online/he/checkout/multi/summary", "reauth": False, "cvv": False,
                               "threeDS": True, "threeDSTest": False}
        self.cost_summary = None  # defaults to {"totalPrice": {"value": subtotal + fee}}
        # The live site (2026-10-19) answers GET /checkout with 302 -> /cart ->
        # /cart/cartsummary until the customer entered the account password / SMS
        # code in the modal that the cart summary's "מעבר לקופה" button
        # (data-requires-pwd="always") opens. checkout_gated=True models that;
        # checkout_reauthed is set when Or passes the modal in a watched run.
        self.checkout_gated = False
        self.checkout_reauthed = False
        self.summary_total_text = None  # None: subtotal + slot fee, as the live page prints it
        self.place_order_response = {"success": True, "redirect": "/checkout/orderConfirmation/123456789"}
        self.confirmation_html = "<h1>תודה</h1><p>מספר הזמנה: 123456789</p>"
        self.orders_placed = 0
        self.fail_reads_after_write = 0  # the next N cart reads after a write fail (uncertain outcome)
        self.hidden_entries = set()      # entry numbers the cart page does not render
        self.checkout_state_sequence = []  # page states returned before checkout_state (e.g. re-auth, then clear)
        self.confirm_on_url_reads = None   # watched run: Or "clicks" place-order after N url reads
        self.confirmation_url = BASE + "/online/he/checkout/orderConfirmation/987654321"
        # The session CSRF token. A page holds the value current when it loaded;
        # writes carrying an older one are redirected home with HTTP 200 and
        # change nothing (seen live 2026-10-19).
        self.csrf_token = "token-1"
        self.reject_all_writes = False
        # The server session (JSESSIONID). It idles out on the server; the next
        # request then gets a NEW anonymous session whose cart is empty, answered
        # with HTTP 200 like any other read. Only a full page load re-logs it in
        # from the remember-me cookie and restores the real cart (reproduced
        # live 2026-10-19 by sending an unknown JSESSIONID).
        self.session_id = "sess-1"
        self.session_logged_in = True
        self.remember_me = True          # False: the remember-me login is gone too
        self._session_expired = False
        self.anon_cart = []
        self.page_loads = 0
        # ---- Unattended checkout UI (live markup 2026-10-20, see the driver) ----
        self.sms = FakeSmsInbox()
        self.login_code = "482913"
        self.benefit_popup = True          # #creditBenefitProductModal after "מעבר לקופה"
        self.benefit_continue_adds = 0     # "המשך לתשלום" would add items: must never be clicked
        self.reauth_modal = True           # #forgottenPwdModal (password / SMS)
        self.login_sms_sender = "SHUFERSAL"
        self.login_sms_text = "קוד האימות שלך לשופרסל הוא: {code}"
        self.send_login_sms = True
        self.stale_login_sms = False       # also deliver an older code from before the trigger
        self.ui = {"modal": None, "sms_tab": False, "sms_sent": False, "sms_value": "", "otp_error": False,
                   "three_ds": False, "error_popup": None, "cvv_value": None, "checkout_popups": []}
        self.checkout_popups = []          # popups the checkout page opens on load (closed by their X)
        self.checkout_cvv_field = False
        self.place_clicks = 0
        self.after_click = "confirm"       # confirm | 3ds | error | nothing | confirm_no_number
        self.issuer_code = "771204"
        self.issuer_sender = "Isracard"
        self.issuer_amount = None          # None: the order total
        self.issuer_last4 = "1111"
        self.issuer_merchant = "Shufersal Online"
        self.send_issuer_sms = True
        self.three_ds_inputs = 1
        self.three_ds_codes = []           # codes typed into the bank frame
        self.cart_change_on_checkout = False
        self.checkout_total_delta = 0.0    # the checkout page total differs from the cart summary
        self.cartsummary_popups = []       # other popups after "מעבר לקופה" (must stop the run)
        self.checkout_selected_slot = None # None: the slot selected via postHomeDeliverySlot
        self.three_ds_form = "ok"          # ok | no_form | two_submits
        self.benefit_close_safe = True     # the benefit popup's X is the plain aria-label="סגור" button
        self.rotate_session_on_checkout_cart_read = False
        self.closed_with = []              # selectors the driver reported clicking to close popups

    def expire_session(self):
        """The server forgets the session (idle timeout); takes effect on the next request."""
        self._session_expired = True

    def _touch_session(self):
        if self._session_expired:
            self._session_expired = False
            self.session_id = f"sess-{int(self.session_id.split('-')[1]) + 1}"
            self.session_logged_in = False
            self.anon_cart = []
            self.rotate_csrf()

    def load_page(self):
        """A full page navigation: remember-me logs an anonymous session back in."""
        self._touch_session()
        self.page_loads += 1
        if not self.session_logged_in and self.remember_me:
            self.session_logged_in = True

    def rotate_csrf(self):
        n = int(self.csrf_token.split("-")[1]) + 1
        self.csrf_token = f"token-{n}"

    # ---- helpers ----
    def _render_cart(self):
        tiles = ['<li class="tileBlock miglog-prod" data-entry-number="" data-product-code="P_4131074" '
                 'data-product-name="recommendation"><input name="qty" value="1"></li>']
        rows = []
        for e in self.cart:
            if e["entry"] in self.hidden_entries:
                continue
            p = CATALOG[e["code"]]
            sm = p["selling"]
            # Live markup (2026-10-19): the method is on the article class and the
            # qty input, the line's choice is the selected <option>; the remover's
            # data-selling-method is empty and there is no hidden sellingMethod input.
            options = "".join(f'<option {"selected " if v == sm else ""}value="{v}">{v}</option>'
                              for v in ("BY_UNIT", "BY_WEIGHT"))
            rows.append(
                f'<article class="miglog-prod miglog-sellingmethod-{sm.lower()} miglog-customer-sellingmethod-{sm.lower()} miglog-incart" '
                f'data-product-code="P_{e["code"]}" data-entry-number="{e["entry"]}" data-entry-qty="{e["qty"]:.1f}">'
                f'<a class="miglog-prod-remove" data-miglog-role="cart-item-remover" data-selling-method=""></a>'
                f'<h3 class="miglog-text3 miglog-prod-name"><a title="{escape(p["name"])}">{escape(p["name"])}</a></h3>'
                f'<form class="add_to_cart_form" action="cart/update">'
                f'<input type="hidden" name="productCodePost" value="P_{e["code"]}"/>'
                f'<input class="spinContainer js-qty-selector-input" data-miglog-sellingmethod="{sm}" type="text" '
                f'value="{e["qty"]:g}" name="qty"/>'
                f'<select class="selectpicker" name="sellingMethod">{options}</select></form>'
                f'<button data-miglog-role="cart-item-remover"></button></article>'
            )
        summary = ('<span class="totalItems">(<span class="miglog-cart-summary-itemnum">%d</span> פריטים)</span>'
                   % len(self.cart))  # server-side: counts every entry, rendered or not
        return ('<section id="cartMiddleContent"><div class="miglog-prodlist">' + "".join(rows)
                + "</div></section><div class='recs'>" + "".join(tiles) + "</div>" + summary)

    def _minicart(self):
        total = sum(CATALOG[e["code"]]["price"] * e["qty"] for e in self.cart)
        # Like the live site: BY_UNIT lines count their quantity, weighed and
        # packaged lines count grams.
        def contribution(e):
            p = CATALOG[e["code"]]
            if p["selling"] == "BY_UNIT":
                return e["qty"]
            if p["selling"] == "BY_PACKAGE":
                return e["qty"] * p["package_g"]
            return e["qty"] * 1000
        units = sum(contribution(e) for e in self.cart)
        return ('\n\n{"miniCartCount": %d, "miniCartPrice": "<span>%.2f</span><span class="currency">'
                '<span aria-hidden="true">₪</span></span>\n\t\t"}' % (round(units), total))

    def order_total(self):
        subtotal = sum(CATALOG[e["code"]]["price"] * e["qty"] for e in self.cart)
        fee = next((s["price"]["value"] for d in SLOTS.values() for s in d if s["code"] == self.selected_slot), 0)
        return round(subtotal + fee, 2)

    def cart_summary_state(self, path):
        """What CART_SUMMARY_STATE_JS reads on the page (live markup 2026-10-19)."""
        on_summary = path.rstrip("/").endswith("/cart/cartsummary")
        total = self.summary_total_text if self.summary_total_text is not None else (
            "\n                            %.2f\n₪\nשקלים חדשים\n" % self.order_total())
        return {"path": path, "loggedIn": self.session_logged_in,
                "total": total if on_summary else None,
                "checkoutUrl": "/online/he/checkout" if on_summary else None,
                "requiresPwd": "always" if on_summary else None,
                "threeDS": True}

    def _renumber(self):
        for i, e in enumerate(self.cart):
            e["entry"] = i

    # ---- router ----
    def handle(self, url, method, body, content_type):
        self._touch_session()
        if self.session_logged_in:
            return self._handle(url, method, body, content_type)
        user_cart, self.cart = self.cart, self.anon_cart
        try:
            return self._handle(url, method, body, content_type)
        finally:
            self.anon_cart, self.cart = self.cart, user_cart

    def _handle(self, url, method, body, content_type):
        u = urlparse(url)
        path, q = u.path, parse_qs(u.query)
        self.requests.append((method, path, u.query, body))
        if path == "/online/he/search/results":
            term = q.get("q", [""])[0]
            hits = [c for c, p in CATALOG.items() if term == c or term in p["name"] or term in c]
            results = [{"code": f"P_{c}", "name": CATALOG[c]["name"], "price": {"value": CATALOG[c]["price"]},
                        "sellingMethod": {"code": CATALOG[c]["selling"]}, "brandName": "x",
                        "stock": {"stockLevelStatus": {"code": "inStock"}}} for c in hits]
            return 200, url, json.dumps({"results": results})
        if path == "/online/he/cart/miniCart/TOTAL" and self.rotate_session_on_checkout_cart_read and self.checkout_reauthed:
            self.rotate_session_on_checkout_cart_read = False
            self.session_id = f"sess-{int(self.session_id.split('-')[1]) + 1}"
        if path == "/online/he/cart/miniCart/TOTAL":
            if self.fail_reads_after_write > 0 and any(r[0] == "POST" for r in self.requests):
                self.fail_reads_after_write -= 1
                return 500, url, "error"
            if self.minicart_error:
                return 500, url, "error"
            return 200, url, self._minicart()
        if path == "/online/he/cart/load":
            return 200, url, self._render_cart()
        if path == "/online/he/cart/add" and method == "POST":
            data = json.loads(body)
            code = data["productCodePost"].removeprefix("P_")
            if not self.fail_add:
                self.cart.append({"entry": len(self.cart), "code": code, "qty": float(data["qty"])})
            return 200, url, self._render_cart()
        if path == "/online/he/cart/update" and method == "POST":
            entry, qty = int(q["entryNumber"][0]), float(q["qty"][0])
            target = next((e for e in self.cart if e["entry"] == entry), None)
            if target and not self.fail_update:
                if qty <= 0:
                    if self.ignore_first_removals > 0:
                        self.ignore_first_removals -= 1
                    else:
                        self.cart.remove(target)
                        self._renumber()
                else:
                    target["qty"] = qty
            return 200, url, self._render_cart()
        if path == "/online/he/timeSlot/preselection/getHomeDeliverySlots":
            return 200, url, json.dumps(SLOTS)
        if path == "/online/he/checkout/payment/customer/paymentInfos":
            return 200, url, json.dumps(PAYMENT_INFOS)
        if path == "/online/he/timeSlot/preselection/postHomeDeliverySlot" and method == "POST":
            self.selected_slot = json.loads(body)["homeDeliveryTimeSlot"]["code"]
            return 200, url, "{}"
        if path == "/online/he/checkout/costSummary/direct":
            if self.checkout_gated and not self.checkout_reauthed:
                return 200, BASE + "/A", "<!DOCTYPE html><html></html>"  # live: redirected home
            if self.cost_summary is not None:
                return 200, url, json.dumps(self.cost_summary)
            subtotal = sum(CATALOG[e["code"]]["price"] * e["qty"] for e in self.cart)
            return 200, url, json.dumps({"subTotal": {"value": round(subtotal, 2)},
                                         "totalAmount": {"value": round(self.order_total() + self.checkout_total_delta, 2)}})
        if path == "/online/he/checkout/placeOrder":
            self.orders_placed += 1
            self.place_order_query = q
            return 200, url, json.dumps(self.place_order_response)
        if path == "/online/he/checkout/timeSlot/getSelectedTimeSlot":
            code = self.checkout_selected_slot or self.selected_slot
            return 200, url, json.dumps({"code": code} if code else {})
        if path == "/online/he/my-account":
            return 200, BASE + "/online/he/my-account/personal-area", "<html></html>"
        return 404, url, "not found"


    # ---- unattended UI ----
    def ui_state(self, page):
        path = urlparse(page._url).path
        on_summary = path.rstrip("/").endswith("/cart/cartsummary")
        on_checkout = path.rstrip("/").endswith("/miglog-checkout")
        ui = self.ui
        popups = []
        if ui["modal"] == "benefit":
            popups.append("creditBenefitProductModal")
        if on_summary or ui["modal"] == "benefit":
            popups += ui.get("summary_popups", [])
        if on_checkout:
            popups += ui["checkout_popups"]
        return {
            "path": path, "href": page._url, "loggedIn": self.session_logged_in,
            "checkoutLink": on_summary, "pwdModal": ui["modal"] == "pwd",
            "smsInput": ui["modal"] == "pwd" and ui["sms_sent"], "strongAuth": False,
            "otpError": "קוד שגוי" if ui["otp_error"] else None, "popups": popups,
            "placeButton": on_checkout and not ui["three_ds"],
            "threeDS": on_checkout and ui["three_ds"], "errorPopup": ui["error_popup"],
            "cvvSelector": "#cvv" if on_checkout and self.checkout_cvv_field else None,
        }

    def ui_click(self, page, sel):
        ui = self.ui
        path = urlparse(page._url).path
        if sel == ".miglog-cart-summary-checkoutLink" and path.endswith("/cart/cartsummary"):
            ui.update(modal=None, sms_tab=False, sms_sent=False, sms_value="", otp_error=False, three_ds=False,
                      error_popup=None, checkout_popups=[], summary_popups=list(self.cartsummary_popups))
            self.checkout_reauthed = False  # the re-auth is asked again for every checkout
            ui["modal"] = "benefit" if self.benefit_popup else ("pwd" if self.reauth_modal else None)
            if ui["modal"] is None:
                self.checkout_reauthed = True
                page._url = BASE + "/online/he/miglog-checkout"
            return True
        if sel == '#forgottenPwdModal a.tab[href="#smsTab"]' and ui["modal"] == "pwd":
            ui["sms_tab"] = True
            return True
        if sel == "#sendSmsBtn" and ui["modal"] == "pwd" and ui["sms_tab"]:
            ui["sms_sent"] = True
            if self.send_login_sms:
                self.sms.add(self.login_sms_sender, self.login_sms_text.format(code=self.login_code))
            return True
        if sel == "#forgottenPwdModal .js-otp-submit-passwordModal" and ui["modal"] == "pwd":
            if len(ui["sms_value"]) != 6:
                return False  # disabled
            if ui["sms_value"] == self.login_code:
                ui["modal"] = None
                self.checkout_reauthed = True
                page._url = BASE + "/online/he/miglog-checkout"
                ui["checkout_popups"] = list(self.checkout_popups)
                if self.cart_change_on_checkout:
                    self.cart.append({"entry": len(self.cart), "code": "4131074", "qty": 1.0})
            else:
                ui["otp_error"] = True
            return True
        if sel == "a.btnConfirm" and path.endswith("/miglog-checkout") and not ui["checkout_popups"]:
            self.place_clicks += 1
            if self.after_click == "confirm":
                self.orders_placed += 1
                page._url = self.confirmation_url
            elif self.after_click == "confirm_no_number":
                self.orders_placed += 1
                page._url = BASE + "/online/he/checkout/orderConfirmation"
                self.confirmation_html = "<p>תודה!</p>"
            elif self.after_click == "3ds":
                ui["three_ds"] = True
                if self.send_issuer_sms:
                    amount = self.order_total() if self.issuer_amount is None else self.issuer_amount
                    self.sms.add(self.issuer_sender, (
                        f"חשוב מאוד, לתשומת ליבך- זיהינו כעת עסקה בסכום {amount:.2f} ILS ב {self.issuer_merchant} "
                        f"בכרטיסך {self.issuer_last4}, ... הקוד שלך להשלמת העסקה הינו {self.issuer_code}\n"
                        "תמיד לשירותך, ישראכרט"))
            elif self.after_click == "error":
                ui["error_popup"] = "אירעה שגיאה בתשלום"
            return True
        return False

    def ui_fill(self, page, sel, value):
        if sel == "#smsCodeInput" and self.ui["modal"] == "pwd":
            self.ui["sms_value"] = value
            return True
        if sel == "#cvv":
            self.ui["cvv_value"] = value
            return True
        return False

    def ui_close_benefit(self, page):
        if self.ui["modal"] != "benefit" or not self.benefit_close_safe:
            return None
        self.ui["modal"] = "pwd" if self.reauth_modal else None
        if self.ui["modal"] is None:
            self.checkout_reauthed = True
            page._url = BASE + "/online/he/miglog-checkout"
        sel = '#creditBenefitProductModal button.btnClose[aria-label="סגור"]'
        self.closed_with.append(sel)
        return sel

    def three_ds_submit(self, page, code):
        if self.three_ds_inputs != 1:
            return f"inputs:{self.three_ds_inputs}"
        if self.three_ds_form == "no_form":
            return "no-form"
        if self.three_ds_form == "two_submits":
            return "no-submit:2"
        self.three_ds_codes.append(code)
        self.ui["three_ds"] = False
        if code == self.issuer_code:
            self.orders_placed += 1
            page._url = self.confirmation_url
        else:
            self.ui["error_popup"] = "התשלום נכשל"
        return "clicked"


class FakeSmsInbox:
    """Stand-in for the sms-api /messages list (newest first)."""

    def __init__(self):
        self.messages = []
        self.rowid = 3150
        self.fail = False

    def add(self, sender, text, age_seconds=0.0):
        self.rowid += 1
        when = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        self.messages.insert(0, {"id": str(self.rowid), "rowid": self.rowid, "service": "SMS", "sender": sender,
                                 "received_at": when.isoformat(), "direction": "incoming", "text": text})

    def fetch(self):
        if self.fail:
            raise OSError("sms-api down")
        return [dict(m) for m in self.messages[:30]]


class FakeFrame:
    def __init__(self, site, page):
        self.site, self.page, self.child_frames = site, page, []

    async def evaluate(self, script, args=None):
        if "/*ua-3ds-probe*/" in script:
            return {"inputs": self.site.three_ds_inputs}
        if "/*ua-3ds-submit*/" in script:
            return self.site.three_ds_submit(self.page, args)
        raise AssertionError("unexpected frame script")


class FakeHandle:
    def __init__(self, frame):
        self.frame = frame

    async def content_frame(self):
        return self.frame


class FakePage:
    def __init__(self, site):
        self.site = site
        self.closed = False
        self.handlers = {}
        self.gotos = []
        self._url = BASE + "/online/he/"
        self.csrf = site.csrf_token
        self.logged_in_marker = site.session_logged_in

    @property
    def url(self):
        n = self.site.confirm_on_url_reads
        if n is not None and ("/checkout" in self._url or "/cart/cartsummary" in self._url):
            if n <= 0:
                self._url = self.site.confirmation_url
            else:
                self.site.confirm_on_url_reads = n - 1
        return self._url

    @url.setter
    def url(self, value):
        self._url = value

    def is_closed(self):
        return self.closed

    def on(self, event, fn):
        self.handlers.setdefault(event, []).append(fn)

    def close_unexpectedly(self):
        self.closed = True
        for fn in self.handlers.get("close", []):
            fn(self)

    async def goto(self, url, **kwargs):
        self.gotos.append(url)
        if (urlparse(url).path.rstrip("/") == "/online/he/checkout" and self.site.checkout_gated
                and not self.site.checkout_reauthed):
            url = BASE + "/online/he/cart/cartsummary"  # 302 -> /cart -> /cart/cartsummary
        self.url = url
        self.site.load_page()
        self.csrf = self.site.csrf_token
        self.logged_in_marker = self.site.session_logged_in

    async def wait_for_timeout(self, ms):
        return None

    async def content(self):
        return self.site.confirmation_html if "order" in self._url.lower() else "<html></html>"

    async def query_selector(self, sel):
        if sel == ".creditCardOtpPopup iframe" and self.site.ui["three_ds"]:
            return FakeHandle(FakeFrame(self.site, self))
        return None

    async def evaluate(self, script, args=None):
        if self.closed:
            raise Exception("Target page, context or browser has been closed")
        if "/*ua-state*/" in script:
            return self.site.ui_state(self)
        if "/*ua-click*/" in script:
            return self.site.ui_click(self, args)
        if "/*ua-fill*/" in script:
            return self.site.ui_fill(self, args[0], args[1])
        if "/*ua-close-benefit*/" in script:
            return self.site.ui_close_benefit(self)
        if args is None and "miglog-cart-summary-totalprice" in script:
            return self.site.cart_summary_state(urlparse(self._url).path)
        if args is None and "miglog.account" in script:  # login marker set when the page loaded
            if "/miglog-checkout" in self._url:
                return None  # the Vue checkout page may not carry window.miglog.account
            return self.logged_in_marker
        if args is None:  # checkout page state probe
            state = dict(self.site.checkout_state)
            if self.site.checkout_state_sequence:
                state.update(self.site.checkout_state_sequence.pop(0))
            if "/checkout" not in self._url:  # redirected away: no checkout page widgets
                state.update(path=urlparse(self._url).path, reauth=False, cvv=False)
            return state
        if isinstance(args, bool):  # PayMe.clientData(isTestMode)
            return "payme-jwt"
        url, method, body, content_type = args
        self.site._touch_session()
        if method != "GET" and (self.csrf != self.site.csrf_token or self.site.reject_all_writes):
            u = urlparse(url)
            self.site.requests.append((method, u.path, u.query, body))
            return {"status": 200, "url": BASE + "/online/he", "redirected": True, "text": "<!DOCTYPE html><html></html>"}
        status, final, text = self.site.handle(url, method, body, content_type)
        return {"status": status, "url": final, "redirected": False, "text": text}


class FakeContext:
    def __init__(self, site):
        self.site = site
        self.pages = []
        self.handlers = {}
        self.closed = False

    def on(self, event, fn):
        self.handlers.setdefault(event, []).append(fn)

    async def new_page(self):
        page = FakePage(self.site)
        self.pages.append(page)
        return page

    async def add_cookies(self, cookies):
        return None

    async def cookies(self, urls=None):
        return [{"name": "JSESSIONID", "value": self.site.session_id, "path": "/online"}]

    async def close(self):
        self.closed = True
        for fn in self.handlers.get("close", []):
            fn(self)


def install_fake_playwright(monkeypatch, site, fail_times=0):
    """Replaces playwright.async_api so the driver's real _start_live runs
    against FakeContext. The first `fail_times` launches raise the same error a
    missing browser binary produces. Returns the list of launch attempts."""
    attempts = []

    class Chromium:
        async def launch_persistent_context(self, **kwargs):
            attempts.append(kwargs)
            if len(attempts) <= fail_times:
                raise Exception("BrowserType.launch_persistent_context: Executable doesn't exist at /tmp/x\n<banner>")
            return FakeContext(site)

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
    return attempts
