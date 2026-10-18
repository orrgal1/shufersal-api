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

    # ---- helpers ----
    def _render_cart(self):
        tiles = ['<li class="tileBlock miglog-prod" data-entry-number="" data-product-code="P_4131074" '
                 'data-product-name="recommendation"><input name="qty" value="1"></li>']
        rows = []
        for e in self.cart:
            p = CATALOG[e["code"]]
            rows.append(
                f'<div class="miglog-prod miglog-sellingmethod-{p["selling"].lower()}" data-entry-number="{e["entry"]}" '
                f'data-product-code="P_{e["code"]}" data-product-name="{escape(p["name"])}">'
                f'<form class="addToCart"><input type="hidden" name="sellingMethod" value="{p["selling"]}">'
                f'<input name="qty" class="js-qty-selector-input" value="{e["qty"]:g}"></form>'
                f'<button data-miglog-role="cart-item-remover"></button></div>'
            )
        return ('<section id="cartMiddleContent"><div class="miglog-prodlist">' + "".join(rows)
                + "</div></section><div class='recs'>" + "".join(tiles) + "</div>")

    def _minicart(self):
        total = sum(CATALOG[e["code"]]["price"] * e["qty"] for e in self.cart)
        return ('\n\n{"miniCartCount": %d, "miniCartPrice": "<span>%.2f</span><span class="currency">'
                '<span aria-hidden="true">₪</span></span>\n\t\t"}' % (len(self.cart), total))

    def _renumber(self):
        for i, e in enumerate(self.cart):
            e["entry"] = i

    # ---- router ----
    def handle(self, url, method, body, content_type):
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
        if path == "/online/he/my-account":
            return 200, BASE + "/online/he/my-account/personal-area", "<html></html>"
        return 404, url, "not found"


class FakePage:
    def __init__(self, site):
        self.site = site
        self.closed = False
        self.handlers = {}
        self.gotos = []
        self.url = BASE + "/online/he/"

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
        self.url = url

    async def evaluate(self, script, args=None):
        if self.closed:
            raise Exception("Target page, context or browser has been closed")
        url, method, body, content_type = args
        status, final, text = self.site.handle(url, method, body, content_type)
        return {"status": status, "url": final, "text": text}


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
