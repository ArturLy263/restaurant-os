import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class OutboxEvent(Base):
    """An integration event waiting to be delivered (transactional outbox).

    Written by app.services.outbox_service in the same transaction as the
    business change it describes, so it exists if and only if that change
    was committed. This is delivery data, not history: the audit trail
    (who did what) is OperationalEvent. Event types and payloads are listed
    in app.schemas.outbox.
    """

    __tablename__ = "outbox_events"

    __table_args__ = (
        CheckConstraint(
            "schema_version >= 1",
            name="ck_outbox_events_schema_version_positive",
        ),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_outbox_events_payload_is_object",
        ),
        CheckConstraint(
            "attempts >= 0",
            name="ck_outbox_events_attempts_non_negative",
        ),
        # Delivered and dead-lettered are final and exclusive.
        CheckConstraint(
            "published_at IS NULL OR dead_lettered_at IS NULL",
            name="ck_outbox_events_delivered_or_dead_lettered",
        ),
        # What a delivery worker scans: undelivered events, oldest first.
        Index(
            "ix_outbox_events_unpublished",
            "created_at",
            postgresql_where=text("published_at IS NULL"),
        ),
        # An aggregate's events in order (consumers' partition key).
        Index(
            "ix_outbox_events_aggregate",
            "aggregate_type",
            "aggregate_id",
            "created_at",
        ),
        # Retention cleanup: delivered events, oldest delivery first.
        Index(
            "ix_outbox_events_published",
            "published_at",
            postgresql_where=text("published_at IS NOT NULL"),
        ),
        # Operators: the dead-letter list.
        Index(
            "ix_outbox_events_dead_lettered",
            "dead_lettered_at",
            postgresql_where=text("dead_lettered_at IS NOT NULL"),
        ),
    )

    # Stable event identifier; consumers deduplicate on it.
    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("restaurants.id"),
        nullable=False,
    )

    # e.g. "order.sent"; see app.schemas.outbox.
    event_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    schema_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    aggregate_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    aggregate_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
    )

    payload: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
    )

    # clock_timestamp(), not now(): taken at insert time, under the
    # aggregate's row lock, so one aggregate's events are in commit order.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.clock_timestamp(),
        nullable=False,
    )

    # NULL until delivered; set only after the publisher has succeeded
    # (app.workers.outbox_worker).
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )

    # Delivery bookkeeping, written only by the worker. The event itself
    # (type, version, scope, aggregate, payload) never changes.
    attempts: Mapped[int] = mapped_column(
        Integer,
        server_default=text("0"),
        nullable=False,
    )

    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )

    # Not retried before this time (backoff after a failed attempt).
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.clock_timestamp(),
        nullable=False,
    )

    # The last failed attempt's exception type and (truncated) message.
    last_error: Mapped[str | None] = mapped_column(
        String(1000),
    )

    # Set when the last allowed attempt failed: never retried
    # automatically. Still undelivered (published_at NULL), so it keeps
    # holding back its aggregate's later events.
    dead_lettered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
