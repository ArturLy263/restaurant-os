"""Outbox delivery worker: hands committed outbox events to a publisher.

Nothing here runs on import. Run it as a process (from backend/):

    python -m app.workers.outbox_worker            # deliver until SIGINT/SIGTERM
    python -m app.workers.outbox_worker cleanup    # delete old delivered events

or create an `OutboxWorker` and call `process_batch()` (one bounded pass)
or `run(stop)` from a thread or process the caller owns.

Each event is delivered in its own short transaction:

1. Claim one due, undelivered, not dead-lettered event with `SELECT ...
   FOR UPDATE SKIP LOCKED`. Concurrent workers skip rows another worker
   has claimed, so no event is handed to two publishers at the same time.
2. Call the publisher with the event, while the row stays locked, and
   wait for it at most `publish_timeout` seconds.
3. Only if the publisher returned in time: set `published_at` and commit.
   If it raised or timed out, record the attempt (count, time, error) and
   a backoff in `next_attempt_at`, commit, and leave the event
   undelivered; after `max_attempts` failed attempts the event is
   dead-lettered instead and never retried automatically.

Delivery is at least once, not exactly once. If the worker dies or loses
the database after the publisher succeeded but before the commit, the
claim is rolled back and the event is delivered again later. Consumers
deduplicate on the event `id`.

Publish timeout: the publisher runs in a separate (daemon) thread and the
worker stops waiting for it after `publish_timeout` seconds. Python cannot
interrupt a blocking call, so the timed-out call is NOT cancelled: it is
abandoned and may still finish, and deliver, later. The worker records the
attempt as failed, which ends the transaction and releases the row lock,
so the event will be published again (a possible duplicate, consistent
with at-least-once). While an abandoned call is still running the worker
claims no events at all, so a worker never has more than one publisher
call in flight. The timeout bounds how long the worker waits and holds a
row lock and a transaction, not how long the publisher runs.

Order: an event is not claimed while an older event of the same aggregate
(the same order) is undelivered, whether that one is claimed by another
worker, waiting for a retry, failing or dead-lettered. One order's events
are therefore delivered in the order they were written; a failing or
dead-lettered event holds back the later events of its order (and only of
its order). Nothing skips a dead-lettered event: there is no recovery
policy yet.

The worker writes only the delivery columns, never the event's type,
version, scope, aggregate or payload, and logs event metadata only, never
payloads or error messages.
"""

import argparse
import logging
import signal
import sys
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import (
    Connection,
    Engine,
    create_engine,
    delete,
    exists,
    func,
    select,
    tuple_,
    update,
)
from sqlalchemy.orm import aliased

from app.db.models.outbox_event import OutboxEvent


logger = logging.getLogger("app.outbox")

MAX_ERROR_LENGTH = 1000

DEFAULT_BATCH_SIZE = 100
DEFAULT_MAX_ATTEMPTS = 10
DEFAULT_PUBLISH_TIMEOUT = 30.0

# Cleanup never deletes events delivered more recently than this.
MIN_RETENTION = timedelta(days=1)

# Identifies the runner's connections in pg_stat_activity.
APPLICATION_NAME = "restaurantos-outbox-worker"


@dataclass(frozen=True)
class OutboxMessage:
    """What a publisher receives: the event's envelope and payload."""

    id: uuid.UUID
    restaurant_id: uuid.UUID
    event_type: str
    schema_version: int
    aggregate_type: str
    aggregate_id: uuid.UUID
    payload: dict[str, Any]
    created_at: datetime
    # 1 for the first delivery attempt, 2 for the first retry, ...
    attempt: int


class Publisher(Protocol):
    """Delivers one message; returning means delivered, raising means not.

    Must be safe to call again with the same message (at-least-once),
    including while an earlier, timed-out call for it is still running.
    Exception messages are stored in `last_error`, so they should not
    contain payload contents.
    """

    def publish(self, message: OutboxMessage) -> None: ...


class LoggingPublisher:
    """Minimal transport: logs that an event was delivered, no payload."""

    def publish(self, message: OutboxMessage) -> None:
        logger.info(
            "Outbox event %s (%s v%d, %s %s) for restaurant %s",
            message.id,
            message.event_type,
            message.schema_version,
            message.aggregate_type,
            message.aggregate_id,
            message.restaurant_id,
        )


class PublishTimeout(Exception):
    """The publisher did not return in time; its call was abandoned."""


