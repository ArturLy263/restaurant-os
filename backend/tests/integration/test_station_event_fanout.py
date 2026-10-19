"""Outbox -> station streams, end to end (Epic D4.2).

Committed API changes write outbox events; an OutboxWorker with the
StationEventPublisher delivers them to real WebSocket connections.
"""

import json
import threading
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.product import Product
from app.main import app
from app.realtime.delivery import THREAD_NAME
from app.realtime.publisher import StationEventPublisher
from app.workers.outbox_worker import OutboxWorker
from tests.integration.test_mutation_concurrency import Live
from tests.integration.test_station_event_stream import (  # noqa: F401
    close_of,
    floor,
    hub,
    receive_json,
    subscribe,
    wait_until,
)


SENTINEL = {"type": "sentinel"}


@pytest.fixture
def kitchen_floor(floor):
    """`floor` plus a beer at the Bar and an API client as A's admin."""

    with Session(floor.engine) as db:
        beer = Product(
            category_id=db.execute(
                text("SELECT category_id FROM products WHERE id = :p"),
                {"p": floor.restaurant_a.burger_id},
            ).scalar_one(),
            name="Beer",
            price=Decimal("4.00"),
            station_id=floor.stations.bar,
        )
        db.add(beer)
        db.commit()
        floor.beer_id = beer.id

    floor.live = Live(floor.engine, floor.restaurant_a)
    return floor


def deliver(floor, hub, **options):
    worker = OutboxWorker(
        floor.engine,
        StationEventPublisher(hub),
        restaurant_id=floor.a_id,
        retry_delay=lambda attempts: timedelta(0),
        **options,
    )
    return worker.process_batch()


def send_burger_and_beer(floor):
    live = floor.live
    order = live.post(
        "/api/v1/orders",
        json={"table_id": str(floor.restaurant_a.table_id), "guest_count": 2},
    )
    assert order.status_code == 201, order.text
    order_id = order.json()["id"]

    items = {}
    for name, product_id in (("burger", floor.restaurant_a.burger_id), ("beer", floor.beer_id)):
        item = live.post(
            f"/api/v1/orders/{order_id}/items",
            json={"product_id": str(product_id), "quantity": 1},
        )
        assert item.status_code == 201, item.text
        items[name] = item.json()["id"]

    assert live.post(f"/api/v1/orders/{order_id}/send").status_code == 200
    return order_id, items


def preparation_of(floor, item_id):
    return floor.live.scalar(
        "SELECT id FROM preparations WHERE order_item_id = :i", i=item_id
    )


def outbox_rows(floor):
    with floor.engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT * FROM outbox_events WHERE restaurant_id = :r "
                "ORDER BY created_at, id"
            ),
            {"r": floor.a_id},
        ).mappings().all()


def assert_nothing_else(hub, ws, restaurant_id, station_id):
    """Nothing was sent to `ws` before a sentinel offered now."""

    for subscription in hub.station_subscribers(restaurant_id, station_id):
        subscription.offer(SENTINEL)

    assert receive_json(ws) == SENTINEL


def insert_event(floor, event_type, version, payload, aggregate_id):
    with floor.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outbox_events (id, restaurant_id, event_type, "
                "schema_version, aggregate_type, aggregate_id, payload) "
                "VALUES (:id, :r, :t, :v, 'order', :a, CAST(:p AS jsonb))"
            ),
            {
                "id": uuid.uuid4(),
                "r": floor.a_id,
                "t": event_type,
                "v": version,
                "a": aggregate_id,
                "p": json.dumps(payload),
            },
        )


# --- Routing ---


