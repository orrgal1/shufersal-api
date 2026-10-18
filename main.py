import argparse
import sys
import json
import uuid
import logging
from pathlib import Path
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Depends, Header, Request, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

import config
from schemas import (
    HealthResponse,
    SearchResponse,
    ProductDetailResponse,
    CartResponse,
    CartItemAddRequest,
    CartItemUpdateRequest,
    CartBatchRequest,
    DeliverySlotsResponse,
    PaymentMethodResponse,
    CheckoutQuoteResponse,
    CheckoutSubmitRequest,
    CheckoutSubmitResponse,
    FeedbackRequest,
    FeedbackResponse,
    ErrorResponse
)
from security import verify_bearer_token, log_mutation, APIException
from idempotency import idempotency_store, quote_store
from shufersal_driver import shufersal_driver
from shufersal_parsers import cart_hash
from checkout_ledger import CheckoutLedger

checkout_ledger = CheckoutLedger(config.CHECKOUT_LEDGER)
FINAL_SUBMIT_STATES = {"placed", "placed_unconfirmed", "three_ds_required", "outcome_unknown"}
SAFE_CHECKOUT_CODES = {
    "total_drift", "quote_expired_or_invalid", "cart_mismatch", "cart_empty", "slot_unavailable",
    "checkout_blocked", "confirmation_required", "idempotency_key_required", "idempotency_key_used",
    "live_checkout_disabled", "order_rejected", "checkout_not_configured", "browser_unavailable",
    "slot_select_failed", "checkout_unreachable", "payment_method_unavailable", "invalid_input",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("shufersal_api")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: start browser / driver
    logger.info("Initializing Shufersal service...")
    await shufersal_driver.start()
    yield
    # Shutdown: clean up browser resources
    logger.info("Shutting down Shufersal service...")
    await shufersal_driver.stop()

app = FastAPI(
    title="Shufersal Local API Wrapper",
    version="1.1",
    lifespan=lifespan,
    dependencies=[Depends(verify_bearer_token)]
)

# ==================== Exception Handlers ====================

@app.exception_handler(APIException)
async def api_exception_handler(request: Request, exc: APIException):
    message = exc.message
    if request.url.path == "/checkout/submit":
        # Driver errors must not be able to reflect a caller's payment secret.
        # Only server-generated messages from known codes pass through.
        if exc.code not in SAFE_CHECKOUT_CODES:
            message = "Checkout could not be completed."
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": message}}
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"error": {"code": "invalid_input", "message": "Request fields are invalid."}}
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    if request.url.path == "/checkout/submit":
        logger.error("Unhandled checkout submission error")
    else:
        logger.error(f"Unhandled server error on {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"error": {"code": "upstream_error", "message": "The upstream service failed."}}
    )

# ==================== Endpoints ====================

@app.get("/health", response_model=HealthResponse)
async def get_health():
    """GET /health -> {status:'ok', logged_in:bool, cart_size:int}"""
    logged_in, cart_size = await shufersal_driver.check_login_status()
    return HealthResponse(
        status="ok",
        logged_in=logged_in,
        cart_size=cart_size
    )

@app.get("/search", response_model=SearchResponse)
async def search_products(q: str, limit: int = 20):
    """GET /search?q=<text>&limit=20 -> {results:[{id, name, brand, price_ils, unit, image_url, in_stock:bool}]}"""
    results = await shufersal_driver.search_products(query=q, limit=limit)
    return SearchResponse(results=results)

@app.get("/product/{product_id}", response_model=ProductDetailResponse)
async def get_product(product_id: str):
    """GET /product/{id} -> {id, name, description, price_ils, sale_price_ils|null, unit, nutrition_url|null, in_stock}"""
    product = await shufersal_driver.get_product(product_id)
    if not product:
        raise APIException(status_code=404, code="not_found", message=f"Product {product_id} not found")
    return ProductDetailResponse(**product)

@app.get("/cart", response_model=CartResponse)
async def get_cart():
    """GET /cart -> {items:[{line_id, product_id, name, qty, unit_price_ils, line_total_ils}], subtotal_ils, delivery_fee_ils, total_ils}"""
    cart = await shufersal_driver.get_cart()
    return CartResponse(**cart)

