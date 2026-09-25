import requests

from app.core.config import settings

BASE_URL = "http://127.0.0.1:8000"

EMAIL = "admin@restaurantos.com"
PASSWORD = settings.admin_password

RESTAURANT_ID = "77779624-2f5d-4835-92ed-dcdd7438a83f"

TABLE_ID = "4afeda34-650f-4301-ab64-d78195388960"

BURGER_ID = "e8def57c-7e31-4ab9-84c4-7f48bc1adc57"
COLA_ID = "f5a55cf1-c5b9-45d1-8fae-91d41589f2a7"

KITCHEN_STATION_ID = "381b1e01-a7c4-432a-8048-c692da42b983"
BAR_STATION_ID = "082fb366-c950-4dd3-9b13-0ba268418148"


def check(response, expected_status, message):
    print(f"\n{response.request.method} {response.request.url}")
    print(f"STATUS: {response.status_code}")

    try:
        body = response.json()
        print(body)
    except Exception:
        body = response.text
        print(body)

    if response.status_code != expected_status:
        raise RuntimeError(
            f"{message}\n"
            f"Expected: {expected_status}\n"
            f"Actual: {response.status_code}\n"
            f"Response: {body}"
        )

    return body


def main():
    print("=" * 85)
    print("# RESTAURANTOS — FULL ORDER LIFECYCLE E2E TEST")
    print("=" * 85)

    session = requests.Session()

    # ============================================================
    # 1. LOGIN
    # ============================================================

    response = session.post(
        f"{BASE_URL}/api/v1/auth/login",
        json={
            "email": EMAIL,
            "password": PASSWORD,
        },
    )

    login_data = check(response, 200, "Login failed")

    access_token = login_data["access_token"]

    session.headers.update(
        {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
    )

    print("\n✅ LOGIN")

    # ============================================================
    # 2. CREATE ORDER
    # ============================================================

    response = session.post(
        f"{BASE_URL}/api/v1/orders",
        json={
            "table_id": TABLE_ID,
            "guest_count": 2,
        },
    )

    order = check(response, 201, "Order creation failed")

    order_id = order["id"]

    assert order["restaurant_id"] == RESTAURANT_ID
    assert order["table_id"] == TABLE_ID
    assert order["status"] == "OPEN"

    print(f"\n✅ ORDER CREATED: {order_id}")

    # ============================================================
    # 3. ADD BURGER
    # ============================================================

    response = session.post(
        f"{BASE_URL}/api/v1/orders/{order_id}/items",
        json={
            "product_id": BURGER_ID,
            "quantity": 1,
        },
    )

    burger_item = check(
        response,
        201,
        "Burger could not be added",
    )

    burger_item_id = burger_item["id"]

    assert burger_item["product_id"] == BURGER_ID
    assert burger_item["quantity"] == 1
    assert burger_item["status"] == "ADDED"

    print(f"\n✅ BURGER ADDED: {burger_item_id}")

    # ============================================================
    # 4. ADD COCA-COLA
    # ============================================================

    response = session.post(
        f"{BASE_URL}/api/v1/orders/{order_id}/items",
        json={
            "product_id": COLA_ID,
            "quantity": 1,
        },
    )

    cola_item = check(
        response,
        201,
        "Coca-Cola could not be added",
    )

    cola_item_id = cola_item["id"]

    assert cola_item["product_id"] == COLA_ID
    assert cola_item["quantity"] == 1
    assert cola_item["status"] == "ADDED"

    print(f"\n✅ COCA-COLA ADDED: {cola_item_id}")

    # ============================================================
    # 5. SEND ORDER
    # ============================================================

    response = session.post(
        f"{BASE_URL}/api/v1/orders/{order_id}/send"
    )

    sent_order = check(
        response,
        200,
        "Order could not be sent",
    )

    assert sent_order["status"] == "SENT"

    print("\n✅ ORDER SENT")

    # ============================================================
    # 6. CHECK KITCHEN QUEUE
    # ============================================================

    response = session.get(
        f"{BASE_URL}/api/v1/stations/"
        f"{KITCHEN_STATION_ID}/preparations"
    )

    kitchen_preparations = check(
        response,
        200,
        "Could not retrieve Kitchen preparations",
    )

    burger_preparations = [
        preparation
        for preparation in kitchen_preparations
        if preparation["order_item_id"] == burger_item_id
    ]

    assert burger_preparations, (
        "Burger preparation not found in Kitchen"
    )

    burger_preparation = burger_preparations[0]
    burger_preparation_id = burger_preparation["id"]

    assert burger_preparation["station_id"] == KITCHEN_STATION_ID
    assert burger_preparation["status"] == "PENDING"

    print(
        f"\n✅ BURGER ROUTED TO KITCHEN: "
        f"{burger_preparation_id}"
    )

    # ============================================================
    # 7. CHECK BAR QUEUE
    # ============================================================

    response = session.get(
        f"{BASE_URL}/api/v1/stations/"
        f"{BAR_STATION_ID}/preparations"
    )

    bar_preparations = check(
        response,
        200,
        "Could not retrieve Bar preparations",
    )

    cola_preparations = [
        preparation
        for preparation in bar_preparations
        if preparation["order_item_id"] == cola_item_id
    ]

    assert cola_preparations, (
        "Coca-Cola preparation not found in Bar"
    )

    cola_preparation = cola_preparations[0]
    cola_preparation_id = cola_preparation["id"]

    assert cola_preparation["station_id"] == BAR_STATION_ID
    assert cola_preparation["status"] == "PENDING"

    print(
        f"\n✅ COCA-COLA ROUTED TO BAR: "
        f"{cola_preparation_id}"
    )

    # ============================================================
    # 8. KITCHEN: START
    # ============================================================

    response = session.post(
        f"{BASE_URL}/api/v1/preparations/"
        f"{burger_preparation_id}/start"
    )

    burger_preparation = check(
        response,
        200,
        "Could not start Burger preparation",
    )

    assert burger_preparation["status"] == "IN_PROGRESS"

    print("\n✅ BURGER PREPARATION STARTED")

    # ============================================================
    # 9. KITCHEN: READY
    # ============================================================

    response = session.post(
        f"{BASE_URL}/api/v1/preparations/"
        f"{burger_preparation_id}/ready"
    )

    burger_preparation = check(
        response,
        200,
        "Could not mark Burger as ready",
    )

    assert burger_preparation["status"] == "READY"

    print("\n✅ BURGER READY")

    # ============================================================
    # 10. KITCHEN: SERVE
    # ============================================================

    response = session.post(
        f"{BASE_URL}/api/v1/preparations/"
        f"{burger_preparation_id}/serve"
    )

    burger_preparation = check(
        response,
        200,
        "Could not serve Burger",
    )

    assert burger_preparation["status"] == "SERVED"

    print("\n✅ BURGER SERVED")

    # ============================================================
    # 11. BAR: START
    # ============================================================

    response = session.post(
        f"{BASE_URL}/api/v1/preparations/"
        f"{cola_preparation_id}/start"
    )

    cola_preparation = check(
        response,
        200,
        "Could not start Coca-Cola preparation",
    )

    assert cola_preparation["status"] == "IN_PROGRESS"

    print("\n✅ COCA-COLA PREPARATION STARTED")

    # ============================================================
    # 12. BAR: READY
    # ============================================================

    response = session.post(
        f"{BASE_URL}/api/v1/preparations/"
        f"{cola_preparation_id}/ready"
    )

    cola_preparation = check(
        response,
        200,
        "Could not mark Coca-Cola as ready",
    )

    assert cola_preparation["status"] == "READY"

    print("\n✅ COCA-COLA READY")

    # ============================================================
    # 13. BAR: SERVE
    # ============================================================

    response = session.post(
        f"{BASE_URL}/api/v1/preparations/"
        f"{cola_preparation_id}/serve"
    )

    cola_preparation = check(
        response,
        200,
        "Could not serve Coca-Cola",
    )

    assert cola_preparation["status"] == "SERVED"

    print("\n✅ COCA-COLA SERVED")

    # ============================================================
    # 14. CREATE BILL
    # ============================================================

    response = session.post(
        f"{BASE_URL}/api/v1/bills/order/{order_id}"
    )

    bill = check(
        response,
        201,
        "Could not create bill",
    )

    bill_id = bill["id"]

    assert bill["order_id"] == order_id
    assert bill["status"] == "OPEN"
    assert bill["subtotal"] == "15.00"
    assert bill["total"] == "15.00"

    print(f"\n✅ BILL CREATED: {bill_id}")
    print(f"   SUBTOTAL: €{bill['subtotal']}")
    print(f"   TOTAL:    €{bill['total']}")

    # ============================================================
    # 15. PAY CASH €10
    # ============================================================

    response = session.post(
        f"{BASE_URL}/api/v1/bills/{bill_id}/payments",
        json={
            "amount": "10.00",
            "method": "CASH",
        },
    )

    cash_payment = check(
        response,
        201,
        "Cash payment failed",
    )

    assert cash_payment["amount"] == "10.00"
    assert cash_payment["method"] == "CASH"
    assert cash_payment["status"] == "COMPLETED"

    print("\n✅ CASH PAYMENT: €10.00")

    # ============================================================
    # 16. PAY CARD €5
    # ============================================================

    response = session.post(
        f"{BASE_URL}/api/v1/bills/{bill_id}/payments",
        json={
            "amount": "5.00",
            "method": "CARD",
        },
    )

    card_payment = check(
        response,
        201,
        "Card payment failed",
    )

    assert card_payment["amount"] == "5.00"
    assert card_payment["method"] == "CARD"
    assert card_payment["status"] == "COMPLETED"

    print("\n✅ CARD PAYMENT: €5.00")

    # ============================================================
    # 17. VERIFY BILL PAID
    # ============================================================

    response = session.get(
        f"{BASE_URL}/api/v1/bills/{bill_id}"
    )

    paid_bill = check(
        response,
        200,
        "Could not retrieve final bill",
    )

    assert paid_bill["status"] == "PAID"
    assert paid_bill["total"] == "15.00"

    print("\n✅ BILL PAID")
    print("   CASH: €10.00")
    print("   CARD: €5.00")
    print("   TOTAL: €15.00")

    # ============================================================
    # 18. VERIFY ORDER CLOSED
    # ============================================================

    response = session.get(
        f"{BASE_URL}/api/v1/orders/{order_id}"
    )

    final_order = check(
        response,
        200,
        "Could not retrieve final order",
    )

    assert final_order["status"] == "CLOSED"

    print("\n✅ ORDER CLOSED")

    # ============================================================
    # 19. CHECK OPERATIONAL EVENTS
    # ============================================================

    # ------------------------------------------------------------
    # Order-level events
    # ------------------------------------------------------------

    response = session.get(
        f"{BASE_URL}/api/v1/events",
        params={
            "entity_id": order_id,
            "limit": 100,
        },
    )

    order_events = check(
        response,
        200,
        "Could not retrieve order operational events",
    )

    order_event_types = {
        event["event_type"]
        for event in order_events
    }

    # ------------------------------------------------------------
    # Burger item events
    # ------------------------------------------------------------

    response = session.get(
        f"{BASE_URL}/api/v1/events",
        params={
            "entity_id": burger_item_id,
            "limit": 100,
        },
    )

    burger_events = check(
        response,
        200,
        "Could not retrieve Burger item events",
    )

    burger_event_types = {
        event["event_type"]
        for event in burger_events
    }

    # ------------------------------------------------------------
    # Coca-Cola item events
    # ------------------------------------------------------------

    response = session.get(
        f"{BASE_URL}/api/v1/events",
        params={
            "entity_id": cola_item_id,
            "limit": 100,
        },
    )

    cola_events = check(
        response,
        200,
        "Could not retrieve Coca-Cola item events",
    )

    cola_event_types = {
        event["event_type"]
        for event in cola_events
    }

    print("\nORDER EVENTS:")

    for event_type in sorted(order_event_types):
        print(f"   - {event_type}")

    print("\nBURGER ITEM EVENTS:")

    for event_type in sorted(burger_event_types):
        print(f"   - {event_type}")

    print("\nCOCA-COLA ITEM EVENTS:")

    for event_type in sorted(cola_event_types):
        print(f"   - {event_type}")

    # ------------------------------------------------------------
    # Verify expected events
    # ------------------------------------------------------------

    expected_order_events = {
        "order.created",
        "order.sent",
        "order.closed",
    }

    missing_order_events = (
        expected_order_events - order_event_types
    )

    assert not missing_order_events, (
        f"Missing order events: "
        f"{sorted(missing_order_events)}"
    )

    assert "item.added" in burger_event_types, (
        "Missing item.added event for Burger"
    )

    assert "item.added" in cola_event_types, (
        "Missing item.added event for Coca-Cola"
    )

    print("\n✅ OPERATIONAL EVENTS VERIFIED")

    # ============================================================
    # 20. FINAL VERIFICATION
    # ============================================================

    response = session.get(
        f"{BASE_URL}/api/v1/orders/{order_id}"
    )

    final_order = check(
        response,
        200,
        "Could not verify final order state",
    )

    assert final_order["status"] == "CLOSED"

    print("\n" + "=" * 85)
    print("🎉 FULL ORDER LIFECYCLE PASSED")
    print("=" * 85)

    print(
        """
LOGIN             ✅
ORDER CREATED     ✅
BURGER ADDED      ✅
COCA-COLA ADDED   ✅
ORDER SENT        ✅

BURGER → KITCHEN  ✅
COLA → BAR        ✅

BURGER START      ✅
BURGER READY      ✅
BURGER SERVED     ✅

COLA START        ✅
COLA READY        ✅
COLA SERVED       ✅

BILL CREATED      ✅
CASH €10          ✅
CARD €5           ✅
BILL PAID         ✅
ORDER CLOSED      ✅
EVENTS            ✅
"""
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("\n" + "=" * 85)
        print("❌ TEST FAILED")
        print("=" * 85)
        print(f"\n{exc}")
        raise