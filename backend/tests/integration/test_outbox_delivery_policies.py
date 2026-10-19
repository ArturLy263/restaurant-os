"""Outbox delivery policies (Epic D2.2) against real PostgreSQL: the retry
limit and dead letters, the publish timeout, retention cleanup and the
`python -m app.workers.outbox_worker` runner.

Events are committed by real API requests (or OutboxService); workers use
their own connections, and state is read back on separate connections.
"""

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.workers.outbox_worker import (
    APPLICATION_NAME,
    cleanup_delivered_events,
    delete_delivered_events,
    main,
)
from tests.integration.test_mutation_concurrency import Live
from tests.integration.test_outbox_worker import (
    NO_DELAY,
    WAIT_SECONDS,
    Recorder,
    add_table,
    event,
    events,
    make_due,
    record_events,
    sent_order,
    start_burger,
    wait_until,
    worker,
)


BACKEND = Path(__file__).resolve().parents[2]

EVENT_COLUMNS = (
    "id",
    "restaurant_id",
    "event_type",
    "schema_version",
    "aggregate_type",
    "aggregate_id",
    "payload_text",
    "created_at",
)


@pytest.fixture
def live(db_engine, committed_restaurants):
    return Live(db_engine, committed_restaurants())


def always_failing():
    return Recorder(fail=lambda message: True)


class Hanging:
    """A publisher whose calls block until `release` is set, then return."""

    def __init__(self):
        self.release = threading.Event()
        self.entered = threading.Event()
        self.finished: list = []

    def publish(self, message):
        self.entered.set()
        assert self.release.wait(WAIT_SECONDS)
        self.finished.append(message.id)


def assert_not_locked(live, event_id):
    with live.engine.connect() as other:
        assert other.scalar(
            text("SELECT id FROM outbox_events WHERE id = :e FOR UPDATE NOWAIT"),
            {"e": event_id},
        ) == event_id
        other.rollback()


# --- Retry limit and dead letters ---


def test_event_is_dead_lettered_exactly_at_the_attempt_limit(db_engine, live):
    sent_order(live)
    [before] = events(live)
    failing = always_failing()

    for attempt in (1, 2):
        result = worker(db_engine, live, failing, max_attempts=3).process_batch()
        assert result.failed == [before["id"]]
        assert result.dead_lettered == []
        row = event(live, before["id"])
        assert row["attempts"] == attempt
        assert row["dead_lettered_at"] is None

    result = worker(db_engine, live, failing, max_attempts=3).process_batch()

    assert result.failed == []
    assert result.dead_lettered == [before["id"]]
    dead = event(live, before["id"])
    assert dead["attempts"] == 3
    assert dead["published_at"] is None
    assert dead["dead_lettered_at"] == dead["last_attempt_at"]
    assert dead["last_error"] == "RuntimeError: broker unavailable"
    for column in EVENT_COLUMNS:
        assert dead[column] == before[column], column


def test_dead_lettered_event_is_never_retried(db_engine, live):
    sent_order(live)
    [row] = events(live)
    worker(db_engine, live, always_failing(), max_attempts=1).process_batch()
    assert event(live, row["id"])["dead_lettered_at"] is not None

    make_due(live)
    publisher = Recorder()
    # Not by the same limit, a higher one, or another worker.
    for max_attempts in (1, 10, 100):
        result = worker(
            db_engine, live, publisher, max_attempts=max_attempts
        ).process_batch()
        assert result.processed == 0

    assert publisher.messages == []
    assert event(live, row["id"])["attempts"] == 1


def test_dead_lettered_event_keeps_blocking_its_orders_later_events(
    db_engine, live
):
    _, (burger_id, _) = sent_order(live)
    start_burger(live, burger_id)
    sent_order(live, add_table(db_engine, live, 2))
    first_sent, first_started, second_sent = events(live)

    result = worker(
        db_engine,
        live,
        Recorder(fail=lambda message: message.id == first_sent["id"]),
        max_attempts=1,
    ).process_batch()

    assert result.dead_lettered == [first_sent["id"]]
    assert result.delivered == [second_sent["id"]]

    publisher = Recorder()
    for _ in range(3):
        make_due(live)
        assert worker(db_engine, live, publisher).process_batch().processed == 0

    held_back = event(live, first_started["id"])
    assert held_back["attempts"] == 0
    assert held_back["published_at"] is None
    assert held_back["dead_lettered_at"] is None


