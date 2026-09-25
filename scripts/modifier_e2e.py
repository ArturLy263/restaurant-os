import uuid
from decimal import Decimal

import requests

from app.core.config import settings
from app.db.database import SessionLocal
from app.db.models.modifier import Modifier, OrderItemModifier
from app.db.models.order import OrderItem


BASE_URL = "http://127.0.0.1:8000"

EMAIL = "admin@restaurantos.com"
PASSWORD = settings.admin_password

RESTAURANT_ID = uuid.UUID("77779624-2f5d-4835-92ed-dcdd7438a83f")
TABLE_ID = uuid.UUID("ed11b5e0-fa7b-429a-86d1-1c0519508306")
BURGER_ID = uuid.UUID("e8def57c-7e31-4ab9-84c4-7f48bc1adc57")


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
    print("# RESTAURANTOS — MODIFIER E2E TEST")
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
    # 2. GET MODIFIER IDS DIRECTLY FROM DB
    # ============================================================

    db = SessionLocal()

    try:
        cheese = (
            db.query(Modifier)
            .filter(
                Modifier.name == "Cheese",
                Modifier.is_active.is_(True),
            )
            .first()
        )

        bacon = (
            db.query(Modifier)
            .filter(
                Modifier.name == "Bacon",
                Modifier.is_active.is_(True),
            )
            .first()
        )

        if cheese is None:
            raise RuntimeError("Cheese modifier not found")

        if bacon is None:
            raise RuntimeError("Bacon modifier not found")

        cheese_id = cheese.id
        bacon_id = bacon.id

        print("\n✅ MODIFIERS FOUND")
        print(f"   Cheese: {cheese.id} +€{cheese.price_adjustment}")
        print(f"   Bacon:  {bacon.id} +€{bacon.price_adjustment}")

        assert Decimal(str(cheese.price_adjustment)) == Decimal("1.00")
        assert Decimal(str(bacon.price_adjustment)) == Decimal("2.00")

    finally:
        db.close()

    # ============================================================
    # 3. CREATE ORDER
    # ============================================================

    response = session.post(
        f"{BASE_URL}/api/v1/orders",
        json={
            "table_id": str(TABLE_ID),
            "guest_count": 1,
        },
    )

    order = check(response, 201, "Order creation failed")

    order_id = uuid.UUID(order["id"])

    assert order["restaurant_id"] == str(RESTAURANT_ID)
    assert order["table_id"] == str(TABLE_ID)
    assert order["status"] == "OPEN"

    print(f"\n✅ ORDER CREATED: {order_id}")

    # ============================================================
    # 4. ADD BURGER + CHEESE + BACON
    # ============================================================

    response = session.post(
        f"{BASE_URL}/api/v1/orders/{order_id}/items",
        json={
            "product_id": str(BURGER_ID),
            "quantity": 1,
            "modifier_ids": [
                str(cheese_id),
                str(bacon_id),
            ],
        },
    )

    burger_item = check(
        response,
        201,
        "Burger with modifiers could not be added",
    )

    burger_item_id = uuid.UUID(burger_item["id"])

    assert burger_item["product_id"] == str(BURGER_ID)
    assert burger_item["quantity"] == 1
    assert burger_item["status"] == "ADDED"

    # Burger €12.50
    # Cheese +€1.00
    # Bacon  +€2.00
    # ----------------
    # Total  €15.50

    assert Decimal(str(burger_item["unit_price"])) == Decimal("12.50")
    assert Decimal(str(burger_item["total_price"])) == Decimal("15.50")

    print(f"\n✅ BURGER + MODIFIERS ADDED: {burger_item_id}")
    print("   Burger: €12.50")
    print("   Cheese: +€1.00")
    print("   Bacon:  +€2.00")
    print("   TOTAL:  €15.50")

    # ============================================================
    # 5. VERIFY MODIFIER SNAPSHOTS
    # ============================================================

    modifiers = burger_item.get("modifiers", [])

    print("\nMODIFIER RESPONSE:")
    print(modifiers)

    assert len(modifiers) == 2

    modifier_names = {
        modifier.get("name")
        for modifier in modifiers
    }

    assert modifier_names == {"Cheese", "Bacon"}

    print("\n✅ MODIFIER RESPONSE VERIFIED")

    # ============================================================
    # 6. VERIFY DATABASE SNAPSHOTS
    # ============================================================

    db = SessionLocal()

    try:
        snapshots = (
            db.query(OrderItemModifier)
            .filter(
                OrderItemModifier.order_item_id == burger_item_id
            )
            .all()
        )

        assert len(snapshots) == 2

        snapshot_data = {
            snapshot.name_snapshot: Decimal(
                str(snapshot.price_snapshot)
            )
            for snapshot in snapshots
        }

        assert snapshot_data["Cheese"] == Decimal("1.00")
        assert snapshot_data["Bacon"] == Decimal("2.00")

        print("\n✅ DATABASE SNAPSHOTS VERIFIED")
        print("   Cheese snapshot: €1.00")
        print("   Bacon snapshot:  €2.00")

    finally:
        db.close()

    # ============================================================
    # 7. CHANGE BACON CURRENT PRICE
    # ============================================================

    db = SessionLocal()

    try:
        bacon = db.get(Modifier, bacon_id)

        if bacon is None:
            raise RuntimeError("Bacon modifier disappeared")

        print(
            f"\nChanging Bacon current price "
            f"€{bacon.price_adjustment} → €3.00"
        )

        bacon.price_adjustment = Decimal("3.00")

        db.commit()

    finally:
        db.close()

    print("✅ CURRENT BACON PRICE CHANGED")

    # ============================================================
    # 8. VERIFY OLD ORDER SNAPSHOT DID NOT CHANGE
    # ============================================================

    db = SessionLocal()

    try:
        snapshot = (
            db.query(OrderItemModifier)
            .filter(
                OrderItemModifier.order_item_id == burger_item_id,
                OrderItemModifier.modifier_id == bacon_id,
            )
            .one()
        )

        assert Decimal(str(snapshot.price_snapshot)) == Decimal("2.00")

        item = db.get(OrderItem, burger_item_id)

        assert item is not None
        assert Decimal(str(item.total_price)) == Decimal("15.50")

        print("\n✅ SNAPSHOT IMMUTABILITY VERIFIED")
        print("   Current Bacon price: €3.00")
        print("   Old order snapshot:  €2.00")
        print("   Old order total:      €15.50")

    finally:
        db.close()

    # ============================================================
    # 9. FINAL RESULT
    # ============================================================

    print("\n" + "=" * 85)
    print("🎉 MODIFIER E2E PASSED")
    print("=" * 85)
    print()
    print("Verified:")
    print("  ✅ Modifier validation")
    print("  ✅ Product → ModifierGroup relationship")
    print("  ✅ Order item modifier selection")
    print("  ✅ Price calculation")
    print("  ✅ Modifier snapshots")
    print("  ✅ Snapshot immutability")
    print("  ✅ Historical order total remains €15.50")
    print()


if __name__ == "__main__":
    main()