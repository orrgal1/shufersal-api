import asyncio
from contextlib import asynccontextmanager
import logging
import re
import time
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import quote, urlencode
import json

import config
from security import APIException
from checkout_diag import PostClickMonitor, save_diagnostics
from shufersal_parsers import (
    ParseError,
    cart_count_check,
    cart_hash,
    classify_place_order,
    clean_code,
    extract_site_total,
    find_exact,
    is_confirmation_url,
    parse_cart_entries,
    parse_cart_line_count,
    parse_home_delivery_slots,
    parse_minicart,
    parse_checkout_recommendations,
    parse_order_number,
    parse_payment_method,
    parse_product_page_stock,
    parse_search_results,
    parse_summary_total,
    public_payment,
    site_code,
)

logger = logging.getLogger("shufersal_driver")

BASE = "/online/he"
CART_CONTEXT = urlencode({"cartContext[openFrom]": "CATALOG", "cartContext[recommendationType]": "REGULAR"})
QTY_TOLERANCE = 0.001
SESSION_COOKIE = "JSESSIONID"
# Set by every site page when it renders: window.miglog.account.anonymous is
# false for a logged-in customer and true for a guest (verified live 2026-10-19).
LOGIN_MARKER_JS = ("() => { const a = window.miglog && window.miglog.account; "
                   "return a && typeof a.anonymous === 'boolean' ? !a.anonymous : null; }")


class _WriteRejected(APIException):
    """A write the site refused by redirecting it (HTTP 200, nothing applied).
    Cart writes retry it once with a fresh page; any other write gets a 502."""

    def __init__(self, final: str):
        super().__init__(status_code=502, code="write_rejected",
                         message=f"The site refused the request and redirected to {final.split('?')[0] or '/'}.")
        self.final = final


def _upstream(message: str, code: str = "upstream_error", status: int = 502) -> APIException:
    return APIException(status_code=status, code=code, message=message)


