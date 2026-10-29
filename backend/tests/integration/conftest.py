from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.models.product import Product
from tests.factories import auth_headers, create_restaurant


# Deletes everything a committed test restaurant can own, children first.
_PURGE_RESTAURANT = [
    "DELETE FROM idempotency_keys WHERE restaurant_id = :r",
    "DELETE FROM operational_events WHERE restaurant_id = :r",
    "DELETE FROM outbox_events WHERE restaurant_id = :r",
    "DELETE FROM payments WHERE bill_id IN ("
    " SELECT b.id FROM bills b JOIN orders o ON o.id = b.order_id"
    " WHERE o.restaurant_id = :r)",
    "DELETE FROM bill_adjustments WHERE restaurant_id = :r",
    "DELETE FROM bills WHERE order_id IN ("
    " SELECT id FROM orders WHERE restaurant_id = :r)",
    "DELETE FROM preparations WHERE order_item_id IN ("
    " SELECT i.id FROM order_items i JOIN orders o ON o.id = i.order_id"
    " WHERE o.restaurant_id = :r)",
    "DELETE FROM order_item_modifiers WHERE order_item_id IN ("
    " SELECT i.id FROM order_items i JOIN orders o ON o.id = i.order_id"
    " WHERE o.restaurant_id = :r)",
    "DELETE FROM order_items WHERE order_id IN ("
    " SELECT id FROM orders WHERE restaurant_id = :r)",
    "DELETE FROM orders WHERE restaurant_id = :r",
    "DELETE FROM products WHERE category_id IN ("
    " SELECT id FROM categories WHERE restaurant_id = :r)",
    "DELETE FROM categories WHERE restaurant_id = :r",
    "DELETE FROM tables WHERE restaurant_id = :r",
    "DELETE FROM station_assignments WHERE user_id IN ("
    " SELECT id FROM users WHERE restaurant_id = :r)"
    " OR station_id IN (SELECT id FROM stations WHERE restaurant_id = :r)",
    "DELETE FROM stations WHERE restaurant_id = :r",
    "DELETE FROM users WHERE restaurant_id = :r",
    "DELETE FROM roles WHERE restaurant_id = :r",
    "DELETE FROM restaurants WHERE id = :r",
]


def _create_committed_restaurant(db_engine, name):
    with Session(db_engine) as setup:
        restaurant = create_restaurant(setup, name=name)
        coke = Product(
            category_id=restaurant.category.id,
            name="Coca-Cola",
            price=Decimal("2.50"),
            station_id=restaurant.station.id,
        )
        setup.add(coke)
        setup.commit()

        return SimpleNamespace(
            restaurant_id=restaurant.restaurant.id,
            user_id=restaurant.user.id,
            table_id=restaurant.table.id,
            burger_id=restaurant.product.id,
            coke_id=coke.id,
            headers=auth_headers(restaurant.user),
        )


@pytest.fixture
def committed_restaurants(db_engine):
    """Factory for restaurants committed to the test database.

    Unlike `restaurant`, the data is visible to other connections, which
    concurrent requests need. Everything the restaurant owns, including
    rows created by the requests, is deleted afterwards.
    """

    created = []

    def create(name="Committed Restaurant"):
        restaurant = _create_committed_restaurant(db_engine, name)
        created.append(restaurant.restaurant_id)
        return restaurant

    try:
        yield create
    finally:
        with db_engine.begin() as connection:
            for restaurant_id in created:
                for statement in _PURGE_RESTAURANT:
                    connection.execute(text(statement), {"r": restaurant_id})