RETRY_NOTE = ("Retries: send an Idempotency-Key. The planned target is recorded before any write, so a "
              "retry after an uncertain failure reconciles against the live cart instead of adding again. "
              "Without a key, GET /cart before retrying a failed write.")
PENDING = 0  # idempotency_store status for a planned write whose outcome is not yet known


async def _planned_add(endpoint: str, items, clear_first: bool, idempotency_key: Optional[str]):
    plan = None
    if idempotency_key:
        cached = idempotency_store.get(idempotency_key)
        if cached and cached["status_code"] != PENDING:
            log_mutation("POST", endpoint, cached["status_code"], idempotency_key, {"cached": True})
            return JSONResponse(status_code=cached["status_code"], content=cached["body"])
        if cached:
            plan = cached["body"]["plan"]  # an earlier attempt with this key may have written
    record = (lambda p: idempotency_store.set(idempotency_key, PENDING, {"plan": p})) if idempotency_key else None
    cart = CartResponse(**(await shufersal_driver.add_items(items, clear_first=clear_first, plan=plan, on_plan=record)))
    if idempotency_key:
        idempotency_store.set(idempotency_key, 200, cart.model_dump())
    log_mutation("POST", endpoint, 200, idempotency_key, {"count": len(items), "reconciled": plan is not None})
    return cart


def _cached(method: str, path: str, idempotency_key: Optional[str]):
    if idempotency_key:
        cached = idempotency_store.get(idempotency_key)
        if cached and cached["status_code"] != PENDING:
            log_mutation(method, path, cached["status_code"], idempotency_key, {"cached": True})
            return JSONResponse(status_code=cached["status_code"], content=cached["body"])
    return None


@app.post("/cart/items", response_model=CartResponse, description=RETRY_NOTE)
async def add_cart_item(
    payload: CartItemAddRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")
):
    """POST /cart/items body:{product_id, qty} -> updated cart (adds qty to any existing line)"""
    return await _planned_add("/cart/items", [(payload.product_id, payload.qty)], False, idempotency_key)

@app.patch("/cart/items/{line_id}", response_model=CartResponse)
async def update_cart_item(
    line_id: str,
    payload: CartItemUpdateRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")
):
    """PATCH /cart/items/{line_id} body:{qty, product_code} (qty=0 removes) -> updated cart.
    product_code is required: line ids shift when lines are removed, so the line is
    re-resolved by product code if line_id no longer points at that product. Setting a
    quantity is idempotent, so retrying is safe."""
    cached = _cached("PATCH", f"/cart/items/{line_id}", idempotency_key)
    if cached:
        return cached
    updated_cart = await shufersal_driver.update_cart_item(line_id=line_id, qty=payload.qty, product_code=payload.product_code)
    if idempotency_key:
        idempotency_store.set(idempotency_key, 200, updated_cart)
    log_mutation("PATCH", f"/cart/items/{line_id}", 200, idempotency_key, {"line_id": line_id, "qty": payload.qty})
    return CartResponse(**updated_cart)

@app.delete("/cart/items/{line_id}", response_model=CartResponse)
async def delete_cart_item(
    line_id: str,
    product_code: Optional[str] = None,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")
):
    """DELETE /cart/items/{line_id}?product_code=<code> -> updated cart.
    product_code is required (see PATCH). Removing a product that is already gone
    returns the current cart, so retrying is safe."""
    cached = _cached("DELETE", f"/cart/items/{line_id}", idempotency_key)
    if cached:
        return cached
    updated_cart = await shufersal_driver.delete_cart_item(line_id=line_id, product_code=product_code)
    if idempotency_key:
        idempotency_store.set(idempotency_key, 200, updated_cart)
    log_mutation("DELETE", f"/cart/items/{line_id}", 200, idempotency_key, {"line_id": line_id})
    return CartResponse(**updated_cart)