def test_committed_events_reach_only_their_stations(kitchen_floor, hub):
    floor = kitchen_floor
    cook = subscribe(floor, floor.stations.kitchen, floor.tokens["cook"])
    second_cook = subscribe(floor, floor.stations.kitchen, floor.tokens["manager"])
    barman = subscribe(floor, floor.stations.bar, floor.tokens["barman"])
    grill = subscribe(floor, floor.stations.grill, floor.tokens["manager"])
    other_restaurant = subscribe(
        floor, floor.stations.b_kitchen, floor.tokens["b_manager"]
    )

    order_id, items = send_burger_and_beer(floor)
    burger_prep = preparation_of(floor, items["burger"])
    assert floor.live.post(f"/api/v1/preparations/{burger_prep}/start").status_code == 200

    result = deliver(floor, hub)
    sent, started = outbox_rows(floor)
    assert result.delivered == [sent["id"], started["id"]]

    for _, ws in (cook, second_cook):
        to_kitchen = receive_json(ws)
        assert to_kitchen["id"] == str(sent["id"])
        assert to_kitchen["event_type"] == "order.sent"
        assert to_kitchen["aggregate_id"] == order_id
        assert [i["order_item_id"] for i in to_kitchen["payload"]["items"]] == [
            items["burger"]
        ]

        change = receive_json(ws)
        assert change["id"] == str(started["id"])
        assert change["payload"]["preparation_id"] == str(burger_prep)
        assert (change["payload"]["previous_status"], change["payload"]["status"]) == (
            "PENDING",
            "IN_PROGRESS",
        )

    to_bar = receive_json(barman[1])
    assert to_bar["id"] == str(sent["id"])
    assert [i["order_item_id"] for i in to_bar["payload"]["items"]] == [items["beer"]]

    # The bar never sees the kitchen's status change; the grill and the
    # other restaurant see nothing at all.
    assert_nothing_else(hub, barman[1], floor.a_id, floor.stations.bar)
    assert_nothing_else(hub, grill[1], floor.a_id, floor.stations.grill)
    assert_nothing_else(hub, other_restaurant[1], floor.b_id, floor.stations.b_kitchen)

    for session, _ in (cook, second_cook, barman, grill, other_restaurant):
        session.__exit__(None, None, None)


def test_events_are_delivered_with_no_stream_open(kitchen_floor, hub):
    """Delivered means offered to the streams open now, possibly none."""

    send_burger_and_beer(kitchen_floor)

    result = deliver(kitchen_floor, hub)

    assert len(result.delivered) == 1
    assert all(row["published_at"] is not None for row in outbox_rows(kitchen_floor))


def test_disconnected_client_does_not_fail_delivery(kitchen_floor, hub):
    floor = kitchen_floor
    session, _ = subscribe(floor, floor.stations.kitchen, floor.tokens["cook"])
    stays, ws = subscribe(floor, floor.stations.kitchen, floor.tokens["manager"])
    session.__exit__(None, None, None)
    wait_until(lambda: hub.connection_count == 1, "cleanup")

    send_burger_and_beer(floor)
    result = deliver(floor, hub)

    assert result.failed == [] and len(result.delivered) == 1
    assert receive_json(ws)["event_type"] == "order.sent"
    stays.__exit__(None, None, None)


# --- Unsupported, malformed, ordering ---


def test_unsupported_version_is_delivered_as_a_resync_notice(kitchen_floor, hub):
    floor = kitchen_floor
    cook = subscribe(floor, floor.stations.kitchen, floor.tokens["cook"])
    barman = subscribe(floor, floor.stations.bar, floor.tokens["barman"])
    other = subscribe(floor, floor.stations.b_kitchen, floor.tokens["b_manager"])
    insert_event(floor, "order.sent", 2, {"order_id": "x", "note": "Jane"}, uuid.uuid4())

    result = deliver(floor, hub, max_attempts=1)

    [row] = outbox_rows(floor)
    assert result.delivered == [row["id"]] and result.dead_lettered == []
    assert row["published_at"] is not None
    for _, ws in (cook, barman):
        notice = receive_json(ws)
        assert notice == {
            "type": "resync_required",
            "reason": "UNSUPPORTED_EVENT",
            "event_id": str(row["id"]),
            "event_type": "order.sent",
            "schema_version": 2,
        }
    assert_nothing_else(hub, other[1], floor.b_id, floor.stations.b_kitchen)

    for session, _ in (cook, barman, other):
        session.__exit__(None, None, None)


