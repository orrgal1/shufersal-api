"""In-memory stand-in for shufersal.co.il, served through a fake Playwright page.

It answers the in-page fetch() the driver runs (page.evaluate(script, [url,
method, body, contentType])) with the response shapes captured from the live
site, and keeps a cart so write paths can be verified end to end.
"""
import json
import sys
import types
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
            return 200, url, json.dumps({"subTotal": {"value": round(subtotal, 2)}, "totalPrice": {"value": self.order_total()}})
        if path == "/online/he/checkout/placeOrder":
            self.orders_placed += 1
            self.place_order_query = q
            return 200, url, json.dumps(self.place_order_response)
        if path == "/online/he/my-account":
            return 200, BASE + "/online/he/my-account/personal-area", "<html></html>"
        return 404, url, "not found"


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

    async def evaluate(self, script, args=None):
        if self.closed:
            raise Exception("Target page, context or browser has been closed")
        if args is None and "miglog-cart-summary-totalprice" in script:
            return self.site.cart_summary_state(urlparse(self._url).path)
        if args is None and "miglog.account" in script:  # login marker set when the page loaded
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
