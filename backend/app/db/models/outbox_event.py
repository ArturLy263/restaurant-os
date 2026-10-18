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

    # NULL until delivered. Nothing delivers events yet.
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