@app.delete("/cart")
async def clear_cart(
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")
):
    """DELETE /cart -> the re-read (empty) cart. Idempotent."""
    cached = _cached("DELETE", "/cart", idempotency_key)
    if cached:
        return cached
    # The driver loops until the site cart is empty and returns the re-read cart.
    response_data = CartResponse(**(await shufersal_driver.clear_cart())).model_dump()
    if idempotency_key:
        idempotency_store.set(idempotency_key, 200, response_data)
    log_mutation("DELETE", "/cart", 200, idempotency_key, {"action": "clear_cart"})
    return response_data

@app.post("/cart/batch", response_model=CartResponse, description=RETRY_NOTE)
async def batch_cart_items(
    payload: CartBatchRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")
):
    """POST /cart/batch body:{items:[{product_id, qty}], clear_first:bool} -> updated cart"""
    return await _planned_add("/cart/batch", [(i.product_id, i.qty) for i in payload.items],
                              payload.clear_first, idempotency_key)

@app.post("/cart/clear")
async def clear_cart_post(
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")
):
    """POST /cart/clear -> alias for DELETE /cart"""
    return await clear_cart(idempotency_key=idempotency_key)

@app.put("/cart/items/{line_id}", response_model=CartResponse)
async def update_cart_item_put(
    line_id: str,
    payload: CartItemUpdateRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")
):
    """PUT /cart/items/{line_id} -> alias for PATCH /cart/items/{line_id}"""
    return await update_cart_item(line_id=line_id, payload=payload, idempotency_key=idempotency_key)

@app.post("/cart/items/{line_id}/delete", response_model=CartResponse)
async def delete_cart_item_post(
    line_id: str,
    product_code: Optional[str] = None,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")
):
    """POST /cart/items/{line_id}/delete?product_code=<code> -> alias for DELETE /cart/items/{line_id}"""
    return await delete_cart_item(line_id=line_id, product_code=product_code, idempotency_key=idempotency_key)

@app.get("/delivery/slots", response_model=DeliverySlotsResponse)
async def get_delivery_slots():
    """GET /delivery/slots -> {slots:[{id, date, window, fee_ils, available:bool}]} for saved address"""
    slots = await shufersal_driver.get_delivery_slots()
    return DeliverySlotsResponse(slots=slots)

@app.get("/payment/method", response_model=PaymentMethodResponse)
async def get_payment_method():
    """GET /payment/method -> {brand, last4, expiry_month, expiry_year, name_on_card}"""
    card = await shufersal_driver.get_payment_method()
    return PaymentMethodResponse(**card)

@app.post("/checkout/quote", response_model=CheckoutQuoteResponse)
async def create_checkout_quote(
    delivery_slot_id: str,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")
):
    """POST /checkout/quote?delivery_slot_id=<slot id from /delivery/slots>
    Read-only. -> {quote_id, cart_hash, items, slot, payment (masked), subtotal_ils,
    delivery_fee_ils, total_ils (estimate: site subtotal + slot fee), expires_at}"""
    if idempotency_key:
        cached = idempotency_store.get(idempotency_key)
        if cached:
            log_mutation("POST", "/checkout/quote", cached["status_code"], idempotency_key, {"cached": True})
            return JSONResponse(status_code=cached["status_code"], content=cached["body"])

    summary = await shufersal_driver.checkout_summary(delivery_slot_id)
    quote = quote_store.create_quote(
        delivery_slot_id=delivery_slot_id,
        subtotal_ils=summary["subtotal_ils"],
        delivery_fee_ils=summary["delivery_fee_ils"],
        total_ils=summary["estimated_total_ils"],
        item_count=sum(int(round(i["qty"])) for i in summary["items"]),
    )
    quote.update({
        "cart_hash": summary["cart_hash"],
        "items": summary["items"],
        "slot": summary["slot"],
        "payment": summary["payment"],
        "prices_complete": summary["prices_complete"],
        "total_is_estimate": True,
    })
    if idempotency_key:
        idempotency_store.set(idempotency_key, 200, quote)

    log_mutation("POST", "/checkout/quote", 200, idempotency_key, {"quote_id": quote["quote_id"]})
    return CheckoutQuoteResponse(**quote)

