"""
Regression tests for audit finding A2: modifier group selection rules
(is_required, min_selections, max_selections) must be enforced for every
group on the product, including when no modifiers are selected.
"""

from decimal import Decimal

import pytest

from app.db.models.modifier import Modifier, ModifierGroup
from app.db.models.order import OrderItem
from tests.factories import auth_headers


def add_group(
    db_session,
    restaurant,
    name,
    modifier_names,
    *,
    is_required=False,
    min_selections=0,
    max_selections=None,
):
    group = ModifierGroup(
        restaurant_id=restaurant.restaurant.id,
        name=name,
        is_required=is_required,
        min_selections=min_selections,
        max_selections=max_selections,
    )
    group.modifiers = [
        Modifier(name=modifier_name, price_adjustment=Decimal("1.00"))
        for modifier_name in modifier_names
    ]
    restaurant.product.modifier_groups.append(group)
    db_session.flush()

    return {modifier.name: modifier.id for modifier in group.modifiers}


@pytest.fixture
def order_id(db_client, restaurant):
    response = db_client.post(
        "/api/v1/orders",
        json={
            "table_id": str(restaurant.table.id),
            "guest_count": 2,
        },
        headers=auth_headers(restaurant.user),
    )
    assert response.status_code == 201

    return response.json()["id"]


@pytest.fixture
def add_item(db_client, restaurant, order_id):
    def _add_item(modifier_ids):
        return db_client.post(
            f"/api/v1/orders/{order_id}/items",
            json={
                "product_id": str(restaurant.product.id),
                "quantity": 1,
                "modifier_ids": [str(modifier_id) for modifier_id in modifier_ids],
            },
            headers=auth_headers(restaurant.user),
        )

    return _add_item


def item_count(db_session, order_id):
    return (
        db_session.query(OrderItem)
        .filter(OrderItem.order_id == order_id)
        .count()
    )


# --- Bug reproduction ---


def test_required_group_without_selection_is_rejected(
    db_session, restaurant, order_id, add_item
):
    add_group(
        db_session, restaurant, "Cooking", ["Rare", "Medium", "Well done"],
        is_required=True, max_selections=1,
    )

    response = add_item([])

    assert response.status_code == 400
    assert "Cooking" in response.json()["detail"]
    assert item_count(db_session, order_id) == 0


def test_min_selections_enforced_when_nothing_selected(
    db_session, restaurant, order_id, add_item
):
    add_group(
        db_session, restaurant, "Sauce", ["Ketchup", "Mayo"],
        min_selections=1,
    )

    response = add_item([])

    assert response.status_code == 400
    assert "Sauce" in response.json()["detail"]
    assert item_count(db_session, order_id) == 0


def test_required_group_enforced_when_other_group_selected(
    db_session, restaurant, order_id, add_item
):
    add_group(
        db_session, restaurant, "Cooking", ["Rare", "Medium"],
        is_required=True, max_selections=1,
    )
    extras = add_group(db_session, restaurant, "Extras", ["Cheese"])

    response = add_item([extras["Cheese"]])

    assert response.status_code == 400
    assert "Cooking" in response.json()["detail"]
    assert item_count(db_session, order_id) == 0


# --- Valid behavior ---


def test_selecting_required_modifier_satisfies_group(
    db_session, restaurant, order_id, add_item
):
    cooking = add_group(
        db_session, restaurant, "Cooking", ["Rare", "Medium"],
        is_required=True, max_selections=1,
    )

    response = add_item([cooking["Medium"]])

    assert response.status_code == 201
    assert [m["name"] for m in response.json()["modifiers"]] == ["Medium"]
    assert item_count(db_session, order_id) == 1


def test_optional_group_can_be_left_empty(
    db_session, restaurant, order_id, add_item
):
    add_group(
        db_session, restaurant, "Extras", ["Cheese", "Bacon"],
        max_selections=3,
    )

    response = add_item([])

    assert response.status_code == 201
    assert response.json()["modifiers"] == []
    assert item_count(db_session, order_id) == 1


def test_below_min_selections_is_rejected(
    db_session, restaurant, order_id, add_item
):
    sides = add_group(
        db_session, restaurant, "Sides", ["Fries", "Salad", "Rice"],
        min_selections=2,
    )

    response = add_item([sides["Fries"]])

    assert response.status_code == 400
    assert "at least 2" in response.json()["detail"]
    assert item_count(db_session, order_id) == 0


def test_exactly_min_selections_is_accepted(
    db_session, restaurant, order_id, add_item
):
    sides = add_group(
        db_session, restaurant, "Sides", ["Fries", "Salad", "Rice"],
        min_selections=2,
    )

    response = add_item([sides["Fries"], sides["Salad"]])

    assert response.status_code == 201
    assert item_count(db_session, order_id) == 1


def test_above_max_selections_is_rejected(
    db_session, restaurant, order_id, add_item
):
    cooking = add_group(
        db_session, restaurant, "Cooking", ["Rare", "Medium"],
        is_required=True, max_selections=1,
    )

    response = add_item([cooking["Rare"], cooking["Medium"]])

    assert response.status_code == 400
    assert "at most 1" in response.json()["detail"]
    assert item_count(db_session, order_id) == 0


def test_required_group_uses_higher_min_selections(
    db_session, restaurant, order_id, add_item
):
    sides = add_group(
        db_session, restaurant, "Sides", ["Fries", "Salad", "Rice"],
        is_required=True, min_selections=2,
    )

    response = add_item([sides["Fries"]])

    assert response.status_code == 400
    assert "at least 2" in response.json()["detail"]


def test_multiple_groups_are_validated_independently(
    db_session, restaurant, order_id, add_item
):
    cooking = add_group(
        db_session, restaurant, "Cooking", ["Rare", "Medium"],
        is_required=True, max_selections=1,
    )
    sauce = add_group(
        db_session, restaurant, "Sauce", ["Ketchup", "Mayo"],
        max_selections=1,
    )
    add_group(db_session, restaurant, "Extras", ["Cheese", "Bacon"])

    # Too many from one group fails even though the others are satisfied.
    response = add_item(
        [cooking["Rare"], sauce["Ketchup"], sauce["Mayo"]]
    )
    assert response.status_code == 400
    assert "Sauce" in response.json()["detail"]

    # One from each constrained group, optional group left empty.
    response = add_item([cooking["Rare"], sauce["Ketchup"]])
    assert response.status_code == 201
    assert {m["name"] for m in response.json()["modifiers"]} == {
        "Rare",
        "Ketchup",
    }

    assert item_count(db_session, order_id) == 1
