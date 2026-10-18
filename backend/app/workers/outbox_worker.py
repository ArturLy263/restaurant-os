"""Outbox delivery worker: hands committed outbox events to a publisher.

Nothing here runs on import. A caller creates a worker and either calls
`process_batch()` (one bounded pass, e.g. from a test or a scheduler) or
`run(stop)` in a thread or process it owns, until it sets `stop`.

Each event is delivered in its own short transaction:

1. Claim one due, undelivered event with `SELECT ... FOR UPDATE SKIP
   LOCKED`. Concurrent workers skip rows another worker has claimed, so
   no event is handed to two publishers at the same time.
2. Call the publisher with the event, while the row stays locked.
3. Only if the publisher returned: set `published_at` and commit. If it
   raised, record the attempt (count, time, error) and a backoff in
   `next_attempt_at`, commit, and leave the event undelivered.

Delivery is at least once, not exactly once. If the worker dies or loses
the database after the publisher succeeded but before the commit, the
claim is rolled back and the event is delivered again later. Consumers
deduplicate on the event `id`.

Order: an event is not claimed while an older event of the same aggregate
(the same order) is undelivered, whether that one is claimed by another
worker, waiting for a retry, or failing. One order's events are therefore
delivered in the order they were written; a failing event holds back the
later events of its order (and only of its order) until it succeeds.

Failed events are retried indefinitely with capped exponential backoff;
nothing gives up on an event yet. The worker writes only the delivery
columns, never the event's type, version, scope, aggregate or payload,
and logs event metadata only, never payloads.
"""

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import Connection, Engine, exists, func, select, tuple_, update
from sqlalchemy.orm import aliased

from app.db.models.outbox_event import OutboxEvent


logger = logging.getLogger("app.outbox")

MAX_ERROR_LENGTH = 1000

DEFAULT_BATCH_SIZE = 100


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

    Must be safe to call again with the same message (at-least-once).
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


def default_retry_delay(attempts: int) -> timedelta:
    """5s after the first failure, doubling, at most 5 minutes."""

    return timedelta(seconds=min(5 * 2 ** (attempts - 1), 300))


@dataclass
class BatchResult:
    delivered: list[uuid.UUID] = field(default_factory=list)
    failed: list[uuid.UUID] = field(default_factory=list)

    @property
    def processed(self) -> int:
        return len(self.delivered) + len(self.failed)


class OutboxWorker:

    def __init__(
        self,
        engine: Engine,
        publisher: Publisher,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        restaurant_id: uuid.UUID | None = None,
        retry_delay: Callable[[int], timedelta] = default_retry_delay,
    ):
        """`restaurant_id` limits the worker to one restaurant's events;
        by default it delivers every restaurant's events, each with its
        own `restaurant_id`. `retry_delay(attempts)` is the wait after the
        `attempts`-th failed attempt."""

        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")

        self.engine = engine
        self.publisher = publisher
        self.batch_size = batch_size
        self.restaurant_id = restaurant_id
        self.retry_delay = retry_delay

    def process_batch(self) -> BatchResult:
        """Attempt up to `batch_size` due events, each at most once."""

        result = BatchResult()
        attempted: list[uuid.UUID] = []

        while len(attempted) < self.batch_size:
            outcome = self._process_one(exclude=attempted)

            if outcome is None:
                break

            event_id, delivered = outcome
            attempted.append(event_id)
            (result.delivered if delivered else result.failed).append(event_id)

        return result

    def run(self, stop: threading.Event, *, poll_interval: float = 1.0) -> None:
        """Process batches until `stop` is set.

        Waits `poll_interval` seconds when there was nothing (more) to do
        or the database failed; a full batch is followed by the next one
        at once.
        """

        while not stop.is_set():
            try:
                result = self.process_batch()
            except Exception:
                logger.exception("Outbox batch failed; retrying after a pause")
                result = None

            if result is None or result.processed < self.batch_size:
                stop.wait(poll_interval)

    # --- One event, one transaction ---

    def _process_one(
        self,
        exclude: list[uuid.UUID],
    ) -> tuple[uuid.UUID, bool] | None:

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
                self.publisher.publish(message)
            except Exception as exc:
                self._record_failure(connection, message, exc)
                return message.id, False

            self._record_delivery(connection, message)
            return message.id, True

    def _claim(self, exclude: list[uuid.UUID]):
        event = OutboxEvent
        older = aliased(OutboxEvent)

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
    ) -> None:
        delay = self.retry_delay(message.attempt)

        connection.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == message.id)
            .values(
                attempts=OutboxEvent.attempts + 1,
                # The same time for both, so the wait is exactly `delay`.
                last_attempt_at=func.statement_timestamp(),
                last_error=describe_error(error),
                next_attempt_at=func.statement_timestamp() + delay,
            )
        )

        # The exception type only: its message may say more than a log should.
        logger.warning(
            "Outbox event %s (%s) attempt %d failed with %s; retry in %ss",
            message.id,
            message.event_type,
            message.attempt,
            type(error).__name__,
            delay.total_seconds(),
        )


def describe_error(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"[:MAX_ERROR_LENGTH]