class ShufersalDriver:
    """
    Playwright persistent context (for the WAF, the session and cookies) driving
    the site's own XHR endpoints from inside the page. Every write is verified
    by re-reading the cart; nothing falls back to fixture data outside MOCK_MODE.
    """
    LIVE_RETRY_SECONDS = 60
    # An empty cart read is only believed from a page loaded (and checked
    # logged in) this recently in the same session; otherwise the page is
    # reloaded and the cart read again first.
    FRESH_PAGE_SECONDS = 120

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
        self._checkout_active = False
        # The server session (JSESSIONID) the page was loaded in. When the site
        # idles that session out, in-page XHRs silently get a new anonymous
        # session with an EMPTY cart (HTTP 200); see _session_read.
        self._page_session: Optional[str] = None
        self._page_loaded_at: float = 0.0
        from sms_codes import SmsCodeReader
        self.sms_reader = SmsCodeReader()
        # product code -> (monotonic time, available: True/False/None)
        self._stock_cache: Dict[str, Tuple[float, Optional[bool]]] = {}

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
            self._page_session = await self._session_id()
            self._page_loaded_at = time.monotonic()
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

    @asynccontextmanager
    async def _guard(self):
        """The driver lock for every call except checkout. While a checkout
        (above all a watched handover, which can wait many minutes) is open,
        calls fail fast with 409 instead of queueing behind it."""
        if self._checkout_active:
            raise APIException(status_code=409, code="checkout_in_progress",
                               message="A checkout is in progress; retry when it has finished.")
        async with self._lock:
            # A checkout may have started while this call waited for the lock.
            if self._checkout_active:
                raise APIException(status_code=409, code="checkout_in_progress",
                                   message="A checkout is in progress; retry when it has finished.")
            yield

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
                return { status: res.status, url: res.url, redirected: res.redirected, text: await res.text() };
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
        if method != "GET" and res.get("redirected") and final.split("?")[0].rstrip("/") != path.split("?")[0].rstrip("/"):
            # The site answers a write it refuses (above all a stale CSRF token)
            # with a redirect to the home page and HTTP 200; nothing was applied.
            raise _WriteRejected(final)
        return status, final, res.get("text") or ""

    async def _write(self, path: str, body: str, content_type: str, what: str) -> int:
        """POSTs a cart write. The CSRF token is read from the page when it
        loaded and goes stale once the site rotates the session (seen live after
        ~a day of uptime: every write was silently redirected home). A refused
        write changed nothing, so the page is reloaded for a fresh token and the
        write is sent once more; a second refusal is an error."""
        for attempt in (1, 2):
            try:
                status, _, _ = await self._fetch(path, method="POST", body=body, content_type=content_type)
                return status
            except _WriteRejected as e:
                logger.warning(f"{what} was refused by the site (redirected to {e.final.split('?')[0] or '/'}); "
                               f"attempt {attempt}, reloading the page for a fresh session token.")
                if attempt == 2:
                    raise _upstream(
                        f"{what} was refused by the site twice (redirected to {e.final.split('?')[0] or '/'}), "
                        "even with a fresh page; the session may need a new login.",
                        code="cart_write_rejected")
                # Never retry a write into a session that is not Or's (it would land in a guest cart).
                await self._refresh_session(f"{what} was refused")
        raise AssertionError("unreachable")

    async def _reload_page(self, code: str = "cart_write_failed"):
        try:
            await self.page.goto(config.SHUFERSAL_BASE_URL + BASE + "/", wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            if not self._is_alive():
                self._mark_dead(f"page unusable: {str(e)[:120]}")
                raise APIException(status_code=503, code="browser_unavailable",
                                   message="The live browser session was lost; retry shortly.")
            raise _upstream(f"Reloading the site page failed: {str(e)[:120]}", code=code)
        self._page_session = await self._session_id()
        self._page_loaded_at = time.monotonic()

    # ==================== Session (logged in, not stale) ====================

    async def _session_id(self) -> Optional[str]:
        try:
            cookies = await self.context.cookies(config.SHUFERSAL_BASE_URL.rstrip("/") + BASE + "/")
        except Exception:
            return None
        return next((c.get("value") for c in cookies if c.get("name") == SESSION_COOKIE), None)

    async def _page_logged_in(self) -> Optional[bool]:
        """The page's own login flag, as of when the page loaded. None if unreadable."""
        try:
            value = await self.page.evaluate(LOGIN_MARKER_JS)
        except Exception:
            return None
        return value if isinstance(value, bool) else None

    async def _session_problem(self) -> Optional[str]:
        """Why the session the last read ran in cannot be trusted, or None."""
        current = await self._session_id()
        if current is not None and current != self._page_session:
            return "the site session changed since the page loaded (it idled out on the server)"
        logged_in = await self._page_logged_in()
        if logged_in is not True:
            return "the page is not logged in" if logged_in is False else "the page's login state is unreadable"
        return None

    async def _refresh_session(self, why: str):
        """Reloads the page (a full navigation re-logs an idled-out session in
        from the remember-me cookie and restores the real cart) and requires
        the reloaded page to be logged in."""
        logger.warning(f"{why}; reloading the site page to restore the logged-in session.")
        await self._reload_page(code="session_refresh_failed")
        logged_in = await self._page_logged_in()
        if logged_in is not True:
            raise APIException(
                status_code=503, code="session_logged_out",
                message="The Shufersal site session is not logged in"
                        + (" (the reloaded page is a guest page)" if logged_in is False else
                           " (the reloaded page's login state could not be read)")
                        + "; its cart would be a guest cart, not Or's. Log in again in Chrome and restart the service.")

    async def _session_read(self, read, looks_empty):
        """Runs a cart read so that a guest or idled-out session can never pass
        for Or's cart. Live 2026-10-19: after ~1.5 h idle the server dropped the
        session, every in-page XHR then got a fresh anonymous session and the
        cart read 0 items with HTTP 200 until a page load restored the login.
        A read that ran in a changed or logged-out session, or that came back
        empty, is repeated once after a page reload that must be logged in; if
        the session still moves, it fails instead of answering."""
        result = await read()
        problem = await self._session_problem()
        fresh_page = time.monotonic() - self._page_loaded_at < self.FRESH_PAGE_SECONDS
        if problem is None and (fresh_page or not looks_empty(result)):
            return result
        await self._refresh_session(problem or "the cart read came back empty")
        result = await read()
        problem = await self._session_problem()
        if problem is not None:
            raise APIException(status_code=503, code="session_unstable",
                               message=f"Cannot read Or's cart: {problem}, even right after a page reload.")
        return result

    async def _fetch_json(self, path: str) -> Any:
        status, final, text = await self._fetch(path)
        if status != 200:
            raise _upstream(f"{path.split('?')[0]} returned HTTP {status}.")
        try:
            return json.loads(text)
        except ValueError:
            raise _upstream(f"{path.split('?')[0]} did not return JSON (landed on {final.split('?')[0]}).")

    # ==================== Health & Login ====================

    async def check_login_status(self) -> Tuple[bool, Optional[int]]:
        async with self._guard():
            return await self._check_login_status_unlocked()

    async def _check_login_status_unlocked(self) -> Tuple[bool, Optional[int]]:
        if config.MOCK_MODE:
            return self._mock_logged_in, sum(int(item["qty"]) for item in self._mock_cart)
        # Not /my-account: a guest is redirected to /my-account/personal-area,
        # not to /login (live 2026-10-19), so that check always said logged in.
        try:
            count, _ = await self._session_read(self._minicart, lambda r: r[0] == 0)
        except APIException as e:
            if e.code == "session_logged_out":
                return False, None
            raise
        return True, count

    # ==================== Catalog & Search ====================

    async def _search_json(self, query: str, limit: int) -> List[Dict[str, Any]]:
        data = await self._fetch_json(f"{BASE}/search/results?q={quote(query, safe='')}&limit={int(limit)}")
        try:
            return parse_search_results(data)
        except ParseError as e:
            raise _upstream(f"Unexpected search response: {e}")

    async def search_products(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        async with self._guard():
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
        async with self._guard():
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

    async def _read_cart_raw(self) -> Tuple[int, float, str]:
        count, subtotal = await self._minicart()
        status, _, html = await self._fetch(BASE + "/cart/load")
        if status != 200:
            raise _upstream(f"Cart request returned HTTP {status}.")
        return count, subtotal, html

    async def _read_cart(self, raw: Optional[Tuple[int, float, str]] = None) -> Dict[str, Any]:
        """Reads the real cart: site item count and subtotal from the mini-cart,
        entries (exact entry numbers, codes, quantities) from the cart HTML.
        `raw` is a (count, subtotal, html) read the caller already verified."""
        count, subtotal, html = raw if raw is not None else await self._session_read(
            self._read_cart_raw, lambda r: r[0] == 0 and not (parse_cart_line_count(r[2]) or 0))
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
        # miniCartCount counts BY_UNIT quantities plus the GRAMS of weighed and
        # packaged lines (verified live 2026-10-19, see parse_minicart). Any
        # disagreement with the parsed lines means the page hid lines.
        lines_complete = cart_count_check(count, items, parse_cart_line_count(html), QTY_TOLERANCE)
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
        status = await self._write(
            f"{BASE}/cart/update?{params}",
            body=urlencode({"quantity": _fmt_qty(qty)}),
            content_type="application/x-www-form-urlencoded; charset=UTF-8",
            what="Cart update",
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
        status = await self._write(
            f"{BASE}/cart/add?{CART_CONTEXT}",
            body=json.dumps(payload),
            content_type="application/json",
            what="Add to cart",
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
        async with self._guard():
            return await self._get_cart_unlocked()

    async def _get_cart_unlocked(self) -> Dict[str, Any]:
        if config.MOCK_MODE:
            cart = self._calculate_mock_cart()
            for i in cart["items"]:
                i.setdefault("available", True)
            cart.update(unavailable_items=[], availability_complete=True)
            return cart
        cart = await self._read_cart()
        await self._add_availability(cart)
        return cart

    # ---- stock (read-only) ----

    STOCK_CACHE_SECONDS = 60
    STOCK_CONCURRENCY = 6

    async def _product_available(self, code: str) -> Optional[bool]:
        """The site's stock state of one product: True, False (out of stock) or
        None (unreadable). The search JSON omits out-of-stock products entirely
        (live 2026-10-20: P_749 was missing from every search while its product
        page said outOfStock), so a code the search returns as inStock is
        available, and anything else is decided by the product page."""
        cached = self._stock_cache.get(code)
        if cached and time.monotonic() - cached[0] < self.STOCK_CACHE_SECONDS:
            return cached[1]
        available: Optional[bool] = None
        try:
            hit = find_exact(await self._search_json(code, 20), code)
            if hit and hit.get("in_stock") is True:
                available = True
            else:
                status, _, html = await self._fetch(f"{BASE}/p/{site_code(code)}")
                available = parse_product_page_stock(html) if status == 200 else None
        except APIException as e:
            if e.status_code == 503:
                raise
            available = None
        self._stock_cache[code] = (time.monotonic(), available)
        return available

    async def _add_availability(self, cart: Dict[str, Any]):
        """Sets items[].available, unavailable_items and availability_complete."""
        sem = asyncio.Semaphore(self.STOCK_CONCURRENCY)

        async def one(code):
            async with sem:
                return await self._product_available(code)

        codes = list(dict.fromkeys(i["product_id"] for i in cart["items"]))
        states = dict(zip(codes, await asyncio.gather(*(one(c) for c in codes))))
        unavailable = []
        for i in cart["items"]:
            i["available"] = states.get(i["product_id"])
            if i["available"] is False:
                unavailable.append({"line_id": i["line_id"], "product_id": i["product_id"], "name": i["name"],
                                    "qty": i["qty"], "reason": "out_of_stock"})
        cart["unavailable_items"] = unavailable
        cart["availability_complete"] = all(i["available"] is not None for i in cart["items"])

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
        async with self._guard():
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
        async with self._guard():
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
        async with self._guard():
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
        async with self._guard():
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
        async with self._guard():
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
        async with self._guard():
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
        async with self._guard():
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
            "items": [{k: i.get(k) for k in ("product_id", "name", "qty", "unit_price_ils", "line_total_ils", "available")}
                      for i in cart["items"]],
            "unavailable_items": cart.get("unavailable_items") or [],
            "availability_complete": cart.get("availability_complete", True),
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

    # /cart/cartsummary, the page the site's own flow passes through on the way
    # to checkout (verified live 2026-10-19, see _checkout_unlocked).
    CART_SUMMARY_STATE_JS = """() => {
        const btn = document.querySelector('.miglog-cart-summary-checkoutLink');
        const total = document.querySelector('.miglog-cart-summary-totalprice');
        const a = window.miglog && window.miglog.account;
        return {
            path: location.pathname,
            loggedIn: a && typeof a.anonymous === 'boolean' ? !a.anonymous : null,
            total: total ? total.textContent : null,
            checkoutUrl: btn ? (btn.getAttribute('data-url') || null) : null,
            requiresPwd: btn ? (btn.getAttribute('data-requires-pwd') || null) : null,
            threeDS: !!(window.miglog && window.miglog.threeDSecure && window.miglog.threeDSecure.enabled),
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
        unattended: bool = False,
        adult_recipient_confirmed: bool = False,
        on_place_order=None,
    ) -> Dict[str, Any]:
        async with self._guard():
            self._checkout_active = True
            relaunched = False
            try:
                if watched and not config.MOCK_MODE and self._headless:
                    # The persistent profile can be open in one browser only, so the
                    # checkout step relaunches it headed (same session) and reverts after.
                    await self._require_live()
                    relaunched = True
                    if not await self._start_live(headless=False):
                        raise APIException(status_code=503, code="browser_unavailable",
                                           message="Could not open a visible browser window for the watched checkout.")
                return await self._checkout_unlocked(slot_id, expected_cart_hash, confirm_total_ils, place_order, watched,
                                                     unattended=unattended, on_place_order=on_place_order,
                                                     adult_recipient_confirmed=adult_recipient_confirmed)
            finally:
                try:
                    if relaunched:
                        await self._start_live(headless=config.HEADLESS)
                finally:
                    self._checkout_active = False

    async def _page_state(self) -> Dict[str, Any]:
        return await self.page.evaluate(self.CHECKOUT_STATE_JS)

    async def _checkout_unlocked(self, slot_id, expected_cart_hash, confirm_total_ils, place_order, watched,
                                 unattended=False, on_place_order=None, adult_recipient_confirmed=False):
        """Walks checkout: verify cart -> select slot -> open the cart summary and
        read the site total -> try the checkout page, then
          - place_order=False: stop (dry run);
          - watched: hand over to Or in the visible window (he clicks the site's
            "מעבר לקופה" button, clears the password/SMS step, the PayMe card field
            and 3-D Secure, and clicks the site's own place-order button); the
            service waits for the confirmation page and returns its order number;
          - unattended (both flags on, watched=false): the service walks the site's
            own UI like Or would (see _unattended_place);
          - otherwise: call the site's placeOrder itself (only with no blockers).
        Never touches the cart contents.

        The site's own route (verified live 2026-10-19 from the page and its
        main JS): cart -> /cart/cartsummary, whose "מעבר לקופה" button carries
        data-url="/online/he/checkout" data-requires-pwd="always". Its click
        handler GETs /checkout/cart-order-updatable, then ALWAYS opens the
        password/SMS modal (/cart/checkout/auth) for a non-agent customer and
        only navigates to /checkout after that succeeds. Without that re-auth
        the server answers GET /checkout with 302 -> /cart -> /cart/cartsummary,
        and /checkout/costSummary/direct redirects home. So the service, which
        never logs in or types codes, cannot open the checkout page by itself:
        the site total is read from the cart summary, which prints the order
        total including the chosen slot's delivery/service fee."""
        summary = await self._checkout_summary_unlocked(slot_id)
        if summary["cart_hash"] != expected_cart_hash:
            raise APIException(status_code=409, code="cart_mismatch",
                               message="The cart no longer matches the expected contents.")

        unattended = bool(unattended and place_order and not watched)
        handled = watched or unattended  # the password/SMS and card steps are passed in the browser
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

        # 2. The cart summary page (the cart's own "go to checkout" link) and its total.
        try:
            await self.page.goto(config.SHUFERSAL_BASE_URL + BASE + "/cart/cartsummary",
                                 wait_until="domcontentloaded", timeout=45000)
            await self.page.wait_for_timeout(4000)
            cs_state = await self.page.evaluate(self.CART_SUMMARY_STATE_JS)
        except APIException:
            raise
        except Exception as e:
            raise _upstream(f"Opening the cart summary page failed: {str(e)[:120]}", code="checkout_unreachable")
        cs_path = str(cs_state.get("path") or "")
        reached_summary = cs_path.rstrip("/").endswith("/cart/cartsummary")
        summary_total = parse_summary_total(cs_state.get("total")) if reached_summary else None
        if not reached_summary:
            blockers.append(f"the cart summary page redirected to {cs_path or 'unknown'}")
        if cs_state.get("loggedIn") is not True:
            blockers.append("the site session on the cart summary page is not a logged-in customer")
        requires_pwd = cs_state.get("requiresPwd") == "always"

        # 3. The checkout page. The site redirects it back to the cart summary
        #    until the account password / SMS code was entered there.
        try:
            await self.page.goto(config.SHUFERSAL_BASE_URL + BASE + "/checkout", wait_until="domcontentloaded", timeout=45000)
            await self.page.wait_for_timeout(4000)
            page_state = await self._page_state()
        except APIException:
            raise
        except Exception as e:
            raise _upstream(f"Opening the checkout page failed: {str(e)[:120]}", code="checkout_unreachable")
        path = str(page_state.get("path") or "")
        reached = "/checkout" in path
        gated = not reached and path.rstrip("/").endswith("/cart/cartsummary")
        if gated:
            # Or passes this gate himself in a watched run; nothing else can.
            (warnings if handled else blockers).append(
                "the checkout page needs the account password or an SMS code first (the site redirects it to "
                "the cart summary); in the visible window click 'מעבר לקופה' and enter it")
        elif not reached:
            blockers.append(f"checkout page redirected to {path or 'unknown'}")
        if page_state.get("reauth"):
            (warnings if handled else blockers).append(
                "the site asks for the account password or an SMS code before checkout")
        if page_state.get("cvv"):
            (warnings if handled else blockers).append(
                "the checkout page shows a CVV / PayMe card field; the service never types card data")

        # 4. The site's own total for the order: the checkout page's cost summary
        #    when the checkout page opened, else the cart summary page's total.
        site_total, total_source = None, None
        if reached:
            try:
                status, _, text = await self._fetch(f"{BASE}/checkout/costSummary/direct")
                if status == 200:
                    site_total = extract_site_total(json.loads(text))
                    total_source = "checkout" if site_total is not None else None
            except (APIException, ValueError):
                site_total = None
        if site_total is None and summary_total is not None:
            site_total, total_source = summary_total, "cart_summary"
        if site_total is None:
            # Blocks watched runs too: the total must be checked by the service, not only by eye.
            blockers.append("could not read the order total from the cart summary or checkout page")
        elif abs(site_total - confirm_total_ils) > 0.01:
            raise APIException(status_code=409, code="total_drift",
                               message=f"The site total ({site_total} ILS) differs from confirm_total_ils.")

        result = {
            "summary": summary,
            "site_total_ils": site_total,
            "blockers": blockers,
            "warnings": warnings,
            "checks": {"reached_cart_summary": reached_summary, "reached_checkout": reached,
                       "checkout_needs_password": bool(gated or (requires_pwd and not reached)),
                       "site_total_source": total_source,
                       "three_ds_enabled": bool(page_state.get("threeDS") or cs_state.get("threeDS")),
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
        if unattended:
            return dict(result, **(await self._unattended_place(summary, confirm_total_ils, result, on_place_order,
                                                                adult_recipient_confirmed=adult_recipient_confirmed)))

        if on_place_order:
            on_place_order()  # atomic ledger re-check + "placing" mark; raises -> no request sent
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

    # ==================== Unattended checkout ====================
    #
    # Evidence (live 2026-10-20, headless copy of the profile, and the site's
    # main / checkout JS bundles):
    #   * cart summary "מעבר לקופה" = .miglog-cart-summary-checkoutLink;
    #   * it can first open #creditBenefitProductModal. Its "המשך לתשלום" button
    #     POSTs /salesPromotions/addToCart/grid (adds items), so only its close
    #     button (.btnClose) is ever clicked; closing it continues to the re-auth;
    #   * re-auth = #forgottenPwdModal, tabs a.tab[href="#passwordTab"|"#smsTab"];
    #     #sendSmsBtn loads the OTP form (POST /otp/send/jsp, SMS to the account
    #     phone), code input #smsCodeInput (6 digits, enabled on "input"),
    #     submit .js-otp-submit-passwordModal -> POST /otp/checkout/validate ->
    #     location = /miglog-checkout; a wrong code shows .otpError;
    #   * checkout page (Vue, inferred from its JS): place-order button
    #     a.btnConfirm ("אישור תשלום") -> placeOrder?jwtPayme=..&after3ds=false;
    #     a 3-D Secure answer opens .creditCardOtpPopup with an <iframe> of the
    #     bank's page; on success the site calls placeOrder again (after3ds=true)
    #     and navigates to the confirmation page; errors open .generalErrorChat.
    #   * after the click (checkout bundle checkoutm8e8d1.js, read 2026-10-20):
    #     AppRoot.placeOrder is a no-op while the store isLoading or the button
    #     is disabled; client validation failures render inline
    #     ([role=alert].colorError, .whiteErrorBg .error) or as toastr toasts
    #     (#toast-container) and send nothing; otherwise .spinner-container gets
    #     .active, PayMe.clientData() builds the jwt, GET placeOrder is sent and
    #     its answer is: success -> location = redirect; allocationTimeout ->
    #     .generalErrorChat; a 3DS link -> .creditCardOtpPopup iframe (the bank
    #     page decides SMS code vs approval in the issuer's app; success writes
    #     sessionStorage, the site re-sends placeOrder with after3ds=true);
    #     an error array -> inline / toast errors. The site opens no window
    #     (no window.open in the bundle); popups are still watched for.
    UA_POST_STATE_JS = """() => { /*ua-post*/
        const vis = (e) => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
        const txt = (e) => (e.innerText || e.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 200);
        const uniq = (a) => [...new Set(a.filter(Boolean))].slice(0, 6);
        const toasts = uniq([...document.querySelectorAll('#toast-container .toast, .toast-error, .toast-warning')].filter(vis).map(txt));
        const alerts = uniq([...document.querySelectorAll('[role=alert], .colorError, .whiteErrorBg .error, .otpError')]
            .filter(e => vis(e) && !e.closest('#forgottenPwdModal')).map(txt));
        const modals = [...document.querySelectorAll('.modal')].filter(m => vis(m) && !m.classList.contains('modalFromSide'))
            .map(m => ({popup: m.id || String(m.className).slice(0, 60), text: txt(m)}));
        const strip = (v) => { try { const u = new URL(v, location.href); return u.origin + u.pathname; } catch (x) { return ''; } };
        const iframes = [...document.querySelectorAll('iframe')].filter(vis).map(f => strip(f.getAttribute('src') || '')).slice(0, 8);
        const spin = document.querySelector('.spinner-container');
        return {toasts, alerts, modals, iframes, spinner: !!spin && spin.classList.contains('active'),
                threeDS: vis(document.querySelector('.creditCardOtpPopup'))};
    }"""
    UA_STATE_JS = """() => { /*ua-state*/
        const vis = (e) => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
        const q = (s) => document.querySelector(s);
        const popups = [...document.querySelectorAll('.modal')].filter(m => vis(m) && m.id !== 'forgottenPwdModal'
            && !m.classList.contains('modalFromSide') && !m.classList.contains('creditCardOtpPopup')
            && !m.classList.contains('generalErrorChat')).map(m => m.id || String(m.className).slice(0, 60));
        const err = [...document.querySelectorAll('.otpError')].find(e => vis(e) && !e.classList.contains('hidden'));
        const gen = [...document.querySelectorAll('.generalErrorChat')].find(vis);
        const cvv = [...document.querySelectorAll('input[name*="cvv" i], input[id*="cvv" i], .form-group.cvv input')].find(vis);
        const a = window.miglog && window.miglog.account;
        return {
            path: location.pathname, href: location.href,
            loggedIn: a && typeof a.anonymous === 'boolean' ? !a.anonymous : null,
            checkoutLink: vis(q('.miglog-cart-summary-checkoutLink')),
            pwdModal: vis(q('#forgottenPwdModal')), smsInput: vis(q('#smsCodeInput')),
            strongAuth: vis(q('#strongAuthModal')), otpError: err ? err.textContent.trim().slice(0, 120) : null,
            popups, placeButton: vis(q('a.btnConfirm')), threeDS: vis(q('.creditCardOtpPopup')),
            errorPopup: gen ? gen.textContent.trim().replace(/\\s+/g, ' ').slice(0, 160) : null,
            cvvSelector: cvv ? (cvv.id ? '#' + cvv.id : (cvv.name ? 'input[name="' + cvv.name + '"]' : '.form-group.cvv input')) : null,
        };
    }"""
    UA_CLICK_JS = """(sel) => { /*ua-click*/
        const vis = (e) => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
        const el = [...document.querySelectorAll(sel)].find(e => vis(e) && !e.disabled && !e.classList.contains('disabled'));
        if (!el) return false;
        el.click();
        return true;
    }"""
    UA_FILL_JS = """([sel, value]) => { /*ua-fill*/
        const el = document.querySelector(sel);
        if (!el) return false;
        el.focus();
        el.value = value;
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        return true;
    }"""
    # The only popup ever closed: the credit-benefit offer, by its own X
    # (live markup 2026-10-20: <button type="button" aria-label="סגור" class="btnClose">
    # in #creditBenefitProductModal's header). Its "המשך לתשלום" button adds
    # items, so nothing inside a form or with add/continue wording is clicked.
    UA_CLOSE_BENEFIT_JS = """() => { /*ua-close-benefit*/
        const vis = (e) => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
        const m = document.querySelector('#creditBenefitProductModal');
        if (!vis(m)) return null;
        const btns = [...m.querySelectorAll('button.btnClose[aria-label="סגור"]')].filter(vis);
        if (btns.length !== 1) return null;
        const b = btns[0];
        const words = (b.textContent || '') + ' ' + (b.getAttribute('aria-label') || '');
        if (b.closest('form') || /המשך|הוסף|הוספה|לתשלום|continue|add/i.test(words) || words.trim() !== 'סגור') return null;
        b.click();
        return '#creditBenefitProductModal button.btnClose[aria-label="סגור"]';
    }"""
    # The checkout's missing-products popup (live 2026-10-20, Vue, id
    # "missingProducts", data-backdrop="static": no X, no Escape). Per missing
    # line <li data-product-code="P_749"> with .productName and the buttons
    # "להחלפה" (swap in a substitute) / "לא תודה" (drop the line), plus a bottom
    # "המשך לתשלום" enabled once every line is decided. Every button changes
    # the order, so the run only READS it and stops.
    UA_MISSING_JS = """() => { /*ua-missing*/
        const vis = (e) => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
        const m = [...document.querySelectorAll('#missingProducts')].find(vis);
        if (!m) return null;
        const items = [...m.querySelectorAll('[data-product-code]')].map(li => ({
            code: li.getAttribute('data-product-code'),
            name: ((li.querySelector('.productName') || {}).textContent || '').trim(),
        }));
        const buttons = [...m.querySelectorAll('button')].filter(vis).map(b => (b.textContent || '').trim()).filter(Boolean);
        const text = (m.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 400);
        return {items, buttons: [...new Set(buttons)], text};
    }"""
    # The checkout's informational card notice (live 2026-10-20, class
    # "modal messageCreditCardModal messagesModal centerModal", no id). Text:
    #   לתשומת ליבך, / כרטיס אשראי שופרסל שלך לא מסומן / המבצעים למחזיקי כרטיס
    #   אשראי שופרסל - לא יכובדו / בכדי לממש את המבצעים יש להחליף לתשלום באשראי שופרסל
    # and a "הבנתי" ("understood") button. It is dismissed ONLY when it is the
    # single visible such modal, its clickable controls are one plain "הבנתי"
    # button (not in a form, no link target) plus at most one empty X, and its
    # text, apart from "הבנתי", is exactly those sentences. With click=false it
    # only checks. Live markup at 08:36 2026-10-20 (it blocked that morning's run):
    #   <div class="modal-dialog"><button type="button" aria-label="סגור" class="btnClose"></button>
    #   <div class="messagesContainer">...<div class="btnContainer"><button type="button" class="btn radius">
    #   הבנתי <span class="sr-only">הבנתי</span></button></div></div></div>
    # i.e. an empty X closer next to "הבנתי", whose textContent reads "הבנתי הבנתי".
    # Both only close the notice; "הבנתי" is the one clicked.
    UA_CREDIT_NOTICE_JS = """(click) => { /*ua-credit-notice*/
        const vis = (e) => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
        const norm = (t) => (t || '').replace(/\\s+/g, ' ').trim();
        const ms = [...document.querySelectorAll('.modal.messageCreditCardModal')].filter(vis);
        if (ms.length !== 1) return {ok: false, why: 'modals:' + ms.length};
        const m = ms[0];
        const ctrls = [...m.querySelectorAll('button, a, input, select, textarea, [role="button"], [onclick]')].filter(vis);
        const top = ctrls.filter(c => !ctrls.some(o => o !== c && o.contains(c)));
        const isX = (c) => c.tagName === 'BUTTON' && (c.getAttribute('type') || '').toLowerCase() === 'button'
            && c.classList.contains('btnClose') && norm(c.getAttribute('aria-label')) === 'סגור'
            && norm(c.textContent) === '' && c.children.length === 0 && !c.closest('form') && !c.disabled
            && !c.hasAttribute('onclick') && !c.hasAttribute('href') && !c.hasAttribute('formaction');
        const xs = top.filter(isX), others = top.filter(c => !isX(c));
        if (xs.length > 1 || others.length !== 1) return {ok: false, why: 'controls:' + top.length};
        const b = others[0];
        const label = norm((b.textContent || '') + ' ' + (b.value || ''));
        const aria = norm(b.getAttribute('aria-label'));
        // "הבנתי", or "הבנתי הבנתי" from a screen-reader copy inside it (.sr-only).
        const srOk = label === 'הבנתי הבנתי' && [...b.querySelectorAll('.sr-only')].length === 1
            && norm(b.querySelector('.sr-only').textContent) === 'הבנתי';
        if ((label !== 'הבנתי' && !srOk) || (aria && aria !== 'הבנתי')) return {ok: false, why: 'label'};
        if (b.closest('form') || b.disabled) return {ok: false, why: 'form'};
        const tag = b.tagName;
        if (tag === 'A' && !/^(#|javascript:void\\(0\\);?$|$)/.test(b.getAttribute('href') || '')) return {ok: false, why: 'href'};
        if (tag === 'INPUT' && (b.getAttribute('type') || '').toLowerCase() !== 'button') return {ok: false, why: 'input'};
        if (tag === 'BUTTON' && (b.getAttribute('type') || '').toLowerCase() === 'submit') return {ok: false, why: 'submit'};
        let rest = norm(m.innerText).split('הבנתי').join(' ');
        const known = ['לתשומת ליבך', 'כרטיס אשראי שופרסל שלך לא מסומן', 'המבצעים למחזיקי כרטיס אשראי שופרסל',
                       'לא יכובדו', 'בכדי לממש את המבצעים יש להחליף לתשלום באשראי שופרסל'];
        for (const k of known) {
            if (!rest.includes(k)) return {ok: false, why: 'text'};
            rest = rest.split(k).join(' ');
        }
        if (!/^[\\s,.:!\\-–]*$/.test(rest)) return {ok: false, why: 'text'};
        if (click) b.click();
        return {ok: true};
    }"""
    # The checkout's age-confirmation block (live 2026-10-20 09:22, section 4
    # "העדפות משלוח", shown when the cart holds alcohol, tobacco or medicine):
    #   <fieldset class="over-18 errorForm"><div class="subTitleContainer"><legend class="topTitle">
    #   <img src=".../icon-over18-error.png"> אופן קבלת המשלוח</legend> <span>קבלת משלוח המכיל
    #   אלכוהול, סיגריות, טבק או תרופות, תתאפשר רק ע"י אדם בגיל 18 ומעלה</span></div>
    #   <div class="radioContainer v2 row space"><label for="true" aria-labelledby="אופן קבלת המשלוח"
    #   class="btn col-xs-6"><div class="checkboxContainer"><label class="btn activeCheckbox">
    #   <span class="checkboxPic"></span></label></div> <strong id="over18text" class="radioText">
    #   יהיה בבית אדם בגיל 18 ומעלה לקבל את המשלוח</strong></label></div></fieldset>
    # There is no <input>: the site's CSS (stylem8e8d1.css) draws the tick for
    # ".over-18 .radioContainer .btn.active ... .checkboxPic:after", so "checked"
    # is the class "active" on the label that holds the text, and ".over-18.errorForm"
    # is the red required-field box. The box is found by its exact label text
    # (never by position); with tick=true and the box unchecked, its own square
    # (.checkboxContainer .activeCheckbox inside that label) is clicked once.
    ADULT_LABEL = "יהיה בבית אדם בגיל 18 ומעלה לקבל את המשלוח"
    UA_ADULT_JS = """(tick) => { /*ua-adult*/
        const vis = (e) => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
        const norm = (t) => (t || '').replace(/\\s+/g, ' ').trim();
        const LABEL = 'יהיה בבית אדם בגיל 18 ומעלה לקבל את המשלוח';
        const hits = [...document.querySelectorAll('body *')].filter(e => norm(e.textContent) === LABEL
            && ![...e.children].some(c => norm(c.textContent) === LABEL));
        const labels = [...new Set(hits.map(e => e.closest('label')).filter(Boolean))].filter(vis);
        if (labels.length === 0) return {present: false};
        if (labels.length > 1) return {present: true, ambiguous: true, count: labels.length, checked: false};
        const label = labels[0];
        const input = label.querySelector('input[type=checkbox]');
        const aria = label.getAttribute('aria-checked') || (label.querySelector('[aria-checked]') || {getAttribute: () => null}).getAttribute('aria-checked');
        const checked = input ? !!input.checked : (aria ? aria === 'true' : label.classList.contains('active'));
        const box = label.closest('.over-18') || label.closest('fieldset');
        const out = {present: true, count: 1, checked, error: !!(box && box.classList.contains('errorForm')),
                     text: box ? norm(box.querySelector('.subTitleContainer') ? box.querySelector('.subTitleContainer').innerText : '').slice(0, 300) : ''};
        if (tick && !checked) {
            const target = input || label.querySelector('.checkboxContainer .activeCheckbox') || label;
            target.click();
            out.clicked = true;
        }
        return out;
    }"""
    # Visible required-field / validation errors on the checkout page: the red
    # ".errorForm" box (live 2026-10-20) and, from the checkout bundle,
    # [role=alert].colorError, .whiteErrorBg .error and .error-text. Hidden
    # elements and empty error containers are ignored.
    UA_REQUIREMENTS_JS = """() => { /*ua-requirements*/
        const vis = (e) => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
        const norm = (t) => (t || '').replace(/\\s+/g, ' ').trim();
        const out = [];
        for (const e of document.querySelectorAll('.errorForm, [role=alert].colorError, .whiteErrorBg .error, .error-text')) {
            if (!vis(e) || e.closest('#forgottenPwdModal')) continue;
            let t = '';
            if (e.classList.contains('errorForm')) {
                const head = e.querySelector('.subTitleContainer, legend');
                t = norm(head ? head.innerText : e.innerText);
                if (!t) t = 'a required field is marked (' + String(e.className).slice(0, 60) + ')';
            } else {
                t = norm(e.innerText);
            }
            if (t && !out.includes(t.slice(0, 300))) out.push(t.slice(0, 300));
        }
        return out.slice(0, 6);
    }"""
    # The visible popups' own text, for the error when a run stops on one.
    UA_POPUP_TEXT_JS = """() => { /*ua-popup-text*/
        const vis = (e) => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
        return [...document.querySelectorAll('.modal, .generalErrorChat')].filter(m => vis(m) && m.id !== 'forgottenPwdModal')
            .map(m => ({popup: m.id || String(m.className).slice(0, 60),
                        text: (m.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 300)}));
    }"""
    # Inside the bank's 3-D Secure frame: the single visible code input.
    # appWait: the bank page asks to approve in the issuer's app (no code field).
    UA_FRAME_OTP_JS = """() => { /*ua-3ds-probe*/
        const vis = (e) => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
        const inputs = [...document.querySelectorAll('input')].filter(e => vis(e) && !e.disabled
            && ['text', 'tel', 'password', 'number', ''].includes((e.getAttribute('type') || '').toLowerCase()));
        const text = ((document.body && document.body.innerText) || '').replace(/\\s+/g, ' ').trim();
        const appWait = /אפליקצי|באפליקציה|ממתין לאישור|ממתינים לאישור|אשר.{0,20}(בטלפון|בנייד)|push|app/i.test(text);
        return {inputs: inputs.length, appWait: inputs.length === 0 && appWait, text: text.slice(0, 160)};
    }"""
    UA_FRAME_SUBMIT_JS = """(code) => { /*ua-3ds-submit*/
        const vis = (e) => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
        const inputs = [...document.querySelectorAll('input')].filter(e => vis(e) && !e.disabled
            && ['text', 'tel', 'password', 'number', ''].includes((e.getAttribute('type') || '').toLowerCase()));
        if (inputs.length !== 1) return 'inputs:' + inputs.length;
        const el = inputs[0];
        const form = el.form;
        if (!form) return 'no-form';
        const bad = /ביטול|בטל|cancel|שלח שוב|שליחה חוזרת|קוד חדש|resend|again/i;
        const label = (b) => ((b.textContent || '') + ' ' + (b.value || '') + ' ' + (b.getAttribute('aria-label') || '')).trim();
        const cands = [...form.querySelectorAll('button, input[type=submit]')].filter(b => vis(b) && !b.disabled && !bad.test(label(b)));
        const submits = cands.filter(b => (b.getAttribute('type') || (b.tagName === 'BUTTON' ? 'submit' : '')).toLowerCase() === 'submit');
        const explicit = submits.filter(b => (b.getAttribute('type') || '').toLowerCase() === 'submit');
        const pick = explicit.length === 1 ? explicit[0] : (submits.length === 1 ? submits[0] : null);
        if (!pick) return 'no-submit:' + submits.length;
        el.focus(); el.value = code;
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        pick.click();
        return 'clicked';
    }"""

    def _ua_log(self, steps: List[str], step: str):
        """A short, non-secret progress line (never a code, CVV or card number)."""
        steps.append(step)
        logger.info(f"unattended checkout: {step}")

    async def _ua_state(self) -> Dict[str, Any]:
        return await self.page.evaluate(self.UA_STATE_JS)

    async def _ua_sleep(self):
        await asyncio.sleep(config.UNATTENDED_POLL_SECONDS)

    async def _ua_popup_fail(self, steps: List[str], message: str) -> APIException:
        """checkout_blocked on a popup, with the popups' own text in the message and details."""
        try:
            texts = await self.page.evaluate(self.UA_POPUP_TEXT_JS) or []
        except Exception:
            texts = []
        shown = "; ".join(f"{t.get('popup')}: \"{t.get('text')}\"" for t in texts if t.get("text"))
        return self._ua_fail(steps, "checkout_blocked", message + (f" Popup text: {shown}" if shown else ""),
                             details={"popups": texts} if texts else None)

    def _ua_fail(self, steps: List[str], code: str, message: str,
                 details: Optional[Dict[str, Any]] = None) -> APIException:
        """Before the place-order click: a clean failure, no order was placed."""
        self._ua_log(steps, f"stopped before placing the order: {code}")
        logger.info(f"unattended checkout: block reason: {message[:300]}")
        return APIException(status_code=409, code=code, message=message, details=details)

    @staticmethod
    def _on_checkout_page(path: str) -> bool:
        p = (path or "").rstrip("/")
        return p.endswith("/miglog-checkout") or p.endswith("/checkout") or "/checkout/multi" in p

    async def _ua_sms_reauth(self, steps: List[str]):
        """The re-auth modal: choose SMS, send the code, read it via sms-api, enter it."""
        from sms_codes import SmsCodeError
        await self.page.evaluate(self.UA_CLICK_JS, '#forgottenPwdModal a.tab[href="#smsTab"]')
        await self._ua_sleep()
        try:
            base = await self.sms_reader.baseline()
        except SmsCodeError as e:
            raise self._ua_fail(steps, e.code, str(e))
        if not await self.page.evaluate(self.UA_CLICK_JS, "#sendSmsBtn"):
            raise self._ua_fail(steps, "reauth_failed", "The SMS option of the re-auth window could not be chosen.")
        self._ua_log(steps, "re-auth: SMS code requested")
        deadline = time.monotonic() + config.UNATTENDED_STEP_SECONDS
        while not (await self._ua_state()).get("smsInput"):
            if time.monotonic() > deadline:
                raise self._ua_fail(steps, "reauth_failed", "The SMS code field did not appear.")
            await self._ua_sleep()
        try:
            code = await self.sms_reader.wait_for_code("login", base)
        except SmsCodeError as e:
            raise self._ua_fail(steps, e.code, str(e))
        await self.page.evaluate(self.UA_FILL_JS, ["#smsCodeInput", code])
        code = None
        await self._ua_sleep()
        if not await self.page.evaluate(self.UA_CLICK_JS, "#forgottenPwdModal .js-otp-submit-passwordModal"):
            raise self._ua_fail(steps, "reauth_failed", "The re-auth confirm button stayed disabled.")
        self._ua_log(steps, "re-auth: SMS code entered and confirmed")

    async def _ua_missing_products(self, steps: List[str]) -> APIException:
        """The missing-products popup is open: name the lines, click nothing.
        The list comes from the checkout's own GET /checkout/recommendation/direct
        (what the popup renders); the popup's markup is the fallback."""
        items: List[Dict[str, Any]] = []
        try:
            status, _, text = await self._fetch(f"{BASE}/checkout/recommendation/direct")
            if status == 200:
                items = parse_checkout_recommendations(json.loads(text))
        except (APIException, ParseError, ValueError):
            items = []
        popup = None
        try:
            popup = await self.page.evaluate(self.UA_MISSING_JS)
        except Exception:
            popup = None
        if not items and popup:
            items = [{"product_id": clean_code(i.get("code")), "name": i.get("name") or clean_code(i.get("code")),
                      "reason": "out_of_stock", "qty": None, "line_id": None, "replaceable": None}
                     for i in popup.get("items") or [] if clean_code(i.get("code"))]
        named = ", ".join(f"{i['name']} ({i['product_id']})" for i in items) or "unreadable"
        self._ua_log(steps, f"missing-products popup: {named}")
        for i in items:  # the next GET /cart must not serve a cached "available"
            self._stock_cache.pop(i["product_id"], None)
        details = {"unavailable_items": items,
                   "popup_options": (popup or {}).get("buttons") or [],
                   "popup_text": (popup or {}).get("text")}
        return self._ua_fail(
            steps, "products_unavailable",
            f"The site cannot supply {len(items) or 'some'} cart line(s) for this slot: {named}. "
            "Nothing was clicked in its popup and no order was placed; remove or replace the line(s) "
            "in the cart, then get a new quote.",
            details=details)

    @staticmethod
    def _is_credit_notice(popups: List[str]) -> bool:
        return len(popups) == 1 and "messageCreditCardModal" in str(popups[0]).split()

    async def _ua_dismiss_credit_notice(self, steps: List[str], popups: List[str]):
        """Clicks "הבנתי" on the card notice when every check holds; otherwise
        checkout_blocked, as for any other popup."""
        check = await self.page.evaluate(self.UA_CREDIT_NOTICE_JS, False)
        if not (check or {}).get("ok"):
            raise await self._ua_popup_fail(steps,
                                            f"An unexpected popup is open on the checkout page: {popups} "
                                            f"(not the plain card notice: {(check or {}).get('why')}).")
        done = await self.page.evaluate(self.UA_CREDIT_NOTICE_JS, True)
        if not (done or {}).get("ok"):
            raise self._ua_fail(steps, "checkout_blocked", "The card notice popup changed before it could be closed.")
        deadline = time.monotonic() + config.UNATTENDED_STEP_SECONDS
        while True:
            await self._ua_sleep()
            try:
                left = (await self._ua_state()).get("popups") or []
            except Exception:
                left = popups
            if not any("messageCreditCardModal" in str(p).split() for p in left):
                break
            if time.monotonic() > deadline:
                raise self._ua_fail(steps, "checkout_blocked", "The card notice popup did not close.")
        self._ua_log(steps, "closed the credit-card notice popup via its 'הבנתי' button")

    async def _ua_close_benefit_popup(self, steps: List[str], popups: List[str], dismissed: int):
        """Closes the credit-benefit popup by its exact X; any other popup stops
        the run before the place-order click."""
        if "missingProducts" in popups:
            raise await self._ua_missing_products(steps)
        others = [p for p in popups if p != "creditBenefitProductModal"]
        if others:
            raise await self._ua_popup_fail(steps, f"An unexpected popup is open: {others}.")
        if dismissed >= 3:
            raise self._ua_fail(steps, "checkout_blocked", "The credit-benefit popup does not close.")
        clicked = await self.page.evaluate(self.UA_CLOSE_BENEFIT_JS)
        if not clicked:
            raise self._ua_fail(steps, "checkout_blocked", "The credit-benefit popup has no safe close button.")
        self._ua_log(steps, f"closed the credit-benefit popup via {clicked}")

    async def _ua_reach_checkout(self, steps: List[str]) -> Dict[str, Any]:
        """Cart summary -> "מעבר לקופה" -> popups -> SMS re-auth -> checkout page."""
        await self.page.goto(config.SHUFERSAL_BASE_URL + BASE + "/cart/cartsummary",
                             wait_until="domcontentloaded", timeout=45000)
        await self.page.wait_for_timeout(4000)
        state = await self._ua_state()
        if not state.get("checkoutLink"):
            raise self._ua_fail(steps, "checkout_unreachable", "The 'מעבר לקופה' button is not on the cart summary page.")
        if not await self.page.evaluate(self.UA_CLICK_JS, ".miglog-cart-summary-checkoutLink"):
            raise self._ua_fail(steps, "checkout_unreachable", "The 'מעבר לקופה' button could not be clicked.")
        self._ua_log(steps, "clicked 'מעבר לקופה'")
        reauthed, dismissed = False, 0
        deadline = time.monotonic() + config.UNATTENDED_STEP_SECONDS + config.SMS_WAIT_SECONDS
        while time.monotonic() < deadline:
            await self._ua_sleep()
            try:
                state = await self._ua_state()
            except Exception:
                continue  # the page is navigating
            if self._on_checkout_page(state.get("path")):
                self._ua_log(steps, "reached the checkout page")
                return state
            if state.get("strongAuth"):
                raise self._ua_fail(steps, "reauth_failed", "The site asks for strong authentication; not automated.")
            if state.get("otpError"):
                raise self._ua_fail(steps, "reauth_failed", "The site rejected the SMS code.")
            if state.get("pwdModal"):
                if reauthed:
                    continue  # waiting for the site to navigate after the code
                await self._ua_sms_reauth(steps)
                reauthed = True
                continue
            if state.get("popups"):
                await self._ua_close_benefit_popup(steps, state["popups"], dismissed)
                dismissed += 1
        raise self._ua_fail(steps, "checkout_unreachable", "The checkout page did not open in time.")

    async def _ua_verify_slot(self, steps: List[str], summary: Dict[str, Any]):
        """The checkout page's selected delivery slot must be the quoted slot.
        Endpoint from the checkout JS (TimeSlot service: x="/checkout/timeSlot",
        selectedTimeSlot -> getSelectedTimeSlot?...; its answer's `code` is the slot)."""
        want = str((summary.get("slot") or {}).get("id") or "")
        status, _, text = await self._fetch(
            f"{BASE}/checkout/timeSlot/getSelectedTimeSlot?sourceOfSupply=DIRECT&vendorCode=&region="
            "&amount=1.0&supportsShipping=true")
        try:
            got = str((json.loads(text) or {}).get("code") or "") if status == 200 else ""
        except (ValueError, AttributeError):
            got = ""
        if not want or got != want:
            raise self._ua_fail(steps, "checkout_blocked",
                                f"The checkout page's delivery slot ({got or 'unreadable'}) is not the quoted slot ({want}).")
        self._ua_log(steps, f"delivery slot verified ({got})")

    async def _ua_requirements(self) -> List[str]:
        try:
            return [str(t) for t in (await self.page.evaluate(self.UA_REQUIREMENTS_JS) or []) if t]
        except Exception:
            return []

    async def _ua_adult_recipient(self, steps: List[str], confirmed: bool):
        """The age-confirmation checkbox (see UA_ADULT_JS): ticked once when Or
        confirmed an adult will be home; otherwise stop before the click."""
        state = await self.page.evaluate(self.UA_ADULT_JS, False) or {}
        if not state.get("present") or state.get("checked"):
            return
        if state.get("ambiguous"):
            raise self._ua_fail(steps, "checkout_requirement",
                                f"The checkout page shows {state.get('count')} age-confirmation checkboxes; none was ticked.")
        if not confirmed:
            raise self._ua_fail(
                steps, "adult_recipient_required",
                "The cart holds alcohol, cigarettes, tobacco or medicine, and the checkout requires confirming that "
                f"an adult 18+ will be home to receive the delivery (\"{self.ADULT_LABEL}\"). Confirm with Or that an "
                "adult will be home, then submit again with adult_recipient_confirmed=true. No order was placed.",
                details={"requirement": state.get("text") or None, "checkbox_label": self.ADULT_LABEL})
        done = await self.page.evaluate(self.UA_ADULT_JS, True) or {}
        if not done.get("clicked") and not done.get("checked"):
            raise self._ua_fail(steps, "checkout_requirement", "The age-confirmation checkbox could not be ticked.")
        self._ua_log(steps, f"ticked the age-confirmation checkbox \"{self.ADULT_LABEL}\" "
                            "(adult_recipient_confirmed=true)")
        deadline = time.monotonic() + config.UNATTENDED_STEP_SECONDS
        while True:
            await self._ua_sleep()
            now = await self.page.evaluate(self.UA_ADULT_JS, False) or {}
            if now.get("checked"):
                break
            if time.monotonic() > deadline:
                raise self._ua_fail(steps, "checkout_requirement",
                                    "The age-confirmation checkbox did not show as ticked after one click; "
                                    "it was not clicked again.")
        self._ua_log(steps, "age-confirmation checkbox verified as ticked")

    async def _ua_requirement_guard(self, steps: List[str], confirmed: bool):
        """Right before the click: an unticked age box or any visible
        required-field error stops the run (the site would not send the order)."""
        adult = await self.page.evaluate(self.UA_ADULT_JS, False) or {}
        if adult.get("present") and not adult.get("checked"):
            if not confirmed:
                await self._ua_adult_recipient(steps, confirmed)
            raise self._ua_fail(steps, "checkout_requirement",
                                "The age-confirmation checkbox is not ticked right before the place-order click.")
        errors = await self._ua_requirements()
        if errors:
            raise self._ua_fail(steps, "checkout_requirement",
                                "The checkout page shows a required field that is not filled: " + "; ".join(errors)
                                + ". The place-order button was not clicked.",
                                details={"requirements": errors})

    async def _unattended_place(self, summary, confirm_total_ils, result, on_place_order,
                                adult_recipient_confirmed: bool = False) -> Dict[str, Any]:
        from sms_codes import SmsCodeError
        steps: List[str] = []
        result["checks"]["unattended_steps"] = steps
        warnings = result["warnings"]
        if confirm_total_ils > config.MAX_ORDER_ILS + 1e-9:
            raise self._ua_fail(steps, "order_over_cap", f"The order total exceeds SHUFERSAL_MAX_ORDER_ILS ({config.MAX_ORDER_ILS}).")
        self._ua_log(steps, "start")
        try:
            await self._ua_reach_checkout(steps)
            # The checkout app renders asynchronously; wait for its place-order button.
            deadline = time.monotonic() + config.UNATTENDED_STEP_SECONDS
            notices = 0
            while True:
                try:
                    state = await self._ua_state()
                except Exception:
                    state = {}
                if state.get("errorPopup"):
                    raise self._ua_fail(steps, "checkout_blocked", f"The checkout page shows an error: {state['errorPopup']}")
                if "missingProducts" in (state.get("popups") or []):
                    raise await self._ua_missing_products(steps)
                if state.get("popups") and self._is_credit_notice(state["popups"]) and notices < 2:
                    await self._ua_dismiss_credit_notice(steps, state["popups"])
                    notices += 1
                    continue
                if state.get("popups"):
                    raise await self._ua_popup_fail(steps,
                                                    f"An unexpected popup is open on the checkout page: {state['popups']}.")
                if state.get("placeButton"):
                    break
                if time.monotonic() > deadline:
                    raise self._ua_fail(steps, "checkout_blocked", "The checkout page's place-order button did not appear.")
                await self._ua_sleep()
            await self._ua_adult_recipient(steps, adult_recipient_confirmed)
            # The checks below run again after a popup dismissed at their end.
            while True:
                # Re-verify on the checkout page itself: the cart and the site's own total.
                # The Vue checkout page may not carry window.miglog.account, so the
                # usual login-marker check (which would reload the home page) is not
                # used here: the read must run in one unchanged session and the
                # cart must hash-match the quoted cart (a guest cart cannot).
                sid_before = await self._session_id()
                raw = await self._read_cart_raw()
                sid_after = await self._session_id()
                if not sid_before or sid_before != sid_after:
                    raise self._ua_fail(steps, "checkout_blocked", "The site session changed while re-reading the cart.")
                cart = await self._read_cart(raw=raw)
                if cart["cart_hash"] != summary["cart_hash"] or not cart.get("lines_complete", False):
                    raise self._ua_fail(steps, "cart_mismatch", "The cart changed on the way to the checkout page.")
                self._ua_log(steps, "cart re-verified on the checkout page")
                await self._ua_verify_slot(steps, summary)
                status, _, text = await self._fetch(f"{BASE}/checkout/costSummary/direct")
                try:
                    checkout_total = extract_site_total(json.loads(text)) if status == 200 else None
                except ValueError:
                    checkout_total = None
                if checkout_total is None:
                    raise self._ua_fail(steps, "checkout_blocked", "Could not read the total on the checkout page.")
                result["site_total_ils"] = checkout_total
                result["checks"]["site_total_source"] = "checkout"
                if abs(checkout_total - confirm_total_ils) > 0.01:
                    raise self._ua_fail(steps, "total_drift", f"The checkout page total ({checkout_total} ILS) differs from confirm_total_ils.")
                if checkout_total > config.MAX_ORDER_ILS + 1e-9:
                    raise self._ua_fail(steps, "order_over_cap", "The checkout total exceeds SHUFERSAL_MAX_ORDER_ILS.")
                self._ua_log(steps, f"checkout total verified ({checkout_total} ILS)")
                state = await self._ua_state()
                if not self._on_checkout_page(state.get("path")) or not state.get("placeButton"):
                    raise self._ua_fail(steps, "checkout_blocked", "Left the checkout page while verifying it.")
                # Popups the checkout app opened while it was being verified (the
                # missing-products one renders after the page's own data loads).
                if "missingProducts" in (state.get("popups") or []):
                    raise await self._ua_missing_products(steps)
                if state.get("popups") and self._is_credit_notice(state["popups"]) and notices < 2:
                    # Dismissed after the checks above: verify everything again
                    # (cart, slot, total vs confirm_total_ils) before any click.
                    await self._ua_dismiss_credit_notice(steps, state["popups"])
                    notices += 1
                    continue
                if state.get("popups") or state.get("errorPopup"):
                    raise await self._ua_popup_fail(steps,
                                                    f"A popup opened on the checkout page: {state.get('popups') or state.get('errorPopup')}.")
                break
            if state.get("cvvSelector"):
                cvv = config.SHUFERSAL_CVV or ""
                if not re.fullmatch(r"\d{3,4}", cvv):
                    raise self._ua_fail(steps, "checkout_not_configured", "The checkout asks for the CVV but SHUFERSAL_CVV is not set.")
                await self.page.evaluate(self.UA_FILL_JS, [state["cvvSelector"], cvv])
                self._ua_log(steps, "entered the saved card's CVV")
            await self._ua_requirement_guard(steps, adult_recipient_confirmed)
            base = await self.sms_reader.baseline()
        except APIException:
            raise
        except SmsCodeError as e:
            raise self._ua_fail(steps, e.code, str(e))
        except Exception as e:
            raise self._ua_fail(steps, "checkout_unreachable", f"Unattended checkout failed before placing the order ({type(e).__name__}).")

        # ---------- The place-order click: at most once; from here every failure is "unknown" ----------
        if on_place_order:
            # Atomically re-checks the ledger for unresolved attempts and marks
            # this one "placing"; raises (no click) if anything is unresolved.
            try:
                on_place_order()
            except APIException as e:
                self._ua_log(steps, f"stopped before placing the order: {e.code}")
                raise
        self._ua_log(steps, "clicking the place-order button (once)")
        before = await self._ua_post_state()
        noted = [0]

        def note(line: str):
            # Every post-click event goes to the log; the first 40 also to the steps.
            logger.info(f"unattended checkout: {line}")
            if noted[0] < 40:
                steps.append(line)
            noted[0] += 1

        monitor = PostClickMonitor(self.page, self.context, note)
        monitor.attach()
        outcome: Dict[str, Any] = {"status": "outcome_unknown", "order_id": None}
        try:
            try:
                clicked = await self.page.evaluate(self.UA_CLICK_JS, "a.btnConfirm")
            except Exception:
                clicked = None
            if clicked is False:
                # The click handler never ran (no visible enabled button): nothing was sent.
                self._ua_log(steps, "place-order button was not clickable")
                warnings.append("the place-order button was not clickable; the outcome is treated as unknown")
                return outcome
            try:
                outcome = await self._ua_await_outcome(steps, warnings, base, summary, confirm_total_ils, monitor, before)
            except Exception as e:
                self._ua_log(steps, f"error after the click: {type(e).__name__}")
                warnings.append("an error occurred after the place-order click; check the site before ordering again")
            if outcome.get("status") != "not_sent":
                return outcome
        finally:
            monitor.detach()
            if outcome.get("status") != "placed" and not getattr(monitor, "diag_saved", False):
                await self._ua_diag(steps, outcome.get("status") or "unknown", monitor)
        # The site's own validation stopped the click and nothing was sent.
        errors = outcome.get("requirements") or []
        raise APIException(
            status_code=409, code="checkout_requirement",
            message="The site's checkout form blocked the place-order click (no order request was sent, no order "
                    "was placed): " + "; ".join(errors),
            details={"requirements": errors, "place_order_sent": False})

    async def _ua_post_state(self) -> Dict[str, Any]:
        try:
            return await self.page.evaluate(self.UA_POST_STATE_JS) or {}
        except Exception:
            return {}

    async def _ua_diag(self, steps, tag: str, monitor=None, extra=None):
        """Masked screenshot + sanitized DOM + post-click events to config.DIAG_DIR."""
        try:
            prefix = await save_diagnostics(self.page, tag, monitor, extra)
        except Exception as e:
            prefix = None
            logger.info(f"unattended checkout: diagnostics failed ({type(e).__name__})")
        if monitor is not None:
            monitor.diag_saved = True
        if prefix:
            self._ua_log(steps, f"diagnostics saved to {prefix}.*")
        return prefix

    async def _ua_three_ds_frames(self, monitor=None) -> List[Any]:
        """Candidate bank frames: the .creditCardOtpPopup iframe (and its child
        frames), and every frame of any window opened after the click."""
        frames: List[Any] = []
        try:
            handle = await self.page.query_selector(".creditCardOtpPopup iframe")
            top = await handle.content_frame() if handle else None
        except Exception:
            top = None
        if top is not None:
            frames += [top] + list(getattr(top, "child_frames", []) or [])
        for popup in (monitor.popups if monitor else []):
            try:
                if not popup.is_closed():
                    frames += list(popup.frames)
            except Exception:
                continue
        return frames

    def _payment_window_open(self, state: Dict[str, Any], monitor=None) -> bool:
        if state.get("threeDS"):
            return True
        for popup in (monitor.popups if monitor else []):
            try:
                if not popup.is_closed():
                    return True
            except Exception:
                continue
        return False

    async def _ua_three_ds(self, steps, base, summary, total, monitor=None) -> str:
        """The bank's 3-D Secure step. Enters the issuer's SMS code in the single
        code field: "submitted"; the bank asks for approval in its app (no code
        field, app wording on 3 probes): "app_wait"; anything else: "failed"."""
        from sms_codes import SmsCodeError
        frame = None
        app_probes = 0
        deadline = time.monotonic() + config.UNATTENDED_STEP_SECONDS
        while frame is None and time.monotonic() < deadline:
            app_seen = False
            for f in await self._ua_three_ds_frames(monitor):
                try:
                    probe = await f.evaluate(self.UA_FRAME_OTP_JS)
                except Exception:
                    continue
                if probe.get("inputs") == 1:
                    frame = f
                    break
                app_seen = app_seen or bool(probe.get("appWait"))
            if frame is not None:
                break
            app_probes = app_probes + 1 if app_seen else 0
            if app_probes >= 3:
                self._ua_log(steps, "3-D Secure: the bank asks to approve the payment in its app; waiting for the site")
                return "app_wait"
            await self._ua_sleep()
        if frame is None:
            self._ua_log(steps, "3-D Secure: no single code field found in the bank's window")
            return "failed"
        self._ua_log(steps, "3-D Secure: waiting for the bank's SMS code")
        last4 = (summary.get("payment") or {}).get("last4")
        try:
            code = await self.sms_reader.wait_for_code("3ds", base, amount=total, last4=last4)
        except SmsCodeError as e:
            self._ua_log(steps, f"3-D Secure: {e.code}")
            return "failed"
        outcome = await frame.evaluate(self.UA_FRAME_SUBMIT_JS, code)
        code = None
        self._ua_log(steps, f"3-D Secure: code entered ({outcome})")
        return "submitted" if outcome == "clicked" else "failed"

    async def _ua_abandon_payment(self, steps, warnings, monitor, why: str):
        """A bank approval is still open while the run gives up. Leaving the
        checkout page (GET of the cart summary; closing any bank window) drops
        the page's 3-D Secure listener, so a late approval cannot make the site
        re-send placeOrder. Never clicks anything on the page."""
        if monitor is not None and monitor.in_flight():
            # A placeOrder request is on the wire: let it settle before leaving.
            settle = time.monotonic() + config.UNATTENDED_STEP_SECONDS
            while monitor.in_flight() and time.monotonic() < settle:
                await self._ua_sleep()
        for popup in (monitor.popups if monitor else []):
            try:
                if not popup.is_closed():
                    await popup.close()
            except Exception:
                pass
        try:
            await self.page.goto(config.SHUFERSAL_BASE_URL + BASE + "/cart/cartsummary",
                                 wait_until="domcontentloaded", timeout=45000)
            left = True
        except Exception:
            left = False
        self._ua_log(steps, f"{why}; left the checkout page so a late bank approval cannot place the order"
                     + ("" if left else " (navigation failed)"))
        warnings.append(f"{why}. The checkout page was left so a late approval cannot place the order; "
                        "if the bank still shows a prompt for this payment, decline it.")

    async def _ua_await_outcome(self, steps, warnings, base, summary, total, monitor=None, before=None) -> Dict[str, Any]:
        """After the click: log what the page does, handle 3-D Secure once
        (SMS code or app approval), stop early on a rejection, and read the
        order number from the real confirmation page only."""
        start = time.monotonic()
        deadline = start + config.UNATTENDED_RESULT_SECONDS
        grace = min(10.0, config.UNATTENDED_RESULT_SECONDS / 2)
        three_ds_done = False
        looked_confirmed = False
        pending = False
        spinner = None
        before = before or {}
        seen_msgs = set(before.get("toasts") or []) | set(before.get("alerts") or [])
        seen_modals = {m.get("popup") for m in (before.get("modals") or [])}
        shown: List[str] = []
        unknown = {"status": "outcome_unknown", "order_id": None}
        while time.monotonic() < deadline:
            await self._ua_sleep()
            if not self._is_alive():
                warnings.append("the browser closed after the place-order click")
                return unknown
            url = self.page.url
            if monitor:
                monitor.check_url(url)
            if is_confirmation_url(url):
                looked_confirmed = True
                order_id = parse_order_number(url, await self.page.content())
                if order_id:
                    self._ua_log(steps, f"confirmation page read: order {order_id}")
                    return {"status": "placed", "order_id": order_id}
                continue
            try:
                state = await self._ua_state()
            except Exception:
                continue  # navigating
            post = await self._ua_post_state()
            for m in (post.get("toasts") or []) + (post.get("alerts") or []):
                if m not in seen_msgs:
                    seen_msgs.add(m)
                    shown.append(m)
                    self._ua_log(steps, f"the site shows a message: {m[:160]}")
            for m in post.get("modals") or []:
                if m.get("popup") not in seen_modals:
                    seen_modals.add(m.get("popup"))
                    self._ua_log(steps, f"a popup opened: {m.get('popup')}: {str(m.get('text') or '')[:120]}")
            if "spinner" in post:
                now = bool(post["spinner"])
                if now != spinner and (spinner is not None or now):
                    self._ua_log(steps, "the site's spinner is " + ("shown" if now else "hidden"))
                spinner = now
            pending = self._payment_window_open(state, monitor)
            if state.get("errorPopup"):
                self._ua_log(steps, "the site showed an error after the click")
                warnings.append(f"the site showed an error after the place-order click: {state['errorPopup']}")
                return unknown
            result = monitor.last_result() if monitor else None
            if (result and result.get("kind") in ("errors", "server_payment_error", "request_failed")
                    and not monitor.in_flight() and not pending):
                detail = ", ".join(result.get("errors") or []) or result.get("kind")
                self._ua_log(steps, f"the site rejected the place-order request ({detail})")
                warnings.append(f"the site answered the place-order request with an error ({detail})"
                                + (f": {'; '.join(shown)[:300]}" if shown else "")
                                + "; the order was most likely not placed, check the site's order list")
                return unknown
            if (monitor is not None and monitor.place_order_requests == 0 and not monitor.in_flight()
                    and not post.get("spinner") and not pending and not three_ds_done
                    and self._on_checkout_page(state.get("path"))
                    and time.monotonic() - start >= min(15.0, config.UNATTENDED_RESULT_SECONDS / 2)):
                # Certain nothing went out: no placeOrder request in ~15 s, no spinner,
                # no bank window, still on the checkout page, and a required field shown.
                errors = await self._ua_requirements()
                if errors and monitor.place_order_requests == 0:
                    self._ua_log(steps, "the site's form validation blocked the click; no place-order request was "
                                        "sent: " + "; ".join(errors)[:200])
                    return {"status": "not_sent", "order_id": None, "requirements": errors}
            if (shown and monitor and monitor.place_order_requests == 0 and not post.get("spinner")
                    and not pending and time.monotonic() - start >= grace):
                self._ua_log(steps, "the site showed a message and sent no place-order request")
                warnings.append("the site stopped the order before sending it: " + "; ".join(shown)[:300])
                return unknown
            if pending and not three_ds_done:
                three_ds_done = True
                self._ua_log(steps, "3-D Secure challenge shown" if state.get("threeDS")
                             else "a new window opened after the click; looking for the bank's page in it")
                how = await self._ua_three_ds(steps, base, summary, total, monitor)
                if how == "failed":
                    warnings.append("the 3-D Secure step could not be completed automatically")
                    await self._ua_diag(steps, "three-ds-failed", monitor, {"post_state": post})
                    await self._ua_abandon_payment(steps, warnings, monitor, "the 3-D Secure step was not completed")
                    return unknown
        self._ua_log(steps, "no confirmation page in time")
        result = monitor.last_result() if monitor else None
        if monitor is not None:
            if monitor.place_order_requests == 0:
                warnings.append("no place-order request was seen after the click"
                                + (f"; the site showed: {'; '.join(shown)[:300]}" if shown else ""))
            elif result and result.get("kind") == "success":
                looked_confirmed = True
                warnings.append("the site accepted the place-order request but the confirmation page was not read")
        warnings.append("no confirmation page was seen after the place-order click; check the site's order list")
        if pending or (monitor is not None and self._payment_window_open({}, monitor)):
            await self._ua_diag(steps, "timeout-bank-pending", monitor)
            await self._ua_abandon_payment(steps, warnings, monitor,
                                           "a 3-D Secure / bank approval was still open when the wait ended")
        return {"status": "placed_unconfirmed" if looked_confirmed else "outcome_unknown", "order_id": None}

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
                if is_confirmation_url(url):
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
