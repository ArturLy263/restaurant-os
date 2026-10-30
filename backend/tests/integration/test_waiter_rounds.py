"""A waiter's rounds reach the stations' live streams (Stage 2.1).

Real sessions and commits, as in production: the waiter's API calls
commit, the outbox worker delivers, and the Kitchen and Bar streams each
receive only their own items, once per round.
"""

from sqlalchemy import text

from app.api.idempotency import IDEMPOTENCY_KEY_HEADER
from tests.integration.test_station_event_fanout import (  # noqa: F401
    assert_nothing_else,
    deliver,
    kitchen_floor,
)
from tests.integration.test_station_event_stream import (  # noqa: F401
    floor,
    hub,
    receive_json,
    subscribe,
)


class Waiter:
    def __init__(self, floor):
        self.floor = floor
        self.client = floor.live.client
        self.headers = {"Authorization": f"Bearer {floor.tokens['waiter']}"}

    def post(self, path, json=None, key=None):
        headers = dict(self.headers)
        if key:
            headers[IDEMPOTENCY_KEY_HEADER] = key
        return self.client.post(path, json=json, headers=headers)

    def get(self, path, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else self.headers
        return self.client.get(path, headers=headers)

    def add(self, order_id, product_id, quantity=1, notes=None):
        response = self.post(
            f"/api/v1/orders/{order_id}/items",
            {"product_id": str(product_id), "quantity": quantity, "notes": notes},
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]


def count(floor, sql, **params):
    with floor.engine.connect() as connection:
        return connection.execute(text(sql), params).scalar_one()


def test_rounds_reach_the_kitchen_and_bar_streams(kitchen_floor, hub):
    floor = kitchen_floor
    waiter = Waiter(floor)
    cook = subscribe(floor, floor.stations.kitchen, floor.tokens["cook"])
    barman = subscribe(floor, floor.stations.bar, floor.tokens["barman"])

    tables = waiter.get("/api/v1/tables").json()
    table = next(t for t in tables if t["id"] == str(floor.restaurant_a.table_id))
    assert table["active_order"] is None
    order = waiter.post("/api/v1/orders", {"table_id": table["id"], "guest_count": 3})
    assert order.status_code == 201, order.text
    order_id = order.json()["id"]

    burger = waiter.add(order_id, floor.restaurant_a.burger_id, 2, "no onions")
    beer = waiter.add(order_id, floor.beer_id)
    first = waiter.post(f"/api/v1/orders/{order_id}/send", key="round-1")
    assert first.status_code == 200, first.text
    # A retry of the same round (e.g. after a timeout) is not a second round.
    assert waiter.post(f"/api/v1/orders/{order_id}/send", key="round-1").status_code == 200

    assert len(deliver(floor, hub).delivered) == 1
    to_kitchen = receive_json(cook[1])
    to_bar = receive_json(barman[1])
    assert to_kitchen["event_type"] == to_bar["event_type"] == "order.sent"
    assert [(i["order_item_id"], i["quantity"]) for i in to_kitchen["payload"]["items"]] == [(burger, 2)]
    assert [i["order_item_id"] for i in to_bar["payload"]["items"]] == [beer]
    assert to_kitchen["payload"]["initial"] is True

    # The KDS reloads its queue on order.sent; the queue has the note.
    [entry] = waiter.get(
        f"/api/v1/stations/{floor.stations.kitchen}/preparations", floor.tokens["cook"]
    ).json()
    assert (entry["order_item_id"], entry["quantity"], entry["notes"]) == (burger, 2, "no onions")

    # Second round: only the new item, only to the bar.
    more_beer = waiter.add(order_id, floor.beer_id, 2)
    assert waiter.post(f"/api/v1/orders/{order_id}/send", key="round-2").status_code == 200
    assert len(deliver(floor, hub).delivered) == 1

    second = receive_json(barman[1])
    assert second["payload"]["initial"] is False
    assert [(i["order_item_id"], i["quantity"]) for i in second["payload"]["items"]] == [(more_beer, 2)]
    assert_nothing_else(hub, cook[1], floor.a_id, floor.stations.kitchen)
    assert_nothing_else(hub, barman[1], floor.a_id, floor.stations.bar)

    bar_queue = waiter.get(
        f"/api/v1/stations/{floor.stations.bar}/preparations", floor.tokens["barman"]
    ).json()
    assert [entry["order_item_id"] for entry in bar_queue] == [beer, more_beer]
    assert count(
        floor,
        "SELECT count(*) FROM preparations p JOIN order_items i ON i.id = p.order_item_id "
        "WHERE i.order_id = :o",
        o=order_id,
    ) == 3

    for session, _ in (cook, barman):
        session.__exit__(None, None, None)


def test_a_failed_round_commits_nothing(kitchen_floor, hub):
    floor = kitchen_floor
    waiter = Waiter(floor)
    order_id = waiter.post(
        "/api/v1/orders",
        {"table_id": str(floor.restaurant_a.table_id), "guest_count": 2},
    ).json()["id"]
    waiter.add(order_id, floor.restaurant_a.burger_id)
    waiter.add(order_id, floor.beer_id)
    with floor.engine.begin() as connection:
        connection.execute(
            text("UPDATE stations SET is_active = false WHERE id = :s"),
            {"s": floor.stations.bar},
        )

    response = waiter.post(f"/api/v1/orders/{order_id}/send")

    assert (response.status_code, response.json()["error"]["code"]) == (409, "STATION_INACTIVE")
    assert count(floor, "SELECT status FROM orders WHERE id = :o", o=order_id) == "OPEN"
    assert count(
        floor,
        "SELECT count(*) FROM order_items WHERE order_id = :o AND status <> 'ADDED'",
        o=order_id,
    ) == 0
    assert count(
        floor,
        "SELECT count(*) FROM preparations p JOIN order_items i ON i.id = p.order_item_id "
        "WHERE i.order_id = :o",
        o=order_id,
    ) == 0
    assert count(
        floor, "SELECT count(*) FROM outbox_events WHERE restaurant_id = :r", r=floor.a_id
    ) == 0
