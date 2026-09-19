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
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}}
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"error": {"code": "invalid_input", "message": str(exc)}}
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled server error on {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"error": {"code": "upstream_error", "message": f"Service encountered an error: {str(exc)}"}}
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

@app.post("/cart/items", response_model=CartResponse)
async def add_cart_item(
    payload: CartItemAddRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")
):
    """POST /cart/items body:{product_id, qty} Idempotency-Key header optional -> updated cart"""
    if idempotency_key:
        cached = idempotency_store.get(idempotency_key)
        if cached:
            log_mutation("POST", "/cart/items", cached["status_code"], idempotency_key, {"cached": True})
            return JSONResponse(status_code=cached["status_code"], content=cached["body"])

    updated_cart = await shufersal_driver.add_to_cart(product_id=payload.product_id, qty=payload.qty)
    
    if idempotency_key:
        idempotency_store.set(idempotency_key, 200, updated_cart)

    log_mutation("POST", "/cart/items", 200, idempotency_key, {"product_id": payload.product_id, "qty": payload.qty})
    return CartResponse(**updated_cart)

@app.patch("/cart/items/{line_id}", response_model=CartResponse)
async def update_cart_item(
    line_id: str,
    payload: CartItemUpdateRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")
):
    """PATCH /cart/items/{line_id} body:{qty} (qty=0 removes) -> updated cart"""
    if idempotency_key:
        cached = idempotency_store.get(idempotency_key)
        if cached:
            log_mutation("PATCH", f"/cart/items/{line_id}", cached["status_code"], idempotency_key, {"cached": True})
            return JSONResponse(status_code=cached["status_code"], content=cached["body"])

    updated_cart = await shufersal_driver.update_cart_item(line_id=line_id, qty=payload.qty)
    
    if idempotency_key:
        idempotency_store.set(idempotency_key, 200, updated_cart)

    log_mutation("PATCH", f"/cart/items/{line_id}", 200, idempotency_key, {"line_id": line_id, "qty": payload.qty})
    return CartResponse(**updated_cart)

@app.delete("/cart/items/{line_id}", response_model=CartResponse)
async def delete_cart_item(
    line_id: str,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")
):
    """DELETE /cart/items/{line_id} -> updated cart"""
    if idempotency_key:
        cached = idempotency_store.get(idempotency_key)
        if cached:
            log_mutation("DELETE", f"/cart/items/{line_id}", cached["status_code"], idempotency_key, {"cached": True})
            return JSONResponse(status_code=cached["status_code"], content=cached["body"])

    updated_cart = await shufersal_driver.delete_cart_item(line_id=line_id)

    if idempotency_key:
        idempotency_store.set(idempotency_key, 200, updated_cart)

    log_mutation("DELETE", f"/cart/items/{line_id}", 200, idempotency_key, {"line_id": line_id})
    return CartResponse(**updated_cart)

@app.delete("/cart")
async def clear_cart(
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")
):
    """DELETE /cart -> {items:[], total_ils:0}"""
    if idempotency_key:
        cached = idempotency_store.get(idempotency_key)
        if cached:
            log_mutation("DELETE", "/cart", cached["status_code"], idempotency_key, {"cached": True})
            return JSONResponse(status_code=cached["status_code"], content=cached["body"])

    empty_cart = await shufersal_driver.clear_cart()
    response_data = {"items": [], "total_ils": 0}

    if idempotency_key:
        idempotency_store.set(idempotency_key, 200, response_data)

    log_mutation("DELETE", "/cart", 200, idempotency_key, {"action": "clear_cart"})
    return response_data
@app.post("/cart/batch", response_model=CartResponse)
async def batch_cart_items(
    payload: CartBatchRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")
):
    """POST /cart/batch body:{items:[{product_id, qty}], clear_first:bool} -> updated cart"""
    if idempotency_key:
        cached = idempotency_store.get(idempotency_key)
        if cached:
            log_mutation("POST", "/cart/batch", cached["status_code"], idempotency_key, {"cached": True})
            return JSONResponse(status_code=cached["status_code"], content=cached["body"])

    if payload.clear_first:
        await shufersal_driver.clear_cart()

    last_cart = None
    for item in payload.items:
        last_cart = await shufersal_driver.add_to_cart(product_id=item.product_id, qty=item.qty)

    if last_cart is None:
        last_cart = await shufersal_driver.get_cart()

    if idempotency_key:
        idempotency_store.set(idempotency_key, 200, last_cart)

    log_mutation("POST", "/cart/batch", 200, idempotency_key, {"count": len(payload.items), "clear_first": payload.clear_first})
    return CartResponse(**last_cart)

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
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")
):
    """POST /cart/items/{line_id}/delete -> alias for DELETE /cart/items/{line_id}"""
    return await delete_cart_item(line_id=line_id, idempotency_key=idempotency_key)

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
    """POST /checkout/quote -> {delivery_slot_id, subtotal_ils, delivery_fee_ils, total_ils, item_count, quote_id, expires_at}"""
    if idempotency_key:
        cached = idempotency_store.get(idempotency_key)
        if cached:
            log_mutation("POST", "/checkout/quote", cached["status_code"], idempotency_key, {"cached": True})
            return JSONResponse(status_code=cached["status_code"], content=cached["body"])

    cart = await shufersal_driver.get_cart()
    slots = await shufersal_driver.get_delivery_slots()
    selected_slot = next((s for s in slots if s["id"] == delivery_slot_id), None)
    delivery_fee = selected_slot["fee_ils"] if selected_slot else cart["delivery_fee_ils"]
    total = cart["subtotal_ils"] + delivery_fee
    item_count = sum(int(item["qty"]) for item in cart["items"])

    quote = quote_store.create_quote(
        delivery_slot_id=delivery_slot_id,
        subtotal_ils=cart["subtotal_ils"],
        delivery_fee_ils=delivery_fee,
        total_ils=total,
        item_count=item_count
    )

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
    POST /checkout/submit body:{quote_id, confirm_total_ils, cvv(optional)}
    Places order ONLY if confirm_total_ils == live re-read site total and quote not expired -> {order_id, eta, total_ils}. Otherwise 409.
    """
    if idempotency_key:
        cached = idempotency_store.get(idempotency_key)
        if cached:
            log_mutation("POST", "/checkout/submit", cached["status_code"], idempotency_key, {"cached": True})
            return JSONResponse(status_code=cached["status_code"], content=cached["body"])

    # 1. Retrieve quote & check expiration
    quote = quote_store.get_quote(payload.quote_id)
    if not quote:
        raise APIException(
            status_code=status.HTTP_409_CONFLICT,
            code="quote_expired_or_invalid",
            message=f"Quote {payload.quote_id} not found or has expired"
        )

    # 2. Check total drift against live re-read total
    result = await shufersal_driver.submit_order(
        quote_id=payload.quote_id,
        confirm_total_ils=payload.confirm_total_ils,
        live_quote=quote,
        cvv=payload.cvv
    )

    if idempotency_key:
        idempotency_store.set(idempotency_key, 200, result)

    log_mutation(
        "POST",
        "/checkout/submit",
        200,
        idempotency_key,
        {"quote_id": payload.quote_id, "order_id": result["order_id"]}
    )
    return CheckoutSubmitResponse(**result)
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
