"""
Records of one restaurant never link to another restaurant's records.

Sending checks each product's station (it must belong to the order's
restaurant and be active), and database triggers (migration e1b7d3a9c4f2)
reject preparations, order items and products that link two restaurants.
"""

from contextlib import contextmanager
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.models.order import OrderItem
from app.db.models.preparation import Preparation
from app.db.models.product import Product
from tests.factories import create_order, create_restaurant, create_station


@pytest.fixture
def other(db_session):
    return create_restaurant(db_session, name="Other Restaurant")


@contextmanager
def trigger_disabled(db, table, trigger):
    """Insert data the triggers reject, as rows written before them were.

    ALTER TABLE is transactional: the test's rollback re-enables it anyway.
    """

    db.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}"))
    try:
        yield
        db.flush()
    finally:
        db.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}"))


def constraint_of(error: IntegrityError) -> str | None:
    return getattr(getattr(error.orig, "diag", None), "constraint_name", None)


def added(pos, order_id, product):
    response = pos.add_item(order_id, product)
    assert response.status_code == 201

    return response.json()["id"]


# --- Sending ---


def test_send_rejects_a_station_of_another_restaurant(pos, other):
    product = Product(
        category_id=pos.restaurant.category.id,
        name="Misrouted",
        price=Decimal("5.00"),
        station_id=other.station.id,
    )
    with trigger_disabled(pos.db, "products", "trg_products_station_restaurant"):
        pos.db.add(product)

    order_id, burger_id, coke_id = pos.open_order()
    misrouted_id = added(pos, order_id, product)

    response = pos.send(order_id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PRODUCT_HAS_NO_STATION"
    # Nothing about the other restaurant's station is revealed.
    assert response.json()["detail"] == f"Product {product.id} has no station"
    assert str(other.station.id) not in response.text
    assert "Other Restaurant" not in response.text

    pos.db.rollback()

    assert {pos.item(i).status for i in (burger_id, coke_id, misrouted_id)} == {"ADDED"}
    assert pos.preparations(order_id) == []
    assert pos.events("order.sent") == []


def test_send_rejects_an_inactive_station(pos):
    order_id, burger_id, coke_id = pos.open_order()

    pos.restaurant.station.is_active = False
    pos.db.flush()

    response = pos.send(order_id)

    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "STATION_INACTIVE",
        "message": "Station Kitchen is inactive",
        "request_id": response.headers["X-Request-ID"],
    }

    pos.db.rollback()

    assert pos.item(burger_id).status == "ADDED"
    assert pos.item(coke_id).status == "ADDED"
    assert pos.preparations(order_id) == []
    assert pos.events("order.sent") == []
    assert pos.order(order_id).status == "OPEN"


def test_one_inactive_station_rejects_the_whole_send(pos, db_session):
    bar = create_station(db_session, pos.restaurant, "Bar", "BAR")
    bar.is_active = False
    pos.coke.station_id = bar.id
    db_session.flush()

    order_id, burger_id, coke_id = pos.open_order()

    response = pos.send(order_id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STATION_INACTIVE"
    assert response.json()["error"]["message"] == "Station Bar is inactive"

    pos.db.rollback()

    # The Burger's active station did not let it through on its own.
    assert pos.item(burger_id).status == "ADDED"
    assert pos.item(coke_id).status == "ADDED"
    assert pos.preparations(order_id) == []


def test_adding_an_item_of_an_inactive_station_is_allowed(pos):
    order_id, _, _ = pos.open_order()

    pos.restaurant.station.is_active = False
    pos.db.flush()

    assert pos.add_item(order_id, pos.burger).status_code == 201


def test_send_succeeds_after_the_station_is_reactivated(pos):
    order_id, burger_id, _ = pos.open_order()

    pos.restaurant.station.is_active = False
    pos.db.flush()
    assert pos.send(order_id).status_code == 409
    pos.db.rollback()

    pos.restaurant.station.is_active = True
    pos.db.flush()

    assert pos.send(order_id).status_code == 200
    assert pos.preparation(burger_id).station_id == pos.restaurant.station.id


# --- Database triggers ---


def test_trigger_rejects_a_preparation_at_another_restaurants_station(
    db_session, restaurant, other
):
    order = create_order(db_session, restaurant, status="SENT")
    item = OrderItem(
        order_id=order.id,
        product_id=restaurant.product.id,
        quantity=1,
        unit_price=Decimal("12.50"),
        total_price=Decimal("12.50"),
        status="SENT",
    )
    db_session.add(item)
    db_session.flush()

    db_session.add(
        Preparation(order_item_id=item.id, station_id=other.station.id, status="PENDING")
    )

    with pytest.raises(IntegrityError) as error:
        db_session.flush()

    assert constraint_of(error.value) == "ck_preparations_station_restaurant"


def test_trigger_rejects_moving_a_preparation_to_another_restaurant(
    pos, other
):
    _, burger_id, _ = pos.sent_order()
    preparation = pos.preparation(burger_id)

    preparation.station_id = other.station.id

    with pytest.raises(IntegrityError) as error:
        pos.db.flush()

    assert constraint_of(error.value) == "ck_preparations_station_restaurant"


def test_trigger_rejects_an_order_item_of_another_restaurants_product(
    db_session, restaurant, other
):
    order = create_order(db_session, restaurant)
    db_session.add(
        OrderItem(
            order_id=order.id,
            product_id=other.product.id,
            quantity=1,
            unit_price=Decimal("12.50"),
            total_price=Decimal("12.50"),
        )
    )

    with pytest.raises(IntegrityError) as error:
        db_session.flush()

    assert constraint_of(error.value) == "ck_order_items_product_restaurant"


def test_trigger_rejects_a_product_with_another_restaurants_station(
    db_session, restaurant, other
):
    db_session.add(
        Product(
            category_id=restaurant.category.id,
            name="Misrouted",
            price=Decimal("1.00"),
            station_id=other.station.id,
        )
    )

    with pytest.raises(IntegrityError) as error:
        db_session.flush()

    assert constraint_of(error.value) == "ck_products_station_restaurant"


def test_trigger_rejects_moving_a_product_to_another_restaurants_category(
    db_session, restaurant, other
):
    restaurant.product.category_id = other.category.id

    with pytest.raises(IntegrityError) as error:
        db_session.flush()

    assert constraint_of(error.value) == "ck_products_station_restaurant"


def test_products_without_a_station_are_allowed(db_session, restaurant):
    db_session.add(
        Product(category_id=restaurant.category.id, name="Unrouted", price=Decimal("1.00"))
    )
    db_session.flush()


def test_status_updates_of_existing_inconsistent_rows_still_work(
    db_session, restaurant, other
):
    """Rows written before the triggers are not re-validated by unrelated
    updates, so they can still be served or cancelled."""

    order = create_order(db_session, restaurant, status="SENT")
    item = OrderItem(
        order_id=order.id,
        product_id=other.product.id,
        quantity=1,
        unit_price=Decimal("12.50"),
        total_price=Decimal("12.50"),
        status="SENT",
    )
    with trigger_disabled(db_session, "order_items", "trg_order_items_product_restaurant"):
        db_session.add(item)

    preparation = Preparation(
        order_item_id=item.id, station_id=other.station.id, status="PENDING"
    )
    with trigger_disabled(db_session, "preparations", "trg_preparations_station_restaurant"):
        db_session.add(preparation)

    preparation.status = "CANCELLED"
    item.status = "CANCELLED"
    db_session.flush()

    db_session.expire_all()
    assert db_session.get(Preparation, preparation.id).status == "CANCELLED"
    assert db_session.get(OrderItem, item.id).status == "CANCELLED"
