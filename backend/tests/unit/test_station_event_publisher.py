"""Fan-out of outbox events to station streams (Epic D4.2); no database.

Subscriptions are the hub's real ones; their buffers show exactly what a
stream would send.
"""

import asyncio
import copy
import uuid
from datetime import datetime, timezone

import pytest

from app.realtime.hub import StationEventHub
from app.realtime.publisher import ROUTES, StationEventPublisher
from app.schemas.outbox import CONTRACTS, InvalidEventPayloadError, parse_payload
from app.workers.outbox_worker import OutboxMessage
from tests.unit.test_event_contracts import (
    GOLDEN,
    ITEM_1,
    ITEM_2,
    ORDER,
    PREP_1,
    PREP_2,
    PRODUCT,
    STATION,
    TABLE,
)


A, B = uuid.uuid4(), uuid.uuid4()
BAR = uuid.uuid4()
GRILL = uuid.uuid4()


def message(event_type, version, payload, *, restaurant_id=A, event_id=None):
    return OutboxMessage(
        id=event_id or uuid.uuid4(),
        restaurant_id=restaurant_id,
        event_type=event_type,
        schema_version=version,
        aggregate_type="order",
        aggregate_id=uuid.UUID(ORDER),
        payload=copy.deepcopy(payload),
        created_at=datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc),
        attempt=1,
    )


def kitchen_and_bar_send():
    """A round with one item at STATION (kitchen) and one at BAR."""

    payload = copy.deepcopy(GOLDEN[("order.sent", 1)])
    payload["items"][1]["station_id"] = str(BAR)
    return message("order.sent", 1, payload)


def status_change(station=STATION, **changes):
    payload = {**GOLDEN[("preparation.status_changed", 1)], "station_id": str(station), **changes}
    return message("preparation.status_changed", 1, payload)


@pytest.fixture
def streams():
    """Run a scenario on one loop with a hub and named subscriptions."""

    def run(scenario, queue_size=10):
        async def main():
            hub = StationEventHub(max_connections=20, queue_size=queue_size)

            def sub(station, restaurant=A):
                return hub.subscribe(
                    restaurant_id=restaurant,
                    station_id=uuid.UUID(str(station)),
                    user_id=uuid.uuid4(),
                )

            return scenario(hub, StationEventPublisher(hub), sub)

        return asyncio.run(main())

    return run


def test_every_contract_has_a_route():
    assert set(ROUTES) == set(CONTRACTS.values())


def test_order_sent_reaches_each_station_with_only_its_items(streams):
    def scenario(hub, publisher, sub):
        kitchen, bar, grill = sub(STATION), sub(BAR), sub(GRILL)
        event = kitchen_and_bar_send()

        publisher.publish(event)

        [to_kitchen] = kitchen.drain()
        [to_bar] = bar.drain()
        assert grill.drain() == []

        for wire in (to_kitchen, to_bar):
            assert wire["type"] == "event"
            assert wire["id"] == str(event.id)
            assert wire["event_type"] == "order.sent"
            assert wire["schema_version"] == 1
            assert wire["aggregate_type"] == "order"
            assert wire["aggregate_id"] == ORDER
            assert wire["created_at"] == "2026-10-19T12:00:00+00:00"
            # Each projection is itself a valid v1 payload.
            parse_payload("order.sent", 1, wire["payload"])
            assert {
                k: v for k, v in wire["payload"].items() if k != "items"
            } == {"order_id": ORDER, "table_id": TABLE, "initial": True}

        assert to_kitchen["payload"]["items"] == [
            {
                "order_item_id": ITEM_1,
                "preparation_id": PREP_1,
                "station_id": STATION,
                "product_id": PRODUCT,
                "quantity": 2,
            }
        ]
        assert [i["order_item_id"] for i in to_bar["payload"]["items"]] == [ITEM_2]
        assert [i["preparation_id"] for i in to_bar["payload"]["items"]] == [PREP_2]

    streams(scenario)


def test_wire_messages_carry_only_the_envelope_and_payload(streams):
    def scenario(hub, publisher, sub):
        kitchen = sub(STATION)
        publisher.publish(status_change())

        [wire] = kitchen.drain()
        assert set(wire) == {
            "type",
            "id",
            "event_type",
            "schema_version",
            "aggregate_type",
            "aggregate_id",
            "created_at",
            "payload",
        }
        assert wire["payload"] == GOLDEN[("preparation.status_changed", 1)]

    streams(scenario)


