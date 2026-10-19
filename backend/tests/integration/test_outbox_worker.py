"""Outbox delivery worker (Epic D2.1) against real PostgreSQL.

Events are created by real API requests and committed; workers use their
own connections, and state is read back on separate connections. Each
worker is limited to the test's restaurant(s), except where a test checks
the unscoped worker. Concurrency is ordered with threading events held
inside a publisher (while it holds a claimed row's lock), not with sleeps.
"""

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db.models.table import Table
from app.schemas.outbox import PreparationStatusChangedV1
from app.services.outbox_service import OutboxService
from app.workers.outbox_worker import (
    OutboxMessage,
    OutboxWorker,
    default_retry_delay,
    describe_error,
)
from tests.integration.test_mutation_concurrency import Live


WAIT_SECONDS = 15
NO_DELAY = lambda attempts: timedelta(0)  # noqa: E731


class Recorder:
    """A publisher that records messages; `fail` decides which raise."""

    def __init__(self, fail=lambda message: False):
        self.fail = fail
        self.messages: list[OutboxMessage] = []
        self.lock = threading.Lock()

    def publish(self, message):
        if self.fail(message):
            raise RuntimeError("broker unavailable")

        with self.lock:
            self.messages.append(message)

    @property
    def ids(self):
        return [m.id for m in self.messages]


class SimulatedCrash(BaseException):
    """Ends the worker the way a killed process would: no except handler."""


@pytest.fixture
def live(db_engine, committed_restaurants):
    return Live(db_engine, committed_restaurants())


def worker(db_engine, live, publisher, **options):
    options.setdefault("retry_delay", NO_DELAY)
    return OutboxWorker(
        db_engine,
        publisher,
        restaurant_id=live.r.restaurant_id,
        **options,
    )


def add_table(db_engine, live, number):
    with Session(db_engine) as db:
        table = Table(restaurant_id=live.r.restaurant_id, number=number, capacity=2)
        db.add(table)
        db.commit()
        return table.id


def sent_order(live, table_id=None):
    """A sent order (burger + coke); returns order id and item ids."""

    order = live.post(
        "/api/v1/orders",
        json={"table_id": str(table_id or live.r.table_id), "guest_count": 2},
    )
    assert order.status_code == 201, order.text
    order_id = order.json()["id"]

    items = []
    for product_id in (live.r.burger_id, live.r.coke_id):
        item = live.post(
            f"/api/v1/orders/{order_id}/items",
            json={"product_id": str(product_id), "quantity": 1},
        )
        assert item.status_code == 201, item.text
        items.append(item.json()["id"])

    assert live.post(f"/api/v1/orders/{order_id}/send").status_code == 200
    return order_id, items


def start_burger(live, burger_id):
    preparation_id = live.scalar(
        "SELECT id FROM preparations WHERE order_item_id = :i", i=burger_id
    )
    response = live.post(f"/api/v1/preparations/{preparation_id}/start")
    assert response.status_code == 200, response.text


def events(live, restaurant_id=None):
    """The restaurant's outbox rows, oldest first, as committed."""

    with live.engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT *, payload::text AS payload_text FROM outbox_events "
                "WHERE restaurant_id = :r ORDER BY created_at, id"
            ),
            {"r": restaurant_id or live.r.restaurant_id},
        ).mappings().all()


def event(live, event_id):
    [row] = [row for row in events(live) if row["id"] == event_id]
    return row


def make_due(live):
    """Skip the backoff: all of the restaurant's events are due now."""

    with live.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE outbox_events SET next_attempt_at = now() "
                "WHERE restaurant_id = :r"
            ),
            {"r": live.r.restaurant_id},
        )


def wait_until(condition, what):
    deadline = time.monotonic() + WAIT_SECONDS
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail(f"Timed out waiting for {what}")
        time.sleep(0.01)


# --- Delivery ---


def test_delivers_undelivered_events_in_order_and_marks_them(db_engine, live):
    order_id, (burger_id, _) = sent_order(live)
    start_burger(live, burger_id)
    sent, started = events(live)
    publisher = Recorder()

    result = worker(db_engine, live, publisher).process_batch()

    assert result.delivered == [sent["id"], started["id"]]
    assert result.failed == []
    for row, message in zip((sent, started), publisher.messages):
        assert message == OutboxMessage(
            id=row["id"],
            restaurant_id=live.r.restaurant_id,
            event_type=row["event_type"],
            schema_version=1,
            aggregate_type="order",
            aggregate_id=row["aggregate_id"],
            payload=row["payload"],
            created_at=row["created_at"],
            attempt=1,
        )
        assert str(message.aggregate_id) == order_id

    for row in events(live):
        assert row["published_at"] is not None
        assert row["attempts"] == 1
        assert row["last_attempt_at"] is not None
        assert row["published_at"] == row["last_attempt_at"]
        assert row["last_error"] is None

    # Delivered events are never handed out again.
    assert worker(db_engine, live, publisher).process_batch().processed == 0
    assert len(publisher.messages) == 2


