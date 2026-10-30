"""Waiter POS (Stage 2.1): tables, menu, item notes and changes to unsent
items, and rounds sent to the right stations."""

import uuid
from decimal import Decimal

import pytest

from app.api.idempotency import IDEMPOTENCY_KEY_HEADER, REPLAYED_HEADER
from app.db.models.category import Category
from app.db.models.modifier import Modifier, ModifierGroup
from app.db.models.operational_event import OperationalEvent
from app.db.models.outbox_event import OutboxEvent
from app.db.models.order import OrderItem
from app.db.models.preparation import Preparation
from app.db.models.product import Product
from app.db.models.table import Table
from tests.factories import (
    auth_headers,
    create_restaurant,
    create_station,
    create_user,
)


@pytest.fixture
def floor(db_client, db_session, restaurant):
    """`restaurant` set up for a waiter: a Bar with drinks, modifiers on
    the burger, more tables, and products that cannot be ordered."""

    db = db_session
    bar = create_station(db, restaurant, "Bar", "BAR")
    closed = create_station(db, restaurant, "Grill", "KITCHEN")
    closed.is_active = False

    restaurant.category.display_order = 2
    drinks = Category(restaurant_id=restaurant.restaurant.id, name="Drinks", display_order=1)
    hidden = Category(
        restaurant_id=restaurant.restaurant.id, name="Seasonal", display_order=0, is_active=False
    )
    db.add_all([drinks, hidden])
    db.flush()

    beer = Product(category_id=drinks.id, name="Beer", price=Decimal("4.00"), station_id=bar.id)
    retired = Product(
        category_id=drinks.id, name="Cider", price=Decimal("3.50"), station_id=bar.id, is_active=False
    )
    unrouted = Product(category_id=drinks.id, name="Lemonade", price=Decimal("3.00"))
    grilled = Product(
        category_id=restaurant.category.id, name="Steak", price=Decimal("21.00"), station_id=closed.id
    )
    eggnog = Product(category_id=hidden.id, name="Eggnog", price=Decimal("5.00"), station_id=bar.id)
    db.add_all([beer, retired, unrouted, grilled, eggnog])

    doneness = ModifierGroup(
        restaurant_id=restaurant.restaurant.id,
        name="Doneness",
        min_selections=1,
        max_selections=1,
        is_required=True,
    )
    doneness.modifiers = [
        Modifier(name="Medium", price_adjustment=Decimal("0.00")),
        Modifier(name="Well done", price_adjustment=Decimal("0.00")),
    ]
    extras = ModifierGroup(
        restaurant_id=restaurant.restaurant.id, name="Extras", min_selections=0, max_selections=2
    )
    extras.modifiers = [
        Modifier(name="Cheese", price_adjustment=Decimal("1.00")),
        Modifier(name="Bacon", price_adjustment=Decimal("2.00")),
        Modifier(name="Truffle", price_adjustment=Decimal("9.00"), is_active=False),
    ]
    restaurant.product.modifier_groups.extend([doneness, extras])

    window = Table(restaurant_id=restaurant.restaurant.id, number=2, name="Window", capacity=2)
    storage = Table(restaurant_id=restaurant.restaurant.id, number=3, capacity=2, is_active=False)
    db.add_all([window, storage])
    db.flush()

    waiter = create_user(db, restaurant, "WAITER")

    class Floor:
        pass

    floor = Floor()
    floor.client = db_client
    floor.db = db
    floor.restaurant = restaurant
    floor.kitchen = restaurant.station
    floor.bar = bar
    floor.closed = closed
    floor.burger = restaurant.product
    floor.beer = beer
    floor.retired = retired
    floor.unrouted = unrouted
    floor.grilled = grilled
    floor.eggnog = eggnog
    floor.medium = doneness.modifiers[0]
    floor.cheese = extras.modifiers[0]
    floor.bacon = extras.modifiers[1]
    floor.truffle = extras.modifiers[2]
    floor.window = window
    floor.waiter = waiter
    floor.headers = auth_headers(waiter)
    return floor


