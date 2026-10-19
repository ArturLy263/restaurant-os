"""The station preparation queue shows what to prepare (KDS Phase 0).

Authorization and station scope of the queue are covered by
test_permissions.py and test_station_scope.py; this is its content.
"""

from decimal import Decimal

import pytest

from app.db.models.modifier import Modifier, ModifierGroup
from app.db.models.table import Table
from tests.factories import auth_headers, create_restaurant, create_station


QUEUE_FIELDS = {
    "id",
    "order_item_id",
    "station_id",
    "status",
    "created_at",
    "started_at",
    "ready_at",
    "order_id",
    "table_number",
    "table_name",
    "product_name",
    "quantity",
    "modifiers",
}


@pytest.fixture
def extras(db_session, restaurant):
    """Cheese and Bacon, offered on the burger."""

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

    return {modifier.name: modifier for modifier in group.modifiers}


def queue(pos, station, user=None):
    response = pos.client.get(
        f"/api/v1/stations/{station.id}/preparations",
        headers=auth_headers(user or pos.restaurant.user),
    )
    assert response.status_code == 200, response.text
    return response.json()


def add(pos, order_id, product, quantity=1, modifiers=()):
    response = pos.client.post(
        f"/api/v1/orders/{order_id}/items",
        json={
            "product_id": str(product.id),
            "quantity": quantity,
            "modifier_ids": [str(modifier.id) for modifier in modifiers],
        },
        headers=pos.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def order_at(pos, table):
    response = pos.client.post(
        "/api/v1/orders",
        json={"table_id": str(table.id), "guest_count": 2},
        headers=pos.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_queue_shows_what_to_prepare(pos, db_session, restaurant, extras):
    restaurant.table.name = "Terrace 1"
    db_session.flush()

    order_id = pos.create_order().json()["id"]
    burger_item = add(
        pos,
        order_id,
        pos.burger,
        quantity=2,
        modifiers=[extras["Cheese"], extras["Bacon"]],
    )
    coke_item = add(pos, order_id, pos.coke)
    assert pos.send(order_id).status_code == 200

    burger, coke = queue(pos, restaurant.station)

    for entry in (burger, coke):
        assert set(entry) == QUEUE_FIELDS
        assert entry["order_id"] == order_id
        assert entry["table_number"] == 1
        assert entry["table_name"] == "Terrace 1"
        assert entry["station_id"] == str(restaurant.station.id)
        assert entry["status"] == "PENDING"
        assert entry["started_at"] is None and entry["ready_at"] is None

    assert burger["id"] == str(pos.preparation(burger_item).id)
    assert burger["order_item_id"] == burger_item
    assert burger["product_name"] == "Burger"
    assert burger["quantity"] == 2
    # By name, with their stored names; no prices.
    assert burger["modifiers"] == [
        {"modifier_id": str(extras["Bacon"].id), "name": "Bacon"},
        {"modifier_id": str(extras["Cheese"].id), "name": "Cheese"},
    ]

    assert coke["order_item_id"] == coke_item
    assert coke["product_name"] == "Coca-Cola"
    assert coke["quantity"] == 1
    assert coke["modifiers"] == []


def test_table_without_a_name(pos, restaurant):
    order_id, _, _ = pos.sent_order()

    entries = queue(pos, restaurant.station)

    assert len(entries) == 2
    for entry in entries:
        assert entry["order_id"] == order_id
        assert entry["table_number"] == 1
        assert entry["table_name"] is None


def test_empty_queue(pos, db_session, restaurant):
    assert queue(pos, restaurant.station) == []

    # A station with nothing sent to it, while another station has work.
    grill = create_station(db_session, restaurant, "Grill", "KITCHEN")
    pos.sent_order()
    assert queue(pos, grill) == []


def test_several_orders_and_tables_oldest_first(pos, db_session, restaurant):
    patio = Table(
        restaurant_id=restaurant.restaurant.id,
        number=7,
        name="Patio",
        capacity=2,
    )
    db_session.add(patio)
    db_session.flush()

    first = pos.create_order().json()["id"]
    first_items = [add(pos, first, pos.burger, quantity=3), add(pos, first, pos.coke)]
    assert pos.send(first).status_code == 200

    second = order_at(pos, patio)
    second_item = add(pos, second, pos.coke, quantity=4)
    assert pos.send(second).status_code == 200

    entries = queue(pos, restaurant.station)

    assert [e["order_item_id"] for e in entries] == [*first_items, second_item]
    assert [
        (e["order_id"], e["table_number"], e["table_name"], e["product_name"], e["quantity"])
        for e in entries
    ] == [
        (first, 1, None, "Burger", 3),
        (first, 1, None, "Coca-Cola", 1),
        (second, 7, "Patio", "Coca-Cola", 4),
    ]


def test_queue_keeps_its_status_filter(pos, restaurant):
    order_id = pos.create_order().json()["id"]
    items = [add(pos, order_id, pos.burger) for _ in range(5)]
    assert pos.send(order_id).status_code == 200

    pending, started, ready, served, cancelled = items
    pos.start_preparation(started, "start")
    pos.start_preparation(ready, "start", "ready")
    pos.start_preparation(served, "start", "ready", "serve")
    assert pos.cancel(order_id, cancelled).status_code == 200

    entries = queue(pos, restaurant.station)

    assert [(e["order_item_id"], e["status"]) for e in entries] == [
        (pending, "PENDING"),
        (started, "IN_PROGRESS"),
        (ready, "READY"),
    ]
    assert entries[1]["started_at"] is not None
    assert entries[2]["ready_at"] is not None
    assert all(e["product_name"] == "Burger" for e in entries)


def test_modifier_names_are_the_stored_snapshots(pos, db_session, restaurant, extras):
    order_id = pos.create_order().json()["id"]
    add(pos, order_id, pos.burger, modifiers=[extras["Cheese"]])
    assert pos.send(order_id).status_code == 200

    extras["Cheese"].name = "Cheddar"
    db_session.flush()

    [entry] = queue(pos, restaurant.station)
    assert entry["modifiers"] == [
        {"modifier_id": str(extras["Cheese"].id), "name": "Cheese"},
    ]


def test_product_name_is_the_current_name(pos, db_session, restaurant):
    """Order items store no product name, so a rename shows at once."""

    pos.sent_order()
    restaurant.product.name = "Cheeseburger"
    db_session.flush()

    burger, coke = queue(pos, restaurant.station)
    assert (burger["product_name"], coke["product_name"]) == ("Cheeseburger", "Coca-Cola")


def test_queue_shows_only_its_restaurants_work(pos, db_session, restaurant):
    other = create_restaurant(db_session, name="Other Restaurant")

    pos.sent_order()

    assert queue(pos, other.station, user=other.user) == []
    response = pos.client.get(
        f"/api/v1/stations/{other.station.id}/preparations",
        headers=pos.headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "STATION_NOT_FOUND"