def test_delivery_on_the_last_allowed_attempt_is_not_dead_lettered(
    db_engine, live
):
    sent_order(live)
    [row] = events(live)
    calls = []

    def fail_first(message):
        calls.append(message.attempt)
        return message.attempt == 1

    publisher = Recorder(fail=fail_first)
    assert worker(db_engine, live, publisher, max_attempts=2).process_batch().failed
    result = worker(db_engine, live, publisher, max_attempts=2).process_batch()

    assert result.delivered == [row["id"]]
    delivered = event(live, row["id"])
    assert delivered["attempts"] == 2
    assert delivered["dead_lettered_at"] is None
    assert calls == [1, 2]


def test_lowered_limit_dead_letters_an_event_past_it(db_engine, live):
    sent_order(live)
    [row] = events(live)
    for _ in range(4):
        worker(db_engine, live, always_failing(), max_attempts=10).process_batch()
    assert event(live, row["id"])["attempts"] == 4

    result = worker(
        db_engine, live, always_failing(), max_attempts=3
    ).process_batch()

    assert result.dead_lettered == [row["id"]]
    assert event(live, row["id"])["attempts"] == 5


def test_database_refuses_an_event_both_delivered_and_dead_lettered(
    db_engine, live
):
    sent_order(live)
    [row] = events(live)

    with pytest.raises(IntegrityError, match="ck_outbox_events_delivered_or_dead_lettered"):
        with db_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE outbox_events SET published_at = now(), "
                    "dead_lettered_at = now() WHERE id = :e"
                ),
                {"e": row["id"]},
            )


def test_dead_letter_log_has_no_payload_or_error_message(
    db_engine, live, caplog, monkeypatch
):
    sent_order(live)
    [row] = events(live)
    table_id = str(live.r.table_id)

    class Failing:
        def publish(self, message):
            raise RuntimeError(f"rejected table {table_id}")

    # The in-process `alembic upgrade` disables existing loggers.
    monkeypatch.setattr(logging.getLogger("app.outbox"), "disabled", False)
    with caplog.at_level(logging.INFO, logger="app.outbox"):
        worker(db_engine, live, Failing(), max_attempts=1).process_batch()

    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert str(row["id"]) in record.getMessage()
    assert "dead-lettered after 1 failed attempts" in record.getMessage()
    assert table_id not in caplog.text
    assert event(live, row["id"])["last_error"] == f"RuntimeError: rejected table {table_id}"


# --- Publish timeout ---


def test_timed_out_publish_is_a_failed_attempt_and_releases_the_lock(
    db_engine, live
):
    sent_order(live)
    [row] = events(live)
    hanging = Hanging()
    timed = worker(db_engine, live, hanging, publish_timeout=0.2)

    started = time.monotonic()
    result = timed.process_batch()
    elapsed = time.monotonic() - started

    try:
        assert hanging.entered.is_set()
        assert 0.2 <= elapsed < 5
        assert result.failed == [row["id"]]
        assert result.stalled is True

        failed = event(live, row["id"])
        assert failed["attempts"] == 1
        assert failed["published_at"] is None
        assert failed["last_error"] == (
            "PublishTimeout: publisher did not return within 0.2s"
        )
        assert_not_locked(live, row["id"])

        # The call was abandoned, not cancelled: it is still running.
        assert timed.publisher_busy
        assert hanging.finished == []
    finally:
        hanging.release.set()


def test_worker_claims_nothing_while_an_abandoned_call_runs(db_engine, live):
    """At most one publisher call in flight; the abandoned call may still
    deliver, and the retry then delivers again (at least once)."""

    sent_order(live)
    sent_order(live, add_table(db_engine, live, 2))
    first, second = events(live)
    hanging = Hanging()
    timed = worker(db_engine, live, hanging, publish_timeout=0.2)

    try:
        assert timed.process_batch().failed == [first["id"]]

        stalled = timed.process_batch()
        assert stalled.stalled is True
        assert stalled.processed == 0
        assert event(live, second["id"])["attempts"] == 0
    finally:
        hanging.release.set()

    wait_until(lambda: not timed.publisher_busy, "the abandoned call to finish")
    # The abandoned call did finish, i.e. it delivered after the timeout.
    assert hanging.finished == [first["id"]]

    result = timed.process_batch()

    assert result.delivered == [first["id"], second["id"]]
    assert hanging.finished == [first["id"], first["id"], second["id"]]
    assert event(live, first["id"])["attempts"] == 2


