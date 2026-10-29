"""Order items keep the product's name from when they were added, as they
keep its price (unit_price): renaming a product changes neither the order
nor the station queue already made."""

from app.db.models.order import OrderItem


def queue_names(pos):
    response = pos.client.get(
        f"/api/v1/stations/{pos.restaurant.station.id}/preparations",
        headers=pos.headers,
    )
    assert response.status_code == 200, response.text
    return sorted(item["product_name"] for item in response.json())


def order_items(pos, order_id):
    response = pos.client.get(f"/api/v1/orders/{order_id}", headers=pos.headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def rename(pos, product, name):
    product.name = name
    pos.db.flush()


def test_adding_an_item_records_the_product_name(pos):
    order_id = pos.create_order().json()["id"]

    response = pos.add_item(order_id, pos.burger)

    assert response.status_code == 201
    assert response.json()["product_name"] == pos.burger.name
    assert pos.item(response.json()["id"]).product_name_snapshot == pos.burger.name


def test_renaming_a_product_keeps_the_name_of_items_already_ordered(pos):
    original = pos.burger.name
    order_id, burger_id, _ = pos.sent_order()

    rename(pos, pos.burger, "Burger (new recipe)")

    burger = next(item for item in order_items(pos, order_id) if item["id"] == burger_id)
    assert burger["product_name"] == original
    assert queue_names(pos) == sorted([original, pos.coke.name])
    # A new item gets the new name, while the old one keeps its own.
    pos.add_item(order_id, pos.burger)
    assert pos.send(order_id).status_code == 200
    assert queue_names(pos) == sorted([original, pos.coke.name, "Burger (new recipe)"])


def test_items_added_before_snapshots_show_the_current_name(pos):
    order_id, burger_id, _ = pos.sent_order()
    # An item from before migration a3d9e7c1f5b2: no snapshot stored.
    pos.db.query(OrderItem).filter(OrderItem.id == burger_id).update({"product_name_snapshot": None})
    rename(pos, pos.burger, "Burger (current)")

    burger = next(item for item in order_items(pos, order_id) if item["id"] == burger_id)
    assert burger["product_name"] is None
    assert queue_names(pos) == sorted(["Burger (current)", pos.coke.name])