@app.post("/checkout/submit", response_model=CheckoutSubmitResponse)
async def submit_checkout(
    payload: CheckoutSubmitRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")
):
    """
    POST /checkout/submit body:{quote_id, confirm_total_ils, expected_cart_hash | expected_items,
                                dry_run (default true), confirm (default false)}
    dry_run=true walks checkout (verify cart, select the slot, open checkout, read the
    site total) and stops before placing the order. Placing a real order needs
    dry_run=false, confirm=true, a never-used Idempotency-Key and
    SHUFERSAL_LIVE_CHECKOUT_ENABLED=true. The cart is never modified.
    """
    quote = quote_store.get_quote(payload.quote_id)
    if not quote:
        raise APIException(
            status_code=status.HTTP_409_CONFLICT,
            code="quote_expired_or_invalid",
            message=f"Quote {payload.quote_id} not found or has expired"
        )

    expected_hash = payload.expected_cart_hash
    if payload.expected_items is not None:
        items_hash = cart_hash([i.model_dump() for i in payload.expected_items])
        if expected_hash and expected_hash != items_hash:
            raise APIException(status_code=400, code="invalid_input",
                               message="expected_cart_hash and expected_items disagree.")
        expected_hash = items_hash
    if expected_hash != quote.get("cart_hash"):
        raise APIException(status_code=409, code="cart_mismatch",
                           message="The expected cart does not match the quoted cart.")

    place_order = not payload.dry_run
    if place_order:
        if payload.confirm is not True:
            raise APIException(status_code=400, code="confirmation_required",
                               message="Placing an order needs confirm=true.")
        if not idempotency_key:
            raise APIException(status_code=400, code="idempotency_key_required",
                               message="Placing an order needs an Idempotency-Key header.")
        if not (config.LIVE_CHECKOUT_ENABLED or config.MOCK_MODE):
            raise APIException(status_code=403, code="live_checkout_disabled",
                               message="Live order placement is disabled on this service.")
        # The payment secret is local to this service. Fail before touching the driver
        # if it has not been configured or is malformed.
        cvv = config.SHUFERSAL_CVV
        if not (len(cvv) in (3, 4) and cvv.isascii() and cvv.isdecimal()):
            raise APIException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="checkout_not_configured",
                message="Checkout is not configured."
            )
        previous = checkout_ledger.claim(idempotency_key, payload.quote_id)
        if previous is not None:
            if previous["state"] in FINAL_SUBMIT_STATES and previous["result"]:
                log_mutation("POST", "/checkout/submit", previous["http_status"], None, {"replayed": True})
                return JSONResponse(status_code=previous["http_status"], content=previous["result"])
            raise APIException(status_code=409, code="idempotency_key_used",
                               message=f"This Idempotency-Key was already used (state: {previous['state']}). Use a new key.")
    else:
        cvv = None

    try:
        result = await shufersal_driver.checkout(
            slot_id=quote["delivery_slot_id"],
            expected_cart_hash=expected_hash,
            confirm_total_ils=payload.confirm_total_ils,
            place_order=place_order,
            cvv=cvv,
        )
    except APIException as exc:
        if place_order:
            checkout_ledger.finish(idempotency_key, f"failed:{exc.code}", exc.status_code, {})
        raise
    except Exception:
        if place_order:
            checkout_ledger.finish(idempotency_key, "outcome_unknown", 502, {})
        raise

    response = CheckoutSubmitResponse(
        status=result["status"],
        dry_run=not place_order,
        order_id=result.get("order_id"),
        total_ils=result.get("site_total_ils"),
        site_total_ils=result.get("site_total_ils"),
        summary=result["summary"],
        checks=result.get("checks") or {},
        blockers=result.get("blockers") or [],
    )
    http_status = 202 if response.status in ("three_ds_required", "outcome_unknown", "placed_unconfirmed") else 200
    body = response.model_dump()
    if place_order:
        checkout_ledger.finish(idempotency_key, response.status, http_status, body)
    log_mutation("POST", "/checkout/submit", http_status, None, {"status": response.status, "dry_run": not place_order})
    return JSONResponse(status_code=http_status, content=body)