def test_malformed_event_fails_and_holds_back_its_order(kitchen_floor, hub):
    floor = kitchen_floor
    cook = subscribe(floor, floor.stations.kitchen, floor.tokens["cook"])
    order_id = uuid.uuid4()
    insert_event(
        floor,
        "preparation.status_changed",
        1,
        {"order_id": str(order_id), "status": "COOKED"},
        order_id,
    )
    insert_event(
        floor,
        "preparation.status_changed",
        1,
        {
            "preparation_id": str(uuid.uuid4()),
            "order_id": str(order_id),
            "order_item_id": str(uuid.uuid4()),
            "station_id": str(floor.stations.kitchen),
            "previous_status": "PENDING",
            "status": "IN_PROGRESS",
        },
        order_id,
    )

    first = deliver(floor, hub, max_attempts=2)
    broken, held = outbox_rows(floor)
    assert first.failed == [broken["id"]] and first.delivered == []
    assert broken["last_error"].startswith("InvalidEventPayloadError")
    assert "COOKED" not in broken["last_error"]

    second = deliver(floor, hub, max_attempts=2)
    assert second.dead_lettered == [broken["id"]]

    # Still held back behind its dead-lettered predecessor; nothing sent.
    assert deliver(floor, hub, max_attempts=2).processed == 0
    assert outbox_rows(floor)[1]["published_at"] is None
    assert_nothing_else(hub, cook[1], floor.a_id, floor.stations.kitchen)
    cook[0].__exit__(None, None, None)


def test_slow_client_is_disconnected_and_others_keep_receiving(kitchen_floor, hub):
    floor = kitchen_floor
    slow_session, slow = subscribe(floor, floor.stations.kitchen, floor.tokens["cook"])
    fast_session, fast = subscribe(floor, floor.stations.kitchen, floor.tokens["manager"])
    [slow_sub] = [
        s for s in hub.station_subscribers(floor.a_id, floor.stations.kitchen)
        if s.user_id == floor.ids["cook"]
    ]
    # A backlog the stream has not sent yet: the buffer is full.
    with slow_sub._lock:
        slow_sub._buffer.extend({"type": "backlog"} for _ in range(hub.queue_size))

    send_burger_and_beer(floor)
    result = deliver(floor, hub)

    assert len(result.delivered) == 1
    assert close_of(slow) == (4429, "SLOW_CONSUMER")
    assert receive_json(fast)["event_type"] == "order.sent"
    wait_until(lambda: hub.connection_count == 1, "cleanup")
    slow_session.__exit__(None, None, None)
    fast_session.__exit__(None, None, None)


# --- In the API process ---


def test_api_lifespan_runs_delivery_only_when_enabled(kitchen_floor, hub, monkeypatch):
    floor = kitchen_floor

    with TestClient(app):
        assert THREAD_NAME not in {t.name for t in threading.enumerate()}

    monkeypatch.setattr(settings, "outbox_websocket_delivery", True)
    monkeypatch.setattr(settings, "outbox_worker_restaurant_id", floor.a_id)
    monkeypatch.setattr(settings, "outbox_worker_poll_interval_seconds", 0.05)

    with TestClient(app) as client:
        assert THREAD_NAME in {t.name for t in threading.enumerate()}
        session, ws = subscribe(
            floor, floor.stations.kitchen, floor.tokens["cook"], client=client
        )

        order_id, _ = send_burger_and_beer(floor)

        event = receive_json(ws)
        assert event["event_type"] == "order.sent"
        assert event["aggregate_id"] == order_id
        session.__exit__(None, None, None)

    assert THREAD_NAME not in {t.name for t in threading.enumerate()}
    wait_until(
        lambda: all(row["published_at"] is not None for row in outbox_rows(floor)),
        "delivery",
    )
