import asyncio
import logging
import re
import time
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import quote, urlencode
import json

import config
from security import APIException
from shufersal_parsers import (
    ParseError,
    cart_hash,
    classify_place_order,
    clean_code,
    extract_site_total,
    find_exact,
    parse_cart_entries,
    parse_home_delivery_slots,
    parse_minicart,
    parse_order_number,
    parse_payment_method,
    parse_search_results,
    public_payment,
    site_code,
)

logger = logging.getLogger("shufersal_driver")

BASE = "/online/he"
CART_CONTEXT = urlencode({"cartContext[openFrom]": "CATALOG", "cartContext[recommendationType]": "REGULAR"})
QTY_TOLERANCE = 0.001


def _upstream(message: str, code: str = "upstream_error", status: int = 502) -> APIException:
    return APIException(status_code=status, code=code, message=message)


class ShufersalDriver:
    """
    Playwright persistent context (for the WAF, the session and cookies) driving
    the site's own XHR endpoints from inside the page. Every write is verified
    by re-reading the cart; nothing falls back to fixture data outside MOCK_MODE.
    """
    LIVE_RETRY_SECONDS = 60

    def __init__(self):
        self.playwright = None
        self.context = None
        self.page = None
        self._lock = asyncio.Lock()
        self._is_started = False
        self._context_closed = False
        # Why the live browser is unavailable (None when live or in MOCK_MODE).
        self._live_error: Optional[str] = None
        self._last_live_attempt: float = 0.0
        self._last_launch_failed = False
        self._headless = True

        # Internal mock store used only when MOCK_MODE=True (tests/demo).
        self._mock_logged_in = True
        self._mock_cart: List[Dict[str, Any]] = [
            {
                "line_id": "line_101",
                "product_id": "P12345",
                "name": "חלב תנובה 3% בקרטון 1 ליטר",
                "qty": 2.0,
                "unit_price_ils": 7.10,
                "line_total_ils": 14.20
            }
        ]
        self._mock_delivery_fee = 29.90

    # ==================== Lifecycle ====================

    async def start(self, headless: Optional[bool] = None, user_data_dir: Optional[str] = None):
        """Initializes Playwright persistent context or mock mode."""
        if self._is_started:
            return
        self._is_started = True
        if config.MOCK_MODE:
            logger.info("Starting ShufersalDriver in MOCK_MODE.")
            return
        await self._start_live(headless=headless, user_data_dir=user_data_dir)

    async def _start_live(self, headless: Optional[bool] = None, user_data_dir: Optional[str] = None) -> bool:
        """Launches the live Playwright context. On failure, records the reason and
        leaves the driver unavailable (callers get 503, never fixture data)."""
        self._last_live_attempt = time.monotonic()
        await self._teardown_browser()
        try:
            from playwright.async_api import async_playwright
            if self.playwright is None:
                self.playwright = await async_playwright().start()

            profile_dir = user_data_dir or config.SHUFERSAL_PROFILE_DIR
            is_headless = config.HEADLESS if headless is None else headless

            logger.info(f"Launching persistent browser context from {profile_dir} (headless={is_headless})")
            self._headless = bool(is_headless)
            self.context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=is_headless,
                locale="he-IL",
                timezone_id="Asia/Jerusalem",
                viewport={"width": 1280, "height": 800},
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox"
                ]
            )
            self._context_closed = False
            ctx = self.context
            ctx.on("close", lambda *_: self._mark_dead("browser context closed", ctx))

            pages = self.context.pages
            page = pages[0] if pages else await self.context.new_page()
            try:
                imported_cookies = self._extract_chrome_session_cookies()
                if imported_cookies:
                    await self.context.add_cookies(imported_cookies)
                    logger.info(f"Imported {len(imported_cookies)} Shufersal session cookies from Chrome profile.")
            except Exception as e:
                logger.warning(f"Could not import Chrome session cookies: {e}")

            await page.goto(config.SHUFERSAL_BASE_URL + BASE + "/", wait_until="domcontentloaded", timeout=45000)
            page.on("close", lambda *_: self._mark_dead("page closed", page))
            page.on("crash", lambda *_: self._mark_dead("page crashed", page))
            self.page = page
            self._live_error = None
            self._last_launch_failed = False
            logger.info("Playwright persistent context started successfully.")
            return True
        except Exception as e:
            reason = str(e).splitlines()[0][:300] if str(e) else type(e).__name__
            if "Executable doesn't exist" in str(e):
                reason += " (run `venv/bin/python -m playwright install chromium`)"
            self._live_error = reason
            self._last_launch_failed = True
            await self._teardown_browser()
            logger.error(f"Live Playwright context unavailable: {reason}. Live endpoints will return 503 until it starts.")
            return False

    def _mark_dead(self, reason: str, owner: Any = None):
        # Events from a context/page we already replaced must not kill the new one.
        if owner is not None and owner is not self.context and owner is not self.page:
            return
        if not self._context_closed:
            logger.warning(f"Live browser lost: {reason}; it will be relaunched on the next request.")
        self._context_closed = True
        self._live_error = reason

    def _is_alive(self) -> bool:
        if self.page is None or self.context is None or self._context_closed:
            return False
        try:
            return not self.page.is_closed()
        except Exception:
            return False

    async def _teardown_browser(self):
        page, context = self.page, self.context
        self.page = None
        self.context = None
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass

    async def _require_live(self):
        """Ensures a live page exists outside MOCK_MODE. A dead page or context
        is relaunched; failed launches are retried at most once per
        LIVE_RETRY_SECONDS. Raises 503 when no live browser is available."""
        if config.MOCK_MODE or self._is_alive():
            return
        # A browser that was running and died is relaunched at once; a launch
        # that failed is retried at most once per LIVE_RETRY_SECONDS.
        throttled = self._last_launch_failed and time.monotonic() - self._last_live_attempt < self.LIVE_RETRY_SECONDS
        if self._is_started and not throttled:
            if await self._start_live():
                return
        raise APIException(
            status_code=503,
            code="browser_unavailable",
            message="The live Shufersal browser session is not running; no live data is available. "
                    + (f"Cause: {self._live_error}" if self._live_error else "The driver has not started.")
        )

    async def stop(self):
        """Cleanly releases browser resources."""
        if not self._is_started:
            return
        await self._teardown_browser()
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
            self.playwright = None
        self._is_started = False


    def _extract_chrome_session_cookies(self) -> List[Dict[str, Any]]:
        """Extracts and decrypts Shufersal cookies from local Chrome profile on macOS in headless mode."""
        import sqlite3, os, hashlib, subprocess
        from pathlib import Path
        chrome_cookies_path = Path.home() / "Library/Application Support/Google/Chrome/Default/Cookies"
        if not chrome_cookies_path.exists():
            return []
        try:
            res = subprocess.run(
                ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage"],
                capture_output=True, text=True, timeout=5
            )
            if res.returncode != 0 or not res.stdout.strip():
                return []
            safe_storage_key = res.stdout.strip().encode("utf-8")
            salt = b"saltysalt"
            derived_key = hashlib.pbkdf2_hmac("sha1", safe_storage_key, salt, 1003, dklen=16)
            iv = b" " * 16
            
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.backends import default_backend
            
            conn = sqlite3.connect(f"file:{chrome_cookies_path}?mode=ro", uri=True)
            cursor = conn.cursor()
            cursor.execute("SELECT name, host_key, encrypted_value, path, is_secure, is_httponly FROM cookies WHERE host_key LIKE '%shufersal%'")
            rows = cursor.fetchall()
            cookies = []
            for name, host, enc_val, c_path, is_secure, is_httponly in rows:
                if not enc_val:
                    continue
                if enc_val[:3] in (b"v10", b"v11"):
                    enc_val = enc_val[3:]
                try:
                    cipher = Cipher(algorithms.AES(derived_key), modes.CBC(iv), backend=default_backend())
                    decryptor = cipher.decryptor()
                    decrypted = decryptor.update(enc_val) + decryptor.finalize()
                    pad = decrypted[-1]
                    val_bytes = decrypted[32:-pad] if 1 <= pad <= 16 else decrypted[32:]
                    val = val_bytes.decode("utf-8", errors="ignore")
                    cookies.append({
                        "name": name,
                        "value": val,
                        "domain": host,
                        "path": c_path,
                        "secure": bool(is_secure),
                        "httpOnly": bool(is_httponly)
                    })
                except Exception:
                    pass
            conn.close()
            return cookies
        except Exception as e:
            logger.warning(f"Error reading Chrome cookies: {e}")
            return []

    # ==================== In-page HTTP ====================

    async def _fetch(
        self,
        path: str,
        method: str = "GET",
        body: Optional[str] = None,
        content_type: Optional[str] = None,
    ) -> Tuple[int, str, str]:
        """Runs fetch() inside the page so the request carries the session
        cookies, the WAF clearance and the page's CSRF token.
        Returns (status, final URL path, body text)."""
        await self._require_live()
        url = f"{config.SHUFERSAL_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
        script = """
        async ([url, method, body, contentType]) => {
            const headers = {
                'Accept': 'application/json, text/html, */*',
                'X-Requested-With': 'XMLHttpRequest'
            };
            const csrf = window.ACC?.config?.CSRFToken;
            if (csrf) headers['CSRFToken'] = csrf;
            if (contentType) headers['Content-Type'] = contentType;
            const opts = { method, headers, credentials: 'include' };
            if (body !== null && method !== 'GET') opts.body = body;
            try {
                const res = await fetch(url, opts);
                return { status: res.status, url: res.url, text: await res.text() };
            } catch (err) {
                return { status: 0, url: url, text: String(err) };
            }
        }
        """
        try:
            res = await self.page.evaluate(script, [url, method, body, content_type])
        except Exception as e:
            if not self._is_alive() or "closed" in str(e).lower() or "crash" in str(e).lower():
                self._mark_dead(f"page unusable: {str(e)[:120]}")
                raise APIException(status_code=503, code="browser_unavailable",
                                   message="The live browser session was lost; retry shortly.")
            raise _upstream(f"In-page request to {path.split('?')[0]} failed.")
        status = int(res.get("status") or 0)
        final = str(res.get("url") or "").replace(config.SHUFERSAL_BASE_URL.rstrip("/"), "")
        if status == 0:
            raise _upstream(f"Network error calling {path.split('?')[0]}.")
        return status, final, res.get("text") or ""

    async def _fetch_json(self, path: str) -> Any:
        status, final, text = await self._fetch(path)
        if status != 200:
            raise _upstream(f"{path.split('?')[0]} returned HTTP {status}.")
        try:
            return json.loads(text)
        except ValueError:
            raise _upstream(f"{path.split('?')[0]} did not return JSON (landed on {final.split('?')[0]}).")

    # ==================== Health & Login ====================

    async def check_login_status(self) -> Tuple[bool, int]:
        async with self._lock:
            return await self._check_login_status_unlocked()

    async def _check_login_status_unlocked(self) -> Tuple[bool, int]:
        if config.MOCK_MODE:
            return self._mock_logged_in, sum(int(item["qty"]) for item in self._mock_cart)
        status, final, _ = await self._fetch(BASE + "/my-account")
        # Anonymous sessions are redirected to the login page.
        logged_in = status == 200 and "/login" not in final
        count, _ = await self._minicart()
        return logged_in, count

    # ==================== Catalog & Search ====================

    async def _search_json(self, query: str, limit: int) -> List[Dict[str, Any]]:
        data = await self._fetch_json(f"{BASE}/search/results?q={quote(query, safe='')}&limit={int(limit)}")
        try:
            return parse_search_results(data)
        except ParseError as e:
            raise _upstream(f"Unexpected search response: {e}")

    async def search_products(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        async with self._lock:
            return await self._search_products_unlocked(query, limit)

    async def _search_products_unlocked(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        if config.MOCK_MODE:
            catalog = [
                {"id": "P12345", "name": "חלב תנובה 3% בקרטון 1 ליטר", "brand": "תנובה", "price_ils": 7.10, "unit": "יחידה", "image_url": "https://media.shufersal.co.il/images/products/milk_3.jpg", "in_stock": True},
                {"id": "P54321", "name": "קוטג' תנובה 5% 250 גרם", "brand": "תנובה", "price_ils": 6.50, "unit": "יחידה", "image_url": "https://media.shufersal.co.il/images/products/cottage.jpg", "in_stock": True}
            ]
            q_low = query.lower()
            matches = [p for p in catalog if q_low in p["name"].lower() or q_low in p["brand"].lower() or q_low in p["id"].lower()]
            return (matches or catalog)[:limit]
        return (await self._search_json(query, limit))[:limit]

    async def _lookup_product(self, product_id: str) -> Optional[Dict[str, Any]]:
        code = clean_code(product_id)
        if not re.fullmatch(r"\d{1,20}", code):
            return None
        return find_exact(await self._search_json(code, 20), code)

    async def get_product(self, product_id: str) -> Optional[Dict[str, Any]]:
        async with self._lock:
            return await self._get_product_unlocked(product_id)

    async def _get_product_unlocked(self, product_id: str) -> Optional[Dict[str, Any]]:
        if config.MOCK_MODE:
            return self._mock_product(product_id)
        p = await self._lookup_product(product_id)
        if not p:
            return None
        return {
            "id": p["id"],
            "name": p["name"],
            "description": p.get("description") or p["name"],
            "price_ils": p["price_ils"],
            "sale_price_ils": p.get("sale_price_ils"),
            "unit": p.get("unit", "יחידה"),
            "nutrition_url": None,
            "in_stock": p.get("in_stock", True),
        }

    # ==================== Cart (live) ====================

    async def _minicart(self) -> Tuple[int, float]:
        status, _, text = await self._fetch(BASE + "/cart/miniCart/TOTAL")
        if status != 200:
            raise _upstream(f"Cart count request returned HTTP {status}.")
        try:
            return parse_minicart(text)
        except ParseError as e:
            raise _upstream(f"Unexpected cart count response: {e}")

    async def _read_cart(self) -> Dict[str, Any]:
        """Reads the real cart: site item count and subtotal from the mini-cart,
        entries (exact entry numbers, codes, quantities) from the cart HTML."""
        count, subtotal = await self._minicart()
        status, _, html = await self._fetch(BASE + "/cart/load")
        if status != 200:
            raise _upstream(f"Cart request returned HTTP {status}.")
        try:
            entries = parse_cart_entries(html)
        except ParseError as e:
            raise _upstream(f"Unexpected cart markup: {e}")
        if count > 0 and not entries:
            raise _upstream(f"The site reports {count} cart items but none could be read.")

        items = []
        prices_complete = True
        for e in entries:
            unit = e.get("unit_price_attr")
            if unit is None:
                try:
                    prod = await self._lookup_product(e["product_id"])
                    unit = prod["price_ils"] if prod else None
                except APIException as exc:
                    if exc.status_code == 503:
                        raise
                    unit = None
            if unit is None:
                prices_complete = False
            items.append({
                "line_id": e["line_id"],
                "product_id": e["product_id"],
                "name": e["name"] or e["product_id"],
                "qty": e["qty"],
                "selling_method": e["selling_method"],
                "unit_price_ils": unit,
                "line_total_ils": round(unit * e["qty"], 2) if unit is not None else None,
            })
        # The mini-cart count must agree with what the page showed (as lines or as
        # units), otherwise the cart fingerprint could be missing hidden lines.
        units = sum(i["qty"] if i["selling_method"] == "BY_UNIT" else 1 for i in items)
        lines_complete = count == len(items) or abs(count - units) < QTY_TOLERANCE
        return {
            "items": items,
            "item_count": count,
            "lines_complete": lines_complete,
            "subtotal_ils": round(subtotal, 2),
            # The fee depends on the chosen slot; see /delivery/slots and the checkout quote.
            "delivery_fee_ils": None,
            "total_ils": round(subtotal, 2),
            "total_includes_delivery": False,
            "prices_complete": prices_complete,
            "cart_hash": cart_hash(items),
        }

    @staticmethod
    def _line_for(cart: Dict[str, Any], product_id: str) -> Optional[Dict[str, Any]]:
        code = clean_code(product_id)
        return next((i for i in cart["items"] if i["product_id"] == code), None)

    @staticmethod
    def _entry(cart: Dict[str, Any], line_id: str) -> Dict[str, Any]:
        entry = next((i for i in cart["items"] if i["line_id"] == str(line_id)), None)
        if entry is None:
            raise APIException(status_code=404, code="item_not_found", message=f"Cart line {line_id} is not in the cart.")
        return entry

    async def _post_update(self, line_id: str, qty: float, selling_method: str):
        params = urlencode({"entryNumber": line_id, "qty": _fmt_qty(qty), "sellingMethod": selling_method})
        status, _, _ = await self._fetch(
            f"{BASE}/cart/update?{params}",
            method="POST",
            body=urlencode({"quantity": _fmt_qty(qty)}),
            content_type="application/x-www-form-urlencoded; charset=UTF-8",
        )
        if status != 200:
            raise _upstream(f"Cart update returned HTTP {status}.", code="cart_write_failed")

    async def _post_add(self, product: Dict[str, Any], qty: float):
        payload = {
            "productCodePost": site_code(product["id"]),
            "productCode": site_code(product["id"]),
            "sellingMethod": product.get("selling_method") or "BY_UNIT",
            "qty": qty,
            "frontQuantity": qty,
            "comment": "",
            "affiliateCode": "",
        }
        status, _, _ = await self._fetch(
            f"{BASE}/cart/add?{CART_CONTEXT}",
            method="POST",
            body=json.dumps(payload),
            content_type="application/json",
        )
        if status != 200:
            raise _upstream(f"Add to cart returned HTTP {status}.", code="cart_write_failed")

    @staticmethod
    def _verify_qty(cart: Dict[str, Any], product_id: str, expected: float, action: str):
        line = ShufersalDriver._line_for(cart, product_id)
        actual = line["qty"] if line else 0.0
        if abs(actual - expected) > QTY_TOLERANCE:
            raise _upstream(
                f"{action} for product {clean_code(product_id)} was not applied: "
                f"expected quantity {expected}, cart shows {actual}.",
                code="cart_write_unverified",
            )

    async def get_cart(self) -> Dict[str, Any]:
        async with self._lock:
            return await self._get_cart_unlocked()

    async def _get_cart_unlocked(self) -> Dict[str, Any]:
        if config.MOCK_MODE:
            return self._calculate_mock_cart()
        return await self._read_cart()

    # ---- primitives (mock or live) ----

    async def _resolve_product(self, product_id: str) -> Dict[str, Any]:
        if config.MOCK_MODE:
            p = self._mock_product(product_id)
            return {"id": product_id, "name": p["name"], "price_ils": p["price_ils"], "selling_method": "BY_UNIT"}
        product = await self._lookup_product(product_id)
        if not product:
            raise APIException(status_code=404, code="product_not_found",
                               message=f"No product with code {clean_code(product_id)} exists on the site.")
        return product

    async def _write_qty(self, entry: Dict[str, Any], qty: float):
        if config.MOCK_MODE:
            line = next(i for i in self._mock_cart if i["line_id"] == entry["line_id"])
            if qty <= 0:
                self._mock_cart.remove(line)
            else:
                line["qty"] = qty
                line["line_total_ils"] = round(qty * line["unit_price_ils"], 2)
            return
        await self._post_update(entry["line_id"], qty, entry["selling_method"])

    async def _write_add(self, product: Dict[str, Any], qty: float):
        if config.MOCK_MODE:
            n = max([int(i["line_id"].split("_")[-1]) for i in self._mock_cart if i["line_id"].split("_")[-1].isdigit()] or [100]) + 1
            self._mock_cart.append({"line_id": f"line_{n}", "product_id": product["id"], "name": product["name"],
                                    "qty": qty, "unit_price_ils": product["price_ils"],
                                    "line_total_ils": round(qty * product["price_ils"], 2)})
            return
        await self._post_add(product, qty)

    @staticmethod
    def _resolve_line(cart: Dict[str, Any], line_id: str, product_code: Optional[str]) -> Optional[Dict[str, Any]]:
        """Maps (line_id, product_code) to the current cart line. The site
        renumbers lines after a removal, so the product code is authoritative:
        a stale line_id that now points at another product is re-resolved by
        code. Returns None when the product is not in the cart."""
        if not product_code:
            raise APIException(status_code=400, code="product_code_required",
                               message="Pass product_code with the line_id; line ids shift when lines are removed.")
        code = clean_code(product_code)
        entry = next((i for i in cart["items"] if i["line_id"] == str(line_id)), None)
        if entry and clean_code(entry["product_id"]) == code:
            return entry
        matches = [i for i in cart["items"] if clean_code(i["product_id"]) == code]
        if len(matches) > 1:
            raise APIException(status_code=409, code="line_ambiguous",
                               message=f"Product {code} appears on several cart lines; GET /cart and use the current line_id.")
        return matches[0] if matches else None

    @staticmethod
    def _qty_of(cart: Dict[str, Any], code: str) -> float:
        line = ShufersalDriver._line_for(cart, code)
        return line["qty"] if line else 0.0

    # ---- adds (planned, so retries reconcile instead of adding twice) ----

    async def add_items(self, items: List[Tuple[str, float]], clear_first: bool = False,
                        plan: Optional[Dict[str, Any]] = None, on_plan=None) -> Dict[str, Any]:
        """Adds quantities to the cart. The target state is planned first and
        handed to on_plan (the API records it under the Idempotency-Key)
        before any write. Re-applying a recorded plan reconciles: lines already
        at target are left alone, lines still at their planned starting
        quantity are written, and anything else is a 409."""
        async with self._lock:
            if plan is None:
                plan = await self._plan_add(items, clear_first)
                if on_plan:
                    on_plan(plan)
            return await self._apply_plan(plan)

    async def add_to_cart(self, product_id: str, qty: float = 1.0) -> Dict[str, Any]:
        return await self.add_items([(product_id, qty)])

    async def _plan_add(self, items: List[Tuple[str, float]], clear_first: bool) -> Dict[str, Any]:
        products = {}
        order = []
        for pid, qty in items:
            p = await self._resolve_product(pid)
            if p["id"] not in products:
                products[p["id"]] = {"product": {k: p.get(k) for k in ("id", "name", "price_ils", "selling_method")},
                                     "add": 0.0}
                order.append(p["id"])
            products[p["id"]]["add"] += float(qty)
        cart = await self._get_cart_unlocked()
        plan_items = []
        for code in order:
            before = 0.0 if clear_first else self._qty_of(cart, code)
            plan_items.append({"product": products[code]["product"], "before": before,
                               "target": round(before + products[code]["add"], 3)})
        return {"clear_first": bool(clear_first), "items": plan_items}

    async def _apply_plan(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        cart = await self._get_cart_unlocked()
        targets = {i["product"]["id"]: i["target"] for i in plan["items"]}
        if plan["clear_first"]:
            exact = ({clean_code(i["product_id"]) for i in cart["items"]} == {clean_code(c) for c in targets}
                     and all(abs(self._qty_of(cart, c) - t) <= QTY_TOLERANCE for c, t in targets.items()))
            if exact:
                return cart
            cart = await self._clear_cart_unlocked()
        for item in plan["items"]:
            code, before, target = item["product"]["id"], item["before"], item["target"]
            current = self._qty_of(cart, code)
            if abs(current - target) <= QTY_TOLERANCE:
                continue
            if abs(current - before) > QTY_TOLERANCE:
                raise APIException(
                    status_code=409, code="cart_state_changed",
                    message=f"Product {clean_code(code)} is at quantity {current}; this request expected {before} "
                            f"(or {target} once applied). GET /cart and decide before retrying.")
            line = self._line_for(cart, code)
            if line:
                await self._write_qty(line, target)
            else:
                await self._write_add(item["product"], target)
            cart = await self._get_cart_unlocked()
        for code, target in targets.items():
            self._verify_qty(cart, code, target, "Add to cart")
        return cart

    # ---- updates and removals (product code is authoritative) ----

    async def update_cart_item(self, line_id: str, qty: float, product_code: Optional[str] = None) -> Dict[str, Any]:
        async with self._lock:
            return await self._update_cart_item_unlocked(line_id, qty, product_code)

    async def _update_cart_item_unlocked(self, line_id: str, qty: float, product_code: Optional[str]) -> Dict[str, Any]:
        if qty <= 0:
            return await self._delete_cart_item_unlocked(line_id, product_code)
        cart = await self._get_cart_unlocked()
        entry = self._resolve_line(cart, line_id, product_code)
        if entry is None:
            raise APIException(status_code=404, code="item_not_found",
                               message=f"Product {clean_code(product_code)} is not in the cart.")
        if abs(entry["qty"] - qty) > QTY_TOLERANCE:
            await self._write_qty(entry, qty)
        after = await self._get_cart_unlocked()
        self._verify_qty(after, entry["product_id"], qty, "Quantity update")
        return after

    async def delete_cart_item(self, line_id: str, product_code: Optional[str] = None) -> Dict[str, Any]:
        async with self._lock:
            return await self._delete_cart_item_unlocked(line_id, product_code)

    async def _delete_cart_item_unlocked(self, line_id: str, product_code: Optional[str]) -> Dict[str, Any]:
        cart = await self._get_cart_unlocked()
        entry = self._resolve_line(cart, line_id, product_code)
        if entry is None:
            return cart  # already absent: removal is idempotent
        await self._write_qty(entry, 0)
        after = await self._get_cart_unlocked()
        self._verify_qty(after, entry["product_id"], 0.0, "Remove from cart")
        return after

    async def clear_cart(self) -> Dict[str, Any]:
        async with self._lock:
            return await self._clear_cart_unlocked()

    async def _clear_cart_unlocked(self) -> Dict[str, Any]:
        cart = await self._get_cart_unlocked()
        passes = len(cart["items"]) + 2
        for _ in range(passes):
            if not cart["items"] and not cart["item_count"]:
                return cart
            # Highest entry number first so the remaining entry numbers stay valid.
            for entry in sorted(cart["items"], key=lambda i: int(i["line_id"]) if i["line_id"].isdigit() else 0, reverse=True):
                await self._write_qty(entry, 0)
            cart = await self._get_cart_unlocked()
        if cart["items"] or cart["item_count"]:
            raise _upstream(f"Cart still has {cart['item_count']} items after clearing.", code="cart_clear_incomplete")
        return cart

    # ==================== Delivery & Payment ====================

    async def get_delivery_slots(self) -> List[Dict[str, Any]]:
        async with self._lock:
            return await self._get_delivery_slots_unlocked()

    async def _get_delivery_slots_unlocked(self) -> List[Dict[str, Any]]:
        if config.MOCK_MODE:
            return self._mock_slots()
        data = await self._fetch_json(f"{BASE}/timeSlot/preselection/getHomeDeliverySlots?amount=1.0")
        try:
            return parse_home_delivery_slots(data)
        except ParseError as e:
            raise _upstream(f"Unexpected delivery slot response: {e}")

    async def get_payment_method(self) -> Dict[str, Any]:
        async with self._lock:
            return public_payment(await self._get_payment_method_unlocked())

    async def _get_payment_method_unlocked(self) -> Dict[str, Any]:
        """Full masked record including the site's payment-info id (internal)."""
        if config.MOCK_MODE:
            return {"payment_info_id": "mock", "payment_type": "CREDIT_CARD", "brand": "Mastercard", "last4": "4242",
                    "expiry_month": "12", "expiry_year": "2028", "name_on_card": "י. י."}
        data = await self._fetch_json(f"{BASE}/checkout/payment/customer/paymentInfos")
        try:
            return parse_payment_method(data)
        except ParseError as e:
            raise APIException(status_code=404, code="payment_method_unavailable",
                               message=f"No usable saved payment method: {e}")

    # ==================== Mock helpers ====================

    def _mock_product(self, product_id: str) -> Dict[str, Any]:
        catalog = {
            "P12345": {"id": "P12345", "name": "חלב תנובה 3% בקרטון 1 ליטר", "description": "חלב מפוסטר הומוגני 3% שומן מועשר בוויטמין D",
                       "price_ils": 7.10, "sale_price_ils": None, "unit": "יחידה", "nutrition_url": None, "in_stock": True},
            "P54321": {"id": "P54321", "name": "קוטג' תנובה 5% 250 גרם", "description": "גבינת קוטג' 5% שומן עשירה בחלבון",
                       "price_ils": 6.50, "sale_price_ils": 5.90, "unit": "יחידה", "nutrition_url": None, "in_stock": True},
        }
        return dict(catalog.get(product_id) or {
            "id": product_id, "name": f"מוצר {product_id}", "description": f"תיאור פריט {product_id}",
            "price_ils": 10.0, "sale_price_ils": None, "unit": "יחידה", "nutrition_url": None, "in_stock": True,
        })

    def _calculate_mock_cart(self) -> Dict[str, Any]:
        items = [dict(i, selling_method="BY_UNIT") for i in self._mock_cart]
        subtotal = round(sum(round(i["qty"] * i["unit_price_ils"], 2) for i in items), 2)
        return {
            "items": items,
            "item_count": len(items),
            "lines_complete": True,
            "subtotal_ils": subtotal,
            "delivery_fee_ils": None,
            "total_ils": subtotal,
            "total_includes_delivery": False,
            "prices_complete": True,
            "cart_hash": cart_hash(items),
        }

    def _mock_slots(self) -> List[Dict[str, Any]]:
        from datetime import datetime, timedelta
        slots = []
        for d in range(1, 4):
            date_str = (datetime.now() + timedelta(days=d)).strftime("%Y-%m-%d")
            for part, window in (("morning", "09:00 - 13:00"), ("evening", "17:00 - 21:00")):
                slots.append({"id": f"slot_{date_str}_{part}", "date": date_str, "window": window,
                              "fee_ils": self._mock_delivery_fee, "available": not (d == 2 and part == "evening")})
        return slots

    # ==================== Checkout ====================

    async def checkout_summary(self, slot_id: str) -> Dict[str, Any]:
        async with self._lock:
            return await self._checkout_summary_unlocked(slot_id)

    async def _checkout_summary_unlocked(self, slot_id: str) -> Dict[str, Any]:
        """Read-only: the cart, the chosen slot and the masked payment method."""
        cart = await self._get_cart_unlocked()
        if not cart["items"]:
            raise APIException(status_code=409, code="cart_empty", message="The cart is empty.")
        if not cart.get("lines_complete", False):
            raise APIException(status_code=409, code="cart_incomplete",
                               message=f"The site counts {cart['item_count']} cart items but the page showed "
                                       f"{len(cart['items'])} lines; refusing to quote a partial cart.")
        slots = await self._get_delivery_slots_unlocked()
        slot = next((s for s in slots if s["id"] == str(slot_id)), None)
        if not slot or not slot["available"]:
            raise APIException(status_code=409, code="slot_unavailable",
                               message=f"Delivery slot {slot_id} is not offered or not available.")
        payment = await self._get_payment_method_unlocked()
        return {
            "items": [{k: i[k] for k in ("product_id", "name", "qty", "unit_price_ils", "line_total_ils")} for i in cart["items"]],
            "item_count": cart["item_count"],
            "cart_hash": cart["cart_hash"],
            "prices_complete": cart["prices_complete"],
            "subtotal_ils": cart["subtotal_ils"],
            "delivery_fee_ils": slot["fee_ils"],
            "estimated_total_ils": round(cart["subtotal_ils"] + slot["fee_ils"], 2),
            "slot": {k: slot[k] for k in ("id", "date", "window", "fee_ils") if k in slot},
            "payment": public_payment(payment),
        }

    CHECKOUT_STATE_JS = """() => {
        const visible = (sel) => [...document.querySelectorAll(sel)].some(e => e.offsetParent !== null);
        return {
            path: location.pathname,
            reauth: visible('.miglog-pwd-modal #j_password, #checkoutPwd, #smsCodeInput'),
            cvv: !!document.querySelector('input[name*="cvv" i], input[id*="cvv" i], iframe[src*="payme" i]'),
            threeDS: !!(window.miglog && window.miglog.threeDSecure && window.miglog.threeDSecure.enabled),
            threeDSTest: !!(window.miglog && window.miglog.threeDSecure && window.miglog.threeDSecure.isTestMode),
        };
    }"""

    async def checkout(
        self,
        *,
        slot_id: str,
        expected_cart_hash: str,
        confirm_total_ils: float,
        place_order: bool,
        watched: bool = False,
    ) -> Dict[str, Any]:
        async with self._lock:
            relaunched = False
            if watched and not config.MOCK_MODE and self._headless:
                # The persistent profile can be open in one browser only, so the
                # checkout step relaunches it headed (same session) and reverts after.
                await self._require_live()
                if not await self._start_live(headless=False):
                    raise APIException(status_code=503, code="browser_unavailable",
                                       message="Could not open a visible browser window for the watched checkout.")
                relaunched = True
            try:
                return await self._checkout_unlocked(slot_id, expected_cart_hash, confirm_total_ils, place_order, watched)
            finally:
                if relaunched:
                    await self._start_live(headless=config.HEADLESS)

    async def _page_state(self) -> Dict[str, Any]:
        return await self.page.evaluate(self.CHECKOUT_STATE_JS)

    async def _checkout_unlocked(self, slot_id, expected_cart_hash, confirm_total_ils, place_order, watched):
        """Walks checkout: verify cart -> select slot -> open checkout -> read the
        site total, then
          - place_order=False: stop (dry run);
          - watched: hand over to Or in the visible window (he clears any
            password/SMS step, the PayMe card field and 3-D Secure, and clicks the
            site's own place-order button); the service waits for the confirmation
            page and returns the order number it shows;
          - otherwise: call the site's placeOrder itself (only with no blockers).
        Never touches the cart contents."""
        summary = await self._checkout_summary_unlocked(slot_id)
        if summary["cart_hash"] != expected_cart_hash:
            raise APIException(status_code=409, code="cart_mismatch",
                               message="The cart no longer matches the expected contents.")

        if config.MOCK_MODE:
            site_total = summary["estimated_total_ils"]
            result = {"summary": summary, "site_total_ils": site_total, "blockers": [], "warnings": [],
                      "checks": {"reached_checkout": True, "three_ds_enabled": False, "cvv_field_present": False}}
            if abs(site_total - confirm_total_ils) > 0.01:
                raise APIException(status_code=409, code="total_drift", message="The live total differs from the confirmed quote.")
            if not place_order:
                return dict(result, status="dry_run", order_id=None)
            return dict(result, status="placed", order_id=f"MOCK-{expected_cart_hash[:6]}")

        blockers: List[str] = []
        warnings: List[str] = []
        # 1. Select the delivery slot on the cart (the site's own preselection call).
        status, _, text = await self._fetch(
            f"{BASE}/timeSlot/preselection/postHomeDeliverySlot",
            method="POST",
            body=json.dumps({"homeDeliveryTimeSlot": {"code": str(slot_id), "sourceOfSupply": "DIRECT"}}),
            content_type="application/json",
        )
        if status != 200:
            raise _upstream(f"Selecting the delivery slot returned HTTP {status}.", code="slot_select_failed")

        # 2. Open the checkout page the way the cart's checkout button does.
        try:
            await self.page.goto(config.SHUFERSAL_BASE_URL + BASE + "/checkout", wait_until="domcontentloaded", timeout=45000)
            await self.page.wait_for_timeout(4000)
            page_state = await self._page_state()
            if watched and page_state.get("reauth"):
                # Or enters the password / SMS code in the visible window.
                deadline = time.monotonic() + config.WATCH_TIMEOUT_SECONDS
                while page_state.get("reauth") and time.monotonic() < deadline:
                    await asyncio.sleep(config.WATCH_POLL_SECONDS)
                    page_state = await self._page_state()
        except APIException:
            raise
        except Exception as e:
            raise _upstream(f"Opening the checkout page failed: {str(e)[:120]}", code="checkout_unreachable")
        path = str(page_state.get("path") or "")
        reached = "checkout" in path
        if not reached:
            blockers.append(f"checkout page redirected to {path or 'unknown'}")
        if page_state.get("reauth"):
            blockers.append("the site asks for the account password or an SMS code before checkout")
        if page_state.get("cvv"):
            (warnings if watched else blockers).append(
                "the checkout page shows a CVV / PayMe card field; the service never types card data")

        # 3. The site's own total for the order.
        site_total = None
        if reached:
            try:
                status, _, text = await self._fetch(f"{BASE}/checkout/costSummary/direct")
                if status == 200:
                    site_total = extract_site_total(json.loads(text))
            except (APIException, ValueError):
                site_total = None
        if site_total is None:
            (warnings if watched else blockers).append("could not read the order total from the checkout page")
        elif abs(site_total - confirm_total_ils) > 0.01:
            raise APIException(status_code=409, code="total_drift",
                               message=f"The site total ({site_total} ILS) differs from confirm_total_ils.")

        result = {
            "summary": summary,
            "site_total_ils": site_total,
            "blockers": blockers,
            "warnings": warnings,
            "checks": {"reached_checkout": reached, "three_ds_enabled": bool(page_state.get("threeDS")),
                       "cvv_field_present": bool(page_state.get("cvv")), "watched": bool(watched)},
        }
        if not place_order:
            return dict(result, status="dry_run", order_id=None)

        # ---------- Real order (confirm + fresh key + enabled flag checked by the API) ----------
        if blockers:
            raise APIException(status_code=409, code="checkout_blocked",
                               message="Checkout cannot be completed: " + "; ".join(blockers))
        if watched:
            return dict(result, **(await self._await_handover()))

        jwt = ""
        if page_state.get("threeDS"):
            try:
                jwt = await self.page.evaluate(
                    "(t) => PayMe.clientData(t).then(r => (r && r.hash) || '').catch(() => '')",
                    bool(page_state.get("threeDSTest")))
            except Exception:
                jwt = ""
        status, _, text = await self._fetch(
            f"{BASE}/checkout/placeOrder?jwtPayme={quote(jwt or '', safe='')}&after3ds=false")
        try:
            outcome = classify_place_order(json.loads(text)) if status == 200 else {"outcome": "unknown"}
        except ValueError:
            outcome = {"outcome": "unknown"}

        if outcome["outcome"] == "placed":
            redirect = outcome["redirect"]
            order_id = None
            try:
                await self.page.goto(config.SHUFERSAL_BASE_URL + BASE + redirect if redirect.startswith("/") else redirect,
                                     wait_until="domcontentloaded", timeout=45000)
                order_id = parse_order_number(self.page.url, await self.page.content())
            except Exception as e:
                logger.warning(f"Order placed but confirmation page could not be read: {str(e)[:120]}")
            if order_id is None:
                order_id = parse_order_number(redirect, "")
            return dict(result, status="placed" if order_id else "placed_unconfirmed", order_id=order_id)
        if outcome["outcome"] == "three_ds_required":
            return dict(result, status="three_ds_required", order_id=None)
        if outcome["outcome"] == "rejected":
            raise APIException(status_code=409, code="order_rejected",
                               message=f"The site rejected the order ({outcome.get('code')}).")
        # The request went out but the answer is not understood: the order may exist.
        return dict(result, status="outcome_unknown", order_id=None)

    async def _await_handover(self) -> Dict[str, Any]:
        """Waits for Or to finish payment and click place-order in the visible
        window, then reads the order number from the confirmation page."""
        logger.info("Watched checkout: waiting for the place-order click in the visible browser.")
        deadline = time.monotonic() + config.WATCH_TIMEOUT_SECONDS
        looked_confirmed = False
        while time.monotonic() < deadline:
            if not self._is_alive():
                return {"status": "outcome_unknown", "order_id": None}
            try:
                url = self.page.url
                if re.search(r"confirm|thank", url, re.I) or parse_order_number(url, ""):
                    looked_confirmed = True
                    order_id = parse_order_number(url, await self.page.content())
                    if order_id:
                        return {"status": "placed", "order_id": order_id}
            except Exception:
                pass
            await asyncio.sleep(config.WATCH_POLL_SECONDS)
        return {"status": "placed_unconfirmed" if looked_confirmed else "handover_timeout", "order_id": None}


def _fmt_qty(qty: float) -> str:
    q = float(qty)
    return str(int(q)) if q.is_integer() else f"{q:.3f}".rstrip("0")


# Global driver singleton
shufersal_driver = ShufersalDriver()
