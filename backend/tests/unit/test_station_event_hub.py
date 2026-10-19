"""The in-process station event hub (Epic D4); no database needed."""

import asyncio
import threading
import uuid

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.realtime.hub import StationEventHub
from tests.unit.test_outbox_worker_config import REQUIRED


A, B = uuid.uuid4(), uuid.uuid4()
KITCHEN, BAR = uuid.uuid4(), uuid.uuid4()


def run(coroutine):
    return asyncio.run(coroutine)


def test_subscriptions_are_registered_per_restaurant_and_station():
    async def scenario():
        hub = StationEventHub(max_connections=10, queue_size=5)
        kitchen = hub.subscribe(restaurant_id=A, station_id=KITCHEN, user_id=uuid.uuid4())
        bar = hub.subscribe(restaurant_id=A, station_id=BAR, user_id=uuid.uuid4())
        other = hub.subscribe(restaurant_id=B, station_id=KITCHEN, user_id=uuid.uuid4())

        assert hub.connection_count == 3
        assert hub.station_subscribers(A, KITCHEN) == [kitchen]
        assert hub.station_subscribers(B, KITCHEN) == [other]
        assert set(hub.restaurant_subscribers(A)) == {kitchen, bar}
        assert hub.station_subscribers(B, BAR) == []

        hub.unsubscribe(kitchen)
        hub.unsubscribe(kitchen)  # idempotent
        assert hub.connection_count == 2
        assert hub.station_subscribers(A, KITCHEN) == []

    run(scenario())


def test_hub_refuses_connections_beyond_its_limit():
    async def scenario():
        hub = StationEventHub(max_connections=2, queue_size=5)
        first = hub.subscribe(restaurant_id=A, station_id=KITCHEN, user_id=uuid.uuid4())
        hub.subscribe(restaurant_id=A, station_id=KITCHEN, user_id=uuid.uuid4())

        assert hub.subscribe(restaurant_id=A, station_id=BAR, user_id=uuid.uuid4()) is None
        assert hub.connection_count == 2

        hub.unsubscribe(first)
        assert hub.subscribe(restaurant_id=A, station_id=BAR, user_id=uuid.uuid4())

    run(scenario())


def test_buffer_keeps_order_and_overflows_instead_of_growing():
    async def scenario():
        hub = StationEventHub(max_connections=1, queue_size=3)
        subscription = hub.subscribe(restaurant_id=A, station_id=KITCHEN, user_id=uuid.uuid4())

        assert [subscription.offer({"n": n}) for n in range(3)] == [True] * 3
        await asyncio.wait_for(subscription.wait(), 1)
        assert subscription.drain() == [{"n": 0}, {"n": 1}, {"n": 2}]
        assert subscription.drain() == []

        for n in range(3):
            subscription.offer({"n": n})
        assert subscription.offer({"n": 3}) is False
        assert subscription.overflowed
        # Nothing more is held for a connection that will be closed.
        assert subscription.drain() == []
        assert subscription.offer({"n": 4}) is False

    run(scenario())


def test_closed_subscription_accepts_nothing():
    async def scenario():
        hub = StationEventHub(max_connections=1, queue_size=3)
        subscription = hub.subscribe(restaurant_id=A, station_id=KITCHEN, user_id=uuid.uuid4())
        hub.unsubscribe(subscription)

        assert subscription.offer({"n": 1}) is False
        assert subscription.drain() == []

    run(scenario())


def test_offers_from_other_threads_wake_the_connection():
    async def scenario():
        hub = StationEventHub(max_connections=1, queue_size=1000)
        subscription = hub.subscribe(restaurant_id=A, station_id=KITCHEN, user_id=uuid.uuid4())

        def produce(start):
            for n in range(start, start + 100):
                subscription.offer({"n": n})

        threads = [threading.Thread(target=produce, args=(i * 100,)) for i in range(5)]
        for thread in threads:
            thread.start()

        received = []
        while len(received) < 500:
            await asyncio.wait_for(subscription.wait(), 2)
            received.extend(m["n"] for m in subscription.drain())

        for thread in threads:
            thread.join()

        assert sorted(received) == list(range(500))
        # Each producer's own messages stay in order.
        for i in range(5):
            mine = [n for n in received if i * 100 <= n < (i + 1) * 100]
            assert mine == sorted(mine)

    run(scenario())


def test_offer_after_the_loop_stopped_does_not_raise():
    async def make():
        hub = StationEventHub(max_connections=1, queue_size=3)
        return hub.subscribe(restaurant_id=A, station_id=KITCHEN, user_id=uuid.uuid4())

    subscription = run(make())  # asyncio.run closed its loop

    assert subscription.offer({"n": 1}) is True


@pytest.mark.parametrize("options", [{"max_connections": 0}, {"queue_size": 0}])
def test_hub_rejects_unsafe_limits(options):
    with pytest.raises(ValueError):
        StationEventHub(**{"max_connections": 1, "queue_size": 1, **options})


def test_websocket_defaults_are_bounded():
    config = Settings(**REQUIRED)

    assert config.websocket_auth_timeout_seconds == 10.0
    assert config.websocket_reauth_interval_seconds == 30.0
    assert config.websocket_queue_size == 100
    assert config.websocket_max_connections == 500


@pytest.mark.parametrize(
    "name, value",
    [
        ("websocket_auth_timeout_seconds", 0),
        ("websocket_auth_timeout_seconds", 61),
        ("websocket_reauth_interval_seconds", 0),
        ("websocket_reauth_interval_seconds", 3601),
        ("websocket_queue_size", 0),
        ("websocket_queue_size", 10001),
        ("websocket_max_connections", 0),
        ("websocket_max_connections", 100001),
    ],
)
def test_unsafe_websocket_settings_are_rejected(name, value):
    with pytest.raises(ValidationError, match=name):
        Settings(**REQUIRED, **{name: value})
