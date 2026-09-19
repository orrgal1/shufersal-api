import pytest
import os

os.environ["SHUFERSAL_API_TOKEN"] = "test-secret-token-xyz-123"
os.environ["JWT_SECRET"] = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
os.environ["MOCK_MODE"] = "true"

from fastapi.testclient import TestClient
from main import app
from security import generate_jwt_token, sanitize_log_message
from idempotency import idempotency_store, quote_store
from shufersal_driver import shufersal_driver

client = TestClient(app, raise_server_exceptions=False)
TEST_JWT = generate_jwt_token(subject="remote-agent-tester", expires_days=30)
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_JWT}"}

def test_openapi_public_inspection():
    """OpenAPI and Swagger docs must be accessible without Bearer token for inspection."""
    res = client.get("/openapi.json")
    assert res.status_code == 200
    spec = res.json()
    assert "paths" in spec
    assert "/health" in spec["paths"]
    assert "/search" in spec["paths"]
    assert "/cart" in spec["paths"]
    assert "/checkout/submit" in spec["paths"]

    docs_res = client.get("/docs")
    assert docs_res.status_code == 200

def test_jwt_auth_verification():
    """All private endpoints must require valid JWT."""
    # No auth -> 401
    res = client.get("/health")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "unauthorized"

    # Invalid JWT -> 401
    bad_res = client.get("/health", headers={"Authorization": "Bearer invalid.jwt.token"})
    assert bad_res.status_code == 401
    assert bad_res.json()["error"]["code"] == "unauthorized"

    # Valid JWT -> 200
    ok_res = client.get("/health", headers=AUTH_HEADERS)
    assert ok_res.status_code == 200
    assert ok_res.json()["status"] == "ok"

def test_health():
    """GET /health -> {status:'ok', logged_in:bool, cart_size:int}"""
    res = client.get("/health", headers=AUTH_HEADERS)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert isinstance(data["logged_in"], bool)
    assert isinstance(data["cart_size"], int)

def test_search_and_product():
    """Search products and retrieve product detail."""
    res = client.get("/search?q=חלב&limit=5", headers=AUTH_HEADERS)
    assert res.status_code == 200
    data = res.json()
    assert "results" in data
    assert len(data["results"]) > 0
    item = data["results"][0]
    assert "id" in item
    assert "name" in item
    assert "price_ils" in item
    assert "in_stock" in item

    # Get product
    prod_res = client.get(f"/product/{item['id']}", headers=AUTH_HEADERS)
    assert prod_res.status_code == 200
    prod_data = prod_res.json()
    assert prod_data["id"] == item["id"]
    assert "price_ils" in prod_data

def test_cart_lifecycle_and_idempotency():
    """Verify add, update, delete, clear and Idempotency-Key caching."""
    # 1. Clear cart
    client.delete("/cart", headers=AUTH_HEADERS)
    cart_res = client.get("/cart", headers=AUTH_HEADERS)
    assert cart_res.status_code == 200
    assert len(cart_res.json()["items"]) == 0

    # 2. Add item with Idempotency-Key
    idem_key = "test-idem-add-100"
    add_payload = {"product_id": "P12345", "qty": 2.0}
    res1 = client.post(
        "/cart/items",
        json=add_payload,
        headers={**AUTH_HEADERS, "Idempotency-Key": idem_key}
    )
    assert res1.status_code == 200
    cart1 = res1.json()
    assert len(cart1["items"]) == 1
    assert cart1["items"][0]["qty"] == 2.0
    line_id = cart1["items"][0]["line_id"]

    # 3. Retry same add with identical Idempotency-Key
    res2 = client.post(
        "/cart/items",
        json=add_payload,
        headers={**AUTH_HEADERS, "Idempotency-Key": idem_key}
    )
    assert res2.status_code == 200
    cart2 = res2.json()
    assert len(cart2["items"]) == 1
    assert cart2["items"][0]["qty"] == 2.0

    # 4. Update item quantity
    update_res = client.patch(
        f"/cart/items/{line_id}",
        json={"qty": 4.0},
        headers=AUTH_HEADERS
    )
    assert update_res.status_code == 200
    assert update_res.json()["items"][0]["qty"] == 4.0

    # 5. Delete item
    del_res = client.delete(f"/cart/items/{line_id}", headers=AUTH_HEADERS)
    assert del_res.status_code == 200
    assert len(del_res.json()["items"]) == 0

