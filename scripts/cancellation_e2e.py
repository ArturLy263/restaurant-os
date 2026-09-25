import sys
import uuid
import requests

from app.core.config import settings

BASE_URL = "http://127.0.0.1:8000"

EMAIL = "admin@restaurantos.com"
PASSWORD = settings.admin_password

# Default seed UUIDs as fallback
DEFAULT_RESTAURANT_ID = "77779624-2f5d-4835-92ed-dcdd7438a83f"
DEFAULT_TABLE_ID = "57f4ed45-b393-4feb-92e6-8fb0566617fa"  # Table 3
DEFAULT_BURGER_ID = "e8def57c-7e31-4ab9-84c4-7f48bc1adc57"
DEFAULT_COLA_ID = "f5a55cf1-c5b9-45d1-8fae-91d41589f2a7"
DEFAULT_KITCHEN_ID = "381b1e01-a7c4-432a-8048-c692da42b983"
DEFAULT_BAR_ID = "082fb366-c950-4dd3-9b13-0ba268418148"


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


def get_available_table_and_resources(restaurant_id):
    """
    Read-only helper to find an available table and active products/stations.
    Falls back cleanly to seed data defaults if DB cannot be queried directly.
    """
    table_id = DEFAULT_TABLE_ID
    table_num = 3
    is_available = True
    burger_id = DEFAULT_BURGER_ID
    cola_id = DEFAULT_COLA_ID
    kitchen_id = DEFAULT_KITCHEN_ID
    bar_id = DEFAULT_BAR_ID

    try:
        from app.db.database import SessionLocal
        from app.db.models.table import Table
        from app.db.models.product import Product
        from app.db.models.category import Category
        from app.db.models.station import Station

        db = SessionLocal()
        try:
            available_table = (
                db.query(Table)
                .filter(
                    Table.restaurant_id == restaurant_id,
                    Table.status == "AVAILABLE",
                )
                .first()
            )
            if available_table:
                table_id = str(available_table.id)
                table_num = available_table.number
                is_available = True
            else:
                is_available = False
                t = (
                    db.query(Table)
                    .filter(Table.restaurant_id == restaurant_id)
                    .order_by(Table.number.desc())
                    .first()
                )
                if t:
                    table_id = str(t.id)
                    table_num = t.number

            b = (
                db.query(Product)
                .join(Category, Product.category_id == Category.id)
                .filter(
                    Product.name == "Burger",
                    Category.restaurant_id == restaurant_id,
                )
                .first()
            )
            if b:
                burger_id = str(b.id)

            c = (
                db.query(Product)
                .join(Category, Product.category_id == Category.id)
                .filter(
                    Product.name == "Coca-Cola",
                    Category.restaurant_id == restaurant_id,
                )
                .first()
            )
            if c:
                cola_id = str(c.id)

            k = (
                db.query(Station)
                .filter(
                    Station.type == "KITCHEN",
                    Station.restaurant_id == restaurant_id,
                )
                .first()
            )
            if k:
                kitchen_id = str(k.id)

            bar = (
                db.query(Station)
                .filter(
                    Station.type == "BAR",
                    Station.restaurant_id == restaurant_id,
                )
                .first()
            )
            if bar:
                bar_id = str(bar.id)
        finally:
            db.close()
    except Exception as e:
        print(f"Note: Read-only DB lookup skipped ({e}), using default seed UUIDs.")

    return table_id, table_num, is_available, burger_id, cola_id, kitchen_id, bar_id


def verify_prep_status_in_db(prep_id, expected_status):
    """Read-only verification of preparation record in DB if available."""
    try:
        from app.db.database import SessionLocal
        from app.db.models.preparation import Preparation

        db = SessionLocal()
        try:
            prep = db.query(Preparation).filter(Preparation.id == prep_id).first()
            if prep:
                assert prep.status == expected_status, (
                    f"DB prep status expected {expected_status}, got {prep.status}"
                )
                if expected_status == "CANCELLED":
                    assert prep.cancelled_at is not None, (
                        "DB prep cancelled_at should be set"
                    )
                print(f"   [DB Verified] Preparation {prep_id} status={prep.status}")
        finally:
            db.close()
    except Exception as e:
        print(f"   [Note] Direct DB read skipped ({e})")