def test_timeouts_count_towards_the_attempt_limit(db_engine, live):
    sent_order(live)
    [row] = events(live)
    hanging = Hanging()

    try:
        result = worker(
            db_engine, live, hanging, publish_timeout=0.2, max_attempts=1
        ).process_batch()
    finally:
        hanging.release.set()

    assert result.dead_lettered == [row["id"]]
    assert event(live, row["id"])["last_error"].startswith("PublishTimeout")


def test_abandoned_call_runs_in_a_daemon_thread(db_engine, live):
    sent_order(live)
    hanging = Hanging()
    timed = worker(db_engine, live, hanging, publish_timeout=0.2)

    try:
        timed.process_batch()
        assert timed._abandoned is not None
        assert timed._abandoned.daemon
    finally:
        hanging.release.set()


# --- Retention cleanup ---


def delivered_ago(live, event_ids, age):
    with live.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE outbox_events SET published_at = now() - :age, "
                "attempts = 1 WHERE id = ANY(:ids)"
            ),
            {"age": age, "ids": list(event_ids)},
        )


@pytest.fixture
def retention(db_engine, committed_restaurants):
    """Five old delivered events, oldest first, and one of each kind that
    cleanup must keep, in restaurant A; an old delivered event in B."""

    a = Live(db_engine, committed_restaurants("A"))
    b = Live(db_engine, committed_restaurants("B"))
    record_events(db_engine, a, 8)
    record_events(db_engine, b, 1)

    old = [row["id"] for row in events(a)[:5]]
    recent, undelivered, dead = (row["id"] for row in events(a)[5:])
    # Half days: no age sits on a whole-day retention boundary.
    for days, event_id in zip((10.5, 9.5, 8.5, 7.5, 6.5), old):
        delivered_ago(a, [event_id], timedelta(days=days))
    delivered_ago(a, [recent], timedelta(hours=1))
    delivered_ago(b, [events(b)[0]["id"]], timedelta(days=30))

    with db_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE outbox_events SET created_at = now() - interval '90 days', "
                "dead_lettered_at = now() - interval '60 days', attempts = 10 "
                "WHERE id = :e"
            ),
            {"e": dead},
        )

    return SimpleNamespace(
        a=a, b=b, old=old, kept={recent, undelivered, dead}
    )


def remaining(live):
    return {row["id"] for row in events(live)}


def test_cleanup_deletes_only_old_delivered_events_in_bounded_batches(
    db_engine, retention
):
    a = retention.a
    older_than = timedelta(days=2)

    def delete_batch():
        return delete_delivered_events(
            db_engine,
            older_than=older_than,
            batch_size=2,
            restaurant_id=a.r.restaurant_id,
        )

    assert delete_batch() == 2
    # Oldest deliveries first.
    assert remaining(a) == set(retention.old[2:]) | retention.kept
    assert delete_batch() == 2
    assert delete_batch() == 1
    assert delete_batch() == 0

    assert remaining(a) == retention.kept
    assert len(events(retention.b)) == 1


def test_cleanup_stops_after_max_batches(db_engine, retention):
    a = retention.a
    options = dict(
        older_than=timedelta(days=2),
        batch_size=2,
        restaurant_id=a.r.restaurant_id,
    )

    assert cleanup_delivered_events(db_engine, max_batches=2, **options) == 4
    assert remaining(a) == {retention.old[4]} | retention.kept
    assert cleanup_delivered_events(db_engine, max_batches=2, **options) == 1
    assert remaining(a) == retention.kept


