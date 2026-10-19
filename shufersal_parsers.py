"""Pure parsers for shufersal.co.il responses.

Kept free of Playwright so every live response shape can be unit tested with
fixtures. Nothing here performs I/O.
"""
import hashlib
import json
import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

IL_TZ = ZoneInfo("Asia/Jerusalem")
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class ParseError(ValueError):
    """The site answered, but not in a shape we understand."""


def clean_code(code: Any) -> str:
    return str(code or "").strip().removeprefix("P_")


def site_code(code: Any) -> str:
    return f"P_{clean_code(code)}"


def _num(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


# ==================== Catalog ====================

def parse_search_results(data: Any) -> List[Dict[str, Any]]:
    """/online/he/search/results JSON -> API search items."""
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ParseError("search response has no results list")
    items = []
    for r in data["results"]:
        code = clean_code(r.get("code"))
        name = (r.get("name") or "").strip()
        if not code or not name:
            continue
        price = _money(r.get("price"))
        effective = _money(r.get("effectivePrice"))
        if price is None:  # fall back to other price fields the search JSON carries
            price = effective if effective is not None else _money((r.get("priceRange") or {}).get("minPrice"))
        stock_code = (((r.get("stock") or {}).get("stockLevelStatus") or {}).get("code") or "").lower()
        images = r.get("images") or []
        image_url = next((i.get("url") for i in images if isinstance(i, dict) and i.get("url")), "") or ""
        selling = (r.get("sellingMethod") or {}).get("code") if isinstance(r.get("sellingMethod"), dict) else r.get("sellingMethod")
        items.append({
            "id": code,
            "name": name,
            "brand": (r.get("brandName") or "").strip() or "שופרסל",
            "price_ils": price,
            "sale_price_ils": effective if effective is not None and price is not None and effective < price else None,
            "price_per_unit_ils": _money(r.get("pricePerUnit")),
            "unit": (r.get("unitDescription") or "").strip() or "יחידה",
            "image_url": image_url,
            "in_stock": stock_code != "outofstock",
            "selling_method": (selling or "BY_UNIT").upper(),
            "description": (r.get("description") or name).strip() if isinstance(r.get("description"), str) else name,
        })
    return items


def _money(v: Any) -> Optional[float]:
    return _num(v.get("value")) if isinstance(v, dict) else _num(v)


def find_exact(items: List[Dict[str, Any]], product_id: str) -> Optional[Dict[str, Any]]:
    code = clean_code(product_id)
    return next((i for i in items if i["id"] == code), None)


# ==================== Cart ====================

def _class_method(classes: List[str], prefix: str) -> Optional[str]:
    for c in classes:
        if c.startswith(prefix) and len(c) > len(prefix):
            return c[len(prefix):].upper()
    return None


class _CartEntryParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.entries: List[Dict[str, Any]] = []
        self._cur: Optional[Dict[str, Any]] = None
        self._depth = 0
        self._name_capture = False
        self._in_sm_select = False

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        classes = a.get("class", "").split()
        if self._cur is None:
            entry = a.get("data-entry-number", "").strip()
            if "miglog-prod" in classes and entry != "" and a.get("data-product-code"):
                self._cur = {
                    "line_id": entry,
                    "product_id": clean_code(a.get("data-product-code")),
                    "name": a.get("data-product-name", "").strip(),
                    "selling_method": None,
                    "unit_price_attr": _num(a.get("data-product-price")),
                    "qty": None,
                    # Candidate selling methods by source, resolved in _finish().
                    "_sm": {
                        "customer_class": _class_method(classes, "miglog-customer-sellingmethod-"),
                        "class": _class_method(classes, "miglog-sellingmethod-"),
                        "attr": (a.get("data-selling-method") or "").upper() or None,
                    },
                }
                self._depth = 0 if tag in VOID_TAGS else 1
                self._in_sm_select = False
            return
        if tag not in VOID_TAGS:
            self._depth += 1
        sm = self._cur["_sm"]
        if tag == "input":
            name = a.get("name", "")
            if name == "qty" and self._cur["qty"] is None:
                self._cur["qty"] = _num(a.get("value"))
                sm.setdefault("qty_input", (a.get("data-miglog-sellingmethod") or "").upper() or None)
            elif name == "sellingMethod":
                sm.setdefault("input", a.get("value", "").upper() or None)
        elif tag == "select" and a.get("name") == "sellingMethod":
            self._in_sm_select = True
        elif tag == "option" and self._in_sm_select and "selected" in a:
            sm.setdefault("selected_option", a.get("value", "").upper() or None)
        if not self._cur["name"] and "miglog-prod-name" in classes:
            self._name_capture = True

    def handle_data(self, data):
        if self._cur is not None and self._name_capture and data.strip():
            self._cur["name"] = data.strip()
            self._name_capture = False

    def handle_endtag(self, tag):
        if self._cur is None or tag in VOID_TAGS:
            return
        if tag == "select":
            self._in_sm_select = False
        self._depth -= 1
        if self._depth <= 0:
            sm = self._cur.pop("_sm")
            # The live cart (2026-10-19) marks the line's method on the article
            # class (miglog-customer-sellingmethod-by_weight is the customer's
            # choice for CUSTOMER_SELECTION products), on the qty input
            # (data-miglog-sellingmethod) and as a <select> option; the article's
            # data-selling-method and the remover's are empty.
            self._cur["selling_method"] = next(
                (sm.get(k) for k in ("customer_class", "class", "qty_input", "selected_option", "input", "attr")
                 if sm.get(k)), None)
            self.entries.append(self._cur)
            self._cur = None
            self._name_capture = False


def parse_cart_entries(html: str) -> List[Dict[str, Any]]:
    """/online/he/cart/load HTML -> cart entries (exact entry numbers and codes)."""
    p = _CartEntryParser()
    p.feed(html or "")
    p.close()
    seen = set()
    out = []
    for e in p.entries:
        if e["line_id"] in seen:
            continue
        seen.add(e["line_id"])
        if e["qty"] is None:
            raise ParseError(f"cart entry {e['line_id']} has no quantity")
        e["selling_method"] = e["selling_method"] or "BY_UNIT"
        out.append(e)
    return out


def parse_cart_line_count(html: str) -> Optional[int]:
    """The cart page's own line count ("(18 פריטים)" in the summary), rendered
    server-side from the cart's entries. None when the page does not show it."""
    m = re.search(r'class="[^"]*\bmiglog-cart-summary-itemnum\b[^"]*"[^>]*>\s*(\d+)\s*<', html or "")
    return int(m.group(1)) if m else None


def parse_minicart(text: Any) -> Tuple[int, float]:
    """/online/he/cart/miniCart/TOTAL -> (item count, site subtotal).

    The count is not a unit count: BY_UNIT lines add their quantity, while
    BY_WEIGHT and BY_PACKAGE lines add GRAMS (verified live 2026-10-19: 20 units
    + 1 kg + 1 kg + 0.5 kg + a 1.3 kg carrot package read 3820). See
    cart_count_check().

    The site labels this JSON but embeds unescaped HTML, so it is read with
    patterns rather than json.loads."""
    if isinstance(text, dict):
        text = json.dumps(text, ensure_ascii=False)
    text = str(text or "")
    count = re.search(r'"miniCartCount"\s*:\s*(\d+)', text)
    price = re.search(r'"miniCartPrice"\s*:\s*"\s*<span>\s*([\d.,]+)\s*</span>', text)
    if not count or not price:
        raise ParseError("minicart response has no count or price")
    return int(count.group(1)), float(price.group(1).replace(",", ""))


def cart_count_check(site_count: int, items: List[Dict[str, Any]],
                     site_lines: Optional[int] = None, tolerance: float = 0.001) -> bool:
    """True when the parsed lines account for the mini-cart count.

    miniCartCount = sum(qty of BY_UNIT lines) + grams of every BY_WEIGHT and
    BY_PACKAGE line. Weighed lines contribute qty_kg * 1000; a package's weight
    is not on the cart page, so package lines only bound the remainder from
    below and the page's own line count must then match the parsed lines."""
    known = 0.0
    unknown_min = 0.0  # lines whose count contribution is not readable (packages, unknown methods)
    for i in items:
        method = (i.get("selling_method") or "BY_UNIT").upper()
        qty = float(i["qty"])
        if method == "BY_UNIT":
            known += qty
        elif method == "BY_WEIGHT":
            known += round(qty * 1000)
        else:
            unknown_min += max(qty, 1.0)  # at least a gram per package
    if site_lines is not None and site_lines != len(items):
        return False
    remainder = site_count - known
    if unknown_min == 0:
        return abs(remainder) < tolerance
    # A package's grams can hide a missing unit line, so require the page's line count too.
    return site_lines is not None and remainder >= unknown_min - tolerance


def cart_hash(items: List[Dict[str, Any]]) -> str:
    """Stable fingerprint of cart contents: sorted (product_id, qty)."""
    norm = sorted((clean_code(i["product_id"]), round(float(i["qty"]), 3)) for i in items)
    return hashlib.sha256(json.dumps(norm, separators=(",", ":")).encode()).hexdigest()[:16]


# ==================== Delivery ====================

def parse_home_delivery_slots(data: Any) -> List[Dict[str, Any]]:
    """/timeSlot/preselection/getHomeDeliverySlots JSON {YYYYMMDD: [slot]} -> API slots."""
    if not isinstance(data, dict):
        raise ParseError("slot response is not an object")
    slots = []
    for day_key in sorted(data):
        if not re.fullmatch(r"\d{8}", day_key) or not isinstance(data[day_key], list):
            continue
        for s in data[day_key]:
            code = str(s.get("code") or "")
            fee = _num((s.get("price") or {}).get("value"))
            if not code or fee is None or s.get("fromHour") is None or s.get("toHour") is None:
                continue
            start = datetime.fromtimestamp(int(s["fromHour"]) / 1000, IL_TZ)
            end = datetime.fromtimestamp(int(s["toHour"]) / 1000, IL_TZ)
            slots.append({
                "id": code,
                "date": f"{day_key[:4]}-{day_key[4:6]}-{day_key[6:]}",
                "window": f"{start:%H:%M} - {end:%H:%M}",
                "fee_ils": fee,
                "available": bool(s.get("selectable")),
                "start": start.isoformat(),
                "end": end.isoformat(),
            })
    return slots


# ==================== Payment ====================

def parse_payment_method(data: Any) -> Dict[str, Any]:
    """/checkout/payment/customer/paymentInfos -> masked default saved card.

    Only the brand, last four digits, expiry and initials leave this function;
    the card BIN, tokens and full holder name are dropped.
    """
    if not isinstance(data, list):
        raise ParseError("payment response is not a list")
    saved = [p for p in data if isinstance(p, dict) and p.get("saved", True) and not p.get("blackList")]
    if not saved:
        raise ParseError("no saved payment method")
    card = next((p for p in saved if p.get("defaultPaymentInfo")), saved[0])
    digits = re.sub(r"\D", "", str(card.get("cardNumber") or card.get("ccNumber") or ""))
    if len(digits) < 4:
        raise ParseError("saved payment method has no last four digits")
    name = str(card.get("accountHolderName") or card.get("ownerName") or "").split()
    brand = card.get("brand") or (card.get("cardTypeData") or {}).get("code") or card.get("cardType") or "unknown"
    return {
        "payment_info_id": str(card.get("id") or ""),
        "payment_type": ((card.get("paymentModeData") or {}).get("type") or ""),
        "brand": str(brand),
        "last4": digits[-4:],
        "expiry_month": str(card.get("expiryMonth") or "").zfill(2),
        "expiry_year": str(card.get("expiryYear") or ""),
        "name_on_card": " ".join(f"{w[0]}." for w in name if w) or "",
    }


def public_payment(method: Dict[str, Any]) -> Dict[str, Any]:
    return {k: method[k] for k in ("brand", "last4", "expiry_month", "expiry_year", "name_on_card")}


# ==================== Order ====================

# The site's order confirmation page (Hybris: /checkout/orderConfirmation/<code>).
CONFIRMATION_PATH_RE = re.compile(r"/order-?confirmation(?:/(\d{5,}))?/?$", re.I)
ORDER_TEXT_RE = re.compile(r"מספר\s*(?:ה)?הזמנה[^\d]{0,60}(\d{5,})")


def is_confirmation_url(url: str) -> bool:
    path = re.sub(r"^https?://[^/]+", "", url or "").split("?")[0].split("#")[0]
    return bool(CONFIRMATION_PATH_RE.search(path))


def parse_order_number(url: str, html: str) -> Optional[str]:
    """Order number from the confirmation page only: its URL segment, or the
    "מספר הזמנה" text on it. Any other page (order history, an order-update
    page, a page that merely says "thanks") yields None."""
    if not is_confirmation_url(url):
        return None
    path = re.sub(r"^https?://[^/]+", "", url).split("?")[0].split("#")[0]
    m = CONFIRMATION_PATH_RE.search(path)
    if m and m.group(1):
        return m.group(1)
    text = re.sub(r"<[^>]+>", " ", html or "")
    m = ORDER_TEXT_RE.search(text)
    return m.group(1) if m else None


# ==================== Checkout ====================

TOTAL_KEYS = ("totalPriceWithDelivery", "totalPriceWithTax", "totalPrice")


def extract_site_total(data: Any) -> Optional[float]:
    """Cart-level order total from a checkout cost-summary payload. Only the
    top-level keys are read, so a line or sub-cart "totalPrice" nested deeper
    can never be mistaken for the order total. The exact shape has not been
    observed with a non-empty cart; None means "not found", never a guess."""
    if not isinstance(data, dict):
        return None
    for key in TOTAL_KEYS:
        if key in data:
            v = data[key]
            n = _num(v.get("value")) if isinstance(v, dict) else _num(v)
            if n is not None:
                return n
    return None


def classify_place_order(data: Any) -> Dict[str, Any]:
    """/checkout/placeOrder JSON -> {"outcome": placed|three_ds_required|rejected|unknown, ...}."""
    if isinstance(data, dict):
        if data.get("success") and data.get("redirect"):
            return {"outcome": "placed", "redirect": str(data["redirect"])}
        link = data.get("otherCreditCard3DSLink") or data.get("directCreditCard3DSLink")
        if link:
            return {"outcome": "three_ds_required"}
        if data.get("allocationTimeout"):
            return {"outcome": "rejected", "code": "allocation_timeout"}
    if isinstance(data, list) and data and isinstance(data[0], dict):
        first = data[0]
        return {"outcome": "rejected", "code": str(first.get("errorCode") or first.get("code") or first.get("type") or "unknown")}
    return {"outcome": "unknown"}