def get(floor, path, headers=None):
    return floor.client.get(path, headers=headers or floor.headers)


def post(floor, path, json=None, headers=None, key=None):
    headers = dict(headers or floor.headers)
    if key:
        headers[IDEMPOTENCY_KEY_HEADER] = key
    return floor.client.post(path, json=json, headers=headers)


def patch(floor, path, json, headers=None, key=None):
    headers = dict(headers or floor.headers)
    if key:
        headers[IDEMPOTENCY_KEY_HEADER] = key
    return floor.client.patch(path, json=json, headers=headers)


def open_order(floor, table=None):
    table = table or floor.restaurant.table
    response = post(floor, "/api/v1/orders", {"table_id": str(table.id), "guest_count": 2})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def add(floor, order_id, product, quantity=1, modifiers=(), notes=None, **kwargs):
    body = {
        "product_id": str(product.id),
        "quantity": quantity,
        "modifier_ids": [str(modifier.id) for modifier in modifiers],
    }
    if notes is not None:
        body["notes"] = notes
    return post(floor, f"/api/v1/orders/{order_id}/items", body, **kwargs)


def add_burger(floor, order_id, quantity=1, notes=None):
    response = add(floor, order_id, floor.burger, quantity, [floor.medium, floor.cheese], notes)
    assert response.status_code == 201, response.text
    return response.json()


def send(floor, order_id, **kwargs):
    return post(floor, f"/api/v1/orders/{order_id}/send", **kwargs)


def item_path(order_id, item_id):
    return f"/api/v1/orders/{order_id}/items/{item_id}"


def preparations(floor):
    floor.db.expire_all()
    return {
        preparation.order_item_id: preparation
        for preparation in floor.db.query(Preparation)
        .join(OrderItem, Preparation.order_item_id == OrderItem.id)
        .filter(OrderItem.product_id.in_([floor.burger.id, floor.beer.id, floor.unrouted.id]))
    }


def order_sent_events(floor):
    floor.db.expire_all()
    return (
        floor.db.query(OutboxEvent)
        .filter(
            OutboxEvent.restaurant_id == floor.restaurant.restaurant.id,
            OutboxEvent.event_type == "order.sent",
        )
        .order_by(OutboxEvent.created_at, OutboxEvent.id)
        .all()
    )


def queue(floor, station):
    response = get(floor, f"/api/v1/stations/{station.id}/preparations", auth_headers(floor.restaurant.user))
    assert response.status_code == 200, response.text
    return response.json()


# --- Tables ---


def test_tables_show_status_and_active_order(floor):
    order_id = open_order(floor)
    add_burger(floor, order_id)
    add(floor, order_id, floor.beer)

    tables = get(floor, "/api/v1/tables").json()

    assert [(t["number"], t["name"], t["status"]) for t in tables] == [
        (1, None, "OCCUPIED"),
        (2, "Window", "AVAILABLE"),
    ]
    first, second = tables
    assert first["active_order"]["id"] == order_id
    assert first["active_order"]["status"] == "OPEN"
    assert first["active_order"]["user_id"] == str(floor.waiter.id)
    assert first["active_order"]["guest_count"] == 2
    assert (first["active_order"]["unsent_items"], first["active_order"]["sent_items"]) == (2, 0)
    assert second["active_order"] is None and second["capacity"] == 2

    assert send(floor, order_id).status_code == 200
    first = get(floor, "/api/v1/tables").json()[0]
    assert first["active_order"]["status"] == "SENT"
    assert (first["active_order"]["unsent_items"], first["active_order"]["sent_items"]) == (0, 2)


def test_tables_are_limited_to_the_users_restaurant(floor, db_session):
    other = create_restaurant(db_session, "Other")
    other.table.number = 99
    db_session.flush()

    numbers = [table["number"] for table in get(floor, "/api/v1/tables").json()]
    other_numbers = [
        table["number"] for table in get(floor, "/api/v1/tables", auth_headers(other.user)).json()
    ]

    assert numbers == [1, 2]
    assert other_numbers == [99]


