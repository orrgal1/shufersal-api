import pytest
import os
import stat
from unittest.mock import AsyncMock

os.environ["SHUFERSAL_API_TOKEN"] = "test-secret-token-xyz-123"
os.environ["JWT_SECRET"] = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
os.environ["MOCK_MODE"] = "true"

from fastapi.testclient import TestClient
import main as main_module
import config
from main import app
from setup_checkout_cvv import set_local_cvv
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
        json={"qty": 4.0, "product_code": "P12345"},
        headers=AUTH_HEADERS
    )
    assert update_res.status_code == 200
    assert update_res.json()["items"][0]["qty"] == 4.0

    # 5. Delete item
    del_res = client.delete(f"/cart/items/{line_id}?product_code=P12345", headers=AUTH_HEADERS)
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

def test_checkout_quote_and_submit_drift_protection(monkeypatch):
    """Checkout uses only the local CVV, defaults to dry run, and keeps drift,
    cart-match and single-use idempotency guards."""
    client.delete("/cart", headers=AUTH_HEADERS)
    client.post("/cart/items", json={"product_id": "P12345", "qty": 1.0}, headers=AUTH_HEADERS)
    slots = client.get("/delivery/slots", headers=AUTH_HEADERS).json()["slots"]
    slot_id = next(s["id"] for s in slots if s["available"])

    quote = client.post(f"/checkout/quote?delivery_slot_id={slot_id}", headers=AUTH_HEADERS).json()
    quote_id, total, chash = quote["quote_id"], quote["total_ils"], quote["cart_hash"]
    assert quote["payment"]["last4"] == "4242" and "payment_info_id" not in quote["payment"]
    base = {"quote_id": quote_id, "confirm_total_ils": total, "expected_cart_hash": chash}
    real = {**base, "dry_run": False, "confirm": True}

    checkout = AsyncMock(wraps=shufersal_driver.checkout)
    monkeypatch.setattr(shufersal_driver, "checkout", checkout)

    # An expected cart is mandatory and must match the quote.
    assert client.post("/checkout/submit", json={"quote_id": quote_id, "confirm_total_ils": total},
                       headers=AUTH_HEADERS).status_code == 400
    res = client.post("/checkout/submit", json={**base, "expected_cart_hash": "0" * 16}, headers=AUTH_HEADERS)
    assert res.status_code == 409 and res.json()["error"]["code"] == "cart_mismatch"
    res = client.post("/checkout/submit", json={"quote_id": quote_id, "confirm_total_ils": total,
                                                "expected_items": [{"product_id": "P12345", "qty": 1}]},
                      headers=AUTH_HEADERS)
    assert res.status_code == 200 and res.json()["status"] == "dry_run"

    # Default is a dry run: no CVV, no key, no order id.
    monkeypatch.setattr(config, "SHUFERSAL_CVV", "")
    dry = client.post("/checkout/submit", json=base, headers=AUTH_HEADERS)
    assert dry.status_code == 200, dry.text
    assert dry.json()["status"] == "dry_run" and dry.json()["order_id"] is None
    assert checkout.await_args.kwargs["place_order"] is False

    # A real order needs confirm=true and an Idempotency-Key, and a configured CVV.
    calls = checkout.await_count
    assert client.post("/checkout/submit", json={**real, "confirm": False},
                       headers={**AUTH_HEADERS, "Idempotency-Key": "k0"}).json()["error"]["code"] == "confirmation_required"
    assert client.post("/checkout/submit", json=real, headers=AUTH_HEADERS).json()["error"]["code"] == "idempotency_key_required"
    missing = client.post("/checkout/submit", json=real, headers={**AUTH_HEADERS, "Idempotency-Key": "k1"})
    assert missing.status_code == 503 and missing.json()["error"]["code"] == "checkout_not_configured"
    assert checkout.await_count == calls

    monkeypatch.setattr(config, "SHUFERSAL_CVV", "042")
    # Caller supplied payment secrets are rejected without reflecting them.
    bad = client.post("/checkout/submit", json={**real, "cvv": "123"}, headers={**AUTH_HEADERS, "Idempotency-Key": "k2"})
    assert bad.status_code == 400 and "123" not in bad.text
    assert checkout.await_count == calls

    # Drift -> 409 before order placement; the key is burned.
    drift = client.post("/checkout/submit", json={**real, "confirm_total_ils": total + 10.0},
                        headers={**AUTH_HEADERS, "Idempotency-Key": "k3"})
    assert drift.status_code == 409 and drift.json()["error"]["code"] == "total_drift"
    reuse = client.post("/checkout/submit", json=real, headers={**AUTH_HEADERS, "Idempotency-Key": "k3"})
    assert reuse.status_code == 409 and reuse.json()["error"]["code"] == "idempotency_key_used"

    ok = client.post("/checkout/submit", json=real, headers={**AUTH_HEADERS, "Idempotency-Key": "k4"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "placed" and ok.json()["order_id"].startswith("MOCK-")
    assert "042" not in ok.text
    assert checkout.await_args.kwargs["cvv"] == "042"
    calls = checkout.await_count
    replay = client.post("/checkout/submit", json=real, headers={**AUTH_HEADERS, "Idempotency-Key": "k4"})
    assert replay.status_code == 200 and replay.json() == ok.json()
    assert checkout.await_count == calls  # the same key never submits twice
    # The cart is untouched by checkout.
    assert len(client.get("/cart", headers=AUTH_HEADERS).json()["items"]) == 1


def test_setup_checkout_cvv_file_is_private_and_preserves_settings(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("HOST=127.0.0.1\nSHUFERSAL_CVV=999\nPORT=8000\n", encoding="utf-8")
    os.chmod(env_file, 0o644)
    set_local_cvv("042", env_file)
    assert env_file.read_text(encoding="utf-8") == "HOST=127.0.0.1\nSHUFERSAL_CVV=042\nPORT=8000\n"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    with pytest.raises(ValueError):
        set_local_cvv("12x", env_file)
    assert "SHUFERSAL_CVV=042" in env_file.read_text(encoding="utf-8")

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