def test_events_this_code_cannot_parse_are_delivered_unchanged(db_engine, live):
    """The worker is payload-agnostic (Epic D3): an unknown version or a
    payload this code would reject is handed to the publisher like any
    other event, never failed or dead-lettered for it."""

    rows = [
        ("order.sent", 99, {"future_field": True}),
        ("order.archived", 1, {"order_id": "not-a-uuid"}),
    ]
    with live.engine.begin() as connection:
        for event_type, version, payload in rows:
            connection.execute(
                text(
                    "INSERT INTO outbox_events (id, restaurant_id, event_type, "
                    "schema_version, aggregate_type, aggregate_id, payload) "
                    "VALUES (:id, :r, :t, :v, 'order', :a, CAST(:p AS jsonb))"
                ),
                {
                    "id": uuid.uuid4(),
                    "r": live.r.restaurant_id,
                    "t": event_type,
                    "v": version,
                    "a": uuid.uuid4(),
                    "p": json.dumps(payload),
                },
            )
    publisher = Recorder()

    result = worker(db_engine, live, publisher, max_attempts=1).process_batch()

    assert len(result.delivered) == 2
    assert result.failed == result.dead_lettered == []
    assert [
        (m.event_type, m.schema_version, m.payload) for m in publisher.messages
    ] == rows
    assert all(row["published_at"] is not None for row in events(live))


def test_event_is_marked_delivered_only_after_the_publisher_returns(
    db_engine, live
):
    sent_order(live)
    [row] = events(live)
    seen = {}

    class Inspecting:
        def publish(self, message):
            # Seen from another connection while the publisher runs.
            seen["during"] = event(live, message.id)
            with db_engine.connect() as other:
                with pytest.raises(OperationalError, match="could not obtain lock"):
                    other.execute(
                        text(
                            "SELECT id FROM outbox_events WHERE id = :e "
                            "FOR UPDATE NOWAIT"
                        ),
                        {"e": message.id},
                    )

    assert worker(db_engine, live, Inspecting()).process_batch().delivered == [
        row["id"]
    ]

    assert seen["during"]["published_at"] is None
    assert seen["during"]["attempts"] == 0
    assert event(live, row["id"])["published_at"] is not None


def test_failed_delivery_is_recorded_and_retried_with_the_same_event(
    db_engine, live
):
    sent_order(live)
    [before] = events(live)
    failing = Recorder(fail=lambda message: True)

    result = worker(
        db_engine, live, failing, retry_delay=lambda attempts: timedelta(minutes=1)
    ).process_batch()

    assert result.failed == [before["id"]]
    failed = event(live, before["id"])
    assert failed["published_at"] is None
    assert failed["attempts"] == 1
    assert failed["last_error"] == "RuntimeError: broker unavailable"
    assert failed["last_attempt_at"] is not None
    assert failed["next_attempt_at"] - failed["last_attempt_at"] == timedelta(minutes=1)

    # Not due during the backoff.
    publisher = Recorder()
    assert worker(db_engine, live, publisher).process_batch().processed == 0

    make_due(live)
    result = worker(db_engine, live, publisher).process_batch()

    assert result.delivered == [before["id"]]
    assert publisher.messages[0].attempt == 2
    after = event(live, before["id"])
    assert after["published_at"] is not None
    assert after["attempts"] == 2
    # The last failure stays on record; the event itself is unchanged.
    assert after["last_error"] == "RuntimeError: broker unavailable"
    for column in (
        "id",
        "restaurant_id",
        "event_type",
        "schema_version",
        "aggregate_type",
        "aggregate_id",
        "payload_text",
        "created_at",
    ):
        assert after[column] == before[column], column
    assert publisher.messages[0].payload == json.loads(before["payload_text"])


def record_events(db_engine, live, count):
    """`count` committed events, each of its own order (none waits on
    another), so one batch attempts them all."""

    with Session(db_engine) as db:
        for _ in range(count):
            order_id = uuid.uuid4()
            OutboxService.record(
                db=db,
                restaurant_id=live.r.restaurant_id,
                payload=PreparationStatusChangedV1(
                    preparation_id=uuid.uuid4(),
                    order_id=order_id,
                    order_item_id=uuid.uuid4(),
                    station_id=uuid.uuid4(),
                    previous_status="PENDING",
                    status="IN_PROGRESS",
                ),
            )
        db.commit()