def test_closed_order_frees_the_table_in_the_list(floor):
    order_id = open_order(floor)
    item = add_burger(floor, order_id)
    assert post(
        floor, item_path(order_id, item["id"]) + "/cancel", {"reason": "Wrong table"}
    ).status_code == 200
    assert post(floor, f"/api/v1/orders/{order_id}/void", {"reason": "Wrong table"}).status_code == 200

    first = get(floor, "/api/v1/tables").json()[0]

    assert (first["status"], first["active_order"]) == ("AVAILABLE", None)


# --- Menu ---


def test_menu_lists_categories_products_and_availability(floor):
    menu = get(floor, "/api/v1/menu").json()

    assert menu["currency"] == "EUR"
    assert [category["name"] for category in menu["categories"]] == ["Drinks", "Food"]

    drinks, food = menu["categories"]
    products = {p["name"]: p for p in drinks["products"] + food["products"]}
    assert [p["name"] for p in drinks["products"]] == ["Beer", "Cider", "Lemonade"]
    assert "Eggnog" not in products

    assert {
        name: (p["is_available"], p["unavailable_reason"]) for name, p in products.items()
    } == {
        "Beer": (True, None),
        "Cider": (False, "PRODUCT_INACTIVE"),
        "Lemonade": (False, "PRODUCT_HAS_NO_STATION"),
        "Burger": (True, None),
        "Steak": (False, "STATION_INACTIVE"),
    }
    assert products["Burger"]["price"] == "12.50"
    assert products["Beer"]["price"] == "4.00"
    assert products["Beer"]["station"] == {
        "id": str(floor.bar.id),
        "name": "Bar",
        "type": "BAR",
        "is_active": True,
    }
    assert products["Burger"]["station"]["name"] == "Kitchen"
    assert products["Lemonade"]["station"] is None
    assert products["Steak"]["station"]["is_active"] is False


def test_menu_lists_active_modifiers_with_their_rules(floor):
    menu = get(floor, "/api/v1/menu").json()
    burger = next(p for c in menu["categories"] for p in c["products"] if p["name"] == "Burger")

    doneness, extras = burger["modifier_groups"]
    assert (doneness["name"], doneness["is_required"], doneness["min_selections"], doneness["max_selections"]) == (
        "Doneness",
        True,
        1,
        1,
    )
    assert [(m["name"], m["price_adjustment"]) for m in doneness["modifiers"]] == [
        ("Medium", "0.00"),
        ("Well done", "0.00"),
    ]
    assert extras["max_selections"] == 2 and extras["is_required"] is False
    # Truffle is inactive.
    assert [(m["name"], m["price_adjustment"]) for m in extras["modifiers"]] == [
        ("Cheese", "1.00"),
        ("Bacon", "2.00"),
    ]


def test_menu_is_limited_to_the_users_restaurant(floor, db_session):
    other = create_restaurant(db_session, "Other")
    other.product.name = "Other burger"
    db_session.flush()

    names = {p["name"] for c in get(floor, "/api/v1/menu").json()["categories"] for p in c["products"]}
    other_menu = get(floor, "/api/v1/menu", auth_headers(other.user)).json()

    assert "Other burger" not in names
    assert [(c["name"], [p["name"] for p in c["products"]]) for c in other_menu["categories"]] == [
        ("Food", ["Other burger"])
    ]


@pytest.mark.parametrize("role", ["KITCHEN", "BAR"])
def test_station_staff_cannot_read_tables_or_menu(floor, role):
    headers = auth_headers(create_user(floor.db, floor.restaurant, role))

    for path in ("/api/v1/tables", "/api/v1/menu"):
        response = get(floor, path, headers)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "PERMISSION_DENIED"


# --- Adding items ---


def test_item_keeps_modifiers_prices_and_notes(floor):
    order_id = open_order(floor)

    item = add_burger(floor, order_id, quantity=2, notes="  no onions, allergy: nuts  ")

    assert item["notes"] == "no onions, allergy: nuts"
    assert item["preparation"] is None
    assert item["unit_price"] == 12.5
    assert item["total_price"] == 27.0
    assert sorted(m["name"] for m in item["modifiers"]) == ["Cheese", "Medium"]

    event = (
        floor.db.query(OperationalEvent)
        .filter(OperationalEvent.event_type == "item.added", OperationalEvent.entity_id == item["id"])
        .one()
    )
    assert event.event_metadata["notes"] == "no onions, allergy: nuts"


