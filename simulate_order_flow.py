"""
Simulates a complete remote agent shopping flow against the local Shufersal API.
Steps:
1. Health & Auth check
2. Search catalog for grocery items
3. Inspect item details
4. Clear & populate cart (with Idempotency-Key)
5. Select a delivery slot
6. Inspect default payment method (last4)
7. Request checkout quote
8. Test total drift protection
9. Submit order with confirmed total and CVV
"""
import time
import json
import httpx

API_BASE = "http://127.0.0.1:8000"
TOKEN = "simulated-agent-token"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

def print_step(title: str):
    print("\n" + "=" * 60)
    print(f"👉 {title}")
    print("=" * 60)

def main():
    client = httpx.Client(base_url=API_BASE, headers=HEADERS, timeout=10.0)

    # 1. Health check
    print_step("Step 1: Health & Session Status")
    res = client.get("/health")
    print(f"Status: {res.status_code}")
    print(json.dumps(res.json(), indent=2, ensure_ascii=False))

    # 2. Search catalog
    print_step("Step 2: Searching for Milk ('חלב')")
    res = client.get("/search", params={"q": "חלב", "limit": 3})
    print(f"Status: {res.status_code}")
    search_data = res.json()
    print(json.dumps(search_data, indent=2, ensure_ascii=False))
    selected_product = search_data["results"][0]
    product_id = selected_product["id"]

    # 3. Product detail
    print_step(f"Step 3: Fetching details for Product {product_id}")
    res = client.get(f"/product/{product_id}")
    print(f"Status: {res.status_code}")
    print(json.dumps(res.json(), indent=2, ensure_ascii=False))

    # 4. Cart management
    print_step("Step 4a: Clearing existing cart")
    res = client.delete("/cart")
    print(f"Status: {res.status_code}, Cart cleared.")

    print_step(f"Step 4b: Adding 2 units of {product_id} to cart (with Idempotency-Key)")
    res = client.post(
        "/cart/items",
        json={"product_id": product_id, "qty": 2.0},
        headers={"Idempotency-Key": "agent-order-idem-001"}
    )
    print(f"Status: {res.status_code}")
    cart_data = res.json()
    print(json.dumps(cart_data, indent=2, ensure_ascii=False))

    print_step("Step 4c: Adding 1 unit of Cottage Cheese (P54321)")
    res = client.post(
        "/cart/items",
        json={"product_id": "P54321", "qty": 1.0},
        headers={"Idempotency-Key": "agent-order-idem-002"}
    )
    print(f"Status: {res.status_code}")
    cart_data = res.json()
    print(json.dumps(cart_data, indent=2, ensure_ascii=False))

    # 5. Delivery Slots
    print_step("Step 5: Querying Available Delivery Slots")
    res = client.get("/delivery/slots")
    print(f"Status: {res.status_code}")
    slots_data = res.json()
    print(json.dumps(slots_data, indent=2, ensure_ascii=False))
    available_slots = [s for s in slots_data["slots"] if s["available"]]
    selected_slot = available_slots[0]
    slot_id = selected_slot["id"]
    print(f"Selected slot: {slot_id} ({selected_slot['date']} {selected_slot['window']})")

    # 6. Payment Method
    print_step("Step 6: Checking Default Payment Card (Masked)")
    res = client.get("/payment/method")
    print(f"Status: {res.status_code}")
    card_data = res.json()
    print(json.dumps(card_data, indent=2, ensure_ascii=False))
    print(f"Remote agent uses last4={card_data['last4']} to select corresponding CVV from secure vault.")

    # 7. Checkout Quote
    print_step("Step 7: Staging Checkout Quote")
    res = client.post(f"/checkout/quote?delivery_slot_id={slot_id}")
    print(f"Status: {res.status_code}")
    quote_data = res.json()
    print(json.dumps(quote_data, indent=2, ensure_ascii=False))
    quote_id = quote_data["quote_id"]
    quote_total = quote_data["total_ils"]

    # 8. Test Drift Protection
    print_step("Step 8: Testing Server-side Drift Protection (Submitting with altered price)")
    bad_submit = client.post(
        "/checkout/submit",
        json={
            "quote_id": quote_id,
            "confirm_total_ils": quote_total + 15.00,
            "cvv": "456"
        }
    )
    print(f"Status: {bad_submit.status_code} (Expected 409 Conflict)")
    print(json.dumps(bad_submit.json(), indent=2, ensure_ascii=False))

    # 9. Submit Order
    print_step("Step 9: Submitting Real Order with Confirmed Total and CVV")
    order_submit = client.post(
        "/checkout/submit",
        json={
            "quote_id": quote_id,
            "confirm_total_ils": quote_total,
            "cvv": "456"
        },
        headers={"Idempotency-Key": "agent-submit-order-999"}
    )
    print(f"Status: {order_submit.status_code}")
    order_data = order_submit.json()
    print(json.dumps(order_data, indent=2, ensure_ascii=False))

    print("\n" + "🎉" * 20)
    print(f"ORDER PLACED SUCCESSFULLY! Order ID: {order_data.get('order_id')}")
    print(f"Total Charged: {order_data.get('total_ils')} ILS")
    print(f"Estimated Delivery: {order_data.get('eta')}")
    print("🎉" * 20 + "\n")

if __name__ == "__main__":
    main()