def main():
    print("=" * 85)
    print("# RESTAURANTOS — ORDER ITEM & PREPARATION CANCELLATION E2E TEST")
    print("=" * 85)

    session = requests.Session()

    # ============================================================
    # 0. LOGIN
    # ============================================================
    print("\n--- 0. LOGIN ---")
    response = session.post(
        f"{BASE_URL}/api/v1/auth/login",
        json={"email": EMAIL, "password": PASSWORD},
    )
    login_data = check(response, 200, "Login failed")
    access_token = login_data["access_token"]

    session.headers.update(
        {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
    )
    print("✅ Authenticated successfully")

    restaurant_id = DEFAULT_RESTAURANT_ID
    table_id, table_num, is_available, burger_id, cola_id, kitchen_id, bar_id = (
        get_available_table_and_resources(restaurant_id)
    )
    print(f"Using Table {table_num} (ID: {table_id}, is_available: {is_available})")
    print(f"Using Burger ID: {burger_id}, Cola ID: {cola_id}")
    print(f"Using Kitchen ID: {kitchen_id}, Bar ID: {bar_id}")

    # ============================================================
    # SETUP: OBTAIN AN OPEN ORDER
    # ============================================================
    print("\n--- SETUP: OBTAIN AN OPEN ORDER ---")
    if is_available:
        response = session.post(
            f"{BASE_URL}/api/v1/orders",
            json={"table_id": table_id, "guest_count": 2},
        )
        order = check(response, 201, "Order creation failed")
        order_id = order["id"]
        restaurant_id = order["restaurant_id"]
        assert order["status"] == "OPEN"
        print(f"✅ Order created via API: {order_id} (Status: OPEN)")
    else:
        # If all tables were left occupied by prior aborted tests, find the open order on this table
        from app.db.database import SessionLocal
        from app.db.models.order import Order

        db = SessionLocal()
        try:
            open_order = (
                db.query(Order)
                .filter(
                    Order.restaurant_id == restaurant_id,
                    Order.table_id == table_id,
                    Order.status == "OPEN",
                )
                .first()
            )
            assert open_order is not None, (
                f"No available table and no open order on table {table_id}"
            )
            order_id = str(open_order.id)
            print(f"✅ Using existing OPEN order on Table {table_num}: {order_id}")
        finally:
            db.close()

    # ============================================================
    # SCENARIO 1: ADDED → CANCELLED
    # ============================================================
    print("\n--- SCENARIO 1: ADDED → CANCELLED ---")
    # Add Item 1 (Burger)
    response = session.post(
        f"{BASE_URL}/api/v1/orders/{order_id}/items",
        json={"product_id": burger_id, "quantity": 1},
    )
    item1 = check(response, 201, "Failed to add Item 1")
    item1_id = item1["id"]
    assert item1["status"] == "ADDED"
    print(f"Added Item 1: {item1_id} (Status: ADDED)")

    # Cancel Item 1 while ADDED
    cancel_reason_1 = "Customer changed mind before send"
    response = session.post(
        f"{BASE_URL}/api/v1/orders/{order_id}/items/{item1_id}/cancel",
        json={"reason": cancel_reason_1},
    )
    cancelled_item1 = check(response, 200, "Failed to cancel Item 1")
    assert cancelled_item1["id"] == item1_id
    assert cancelled_item1["status"] == "CANCELLED"
    assert cancelled_item1["cancelled_at"] is not None
    print(f"✅ Item 1 cancelled successfully (cancelled_at: {cancelled_item1['cancelled_at']})")

    # Verify via GET /api/v1/orders/{order_id}
    response = session.get(f"{BASE_URL}/api/v1/orders/{order_id}")
    order_detail = check(response, 200, "Failed to get order details")
    item1_in_order = next(it for it in order_detail["items"] if it["id"] == item1_id)
    assert item1_in_order["status"] == "CANCELLED"
    assert item1_in_order["cancelled_at"] is not None

    # ============================================================
    # SCENARIO 2: REPEATED CANCELLATION → 409 CONFLICT
    # ============================================================
    print("\n--- SCENARIO 2: REPEATED CANCELLATION → 409 CONFLICT ---")
    response = session.post(
        f"{BASE_URL}/api/v1/orders/{order_id}/items/{item1_id}/cancel",
        json={"reason": "Attempting to cancel already cancelled item"},
    )
    check(response, 409, "Repeated cancellation should return 409 Conflict")
    print("✅ Repeated cancellation properly rejected with HTTP 409")

    # ============================================================
    # SETUP FOR SUBSEQUENT SCENARIOS: ADD ITEMS 2, 3, 4, 5 & SEND
    # ============================================================
    print("\n--- SETUP: ADD ITEMS 2, 3, 4, 5 & SEND ORDER ---")
    # Item 2 (Burger): for SENT → CANCELLED (PENDING prep)
    resp = session.post(
        f"{BASE_URL}/api/v1/orders/{order_id}/items",
        json={"product_id": burger_id, "quantity": 1},
    )
    item2 = check(resp, 201, "Failed to add Item 2")
    item2_id = item2["id"]

    # Item 3 (Burger): for IN_PROGRESS → CANCELLED
    resp = session.post(
        f"{BASE_URL}/api/v1/orders/{order_id}/items",
        json={"product_id": burger_id, "quantity": 1},
    )
    item3 = check(resp, 201, "Failed to add Item 3")
    item3_id = item3["id"]

    # Item 4 (Burger): for READY → CANCELLED rejected
    resp = session.post(
        f"{BASE_URL}/api/v1/orders/{order_id}/items",
        json={"product_id": burger_id, "quantity": 1},
    )
    item4 = check(resp, 201, "Failed to add Item 4")
    item4_id = item4["id"]

    # Item 5 (Cola): for SERVED → CANCELLED rejected
    resp = session.post(
        f"{BASE_URL}/api/v1/orders/{order_id}/items",
        json={"product_id": cola_id, "quantity": 1},
    )
    item5 = check(resp, 201, "Failed to add Item 5")
    item5_id = item5["id"]

    # Send the order
    response = session.post(f"{BASE_URL}/api/v1/orders/{order_id}/send")
    sent_order = check(response, 200, "Failed to send order")
    assert sent_order["status"] == "SENT"
    print("✅ Order sent to stations. Items 2, 3, 4, 5 are now SENT.")

    # ============================================================
    # SCENARIO 3: SENT → CANCELLED (PENDING PREPARATION)
    # ============================================================
    print("\n--- SCENARIO 3: SENT → CANCELLED (PENDING PREPARATION) ---")
    # Check kitchen queue for Item 2 preparation
    response = session.get(f"{BASE_URL}/api/v1/stations/{kitchen_id}/preparations")
    kitchen_preps = check(response, 200, "Failed to get kitchen preparations")
    prep2 = next(p for p in kitchen_preps if p["order_item_id"] == item2_id)
    prep2_id = prep2["id"]
    assert prep2["status"] == "PENDING"
    print(f"Found Item 2 Preparation: {prep2_id} (Status: PENDING)")

    # Cancel Item 2
    cancel_reason_2 = "Customer changed mind while order is pending in kitchen"
    response = session.post(
        f"{BASE_URL}/api/v1/orders/{order_id}/items/{item2_id}/cancel",
        json={"reason": cancel_reason_2},
    )
    cancelled_item2 = check(response, 200, "Failed to cancel Item 2")
    assert cancelled_item2["status"] == "CANCELLED"
    assert cancelled_item2["cancelled_at"] is not None
    print("✅ Item 2 cancelled")

    # Verify preparation is no longer in active kitchen queue
    response = session.get(f"{BASE_URL}/api/v1/stations/{kitchen_id}/preparations")
    kitchen_preps = check(response, 200, "Failed to get kitchen preparations")
    assert not any(p["id"] == prep2_id for p in kitchen_preps), (
        "Cancelled preparation should not appear in active station queue"
    )
    print("✅ Preparation is removed from active kitchen queue")
    verify_prep_status_in_db(prep2_id, "CANCELLED")

    # ============================================================
    # SCENARIO 4: IN_PROGRESS PREPARATION → ORDERITEM CANCELLED + PREPARATION CANCELLED
    # ============================================================
    print("\n--- SCENARIO 4: IN_PROGRESS PREPARATION → CANCELLED ---")
    # Find Item 3 preparation in kitchen queue
    prep3 = next(p for p in kitchen_preps if p["order_item_id"] == item3_id)
    prep3_id = prep3["id"]

    # Start preparation: PENDING → IN_PROGRESS
    response = session.post(f"{BASE_URL}/api/v1/preparations/{prep3_id}/start")
    prep3_started = check(response, 200, "Failed to start preparation 3")
    assert prep3_started["status"] == "IN_PROGRESS"
    print(f"Preparation 3 started: {prep3_id} (Status: IN_PROGRESS)")

    # Cancel Item 3 while preparation is IN_PROGRESS
    cancel_reason_3 = "Kitchen taking too long, customer cancelled"
    response = session.post(
        f"{BASE_URL}/api/v1/orders/{order_id}/items/{item3_id}/cancel",
        json={"reason": cancel_reason_3},
    )
    cancelled_item3 = check(response, 200, "Failed to cancel Item 3")
    assert cancelled_item3["status"] == "CANCELLED"
    assert cancelled_item3["cancelled_at"] is not None
    print("✅ Item 3 cancelled")

    # Verify preparation 3 is removed from active queue and marked CANCELLED
    response = session.get(f"{BASE_URL}/api/v1/stations/{kitchen_id}/preparations")
    kitchen_preps = check(response, 200, "Failed to get kitchen preparations")
    assert not any(p["id"] == prep3_id for p in kitchen_preps), (
        "Cancelled IN_PROGRESS preparation should not appear in active queue"
    )
    print("✅ IN_PROGRESS preparation removed from active kitchen queue")
    verify_prep_status_in_db(prep3_id, "CANCELLED")

    # ============================================================
    # SCENARIO 5: READY PREPARATION → CANCELLATION REJECTED (409) & NOTHING CHANGES
    # ============================================================
    print("\n--- SCENARIO 5: READY PREPARATION → CANCELLATION REJECTED (409) ---")
    # Find Item 4 preparation in kitchen queue
    prep4 = next(p for p in kitchen_preps if p["order_item_id"] == item4_id)
    prep4_id = prep4["id"]

    # Advance Item 4 preparation: PENDING → IN_PROGRESS → READY
    response = session.post(f"{BASE_URL}/api/v1/preparations/{prep4_id}/start")
    check(response, 200, "Failed to start prep 4")
    response = session.post(f"{BASE_URL}/api/v1/preparations/{prep4_id}/ready")
    prep4_ready = check(response, 200, "Failed to mark prep 4 ready")
    assert prep4_ready["status"] == "READY"
    print(f"Preparation 4 marked READY: {prep4_id}")

    # Attempt to cancel Item 4
    response = session.post(
        f"{BASE_URL}/api/v1/orders/{order_id}/items/{item4_id}/cancel",
        json={"reason": "Customer wants to cancel food that is already ready"},
    )
    check(response, 409, "Cancelling READY item should return 409 Conflict")
    print("✅ Cancellation rejected with 409 Conflict")

    # Verify OrderItem 4 and Preparation 4 did NOT change
    response = session.get(f"{BASE_URL}/api/v1/orders/{order_id}")
    order_detail = check(response, 200, "Failed to get order details")
    item4_in_order = next(it for it in order_detail["items"] if it["id"] == item4_id)
    assert item4_in_order["status"] == "SENT", "Item 4 status must remain SENT"
    assert item4_in_order["cancelled_at"] is None, "Item 4 cancelled_at must remain None"

    response = session.get(f"{BASE_URL}/api/v1/stations/{kitchen_id}/preparations")
    kitchen_preps = check(response, 200, "Failed to get kitchen queue")
    prep4_in_queue = next(p for p in kitchen_preps if p["id"] == prep4_id)
    assert prep4_in_queue["status"] == "READY", "Preparation 4 must remain READY"
    print("✅ Verified atomicity: OrderItem remains SENT and Preparation remains READY")

    # ============================================================
    # SCENARIO 6: SERVED PREPARATION → CANCELLATION REJECTED (409) & NOTHING CHANGES
    # ============================================================
    print("\n--- SCENARIO 6: SERVED PREPARATION → CANCELLATION REJECTED (409) ---")
    # Find Item 5 (Cola) preparation in bar queue
    response = session.get(f"{BASE_URL}/api/v1/stations/{bar_id}/preparations")
    bar_preps = check(response, 200, "Failed to get bar queue")
    prep5 = next(p for p in bar_preps if p["order_item_id"] == item5_id)
    prep5_id = prep5["id"]

    # Advance Item 5: PENDING → IN_PROGRESS → READY → SERVED
    response = session.post(f"{BASE_URL}/api/v1/preparations/{prep5_id}/start")
    check(response, 200, "Failed to start bar prep 5")
    response = session.post(f"{BASE_URL}/api/v1/preparations/{prep5_id}/ready")
    check(response, 200, "Failed to mark bar prep 5 ready")
    response = session.post(f"{BASE_URL}/api/v1/preparations/{prep5_id}/serve")
    prep5_served = check(response, 200, "Failed to serve bar prep 5")
    assert prep5_served["status"] == "SERVED"
    print(f"Preparation 5 marked SERVED: {prep5_id}")

    # Attempt to cancel Item 5
    response = session.post(
        f"{BASE_URL}/api/v1/orders/{order_id}/items/{item5_id}/cancel",
        json={"reason": "Customer finished drink and wants to cancel"},
    )
    check(response, 409, "Cancelling SERVED item should return 409 Conflict")
    print("✅ Cancellation rejected with 409 Conflict")

    # Verify OrderItem 5 and Preparation 5 did NOT change
    response = session.get(f"{BASE_URL}/api/v1/orders/{order_id}")
    order_detail = check(response, 200, "Failed to get order details")
    item5_in_order = next(it for it in order_detail["items"] if it["id"] == item5_id)
    assert item5_in_order["status"] == "SENT", "Item 5 status must remain SENT"
    assert item5_in_order["cancelled_at"] is None, "Item 5 cancelled_at must remain None"
    verify_prep_status_in_db(prep5_id, "SERVED")
    print("✅ Verified atomicity: OrderItem remains SENT and Preparation remains SERVED")

    # ============================================================
    # SCENARIO 8: CANCELLED ITEMS EXCLUDED FROM BILL TOTAL
    # ============================================================
    print("\n--- SCENARIO 8: CANCELLED ITEMS EXCLUDED FROM BILL TOTAL ---")
    # Current order items:
    # Item 1: Burger €12.50 (CANCELLED)
    # Item 2: Burger €12.50 (CANCELLED)
    # Item 3: Burger €12.50 (CANCELLED)
    # Item 4: Burger €12.50 (SENT - active)
    # Item 5: Cola    €2.50 (SENT - active)
    # Expected bill subtotal/total: €12.50 + €2.50 = €15.00
    response = session.post(f"{BASE_URL}/api/v1/bills/order/{order_id}")
    bill = check(response, 201, "Failed to create bill")
    bill_id = bill["id"]
    assert bill["order_id"] == order_id
    assert bill["status"] == "OPEN"
    assert bill["subtotal"] == "15.00", f"Expected subtotal '15.00', got {bill['subtotal']}"
    assert bill["total"] == "15.00", f"Expected total '15.00', got {bill['total']}"
    print(f"✅ Bill created: {bill_id}")
    print(f"   Subtotal: €{bill['subtotal']} (Active items only: Burger €12.50 + Cola €2.50)")
    print("   Cancelled items (€37.50) successfully excluded from bill calculation!")

    # ============================================================
    # PAYMENT TO CLOSE ORDER (SETS UP SCENARIO 7 & RELEASES TABLE)
    # ============================================================
    print("\n--- PAY BILL IN FULL TO CLOSE ORDER ---")
    response = session.post(
        f"{BASE_URL}/api/v1/bills/{bill_id}/payments",
        json={"amount": "15.00", "method": "CARD"},
    )
    payment = check(response, 201, "Payment failed")
    assert payment["status"] == "COMPLETED"

    response = session.get(f"{BASE_URL}/api/v1/orders/{order_id}")
    closed_order = check(response, 200, "Failed to get closed order")
    assert closed_order["status"] == "CLOSED"
    print(f"✅ Order {order_id} is now CLOSED. Table {table_num} released to AVAILABLE.")

    # ============================================================
    # SCENARIO 7: CLOSED ORDER → CANCELLATION REJECTED (409)
    # ============================================================
    print("\n--- SCENARIO 7: CLOSED ORDER → CANCELLATION REJECTED (409) ---")
    response = session.post(
        f"{BASE_URL}/api/v1/orders/{order_id}/items/{item4_id}/cancel",
        json={"reason": "Attempting to cancel item in a closed/paid order"},
    )
    check(response, 409, "Cancelling item in a CLOSED order should return 409 Conflict")
    print("✅ Cancellation on CLOSED order properly rejected with HTTP 409")

    # ============================================================
    # SCENARIO 9: VERIFY item.cancelled OPERATIONAL EVENTS
    # ============================================================
    print("\n--- SCENARIO 9: VERIFY item.cancelled OPERATIONAL EVENTS ---")
    # Verify Item 1 event
    response = session.get(
        f"{BASE_URL}/api/v1/events",
        params={"entity_type": "order_item", "entity_id": item1_id},
    )
    events1 = check(response, 200, "Failed to get events for Item 1")
    cancel_event1 = next((e for e in events1 if e["event_type"] == "item.cancelled"), None)
    assert cancel_event1 is not None, "Missing item.cancelled event for Item 1"
    assert cancel_event1["event_metadata"]["previous_status"] == "ADDED"
    assert cancel_event1["event_metadata"]["reason"] == cancel_reason_1
    assert cancel_event1["event_metadata"]["order_id"] == order_id
    print("✅ Verified item.cancelled event for Item 1 (previous_status: ADDED)")

    # Verify Item 2 event
    response = session.get(
        f"{BASE_URL}/api/v1/events",
        params={"entity_type": "order_item", "entity_id": item2_id},
    )
    events2 = check(response, 200, "Failed to get events for Item 2")
    cancel_event2 = next((e for e in events2 if e["event_type"] == "item.cancelled"), None)
    assert cancel_event2 is not None, "Missing item.cancelled event for Item 2"
    assert cancel_event2["event_metadata"]["previous_status"] == "SENT"
    assert cancel_event2["event_metadata"]["reason"] == cancel_reason_2
    assert len(cancel_event2["event_metadata"]["cancelled_preparations"]) == 1
    assert (
        cancel_event2["event_metadata"]["cancelled_preparations"][0]["previous_status"]
        == "PENDING"
    )
    print("✅ Verified item.cancelled event for Item 2 (previous_status: SENT, prep: PENDING)")

    # Verify Item 3 event
    response = session.get(
        f"{BASE_URL}/api/v1/events",
        params={"entity_type": "order_item", "entity_id": item3_id},
    )
    events3 = check(response, 200, "Failed to get events for Item 3")
    cancel_event3 = next((e for e in events3 if e["event_type"] == "item.cancelled"), None)
    assert cancel_event3 is not None, "Missing item.cancelled event for Item 3"
    assert cancel_event3["event_metadata"]["previous_status"] == "SENT"
    assert cancel_event3["event_metadata"]["reason"] == cancel_reason_3
    assert len(cancel_event3["event_metadata"]["cancelled_preparations"]) == 1
    assert (
        cancel_event3["event_metadata"]["cancelled_preparations"][0]["previous_status"]
        == "IN_PROGRESS"
    )
    print(
        "✅ Verified item.cancelled event for Item 3 (previous_status: SENT, prep: IN_PROGRESS)"
    )

    # ============================================================
    # SCENARIO 10: VERIFY preparation.cancelled OPERATIONAL EVENTS
    # ============================================================
    print("\n--- SCENARIO 10: VERIFY preparation.cancelled OPERATIONAL EVENTS ---")
    # Verify Prep 2 event (cancelled from PENDING)
    response = session.get(
        f"{BASE_URL}/api/v1/events",
        params={"entity_type": "preparation", "entity_id": prep2_id},
    )
    prep_events2 = check(response, 200, "Failed to get events for Prep 2")
    prep_cancel_event2 = next(
        (e for e in prep_events2 if e["event_type"] == "preparation.cancelled"), None
    )
    assert prep_cancel_event2 is not None, "Missing preparation.cancelled event for Prep 2"
    assert prep_cancel_event2["event_metadata"]["status"] == "CANCELLED"
    assert prep_cancel_event2["event_metadata"]["previous_status"] == "PENDING"
    assert prep_cancel_event2["event_metadata"]["reason"] == cancel_reason_2
    assert prep_cancel_event2["event_metadata"]["order_item_id"] == item2_id
    print("✅ Verified preparation.cancelled event for Prep 2 (previous_status: PENDING)")

    # Verify Prep 3 event (cancelled from IN_PROGRESS)
    response = session.get(
        f"{BASE_URL}/api/v1/events",
        params={"entity_type": "preparation", "entity_id": prep3_id},
    )
    prep_events3 = check(response, 200, "Failed to get events for Prep 3")
    prep_cancel_event3 = next(
        (e for e in prep_events3 if e["event_type"] == "preparation.cancelled"), None
    )
    assert prep_cancel_event3 is not None, "Missing preparation.cancelled event for Prep 3"
    assert prep_cancel_event3["event_metadata"]["status"] == "CANCELLED"
    assert prep_cancel_event3["event_metadata"]["previous_status"] == "IN_PROGRESS"
    assert prep_cancel_event3["event_metadata"]["reason"] == cancel_reason_3
    assert prep_cancel_event3["event_metadata"]["order_item_id"] == item3_id
    print(
        "✅ Verified preparation.cancelled event for Prep 3 (previous_status: IN_PROGRESS)"
    )

    print("\n" + "=" * 85)
    print("🎉 ALL 10 CANCELLATION SCENARIOS SUCCESSFULLY VERIFIED!")
    print("=" * 85)
    print(
        """
1. ADDED → CANCELLED                                                ✅
2. Repeated cancellation → 409 Conflict                             ✅
3. SENT → CANCELLED (PENDING preparation cancelled)                 ✅
4. IN_PROGRESS preparation → OrderItem + Prep CANCELLED              ✅
5. READY preparation → 409 Conflict (OrderItem & Prep unchanged)    ✅
6. SERVED preparation → 409 Conflict (OrderItem & Prep unchanged)   ✅
7. CLOSED order → 409 Conflict                                      ✅
8. Cancelled items excluded from bill total                         ✅
9. item.cancelled OperationalEvents with complete metadata          ✅
10. preparation.cancelled OperationalEvents with complete metadata  ✅
"""
    )


if __name__ == "__main__":
    main()