@pytest.mark.parametrize("notes", ["", "   "])
def test_blank_notes_are_no_notes(floor, notes):
    order_id = open_order(floor)

    assert add_burger(floor, order_id, notes=notes)["notes"] is None


def test_notes_longer_than_500_characters_are_rejected(floor):
    order_id = open_order(floor)

    response = add(floor, order_id, floor.beer, notes="x" * 501)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert add(floor, order_id, floor.beer, notes="x" * 500).status_code == 201


def test_products_in_an_inactive_category_cannot_be_added(floor):
    order_id = open_order(floor)

    response = add(floor, order_id, floor.eggnog)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PRODUCT_INACTIVE"


def test_inactive_products_and_modifiers_cannot_be_added(floor):
    order_id = open_order(floor)

    retired = add(floor, order_id, floor.retired)
    truffle = add(floor, order_id, floor.burger, modifiers=[floor.medium, floor.truffle])
    skipped = add(floor, order_id, floor.burger, modifiers=[floor.cheese])

    assert (retired.status_code, retired.json()["error"]["code"]) == (409, "PRODUCT_INACTIVE")
    assert (truffle.status_code, truffle.json()["error"]["code"]) == (400, "MODIFIER_INVALID")
    assert (skipped.status_code, skipped.json()["error"]["code"]) == (400, "MODIFIER_SELECTION_INVALID")


def test_another_restaurants_product_or_modifier_cannot_be_added(floor, db_session):
    other = create_restaurant(db_session, "Other")
    group = ModifierGroup(restaurant_id=other.restaurant.id, name="Theirs", min_selections=0)
    group.modifiers = [Modifier(name="Theirs", price_adjustment=Decimal("1.00"))]
    db_session.add(group)
    db_session.flush()
    order_id = open_order(floor)

    product = add(floor, order_id, other.product)
    modifier = add(floor, order_id, floor.burger, modifiers=[floor.medium, group.modifiers[0]])

    assert (product.status_code, product.json()["error"]["code"]) == (404, "PRODUCT_NOT_FOUND")
    assert (modifier.status_code, modifier.json()["error"]["code"]) == (400, "MODIFIER_INVALID")


# --- Changing unsent items ---


def test_quantity_change_recalculates_the_line_total(floor):
    order_id = open_order(floor)
    item = add_burger(floor, order_id)
    assert item["total_price"] == 13.5

    response = patch(floor, item_path(order_id, item["id"]), {"quantity": 3})

    assert response.status_code == 200, response.text
    changed = response.json()
    assert (changed["quantity"], changed["unit_price"], changed["total_price"]) == (3, 12.5, 40.5)
    assert sorted(m["name"] for m in changed["modifiers"]) == ["Cheese", "Medium"]

    event = (
        floor.db.query(OperationalEvent)
        .filter(OperationalEvent.event_type == "item.updated", OperationalEvent.entity_id == item["id"])
        .one()
    )
    assert event.user_id == floor.waiter.id
    assert event.event_metadata["before"] == {"quantity": 1, "total_price": "13.50", "notes": None}
    assert event.event_metadata["after"] == {"quantity": 3, "total_price": "40.50", "notes": None}


def test_notes_can_be_set_kept_and_removed(floor):
    order_id = open_order(floor)
    item = add_burger(floor, order_id, notes="rare")
    path = item_path(order_id, item["id"])

    assert patch(floor, path, {"notes": " no salt "}).json()["notes"] == "no salt"
    assert patch(floor, path, {"quantity": 2}).json()["notes"] == "no salt"
    assert patch(floor, path, {"notes": None}).json()["notes"] is None
    assert patch(floor, path, {"notes": "again"}).json()["notes"] == "again"
    assert patch(floor, path, {"notes": ""}).json()["notes"] is None