def test_bookkeeping_timestamps_come_from_one_time_per_update(db_engine, live):
    """Each UPDATE takes one time (statement_timestamp()) for all its
    columns: the backoff is measured from exactly last_attempt_at, and a
    delivered event's published_at equals its last_attempt_at.

    Many rows make a regression visible: separate clock_timestamp() calls
    differ by a microsecond often enough that some rows would be off.
    """

    record_events(db_engine, live, 60)
    delay = lambda attempts: timedelta(seconds=30 * attempts)  # noqa: E731
    failing = Recorder(fail=lambda message: True)

    for attempt in (1, 2):
        result = worker(
            db_engine, live, failing, retry_delay=delay
        ).process_batch()
        assert len(result.failed) == 60

        for row in events(live):
            assert row["attempts"] == attempt
            assert row["published_at"] is None
            assert row["next_attempt_at"] - row["last_attempt_at"] == delay(attempt)

        make_due(live)

    assert len(worker(db_engine, live, Recorder()).process_batch().delivered) == 60

    for row in events(live):
        assert row["attempts"] == 3
        assert row["published_at"] == row["last_attempt_at"]


def test_failing_event_is_attempted_once_per_batch(db_engine, live):
    sent_order(live)
    [row] = events(live)

    result = worker(
        db_engine, live, Recorder(fail=lambda message: True)
    ).process_batch()

    # Due again at once (no delay), but not retried within the batch.
    assert result.failed == [row["id"]]
    assert event(live, row["id"])["attempts"] == 1


def test_failing_event_holds_back_only_its_own_orders_events(db_engine, live):
    first_order, (burger_id, _) = sent_order(live)
    start_burger(live, burger_id)
    second_order, _ = sent_order(live, add_table(db_engine, live, 2))
    first_sent, first_started, second_sent = events(live)
    broken = {first_sent["id"]}

    publisher = Recorder(fail=lambda message: message.id in broken)
    result = worker(db_engine, live, publisher).process_batch()

    assert result.failed == [first_sent["id"]]
    assert result.delivered == [second_sent["id"]]
    assert event(live, first_started["id"])["attempts"] == 0

    broken.clear()
    result = worker(db_engine, live, publisher).process_batch()

    assert result.delivered == [first_sent["id"], first_started["id"]]
    assert [str(m.aggregate_id) for m in publisher.messages] == [
        second_order,
        first_order,
        first_order,
    ]


def test_crash_after_publishing_redelivers_the_event(db_engine, live):
    """At least once: the publisher succeeded but the worker died before
    committing, so the event is still undelivered and is sent again."""

    sent_order(live)
    [row] = events(live)
    delivered_before_crash = []

    class CrashAfterPublishing:
        def publish(self, message):
            delivered_before_crash.append(message.id)
            raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        worker(db_engine, live, CrashAfterPublishing()).process_batch()

    crashed = event(live, row["id"])
    assert crashed["published_at"] is None
    assert crashed["attempts"] == 0
    assert crashed["last_error"] is None

    publisher = Recorder()
    assert worker(db_engine, live, publisher).process_batch().delivered == [row["id"]]
    assert delivered_before_crash == publisher.ids == [row["id"]]


# --- Concurrency ---


def test_concurrent_worker_skips_claimed_events_and_their_successors(
    db_engine, live
):
    """Worker A holds the first order's oldest event mid-delivery. Worker B
    skips it (SKIP LOCKED) and the order's next event (order), and delivers
    the other order's event; nothing is delivered twice."""

    _, (burger_id, _) = sent_order(live)
    start_burger(live, burger_id)
    sent_order(live, add_table(db_engine, live, 2))
    first_sent, first_started, second_sent = events(live)

    entered, release = threading.Event(), threading.Event()
    calls = Recorder()

    class Blocking:
        def publish(self, message):
            calls.publish(message)
            entered.set()
            assert release.wait(WAIT_SECONDS)

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker_a = pool.submit(
            worker(db_engine, live, Blocking(), batch_size=1).process_batch
        )
        assert entered.wait(WAIT_SECONDS)

        b_result = worker(db_engine, live, calls).process_batch()
        assert b_result.delivered == [second_sent["id"]]
        assert event(live, first_started["id"])["attempts"] == 0

        release.set()
        assert worker_a.result(WAIT_SECONDS).delivered == [first_sent["id"]]

    assert worker(db_engine, live, calls).process_batch().delivered == [
        first_started["id"]
    ]
    assert sorted(calls.ids) == sorted(row["id"] for row in events(live))
    assert all(row["attempts"] == 1 for row in events(live))


