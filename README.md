# Shufersal Local API Wrapper (Spec v1.1)

A persistent local FastAPI service driven by Playwright and in-page network protocol requests wrapping `shufersal.co.il` behind an authenticated JSON HTTP API, exposed publicly via secure tunnel with **JWT authentication** and an unauthenticated **OpenAPI** inspection endpoint.

---

## 🌐 Public Deployment & Access

- **Public Base URL:** `https://judge-bishop-jelsoft-multi.trycloudflare.com`
- **OpenAPI Schema (Inspection):** `https://judge-bishop-jelsoft-multi.trycloudflare.com/openapi.json`
- **Interactive Swagger UI:** `https://judge-bishop-jelsoft-multi.trycloudflare.com/docs`
- **Authentication:** `Authorization: Bearer <JWT_TOKEN>`

---

## 🔑 Generating JWT Tokens for Remote Agents

A JWT token generator utility is included:

### Generating a Token
```bash
./venv/bin/python generate_token.py --subject "remote-shopping-agent" --days 365
```
*(Do not commit or publish JWT tokens).*

---

## 🚀 Service Architecture & Guarantees

- **100% Headless Operation:** Runs completely headless using persistent browser context and session cookies.
- **Direct Network Protocol:** Catalog search, item details, and cart operations execute via in-page JSON protocol requests (`fetch`) reusing the session cookies for sub-100ms response times.
- **JWT Authentication:** Strict signature and expiration verification on every private endpoint.
- **OpenAPI Inspection:** `/openapi.json` and `/docs` are open for external agent discovery without requiring authentication headers.
- **Payment & CVV Safety:** Full card numbers are never accepted, stored, or logged. CVV is strictly ephemeral and never returned.
- **Total Drift Protection:** Re-reads live site total before submitting; aborts with `409 Conflict` if drift occurs.
- **Idempotency:** Accepts `Idempotency-Key` headers on mutations to prevent double-charging or duplicate cart entries.

---

## 📚 Remote Agent Quick Reference

### 1. Inspect OpenAPI Specification
```bash
curl -s https://judge-bishop-jelsoft-multi.trycloudflare.com/openapi.json | jq .
```

### 2. Check Health & Session Status
```bash
curl -X GET https://judge-bishop-jelsoft-multi.trycloudflare.com/health \
  -H "Authorization: Bearer $JWT"
```

### 3. Search Products
```bash
curl -X GET "https://judge-bishop-jelsoft-multi.trycloudflare.com/search?q=חלב&limit=5" \
  -H "Authorization: Bearer $JWT"
```

### 4. Manage Cart
```bash
# Add to cart with Idempotency Key
curl -X POST https://judge-bishop-jelsoft-multi.trycloudflare.com/cart/items \
  -H "Authorization: Bearer $JWT" \
  -H "Idempotency-Key: req-001" \
  -H "Content-Type: application/json" \
  -d '{"product_id": "P12345", "qty": 2.0}'

# View Cart
curl -X GET https://judge-bishop-jelsoft-multi.trycloudflare.com/cart \
  -H "Authorization: Bearer $JWT"

# Clear Cart
curl -X DELETE https://judge-bishop-jelsoft-multi.trycloudflare.com/cart \
  -H "Authorization: Bearer $JWT"
```

### 5. Delivery Slots
```bash
curl -X GET https://judge-bishop-jelsoft-multi.trycloudflare.com/delivery/slots \
  -H "Authorization: Bearer $JWT"
```

### 6. Masked Payment Card Details
```bash
curl -X GET https://judge-bishop-jelsoft-multi.trycloudflare.com/payment/method \
  -H "Authorization: Bearer $JWT"
```

### 7. Checkout Flow
```bash
# 1. Stage quote
curl -X POST "https://judge-bishop-jelsoft-multi.trycloudflare.com/checkout/quote?delivery_slot_id=slot_2026-09-20_morning" \
  -H "Authorization: Bearer $JWT"

# 2. Submit order (requires matching confirm_total_ils and CVV)
curl -X POST https://judge-bishop-jelsoft-multi.trycloudflare.com/checkout/submit \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "quote_id": "quote_xxx",
    "confirm_total_ils": 50.60,
    "cvv": "123"
  }'
```
