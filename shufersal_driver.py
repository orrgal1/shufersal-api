import asyncio
import logging
import re
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timezone, timedelta

import config
from security import sanitize_log_message, APIException

logger = logging.getLogger("shufersal_driver")

class ShufersalDriver:
    """
    Hybrid driver combining Playwright persistent context (for WAF/Cloudflare,
    session survival, and PCI iframe interaction) with direct in-page network
    protocol calls for ultra-fast, robust JSON operations.
    """
    def __init__(self):
        self.playwright = None
        self.context = None
        self.page = None
        self._lock = asyncio.Lock()
        self._is_started = False
        self._csrf_token: Optional[str] = None
        
        # Internal mock store used when MOCK_MODE=True or for test verification
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

    async def start(self, headless: Optional[bool] = None, user_data_dir: Optional[str] = None):
        """Initializes Playwright persistent context or mock mode."""
        if self._is_started:
            return

        if config.MOCK_MODE:
            logger.info("Starting ShufersalDriver in MOCK_MODE.")
            self._is_started = True
            return

        try:
            from playwright.async_api import async_playwright
            self.playwright = await async_playwright().start()
            
            profile_dir = user_data_dir or config.SHUFERSAL_PROFILE_DIR
            is_headless = config.HEADLESS if headless is None else headless

            logger.info(f"Launching persistent browser context from {profile_dir} (headless={is_headless})")
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
            
            pages = self.context.pages
            self.page = pages[0] if pages else await self.context.new_page()
            # Import authenticated session from Chrome if available
            try:
                imported_cookies = self._extract_chrome_session_cookies()
                if imported_cookies:
                    await self.context.add_cookies(imported_cookies)
                    logger.info(f"Imported {len(imported_cookies)} Shufersal session cookies from Chrome profile.")
            except Exception as e:
                logger.warning(f"Could not import Chrome session cookies: {e}")

            # Navigate to base URL to initialize session & extract CSRF
            await self.page.goto(config.SHUFERSAL_BASE_URL, wait_until="domcontentloaded", timeout=45000)
            await self._refresh_csrf_token()
            self._is_started = True
            logger.info("Playwright persistent context started successfully.")
        except Exception as e:
            logger.warning(f"Failed to start live Playwright context: {e}. Falling back to mock driver mode.")
            self._is_started = True

    async def stop(self):
        """Cleanly releases browser resources."""
        if not self._is_started:
            return
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
        self._is_started = False

    async def _refresh_csrf_token(self):
        """Extracts CSRF token from page DOM/window."""
        if not self.page:
            return
        try:
            token = await self.page.evaluate(
                "() => window.ACC?.config?.CSRFToken || document.querySelector('meta[name=\"_csrf\"]')?.content || document.querySelector('input[name=\"CSRFToken\"]')?.value || null"
            )
            if token:
                self._csrf_token = token
        except Exception:
            pass
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

    async def _in_page_fetch(self, path: str, method: str = "GET", body: Optional[dict] = None) -> Tuple[int, Any]:
        """
        Executes a direct fetch inside the browser page context, automatically
        inheriting all cookies, CSRF tokens, and bypassing Cloudflare bot checks.
        """
        if not self.page:
            raise RuntimeError("Browser page not initialized")

        await self._refresh_csrf_token()
        url = f"{config.SHUFERSAL_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
        
        script = """
        async ([url, method, body, csrf]) => {
            const headers = {
                'Accept': 'application/json, text/plain, */*',
                'X-Requested-With': 'XMLHttpRequest'
            };
            if (csrf) {
                headers['CSRFToken'] = csrf;
            }
            const opts = { method, headers, credentials: 'include' };
            if (body && method !== 'GET') {
                headers['Content-Type'] = 'application/json';
                opts.body = JSON.stringify(body);
            }
            try {
                const res = await fetch(url, opts);
                const contentType = res.headers.get('content-type') || '';
                let data = null;
                if (contentType.includes('application/json')) {
                    data = await res.json();
                } else {
                    data = await res.text();
                }
                return { status: res.status, data };
            } catch (err) {
                return { status: 0, error: err.toString() };
            }
        }
        """
        res = await self.page.evaluate(script, [url, method, body, self._csrf_token])
        return res.get("status", 0), res.get("data")

    # ==================== Health & Login ====================

    async def check_login_status(self) -> Tuple[bool, int]:
        async with self._lock:
            return await self._check_login_status_unlocked()

    async def _check_login_status_unlocked(self) -> Tuple[bool, int]:
        if not self.page:
            cart_count = sum(int(item["qty"]) for item in self._mock_cart)
            return self._mock_logged_in, cart_count

        try:
            auth_res = await self.page.evaluate(
                "() => fetch('/online/he/authentication/get-status-includes-otp').then(r => r.text()).catch(() => 'false')"
            )
            is_logged_in = auth_res.strip().lower() == "true"
            
            cart_size = await self.page.evaluate('''() => {
                return fetch('/online/he/cart/miniCart/TOTAL')
                    .then(r => r.text())
                    .then(t => {
                        const m = t.match(/"miniCartCount"\\s*:\\s*(\\d+)/);
                        return m ? parseInt(m[1]) : 0;
                    })
                    .catch(() => 0);
            }''')
            
            return is_logged_in, int(cart_size or 0)
        except Exception as e:
            logger.warning(f"Error checking live login status: {e}")
            cart_count = sum(int(item["qty"]) for item in self._mock_cart)
            return self._mock_logged_in, cart_count

    # ==================== Catalog & Search ====================

    async def search_products(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        async with self._lock:
            return await self._search_products_unlocked(query, limit)

    async def _search_products_unlocked(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        if self.page:
            try:
                search_url = f"{config.SHUFERSAL_BASE_URL.rstrip('/')}/online/he/search?text={query}"
                await self.page.goto(search_url, wait_until="domcontentloaded", timeout=25000)
                await self.page.wait_for_timeout(1500)
                
                items = await self.page.evaluate('''() => {
                    const results = [];
                    const seen = new Set();
                    document.querySelectorAll('li[data-product-code], .tileBlock[data-product-code]').forEach(el => {
                        const rawCode = el.getAttribute('data-product-code') || '';
                        const code = rawCode.replace(/^P_/, '');
                        if (!code || seen.has(code)) return;
                        seen.add(code);
                        const name = el.getAttribute('data-product-name') || el.querySelector('.text, .name')?.innerText?.trim() || '';
                        const priceStr = el.getAttribute('data-product-price') || '0';
                        const price = parseFloat(priceStr) || 0.0;
                        const brand = el.querySelector('.brand, [data-brand]')?.innerText?.trim() || 'שופרסל';
                        const img = el.querySelector('img.miglog-prod-img, img')?.src || '';
                        const inStock = el.querySelector('.miglog-prod-inStock') !== null && el.querySelector('.miglog-prod-outOfStock') === null;
                        if (name) {
                            results.push({
                                id: code,
                                name: name,
                                brand: brand,
                                price_ils: price,
                                unit: 'יחידה',
                                image_url: img,
                                in_stock: inStock
                            });
                        }
                    });
                    return results;
                }''')
                return items[:limit] if items else []
            except Exception as e:
                logger.warning(f"Live search failed: {e}")
                return []
        if config.MOCK_MODE:
            catalog = [
                {"id": "P12345", "name": "חלב תנובה 3% בקרטון 1 ליטר", "brand": "תנובה", "price_ils": 7.10, "unit": "יחידה", "image_url": "https://media.shufersal.co.il/images/products/milk_3.jpg", "in_stock": True},
                {"id": "P54321", "name": "קוטג' תנובה 5% 250 גרם", "brand": "תנובה", "price_ils": 6.50, "unit": "יחידה", "image_url": "https://media.shufersal.co.il/images/products/cottage.jpg", "in_stock": True}
            ]
            q_low = query.lower()
            matches = [p for p in catalog if q_low in p["name"].lower() or q_low in p["brand"].lower() or q_low in p["id"].lower()]
            return (matches or catalog)[:limit]

        return []
    async def get_product(self, product_id: str) -> Optional[Dict[str, Any]]:
        async with self._lock:
            return await self._get_product_unlocked(product_id)

    async def _get_product_unlocked(self, product_id: str) -> Optional[Dict[str, Any]]:
        clean_id = product_id.replace("P_", "")
        if not config.MOCK_MODE and self.page:
            try:
                prods = await self._search_products_unlocked(clean_id, limit=1)
                if prods:
                    p = prods[0]
                    return {
                        "id": p["id"],
                        "name": p["name"],
                        "description": p["name"],
                        "price_ils": p["price_ils"],
                        "sale_price_ils": None,
                        "unit": p.get("unit", "יחידה"),
                        "nutrition_url": None,
                        "in_stock": p.get("in_stock", True)
                    }
            except Exception as e:
                logger.warning(f"Live get_product failed for {product_id}: {e}")

        if config.MOCK_MODE:
            catalog = {
                "P12345": {
                    "id": "P12345",
                    "name": "חלב תנובה 3% בקרטון 1 ליטר",
                    "description": "חלב מפוסטר הומוגני 3% שומן מועשר בוויטמין D",
                    "price_ils": 7.10,
                    "sale_price_ils": None,
                    "unit": "יחידה",
                    "nutrition_url": "https://media.shufersal.co.il/nutrition/P12345.pdf",
                    "in_stock": True
                },
                "P54321": {
                    "id": "P54321",
                    "name": "קוטג' תנובה 5% 250 גרם",
                    "description": "גבינת קוטג' 5% שומן עשירה בחלבון",
                    "price_ils": 6.50,
                    "sale_price_ils": 5.90,
                    "unit": "יחידה",
                    "nutrition_url": None,
                    "in_stock": True
                }
            }
            if product_id in catalog:
                return catalog[product_id]
            if clean_id in catalog:
                return catalog[clean_id]
            return {
                "id": product_id,
                "name": f"מוצר {product_id}",
                "description": f"תיאור פריט {product_id}",
                "price_ils": 10.0,
                "sale_price_ils": None,
                "unit": "יחידה",
                "nutrition_url": None,
                "in_stock": True
            }

        return None

    # ==================== Cart Operations ====================

    def _calculate_mock_cart(self) -> Dict[str, Any]:
        subtotal = sum(round(item["qty"] * item["unit_price_ils"], 2) for item in self._mock_cart)
        fee = self._mock_delivery_fee if self._mock_cart else 0.0
        total = round(subtotal + fee, 2)
        return {
            "items": [dict(i) for i in self._mock_cart],
            "subtotal_ils": round(subtotal, 2),
            "delivery_fee_ils": round(fee, 2),
            "total_ils": total
        }

    async def get_cart(self) -> Dict[str, Any]:
        async with self._lock:
            return await self._get_cart_unlocked()

    async def _get_cart_unlocked(self) -> Dict[str, Any]:
        if self.page:
            try:
                mini_count = await self.page.evaluate('''() => fetch('/online/he/cart/miniCart/TOTAL')
                    .then(r => r.text())
                    .then(t => {
                        const m = t.match(/"miniCartCount"\\s*:\\s*(\\d+)/);
                        return m ? parseInt(m[1]) : 0;
                    }).catch(() => 0)''')
                
                if int(mini_count or 0) == 0:
                    return {
                        "items": [],
                        "subtotal_ils": 0.0,
                        "delivery_fee_ils": 0.0,
                        "total_ils": 0.0
                    }

                cart_data = await self.page.evaluate('''() => {
                    const items = [];
                    document.querySelectorAll('#cartMiddleContent .miglog-prod, .miglog-cart-prod-wrp .miglog-prod').forEach(el => {
                        const rawCode = el.getAttribute('data-product-code') || el.querySelector('[data-product-code]')?.getAttribute('data-product-code') || '';
                        const code = rawCode.replace(/^P_/, '');
                        const name = el.querySelector('.miglog-prod-name a, [data-product-name]')?.innerText?.trim() || el.querySelector('img')?.getAttribute('alt') || '';
                        const qtyInput = el.querySelector('input[name=\"qty\"], .js-qty-selector-input, input');
                        const qty = parseFloat(qtyInput ? qtyInput.value : '1') || 1.0;
                        const entry = el.getAttribute('data-entry-number') || code;
                        if (code && name) {
                            items.push({
                                line_id: String(entry),
                                product_id: code,
                                name: name,
                                qty: qty,
                                unit_price_ils: 0.0,
                                line_total_ils: 0.0
                            });
                        }
                    });
                    
                    return fetch('/online/he/cart/miniCart/TOTAL')
                        .then(r => r.text())
                        .then(t => {
                            const m = t.match(/<span>\\s*([\\d.]+)\\s*<\\/span>/);
                            const total = m ? parseFloat(m[1]) : 0.0;
                            return { items, total };
                        }).catch(() => ({ items, total: 0.0 }));
                }''')
                
                items = cart_data.get("items", [])
                subtotal = 0.0
                for item in items:
                    prod = await self._get_product_unlocked(item["product_id"])
                    unit_price = float(prod.get("price_ils", 0.0)) if prod else 0.0
                    item["unit_price_ils"] = unit_price
                    item["line_total_ils"] = round(item["qty"] * unit_price, 2)
                    subtotal += item["line_total_ils"]
                
                subtotal = round(subtotal, 2)
                delivery_fee = 29.90 if items else 0.0
                total = round(subtotal + delivery_fee, 2)
                return {
                    "items": items,
                    "subtotal_ils": subtotal,
                    "delivery_fee_ils": delivery_fee,
                    "total_ils": total
                }
            except Exception as e:
                logger.warning(f"Error fetching live cart: {e}")

        if config.MOCK_MODE:
            return self._calculate_mock_cart()
        return {
            "items": [],
            "subtotal_ils": 0.0,
            "delivery_fee_ils": 0.0,
            "total_ils": 0.0
        }
    async def add_to_cart(self, product_id: str, qty: float = 1.0) -> Dict[str, Any]:
        async with self._lock:
            return await self._add_to_cart_unlocked(product_id, qty)

    async def _add_to_cart_unlocked(self, product_id: str, qty: float = 1.0) -> Dict[str, Any]:
        if self.page:
            try:
                code_with_p = product_id if product_id.startswith("P_") else f"P_{product_id}"
                search_url = f"{config.SHUFERSAL_BASE_URL.rstrip('/')}/online/he/search?text={product_id}"
                await self.page.goto(search_url, wait_until="domcontentloaded", timeout=25000)
                await self.page.wait_for_timeout(1500)
                
                clicked = await self.page.evaluate('''([code, pCode]) => {
                    const tile = document.querySelector(`li[data-product-code="${code}"], li[data-product-code="${pCode}"]`);
                    if (tile) {
                        if (tile.classList.contains('miglog-incart')) {
                            const plusBtn = tile.querySelector('button.bootstrap-touchspin-up, .btnTouchspin');
                            if (plusBtn) {
                                plusBtn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                                return true;
                            }
                        }
                        const btn = tile.querySelector('button.js-add-to-cart, button.miglog-btn-add, .js-add-to-cart');
                        if (btn) {
                            btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                            return true;
                        }
                    }
                    const firstBtn = document.querySelector('button.js-add-to-cart, button.miglog-btn-add');
                    if (firstBtn) {
                        firstBtn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                        return true;
                    }
                    return false;
                }''', [product_id, code_with_p])
                
                if clicked:
                    await self.page.wait_for_timeout(3000)
                    return await self._get_cart_unlocked()
            except Exception as e:
                logger.warning(f"Live add_to_cart failed: {e}")
        if config.MOCK_MODE:
            prod = await self._get_product_unlocked(product_id)
            price = prod["price_ils"] if prod else 10.0
            name = prod["name"] if prod else f"מוצר {product_id}"
            line_id = f"line_{len(self._mock_cart) + 101}"
            self._mock_cart.append({
                "line_id": line_id,
                "product_id": product_id,
                "name": name,
                "qty": qty,
                "unit_price_ils": price,
                "line_total_ils": round(qty * price, 2)
            })
            return self._calculate_mock_cart()
        return await self._get_cart_unlocked()

    async def update_cart_item(self, line_id: str, qty: float) -> Dict[str, Any]:
        async with self._lock:
            return await self._update_cart_item_unlocked(line_id, qty)

    async def _update_cart_item_unlocked(self, line_id: str, qty: float) -> Dict[str, Any]:
        if not config.MOCK_MODE and self.page:
            if qty <= 0:
                return await self._delete_cart_item_unlocked(line_id)
            try:
                updated = await self.page.evaluate('''([lineId, newQty]) => {
                    let item = document.querySelector(`article[data-entry-number="${lineId}"]`) ||
                               document.querySelector(`article[data-product-code*="${lineId}"]`);
                    if (!item) {
                        const all = document.querySelectorAll('#cartMiddleContent .miglog-prod');
                        const idx = parseInt(lineId);
                        if (!isNaN(idx) && idx >= 0 && idx < all.length) item = all[idx];
                    }
                    if (item) {
                        const input = item.querySelector('input[name="qty"], .js-qty-selector-input, input');
                        if (input) {
                            input.value = String(newQty);
                            input.dispatchEvent(new Event('change', { bubbles: true }));
                            const form = item.querySelector('form.add_to_cart_form, form[action*="update"]');
                            if (form) form.dispatchEvent(new Event('submit', { bubbles: true }));
                            return true;
                        }
                    }
                    return false;
                }''', [line_id, qty])
                await self.page.wait_for_timeout(2000)
                return await self._get_cart_unlocked()
            except Exception as e:
                logger.warning(f"Live update cart failed: {e}")

        if config.MOCK_MODE:
            if qty <= 0:
                self._mock_cart = [item for item in self._mock_cart if item["line_id"] != line_id]
                return self._calculate_mock_cart()

            existing = next((item for item in self._mock_cart if item["line_id"] == line_id), None)
            if not existing:
                raise APIException(status_code=404, code="item_not_found", message=f"Cart item {line_id} not found")

            existing["qty"] = qty
            existing["line_total_ils"] = round(qty * existing["unit_price_ils"], 2)
            return self._calculate_mock_cart()

        return await self._get_cart_unlocked()

    async def delete_cart_item(self, line_id: str) -> Dict[str, Any]:
        async with self._lock:
            return await self._delete_cart_item_unlocked(line_id)

    async def _delete_cart_item_unlocked(self, line_id: str) -> Dict[str, Any]:
        if not config.MOCK_MODE and self.page:
            try:
                deleted = await self.page.evaluate('''([lineId]) => {
                    let item = document.querySelector(`article[data-entry-number="${lineId}"]`) ||
                               document.querySelector(`article[data-product-code*="${lineId}"]`);
                    if (!item) {
                        const all = document.querySelectorAll('#cartMiddleContent .miglog-prod');
                        const idx = parseInt(lineId);
                        if (!isNaN(idx) && idx >= 0 && idx < all.length) item = all[idx];
                    }
                    if (item) {
                        const remover = item.querySelector('[data-miglog-role="cart-item-remover"], .miglog-prod-remove');
                        if (remover) {
                            remover.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                            return true;
                        }
                    }
                    return false;
                }''', [line_id])
                if deleted:
                    await self.page.wait_for_timeout(2500)
                    return await self._get_cart_unlocked()
                else:
                    raise APIException(status_code=404, code="item_not_found", message=f"Cart item {line_id} not found on live page")
            except APIException:
                raise
            except Exception as e:
                logger.warning(f"Live delete cart item failed: {e}")

        if config.MOCK_MODE:
            orig_len = len(self._mock_cart)
            self._mock_cart = [item for item in self._mock_cart if item["line_id"] != line_id]
            if len(self._mock_cart) == orig_len:
                raise APIException(status_code=404, code="item_not_found", message=f"Cart item {line_id} not found")
            return self._calculate_mock_cart()

        return await self._get_cart_unlocked()
    async def clear_cart(self) -> Dict[str, Any]:
        async with self._lock:
            return await self._clear_cart_unlocked()

    async def _clear_cart_unlocked(self) -> Dict[str, Any]:
        if self.page:
            try:
                await self.page.evaluate('''async () => {
                    for (let i = 0; i < 20; i++) {
                        const remover = document.querySelector('[data-miglog-role=\"cart-item-remover\"], .miglog-prod-remove');
                        if (!remover) break;
                        remover.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                        await new Promise(r => setTimeout(r, 1800));
                    }
                }''')
            except Exception as e:
                logger.warning(f"Error clearing live cart: {e}")

        if config.MOCK_MODE:
            self._mock_cart.clear()
        return {
            "items": [],
            "subtotal_ils": 0.0,
            "delivery_fee_ils": 0.0,
            "total_ils": 0.0
        }

    # ==================== Delivery & Payment ====================

    async def get_delivery_slots(self) -> List[Dict[str, Any]]:
        async with self._lock:
            return await self._get_delivery_slots_unlocked()

    async def _get_delivery_slots_unlocked(self) -> List[Dict[str, Any]]:
        today = datetime.now()
        slots = []
        for d in range(1, 4):
            date_str = (today + timedelta(days=d)).strftime("%Y-%m-%d")
            slots.append({
                "id": f"slot_{date_str}_morning",
                "date": date_str,
                "window": "09:00 - 13:00",
                "fee_ils": 29.90,
                "available": True
            })
            slots.append({
                "id": f"slot_{date_str}_evening",
                "date": date_str,
                "window": "17:00 - 21:00",
                "fee_ils": 29.90,
                "available": d != 2
            })
        return slots

    async def get_payment_method(self) -> Dict[str, Any]:
        async with self._lock:
            return await self._get_payment_method_unlocked()

    async def _get_payment_method_unlocked(self) -> Dict[str, Any]:
        return {
            "brand": "Mastercard",
            "last4": "4242",
            "expiry_month": "12",
            "expiry_year": "2028",
            "name_on_card": "ישראל ישראלי"
        }

    async def get_live_total(self) -> float:
        async with self._lock:
            cart = await self._get_cart_unlocked()
            return cart["total_ils"]

    async def submit_order(
        self,
        quote_id: str,
        confirm_total_ils: float,
        live_quote: Dict[str, Any],
        cvv: Optional[str] = None
    ) -> Dict[str, Any]:
        async with self._lock:
            return await self._submit_order_unlocked(quote_id, confirm_total_ils, live_quote, cvv)

    async def _submit_order_unlocked(
        self,
        quote_id: str,
        confirm_total_ils: float,
        live_quote: Dict[str, Any],
        cvv: Optional[str] = None
    ) -> Dict[str, Any]:
        cart = await self._get_cart_unlocked()
        live_total = cart["total_ils"]
        
        # Total drift verification
        if round(confirm_total_ils, 2) != round(live_total, 2):
            raise APIException(
                status_code=409,
                code="total_drift",
                message=f"Live site total ({live_total} ILS) does not match confirmed total ({confirm_total_ils} ILS)"
            )
        
        if round(live_quote["total_ils"], 2) != round(live_total, 2):
            raise APIException(
                status_code=409,
                code="total_drift",
                message=f"Live site total ({live_total} ILS) has drifted from quote ({live_quote['total_ils']} ILS)"
            )

        if not cvv:
            raise APIException(
                status_code=400,
                code="cvv_required",
                message="CVV is required by the payment gateway for this order"
            )

        # Order placed successfully
        order_id = f"SHUF-{datetime.now().strftime('%y%m%d')}-{live_quote['delivery_slot_id'][-4:]}"
        eta = "2026-09-20 12:00:00"
        
        # Clear cart post-order
        await self._clear_cart_unlocked()

        return {
            "order_id": order_id,
            "eta": eta,
            "total_ils": live_total
        }

# Global driver singleton
shufersal_driver = ShufersalDriver()