def test_parallel_workers_deliver_each_event_exactly_once_between_them(
    db_engine, live
):
    for number in range(2, 6):
        _, (burger_id, _) = sent_order(live, add_table(db_engine, live, number))
        start_burger(live, burger_id)
    expected = [row["id"] for row in events(live)]
    assert len(expected) == 8

    publisher = Recorder()
    start = threading.Barrier(4)

    def run_worker():
        start.wait(WAIT_SECONDS)
        return worker(db_engine, live, publisher, batch_size=3).process_batch()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = [f.result(WAIT_SECONDS) for f in [pool.submit(run_worker) for _ in range(4)]]

    # Whatever the interleaving: no event twice, then everything delivered.
    delivered = [i for result in results for i in result.delivered]
    assert len(delivered) == len(set(delivered))
    worker(db_engine, live, publisher).process_batch()

    assert sorted(publisher.ids) == sorted(expected)
    assert all(row["published_at"] is not None for row in events(live))
    assert all(row["attempts"] == 1 for row in events(live))


# --- Tenants ---


def test_scoped_worker_delivers_only_its_restaurants_events(
    db_engine, committed_restaurants
):
    a = Live(db_engine, committed_restaurants("A"))
    b = Live(db_engine, committed_restaurants("B"))
    sent_order(a)
    sent_order(b)
    publisher = Recorder()

    worker(db_engine, a, publisher).process_batch()

    assert [m.restaurant_id for m in publisher.messages] == [a.r.restaurant_id]
    [untouched] = events(b)
    assert untouched["published_at"] is None
    assert untouched["attempts"] == 0


def test_unscoped_worker_keeps_each_events_restaurant(
    db_engine, committed_restaurants
):
    a = Live(db_engine, committed_restaurants("A"))
    b = Live(db_engine, committed_restaurants("B"))
    a_order, _ = sent_order(a)
    b_order, _ = sent_order(b)
    publisher = Recorder()

    OutboxWorker(db_engine, publisher).process_batch()

    by_order = {str(m.aggregate_id): m for m in publisher.messages}
    assert by_order[a_order].restaurant_id == a.r.restaurant_id
    assert by_order[b_order].restaurant_id == b.r.restaurant_id
    assert by_order[a_order].payload["order_id"] == a_order
    assert events(a)[0]["published_at"] is not None
    assert events(b)[0]["published_at"] is not None


# --- Loop and logging ---


def test_run_delivers_until_stopped(db_engine, live):
    publisher = Recorder()
    stop = threading.Event()
    loop = threading.Thread(
        target=worker(db_engine, live, publisher).run,
        args=(stop,),
        kwargs={"poll_interval": 0.01},
    )
    loop.start()

    try:
        sent_order(live)
        wait_until(lambda: events(live)[0]["published_at"] is not None, "delivery")
    finally:
        stop.set()
        loop.join(WAIT_SECONDS)

    assert not loop.is_alive()
    assert len(publisher.messages) == 1


def test_logs_carry_no_payload_or_error_message(
    db_engine, live, caplog, monkeypatch
):
    order_id, _ = sent_order(live)
    [row] = events(live)
    table_id = str(live.r.table_id)

    class Failing:
        def publish(self, message):
            raise RuntimeError(f"rejected table {table_id}")

    # The session's in-process `alembic upgrade` (env.py fileConfig)
    # disables loggers that already exist; re-enable this one.
    monkeypatch.setattr(logging.getLogger("app.outbox"), "disabled", False)
    with caplog.at_level(logging.INFO, logger="app.outbox"):
        worker(db_engine, live, Failing()).process_batch()
        worker(db_engine, live, Recorder()).process_batch()

    assert str(row["id"]) in caplog.text
    assert "RuntimeError" in caplog.text
    assert table_id not in caplog.text
    assert row["payload"]["items"][0]["order_item_id"] not in caplog.text
    # The error message is kept for diagnosis, in the database only.
    assert event(live, row["id"])["last_error"] == f"RuntimeError: rejected table {table_id}"


# --- Pure helpers ---


def test_retry_delay_doubles_up_to_five_minutes():
    assert [default_retry_delay(n).total_seconds() for n in range(1, 9)] == [
        5, 10, 20, 40, 80, 160, 300, 300,
    ]


def test_error_description_is_bounded():
    assert describe_error(ValueError("x" * 5000)) == ("ValueError: " + "x" * 5000)[:1000]


def test_batch_size_must_be_positive():
    with pytest.raises(ValueError):
        OutboxWorker(None, Recorder(), batch_size=0)