def test_v2_status_change_reaches_only_its_station(streams):
    def scenario(hub, publisher, sub):
        kitchen, bar = sub(STATION), sub(BAR)
        payload = {
            **GOLDEN[("preparation.status_changed", 2)],
            "station_id": str(BAR),
        }

        publisher.publish(message("preparation.status_changed", 2, payload))

        assert kitchen.drain() == []
        [wire] = bar.drain()
        assert wire["type"] == "event"
        assert wire["schema_version"] == 2
        assert wire["payload"] == payload

    streams(scenario)


def test_status_change_reaches_only_its_station(streams):
    def scenario(hub, publisher, sub):
        kitchen, other_kitchen, bar = sub(STATION), sub(STATION), sub(BAR)

        publisher.publish(status_change(station=BAR))

        assert kitchen.drain() == other_kitchen.drain() == []
        [wire] = bar.drain()
        assert wire["payload"]["station_id"] == str(BAR)

    streams(scenario)


def test_no_event_crosses_restaurants(streams):
    def scenario(hub, publisher, sub):
        # B's streams for the same station ids (as if ids were shared).
        b_kitchen, b_bar = sub(STATION, restaurant=B), sub(BAR, restaurant=B)

        publisher.publish(kitchen_and_bar_send())
        publisher.publish(status_change())
        publisher.publish(message("order.sent", 99, {}))

        assert b_kitchen.drain() == b_bar.drain() == []

    streams(scenario)


def test_every_client_of_a_station_gets_the_event(streams):
    def scenario(hub, publisher, sub):
        clients = [sub(STATION) for _ in range(3)]
        gone = sub(STATION)
        hub.unsubscribe(gone)

        publisher.publish(status_change())

        assert [len(c.drain()) for c in clients] == [1, 1, 1]
        assert gone.drain() == []

    streams(scenario)


def test_redelivered_event_is_sent_again_with_the_same_id(streams):
    """At least once: the stream does not deduplicate, displays do."""

    def scenario(hub, publisher, sub):
        kitchen = sub(STATION)
        event = status_change()

        publisher.publish(event)
        publisher.publish(event)

        first, second = kitchen.drain()
        assert first == second
        assert first["id"] == str(event.id)

    streams(scenario)


def test_events_keep_their_order_per_stream(streams):
    def scenario(hub, publisher, sub):
        kitchen = sub(STATION)
        steps = [
            ("PENDING", "IN_PROGRESS"),
            ("IN_PROGRESS", "READY"),
            ("READY", "SERVED"),
        ]
        events = [
            status_change(previous_status=before, status=after)
            for before, after in steps
        ]

        for event in events:
            publisher.publish(event)

        assert [w["id"] for w in kitchen.drain()] == [str(e.id) for e in events]

    streams(scenario)


@pytest.mark.parametrize(
    "event_type, version",
    [("order.sent", 2), ("preparation.status_changed", 99), ("order.archived", 1)],
)
def test_unsupported_event_asks_the_restaurants_streams_to_resync(
    streams, event_type, version
):
    def scenario(hub, publisher, sub):
        kitchen, bar = sub(STATION), sub(BAR)
        event = message(event_type, version, {"order_id": ORDER, "secret": "Jane"})

        publisher.publish(event)  # does not raise: delivered

        for subscription in (kitchen, bar):
            assert subscription.drain() == [
                {
                    "type": "resync_required",
                    "reason": "UNSUPPORTED_EVENT",
                    "event_id": str(event.id),
                    "event_type": event_type,
                    "schema_version": version,
                }
            ]

    streams(scenario)


def test_malformed_payload_fails_the_attempt_and_reaches_nobody(streams):
    def scenario(hub, publisher, sub):
        kitchen = sub(STATION)
        payload = {**GOLDEN[("preparation.status_changed", 1)], "status": "COOKED"}

        with pytest.raises(InvalidEventPayloadError) as raised:
            publisher.publish(message("preparation.status_changed", 1, payload))

        assert "COOKED" not in str(raised.value)
        assert kitchen.drain() == []

    streams(scenario)


def test_slow_client_overflows_without_affecting_the_others(streams):
    def scenario(hub, publisher, sub):
        slow, fast = sub(STATION), sub(STATION)

        for _ in range(2):
            publisher.publish(status_change())
            fast.drain()
        publisher.publish(status_change())  # third: slow's buffer is full

        assert slow.overflowed
        assert not fast.overflowed
        assert len(fast.drain()) == 1
        assert slow.drain() == []

    streams(scenario, queue_size=2)