def default_retry_delay(attempts: int) -> timedelta:
    """5s after the first failure, doubling, at most 5 minutes."""

    return timedelta(seconds=min(5 * 2 ** (attempts - 1), 300))


@dataclass
class BatchResult:
    delivered: list[uuid.UUID] = field(default_factory=list)
    # Failed and will be retried.
    failed: list[uuid.UUID] = field(default_factory=list)
    # Failed for the last allowed time; not retried.
    dead_lettered: list[uuid.UUID] = field(default_factory=list)
    # An abandoned (timed-out) publisher call is still running, so the
    # batch stopped claiming events.
    stalled: bool = False

    @property
    def processed(self) -> int:
        return len(self.delivered) + len(self.failed) + len(self.dead_lettered)


class OutboxWorker:

    def __init__(
        self,
        engine: Engine,
        publisher: Publisher,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        restaurant_id: uuid.UUID | None = None,
        retry_delay: Callable[[int], timedelta] = default_retry_delay,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        publish_timeout: float = DEFAULT_PUBLISH_TIMEOUT,
    ):
        """`restaurant_id` limits the worker to one restaurant's events;
        by default it delivers every restaurant's events, each with its
        own `restaurant_id`. `retry_delay(attempts)` is the wait after the
        `attempts`-th failed attempt. The `max_attempts`-th failed attempt
        dead-letters the event. `publish_timeout` is in seconds."""

        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")

        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

        if not publish_timeout > 0:
            raise ValueError("publish_timeout must be positive")

        self.engine = engine
        self.publisher = publisher
        self.batch_size = batch_size
        self.restaurant_id = restaurant_id
        self.retry_delay = retry_delay
        self.max_attempts = max_attempts
        self.publish_timeout = publish_timeout

        # A timed-out publisher call that has not returned yet.
        self._abandoned: threading.Thread | None = None

    @property
    def publisher_busy(self) -> bool:
        """True while an abandoned publisher call is still running."""

        if self._abandoned is not None and not self._abandoned.is_alive():
            self._abandoned = None

        return self._abandoned is not None

    def process_batch(self, stop: threading.Event | None = None) -> BatchResult:
        """Attempt up to `batch_size` due events, each at most once.

        Stops early when `stop` is set or a publisher call times out.
        """

        result = BatchResult()
        attempted: list[uuid.UUID] = []

        while len(attempted) < self.batch_size:
            if stop is not None and stop.is_set():
                break

            if self.publisher_busy:
                result.stalled = True
                break

            outcome = self._process_one(exclude=attempted)

            if outcome is None:
                break

            event_id, state = outcome
            attempted.append(event_id)
            getattr(result, state).append(event_id)

        return result

    def run(self, stop: threading.Event, *, poll_interval: float = 1.0) -> None:
        """Process batches until `stop` is set.

        Waits `poll_interval` seconds when there was nothing (more) to do,
        the publisher is stalled or the database failed; a full batch is
        followed by the next one at once. Once `stop` is set, the event
        being delivered (if any) is finished first.
        """

        while not stop.is_set():
            try:
                result = self.process_batch(stop)
            except Exception:
                logger.exception("Outbox batch failed; retrying after a pause")
                result = None

            if (
                result is None
                or result.stalled
                or result.processed < self.batch_size
            ):
                stop.wait(poll_interval)

    # --- One event, one transaction ---

    def _process_one(
        self,
        exclude: list[uuid.UUID],
    ) -> tuple[uuid.UUID, str] | None:

        with self.engine.connect() as connection, connection.begin():
            row = connection.execute(self._claim(exclude)).mappings().first()

            if row is None:
                return None

            message = OutboxMessage(
                id=row["id"],
                restaurant_id=row["restaurant_id"],
                event_type=row["event_type"],
                schema_version=row["schema_version"],
                aggregate_type=row["aggregate_type"],
                aggregate_id=row["aggregate_id"],
                payload=row["payload"],
                created_at=row["created_at"],
                attempt=row["attempts"] + 1,
            )

            try:
                self._publish(message)
            except Exception as exc:
                return message.id, self._record_failure(connection, message, exc)

            self._record_delivery(connection, message)
            return message.id, "delivered"

    def _publish(self, message: OutboxMessage) -> None:
        """Call the publisher in a thread; wait at most publish_timeout."""

        outcome: dict[str, BaseException] = {}

        def call() -> None:
            try:
                self.publisher.publish(message)
            except BaseException as exc:
                outcome["error"] = exc

        # Daemon: an abandoned call never keeps the process alive.
        thread = threading.Thread(
            target=call,
            name=f"outbox-publish-{message.id}",
            daemon=True,
        )
        thread.start()
        thread.join(self.publish_timeout)

        if thread.is_alive():
            self._abandoned = thread
            raise PublishTimeout(
                f"publisher did not return within {self.publish_timeout:g}s"
            )

        if "error" in outcome:
            raise outcome["error"]

    def _claim(self, exclude: list[uuid.UUID]):
        event = OutboxEvent
        older = aliased(OutboxEvent)

        # Dead-lettered events are undelivered too, so they hold back
        # their aggregate's later events.
        older_undelivered = exists().where(
            older.restaurant_id == event.restaurant_id,
            older.aggregate_type == event.aggregate_type,
            older.aggregate_id == event.aggregate_id,
            older.published_at.is_(None),
            tuple_(older.created_at, older.id)
            < tuple_(event.created_at, event.id),
        )

        query = (
            select(
                event.id,
                event.restaurant_id,
                event.event_type,
                event.schema_version,
                event.aggregate_type,
                event.aggregate_id,
                event.payload,
                event.created_at,
                event.attempts,
            )
            .where(
                event.published_at.is_(None),
                event.dead_lettered_at.is_(None),
                event.next_attempt_at <= func.now(),
                ~older_undelivered,
            )
            .order_by(event.created_at, event.id)
            .limit(1)
            .with_for_update(skip_locked=True, of=event)
        )

        if self.restaurant_id is not None:
            query = query.where(event.restaurant_id == self.restaurant_id)

        if exclude:
            query = query.where(event.id.not_in(exclude))

        return query

    def _record_delivery(
        self,
        connection: Connection,
        message: OutboxMessage,
    ) -> None:
        delivered = connection.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.id == message.id,
                OutboxEvent.published_at.is_(None),
            )
            .values(
                # One time for the whole statement (clock_timestamp() would
                # be read separately for each column).
                published_at=func.statement_timestamp(),
                attempts=OutboxEvent.attempts + 1,
                last_attempt_at=func.statement_timestamp(),
            )
        )

        # The row is locked by this transaction, so it cannot have changed.
        if delivered.rowcount != 1:
            raise RuntimeError(f"Claimed outbox event {message.id} vanished")

        logger.info(
            "Delivered outbox event %s (%s) on attempt %d",
            message.id,
            message.event_type,
            message.attempt,
        )

    def _record_failure(
        self,
        connection: Connection,
        message: OutboxMessage,
        error: Exception,
    ) -> str:
        """Record a failed attempt; returns "failed" or "dead_lettered"."""

        values = {
            "attempts": OutboxEvent.attempts + 1,
            # The same time for both, so the wait is exactly `delay`.
            "last_attempt_at": func.statement_timestamp(),
            "last_error": describe_error(error),
        }

        # `>=`, not `==`: a lowered limit also stops events past it.
        if message.attempt >= self.max_attempts:
            values["dead_lettered_at"] = func.statement_timestamp()
        else:
            delay = self.retry_delay(message.attempt)
            values["next_attempt_at"] = func.statement_timestamp() + delay

        connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == message.id)
            .values(**values)
        )

        # The exception type only: its message may say more than a log should.
        if "dead_lettered_at" in values:
            logger.error(
                "Outbox event %s (%s) dead-lettered after %d failed attempts "
                "(last: %s); it is not retried and holds back later events "
                "of %s %s",
                message.id,
                message.event_type,
                message.attempt,
                type(error).__name__,
                message.aggregate_type,
                message.aggregate_id,
            )
            return "dead_lettered"

        logger.warning(
            "Outbox event %s (%s) attempt %d failed with %s; retry in %ss",
            message.id,
            message.event_type,
            message.attempt,
            type(error).__name__,
            delay.total_seconds(),
        )
        return "failed"