def test_an_unchanged_item_records_no_event(floor):
    order_id = open_order(floor)
    item = add_burger(floor, order_id, quantity=2)

    response = patch(floor, item_path(order_id, item["id"]), {"quantity": 2})

    assert response.status_code == 200
    assert (
        floor.db.query(OperationalEvent)
        .filter(OperationalEvent.event_type == "item.updated")
        .count()
        == 0
    )


@pytest.mark.parametrize("body", [{"quantity": 0}, {"quantity": -1}, {"notes": "x" * 501}])
def test_invalid_changes_are_rejected(floor, body):
    order_id = open_order(floor)
    item = add_burger(floor, order_id)

    response = patch(floor, item_path(order_id, item["id"]), body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_sent_or_cancelled_items_cannot_be_changed(floor):
    order_id = open_order(floor)
    sent = add_burger(floor, order_id)
    assert send(floor, order_id).status_code == 200
    cancelled = add_burger(floor, order_id)
    assert post(floor, item_path(order_id, cancelled["id"]) + "/cancel", {"reason": "Changed mind"}).status_code == 200

    after_send = patch(floor, item_path(order_id, sent["id"]), {"quantity": 2})
    after_cancel = patch(floor, item_path(order_id, cancelled["id"]), {"quantity": 2})

    assert (after_send.status_code, after_send.json()["error"]["code"]) == (409, "ORDER_ITEM_ALREADY_SENT")
    assert (after_cancel.status_code, after_cancel.json()["error"]["code"]) == (
        409,
        "ORDER_ITEM_ALREADY_CANCELLED",
    )


def test_items_of_a_billed_order_cannot_be_changed(floor):
    order_id = open_order(floor)
    item = add_burger(floor, order_id)
    assert send(floor, order_id).status_code == 200
    admin = auth_headers(floor.restaurant.user)
    assert post(floor, f"/api/v1/bills/order/{order_id}", headers=admin).status_code == 201
    unsent = add_burger(floor, order_id)

    for item_id in (item["id"], unsent["id"]):
        response = patch(floor, item_path(order_id, item_id), {"quantity": 2})
        assert (response.status_code, response.json()["error"]["code"]) == (409, "ORDER_BILLED")


def test_only_the_orders_waiter_or_a_manager_changes_items(floor):
    order_id = open_order(floor)
    item = add_burger(floor, order_id)
    path = item_path(order_id, item["id"])
    colleague = auth_headers(create_user(floor.db, floor.restaurant, "WAITER"))
    manager = auth_headers(create_user(floor.db, floor.restaurant, "MANAGER"))
    cashier = auth_headers(create_user(floor.db, floor.restaurant, "CASHIER"))

    denied = patch(floor, path, {"quantity": 2}, headers=colleague)
    forbidden = patch(floor, path, {"quantity": 2}, headers=cashier)

    assert (denied.status_code, denied.json()["error"]["code"]) == (403, "ORDER_ACCESS_DENIED")
    assert (forbidden.status_code, forbidden.json()["error"]["code"]) == (403, "PERMISSION_DENIED")
    assert patch(floor, path, {"quantity": 2}, headers=manager).status_code == 200


def test_changes_to_another_restaurants_items_look_missing(floor, db_session):
    order_id = open_order(floor)
    item = add_burger(floor, order_id)
    other = create_restaurant(db_session, "Other")
    other_manager = auth_headers(create_user(db_session, other, "MANAGER"))

    response = patch(floor, item_path(order_id, item["id"]), {"quantity": 2}, headers=other_manager)
    missing_item = patch(floor, item_path(order_id, order_id), {"quantity": 2})

    assert (response.status_code, response.json()["error"]["code"]) == (404, "ORDER_NOT_FOUND")
    assert (missing_item.status_code, missing_item.json()["error"]["code"]) == (404, "ORDER_ITEM_NOT_FOUND")


def test_item_change_with_a_key_is_applied_once(floor):
    order_id = open_order(floor)
    item = add_burger(floor, order_id)
    path = item_path(order_id, item["id"])

    first = patch(floor, path, {"quantity": 4}, key="qty-4")
    patch(floor, path, {"quantity": 2})
    replay = patch(floor, path, {"quantity": 4}, key="qty-4")
    reused = patch(floor, path, {"quantity": 5}, key="qty-4")

    assert first.status_code == replay.status_code == 200
    assert replay.headers[REPLAYED_HEADER] == "true"
    assert replay.json() == first.json()
    assert (reused.status_code, reused.json()["error"]["code"]) == (422, "IDEMPOTENCY_KEY_REUSED")
    # The replay did not set the quantity back to 4.
    assert get(floor, f"/api/v1/orders/{order_id}").json()["items"][0]["quantity"] == 2


def test_removing_an_unsent_item_leaves_it_out_of_the_round(floor):
    order_id = open_order(floor)
    keep = add_burger(floor, order_id)
    drop = add(floor, order_id, floor.beer).json()

    removed = post(floor, item_path(order_id, drop["id"]) + "/cancel", {"reason": "Removed before sending"})
    assert (removed.status_code, removed.json()["status"]) == (200, "CANCELLED")
    sent = send(floor, order_id)

    assert sent.status_code == 200
    statuses = {item["id"]: item["status"] for item in sent.json()["items"]}
    assert statuses == {keep["id"]: "SENT", drop["id"]: "CANCELLED"}
    assert set(preparations(floor)) == {uuid.UUID(keep["id"])}


# --- Sending rounds ---


def test_round_routes_each_item_to_its_station(floor):
    order_id = open_order(floor)
    burger = add_burger(floor, order_id, quantity=2, notes="one well done")
    beer = add(floor, order_id, floor.beer, quantity=3, notes="cold glasses").json()

    response = send(floor, order_id)

    assert response.status_code == 200, response.text
    items = {item["id"]: item for item in response.json()["items"]}
    assert response.json()["status"] == "SENT"
    assert items[burger["id"]]["preparation"]["station_id"] == str(floor.kitchen.id)
    assert items[burger["id"]]["preparation"]["station_name"] == "Kitchen"
    assert items[burger["id"]]["preparation"]["status"] == "PENDING"
    assert items[beer["id"]]["preparation"]["station_name"] == "Bar"

    [event] = order_sent_events(floor)
    assert event.payload["initial"] is True
    assert {(i["order_item_id"], i["station_id"]) for i in event.payload["items"]} == {
        (burger["id"], str(floor.kitchen.id)),
        (beer["id"], str(floor.bar.id)),
    }

    [kitchen_entry] = queue(floor, floor.kitchen)
    [bar_entry] = queue(floor, floor.bar)
    assert (kitchen_entry["product_name"], kitchen_entry["quantity"], kitchen_entry["notes"]) == (
        "Burger",
        2,
        "one well done",
    )
    assert [m["name"] for m in kitchen_entry["modifiers"]] == ["Cheese", "Medium"]
    assert (bar_entry["product_name"], bar_entry["quantity"], bar_entry["notes"]) == ("Beer", 3, "cold glasses")


def test_second_round_sends_only_the_new_items(floor):
    order_id = open_order(floor)
    burger = add_burger(floor, order_id)
    beer = add(floor, order_id, floor.beer).json()
    assert send(floor, order_id).status_code == 200
    first_round = preparations(floor)

    more_beer = add(floor, order_id, floor.beer, quantity=2).json()
    second = send(floor, order_id)

    assert second.status_code == 200, second.text
    after = preparations(floor)
    assert len(after) == 3
    for item_id, preparation in first_round.items():
        assert after[item_id].id == preparation.id
    first_event, second_event = order_sent_events(floor)
    assert second_event.payload["initial"] is False
    assert [i["order_item_id"] for i in second_event.payload["items"]] == [more_beer["id"]]

    statuses = {item["id"]: item["status"] for item in second.json()["items"]}
    assert statuses == {burger["id"]: "SENT", beer["id"]: "SENT", more_beer["id"]: "SENT"}
    assert [entry["quantity"] for entry in queue(floor, floor.bar)] == [1, 2]
    assert len(queue(floor, floor.kitchen)) == 1


def test_a_retried_round_is_not_sent_twice(floor):
    order_id = open_order(floor)
    add_burger(floor, order_id)

    first = send(floor, order_id, key="round-1")
    retry = send(floor, order_id, key="round-1")
    again = send(floor, order_id)

    assert first.status_code == retry.status_code == 200
    assert retry.headers[REPLAYED_HEADER] == "true"
    assert retry.json() == first.json()
    assert (again.status_code, again.json()["error"]["code"]) == (409, "ORDER_HAS_NO_ITEMS_TO_SEND")
    assert len(preparations(floor)) == 1
    assert len(order_sent_events(floor)) == 1


def test_a_round_is_sent_completely_or_not_at_all(floor):
    order_id = open_order(floor)
    burger = add_burger(floor, order_id)
    lemonade = add(floor, order_id, floor.unrouted)
    assert lemonade.status_code == 201

    response = send(floor, order_id)

    assert (response.status_code, response.json()["error"]["code"]) == (409, "PRODUCT_HAS_NO_STATION")
    order = get(floor, f"/api/v1/orders/{order_id}").json()
    assert order["status"] == "OPEN"
    assert {item["status"] for item in order["items"]} == {"ADDED"}
    assert preparations(floor) == {}
    assert order_sent_events(floor) == []

    # Removing the item that cannot be routed lets the round go.
    assert post(
        floor, item_path(order_id, lemonade.json()["id"]) + "/cancel", {"reason": "No station"}
    ).status_code == 200
    assert send(floor, order_id).status_code == 200
    assert list(preparations(floor)) == [uuid.UUID(burger["id"])]


def test_a_station_closed_after_adding_stops_the_round(floor):
    order_id = open_order(floor)
    add_burger(floor, order_id)
    add(floor, order_id, floor.beer)
    floor.bar.is_active = False
    floor.db.flush()

    response = send(floor, order_id)

    assert (response.status_code, response.json()["error"]["code"]) == (409, "STATION_INACTIVE")
    assert preparations(floor) == {}


def test_order_shows_preparation_progress(floor):
    order_id = open_order(floor)
    burger = add_burger(floor, order_id)
    assert send(floor, order_id).status_code == 200
    preparation_id = preparations(floor)[uuid.UUID(burger["id"])].id
    admin = auth_headers(floor.restaurant.user)
    assert post(floor, f"/api/v1/preparations/{preparation_id}/start", headers=admin).status_code == 200

    item = get(floor, f"/api/v1/orders/{order_id}").json()["items"][0]

    assert item["preparation"] == {
        "id": str(preparation_id),
        "station_id": str(floor.kitchen.id),
        "station_name": "Kitchen",
        "status": "IN_PROGRESS",
    }


def test_waiter_reopens_a_tables_order_and_adds_a_round(floor):
    order_id = open_order(floor)
    add_burger(floor, order_id)
    assert send(floor, order_id).status_code == 200

    # The table list leads back to the order, which still takes items.
    table = get(floor, "/api/v1/tables").json()[0]
    assert table["active_order"]["id"] == order_id
    again = post(floor, "/api/v1/orders", {"table_id": str(floor.restaurant.table.id), "guest_count": 2})
    assert (again.status_code, again.json()["error"]["code"]) == (409, "TABLE_NOT_AVAILABLE")

    add(floor, order_id, floor.beer)
    assert send(floor, order_id).status_code == 200
    assert get(floor, "/api/v1/tables").json()[0]["active_order"]["sent_items"] == 2


def test_station_assignment_is_unchanged_for_items_already_sent(floor):
    """Moving a product to another station later does not move items
    already with the kitchen."""

    order_id = open_order(floor)
    burger = add_burger(floor, order_id)
    assert send(floor, order_id).status_code == 200
    floor.burger.station_id = floor.bar.id
    floor.db.flush()

    item = get(floor, f"/api/v1/orders/{order_id}").json()["items"][0]

    assert item["id"] == burger["id"]
    assert item["preparation"]["station_name"] == "Kitchen"
