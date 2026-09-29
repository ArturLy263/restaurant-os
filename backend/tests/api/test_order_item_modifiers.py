"""
Regression tests for audit finding A1: order responses must serialize
order item modifiers from their name/price snapshots.
"""

from decimal import Decimal

import pytest

from app.db.models.modifier import Modifier, ModifierGroup
from tests.factories import auth_headers


EXPECTED_MODIFIERS = {
    ("Cheese", 1.0),
    ("Bacon", 2.0),
}


@pytest.fixture
def order_with_modifier_item(db_client, db_session, restaurant):
    group = ModifierGroup(
        restaurant_id=restaurant.restaurant.id,
        name="Extras",
        min_selections=0,
        max_selections=3,
    )
    group.modifiers = [
        Modifier(name="Cheese", price_adjustment=Decimal("1.00")),
        Modifier(name="Bacon", price_adjustment=Decimal("2.00")),
    ]
    restaurant.product.modifier_groups.append(group)
    db_session.flush()

    headers = auth_headers(restaurant.user)

    order = db_client.post(
        "/api/v1/orders",
        json={
            "table_id": str(restaurant.table.id),
            "guest_count": 2,
        },
        headers=headers,
    )
    assert order.status_code == 201

    item = db_client.post(
        f"/api/v1/orders/{order.json()['id']}/items",
        json={
            "product_id": str(restaurant.product.id),
            "quantity": 1,
            "modifier_ids": [
                str(modifier.id) for modifier in group.modifiers
            ],
        },
        headers=headers,
    )
    assert item.status_code == 201

    return order.json()["id"], item.json(), headers


def _modifiers(item: dict) -> set[tuple[str, float]]:
    return {
        (modifier["name"], modifier["price"])
        for modifier in item["modifiers"]
    }


def test_add_item_returns_modifiers(order_with_modifier_item):
    _, item, _ = order_with_modifier_item

    assert _modifiers(item) == EXPECTED_MODIFIERS
    assert item["unit_price"] == 12.5
    assert item["total_price"] == 15.5


def test_get_order_returns_item_modifiers(
    db_client,
    order_with_modifier_item,
):
    order_id, item, headers = order_with_modifier_item

    response = db_client.get(
        f"/api/v1/orders/{order_id}",
        headers=headers,
    )

    assert response.status_code == 200

    [returned_item] = response.json()["items"]
    assert returned_item["id"] == item["id"]
    assert _modifiers(returned_item) == EXPECTED_MODIFIERS
    assert returned_item["total_price"] == 15.5


def test_send_order_returns_item_modifiers(
    db_client,
    order_with_modifier_item,
):
    order_id, item, headers = order_with_modifier_item

    response = db_client.post(
        f"/api/v1/orders/{order_id}/send",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "SENT"

    [returned_item] = response.json()["items"]
    assert returned_item["id"] == item["id"]
    assert returned_item["status"] == "SENT"
    assert _modifiers(returned_item) == EXPECTED_MODIFIERS

    response = db_client.get(
        f"/api/v1/orders/{order_id}",
        headers=headers,
    )

    assert response.status_code == 200
    assert _modifiers(response.json()["items"][0]) == EXPECTED_MODIFIERS


def test_cancel_item_returns_modifiers(
    db_client,
    order_with_modifier_item,
):
    order_id, item, headers = order_with_modifier_item

    response = db_client.post(
        f"/api/v1/orders/{order_id}/items/{item['id']}/cancel",
        json={"reason": "Customer changed mind"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert response.json()["cancelled_at"] is not None
    assert _modifiers(response.json()) == EXPECTED_MODIFIERS
