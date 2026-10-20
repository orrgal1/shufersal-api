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

# 2. Dry run (synchronous: walks checkout, stops before placing, answers 200)
curl -X POST https://judge-bishop-jelsoft-multi.trycloudflare.com/checkout/submit \
  -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" \
  -d '{"quote_id": "quote_xxx", "confirm_total_ils": 50.60, "expected_cart_hash": "<from quote>", "dry_run": true}'

# 3. Real order (asynchronous; the CVV is configured locally, never sent)
curl -X POST https://judge-bishop-jelsoft-multi.trycloudflare.com/checkout/submit \
  -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" \
  -H "Idempotency-Key: order-2026-09-20-1" \
  -d '{"quote_id": "quote_xxx", "confirm_total_ils": 50.60, "expected_cart_hash": "<from quote>",
       "dry_run": false, "confirm": true}'

# 4. Poll for the result
curl https://judge-bishop-jelsoft-multi.trycloudflare.com/checkout/submit/order-2026-09-20-1 \
  -H "Authorization: Bearer $JWT"
```

A real order (`dry_run: false`) takes 1-6 minutes (SMS re-auth, checkout
checks, the place-order click, 3-D Secure and the confirmation page), longer
than the gateway's 60 s upstream timeout. So it runs in the background:

- Once the request is validated and the Idempotency-Key is claimed, the first
  answer is immediately `202` with `Retry-After: 30` and
  `{"status": "in_progress", "poll": "same request, same Idempotency-Key", "retry_after": 30, "poll_url": "/checkout/submit/<key>", ...}`.
  Validation failures (quote, cart hash, confirm, cap, key reuse) still answer
  at once with their 4xx code and start nothing.
- Poll every `retry_after` seconds, either by sending the same request with the
  same Idempotency-Key or with `GET /checkout/submit/{idempotency_key}` (bearer
  auth required). While the attempt runs both answer `202 in_progress`; then
  both answer the stored final result with `Idempotent-Replayed: true`: the same
  HTTP status and body a synchronous submit used to return (`200 placed`,
  `202 three_ds_required | placed_unconfirmed | outcome_unknown | handover_timeout`,
  or the 4xx/502 error).
- Never retry with a new key while an attempt is `in_progress`. A polled key
  never runs the checkout again.
- After a service restart, a key whose attempt was claimed but never reached
  the click answers `409 attempt_interrupted` (get a new quote and key); one
  that reached the click (`placing`) answers `409 order_outcome_unresolved`
  until Or resolves it with `checkout_ledger.py resolve`. `GET` on a key no
  attempt used answers `404 idempotency_key_unknown`.
- A graceful service shutdown waits for a running attempt before closing the browser.