def test_cleanup_batch_bound_does_not_depend_on_table_statistics(
    db_engine, committed_restaurants
):
    """Regression: with statistics saying the table is almost empty,
    PostgreSQL ran `DELETE ... WHERE id IN (SELECT ... LIMIT n FOR UPDATE
    SKIP LOCKED)` as a nested loop that re-ran the subquery for every
    row; each re-run skipped the rows already deleted and picked new
    ones, so one batch deleted every eligible event."""

    a = Live(db_engine, committed_restaurants("A"))
    record_events(db_engine, a, 1)

    with db_engine.connect() as connection:
        connection.execute(text("ANALYZE outbox_events"))
        connection.commit()

    record_events(db_engine, a, 5)
    old = [row["id"] for row in events(a)[1:]]
    # Oldest first, as the table stores them: the order that over-deleted.
    for days, event_id in zip((10.5, 9.5, 8.5, 7.5, 6.5), old):
        delivered_ago(a, [event_id], timedelta(days=days))

    deleted = delete_delivered_events(
        db_engine,
        older_than=timedelta(days=2),
        batch_size=2,
        restaurant_id=a.r.restaurant_id,
    )

    assert deleted == 2
    assert remaining(a) == {events(a)[0]["id"]} | set(old[2:])


def test_cleanup_respects_the_retention_period(db_engine, retention):
    deleted = delete_delivered_events(
        db_engine,
        older_than=timedelta(days=8),
        batch_size=100,
        restaurant_id=retention.a.r.restaurant_id,
    )

    # Delivered 10.5, 9.5 and 8.5 days ago; 7.5 and 6.5 are kept.
    assert deleted == 3
    assert remaining(retention.a) == set(retention.old[3:]) | retention.kept


def test_cleanup_skips_locked_rows_instead_of_waiting(db_engine, retention):
    a = retention.a

    with db_engine.connect() as holder:
        holder.execute(
            text("SELECT id FROM outbox_events WHERE id = :e FOR UPDATE"),
            {"e": retention.old[0]},
        )

        deleted = delete_delivered_events(
            db_engine,
            older_than=timedelta(days=2),
            batch_size=100,
            restaurant_id=a.r.restaurant_id,
        )
        holder.rollback()

    assert deleted == 4
    assert remaining(a) == {retention.old[0]} | retention.kept


def test_cleanup_command_uses_the_configured_retention(db_engine, retention):
    config = SimpleNamespace(
        database_url=db_engine.url.render_as_string(hide_password=False),
        outbox_retention_days=7,
        outbox_cleanup_batch_size=2,
        outbox_worker_restaurant_id=retention.a.r.restaurant_id,
    )

    assert main(["cleanup"], config=config) == 0

    # Delivered 10.5 to 7.5 days ago (two batches of 2); 6.5 is kept.
    assert remaining(retention.a) == {retention.old[4]} | retention.kept
    assert len(events(retention.b)) == 1


@pytest.mark.parametrize(
    "options",
    [
        {"older_than": timedelta(hours=23), "batch_size": 10},
        {"older_than": timedelta(days=1), "batch_size": 0},
    ],
)
def test_cleanup_rejects_unsafe_arguments(db_engine, options):
    with pytest.raises(ValueError):
        delete_delivered_events(db_engine, **options)

    with pytest.raises(ValueError):
        cleanup_delivered_events(
            db_engine,
            older_than=timedelta(days=7),
            batch_size=10,
            max_batches=0,
        )


# --- Runner process ---


def worker_connections(db_engine):
    with db_engine.connect() as connection:
        return connection.scalar(
            text("SELECT count(*) FROM pg_stat_activity WHERE application_name = :a"),
            {"a": APPLICATION_NAME},
        )


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT])
def test_runner_delivers_and_shuts_down_on_signal(db_engine, live, stop_signal):
    database_url = db_engine.url.render_as_string(hide_password=False)
    # The runner must only ever see the test database.
    assert make_url(database_url).database.endswith("_test")

    sent_order(live)
    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "OUTBOX_WORKER_RESTAURANT_ID": str(live.r.restaurant_id),
        "OUTBOX_WORKER_POLL_INTERVAL_SECONDS": "0.05",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "app.workers.outbox_worker"],
        cwd=BACKEND,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        wait_until(lambda: events(live)[0]["published_at"] is not None, "delivery")
        assert worker_connections(db_engine) >= 1

        process.send_signal(stop_signal)
        _, stderr = process.communicate(timeout=WAIT_SECONDS)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    assert process.returncode == 0, stderr
    assert "Outbox worker started" in stderr
    assert f"Received {stop_signal.name}" in stderr
    assert "Outbox worker stopped" in stderr
    assert "Traceback" not in stderr
    wait_until(lambda: worker_connections(db_engine) == 0, "connections to close")