def test_delivery_and_payment():
    """Verify delivery slots and masked payment method."""
    slots_res = client.get("/delivery/slots", headers=AUTH_HEADERS)
    assert slots_res.status_code == 200
    slots_data = slots_res.json()
    assert "slots" in slots_data
    assert len(slots_data["slots"]) > 0

    pay_res = client.get("/payment/method", headers=AUTH_HEADERS)
    assert pay_res.status_code == 200
    pay_data = pay_res.json()
    assert "brand" in pay_data
    assert "last4" in pay_data
    assert "cvv" not in pay_data
    assert len(pay_data["last4"]) == 4

def test_checkout_quote_and_submit_drift_protection():
    """Verify quote generation and total drift / CVV protections on checkout submit."""
    client.delete("/cart", headers=AUTH_HEADERS)
    add_res = client.post("/cart/items", json={"product_id": "P12345", "qty": 1.0}, headers=AUTH_HEADERS)
    cart = add_res.json()

    slots = client.get("/delivery/slots", headers=AUTH_HEADERS).json()["slots"]
    slot_id = slots[0]["id"]

    quote_res = client.post(f"/checkout/quote?delivery_slot_id={slot_id}", headers=AUTH_HEADERS)
    assert quote_res.status_code == 200
    quote = quote_res.json()
    quote_id = quote["quote_id"]
    correct_total = quote["total_ils"]

    # Drift -> 409
    drift_res = client.post(
        "/checkout/submit",
        json={
            "quote_id": quote_id,
            "confirm_total_ils": correct_total + 10.0,
            "cvv": "123"
        },
        headers=AUTH_HEADERS
    )
    assert drift_res.status_code == 409
    assert drift_res.json()["error"]["code"] == "total_drift"

    # Missing CVV -> 400
    no_cvv_res = client.post(
        "/checkout/submit",
        json={
            "quote_id": quote_id,
            "confirm_total_ils": correct_total
        },
        headers=AUTH_HEADERS
    )
    assert no_cvv_res.status_code == 400
    assert no_cvv_res.json()["error"]["code"] == "cvv_required"

    # Success
    success_res = client.post(
        "/checkout/submit",
        json={
            "quote_id": quote_id,
            "confirm_total_ils": correct_total,
            "cvv": "123"
        },
        headers=AUTH_HEADERS
    )
    assert success_res.status_code == 200
    submit_data = success_res.json()
    assert "order_id" in submit_data
    assert submit_data["total_ils"] == correct_total

def test_sensitive_log_sanitizer():
    """Verify regex redacts credit card numbers, CVVs, and JWT signatures."""
    raw_log = f'User with token {TEST_JWT} entered card 4580-1234-5678-9012 with cvv: 789'
    sanitized = sanitize_log_message(raw_log)
    assert "4580-1234-5678-9012" not in sanitized
    assert "[REDACTED_CARD]" in sanitized
    assert "789" not in sanitized
    assert "[REDACTED_JWT]" in sanitized


def test_feedback():
    """Verify agent can submit feedback and list feedback entries."""
    fb_res = client.post(
        "/feedback",
        json={
            "message": "Testing agent feedback relay mechanism",
            "category": "request",
            "details": {"test_metric": 42}
        },
        headers=AUTH_HEADERS
    )
    assert fb_res.status_code == 200
    data = fb_res.json()
    assert data["status"] == "received"
    assert "feedback_id" in data

    list_res = client.get("/feedback?limit=5", headers=AUTH_HEADERS)
    assert list_res.status_code == 200
    feedbacks = list_res.json()["feedbacks"]
    assert len(feedbacks) > 0
    assert feedbacks[0]["message"] == "Testing agent feedback relay mechanism"