def describe_error(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"[:MAX_ERROR_LENGTH]


# --- Retention ---


def delete_delivered_events(
    engine: Engine,
    *,
    older_than: timedelta,
    batch_size: int,
    restaurant_id: uuid.UUID | None = None,
) -> int:
    """Delete at most `batch_size` events delivered more than `older_than`
    ago, in one short transaction; returns how many were deleted.

    Only delivered events qualify: undelivered and dead-lettered events
    (published_at NULL) are never deleted. Rows locked by someone else are
    skipped, not waited for.
    """

    if older_than < MIN_RETENTION:
        raise ValueError(f"older_than must be at least {MIN_RETENTION}")

    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")

    eligible = (
        select(OutboxEvent.id)
        .where(
            OutboxEvent.published_at.is_not(None),
            OutboxEvent.published_at < func.now() - older_than,
        )
        .order_by(OutboxEvent.published_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )

    if restaurant_id is not None:
        eligible = eligible.where(OutboxEvent.restaurant_id == restaurant_id)

    # Two statements, not `DELETE ... WHERE id IN (eligible)`: PostgreSQL
    # may re-run that subquery once per row (a nested loop, chosen from
    # table statistics), and each re-run skips the rows already deleted
    # and locks new ones, so one batch could delete every eligible event.
    # The ids stay locked until the commit.
    with engine.begin() as connection:
        ids = connection.execute(eligible).scalars().all()

        if not ids:
            return 0

        deleted = connection.execute(
            delete(OutboxEvent).where(OutboxEvent.id.in_(ids))
        )

    return deleted.rowcount


def cleanup_delivered_events(
    engine: Engine,
    *,
    older_than: timedelta,
    batch_size: int,
    max_batches: int,
    restaurant_id: uuid.UUID | None = None,
) -> int:
    """Run up to `max_batches` deletion batches, each its own transaction,
    until one deletes less than a full batch; returns the total deleted."""

    if max_batches < 1:
        raise ValueError("max_batches must be at least 1")

    total = 0

    for _ in range(max_batches):
        deleted = delete_delivered_events(
            engine,
            older_than=older_than,
            batch_size=batch_size,
            restaurant_id=restaurant_id,
        )
        total += deleted

        if deleted < batch_size:
            break

    return total


# --- Runner: python -m app.workers.outbox_worker ---


def create_worker_engine(database_url: str) -> Engine:
    # hide_parameters: database errors are logged without bound values.
    return create_engine(
        database_url,
        hide_parameters=True,
        connect_args={"application_name": APPLICATION_NAME},
    )


def main(argv: Sequence[str] | None = None, *, config=None) -> int:
    """Command-line entry point; `config` defaults to app.core.config."""

    parser = argparse.ArgumentParser(
        prog="python -m app.workers.outbox_worker",
        description="Deliver outbox events, or delete old delivered ones.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("run", "cleanup"),
        default="run",
        help="run: deliver until SIGINT/SIGTERM (default); "
        "cleanup: delete delivered events past the retention period",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=100,
        help="cleanup only: at most this many deletion batches (default 100)",
    )
    args = parser.parse_args(argv)

    if config is None:
        # Validates every OUTBOX_* setting before anything starts.
        from app.core.config import settings as config

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    engine = create_worker_engine(config.database_url)

    try:
        if args.command == "cleanup":
            deleted = cleanup_delivered_events(
                engine,
                older_than=timedelta(days=config.outbox_retention_days),
                batch_size=config.outbox_cleanup_batch_size,
                max_batches=args.max_batches,
                restaurant_id=config.outbox_worker_restaurant_id,
            )
            logger.info(
                "Deleted %d delivered outbox events older than %d days",
                deleted,
                config.outbox_retention_days,
            )
            return 0

        _run_until_signalled(
            OutboxWorker(
                engine,
                LoggingPublisher(),
                batch_size=config.outbox_worker_batch_size,
                restaurant_id=config.outbox_worker_restaurant_id,
                max_attempts=config.outbox_max_attempts,
                publish_timeout=config.outbox_publish_timeout_seconds,
            ),
            poll_interval=config.outbox_worker_poll_interval_seconds,
        )
        return 0
    finally:
        # Close every pooled connection before the process exits.
        engine.dispose()


def _run_until_signalled(worker: OutboxWorker, *, poll_interval: float) -> None:
    stop = threading.Event()

    def request_stop(signum, frame):
        logger.info(
            "Received %s; stopping after the current event",
            signal.Signals(signum).name,
        )
        stop.set()

    previous = {
        signum: signal.signal(signum, request_stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }

    logger.info(
        "Outbox worker started (batch size %d, max attempts %d, "
        "publish timeout %gs, restaurant %s)",
        worker.batch_size,
        worker.max_attempts,
        worker.publish_timeout,
        worker.restaurant_id or "all",
    )

    try:
        worker.run(stop, poll_interval=poll_interval)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    logger.info("Outbox worker stopped")


if __name__ == "__main__":
    sys.exit(main())