# ==================== Feedback ====================

@app.post("/feedback", response_model=FeedbackResponse)
async def submit_feedback(payload: FeedbackRequest, request: Request):
    """POST /feedback body:{message:str, category:str, details:dict} -> {status:'received', feedback_id, timestamp}"""
    feedback_id = f"fb_{uuid.uuid4().hex[:10]}"
    now_iso = datetime.now(timezone.utc).isoformat()
    entry = {
        "feedback_id": feedback_id,
        "timestamp": now_iso,
        "category": payload.category,
        "message": payload.message,
        "details": payload.details or {},
        "client_host": request.client.host if request.client else "unknown"
    }
    logger.info(f"FEEDBACK RECEIVED [{payload.category}]: {payload.message}")
    sys.stdout.write(f"\n📢 [AGENT FEEDBACK {now_iso}] [{payload.category}] {payload.message}\n")
    sys.stdout.flush()

    try:
        feedback_file = Path("feedback.jsonl")
        with feedback_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"Failed to persist feedback to file: {e}")

    return FeedbackResponse(
        status="received",
        feedback_id=feedback_id,
        timestamp=now_iso
    )

@app.get("/feedback")
async def list_feedback(limit: int = 20):
    """GET /feedback?limit=20 -> list of recent agent feedbacks"""
    feedback_file = Path("feedback.jsonl")
    if not feedback_file.exists():
        return {"feedbacks": []}
    lines = feedback_file.read_text(encoding="utf-8").strip().splitlines()
    entries = []
    for line in lines[-limit:]:
        try:
            entries.append(json.loads(line))
        except Exception:
            pass
    return {"feedbacks": list(reversed(entries))}

# ==================== CLI Entrypoint ====================

def run_cli():
    parser = argparse.ArgumentParser(description="Shufersal Local API Wrapper")
    parser.add_argument("--host", default=config.HOST, help="Host to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=config.PORT, help="Port to bind (default: 8000)")
    parser.add_argument("--profile-dir", default=config.SHUFERSAL_PROFILE_DIR, help="Playwright profile directory")
    parser.add_argument("--headful", action="store_true", help="Launch browser with GUI visible")
    parser.add_argument("--login", action="store_true", help="Launch interactive headful browser to log into Shufersal")
    args = parser.parse_args()

    if args.login:
        import asyncio
        from playwright.async_api import async_playwright
        print("Starting interactive browser for one-time Shufersal login...")
        print(f"Profile directory: {args.profile_dir}")
        print("Please log into your account. Once done, close the browser window.")
        
        async def interactive_login():
            async with async_playwright() as p:
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=args.profile_dir,
                    headless=False,
                    locale="he-IL",
                    timezone_id="Asia/Jerusalem",
                    viewport={"width": 1280, "height": 800}
                )
                page = context.pages[0] if context.pages else await context.new_page()
                print("Navigating to Shufersal login page...")
                await page.goto("https://www.shufersal.co.il/online/he/login", wait_until="domcontentloaded")
                print("Browser window is open. Please log into your Shufersal account.")
                print("Waiting for login completion...")
                
                # Poll for login success
                for _ in range(150): # 5 minutes max (150 * 2s)
                    await asyncio.sleep(2)
                    if context.pages and not page.is_closed():
                        try:
                            status = await page.evaluate("() => fetch('/online/he/authentication/get-status-includes-otp').then(r => r.text()).catch(() => 'false')")
                            if status.strip().lower() == "true":
                                print("✅ Login detected successfully!")
                                await asyncio.sleep(2)
                                break
                        except Exception:
                            pass
                    else:
                        break
                await context.close()
            print("Login session saved to profile! The service can now run against your real account.")
        asyncio.run(interactive_login())
        sys.exit(0)

    import uvicorn
    uvicorn.run("main:app", host=args.host, port=args.port, reload=False)

if __name__ == "__main__":
    run_cli()
