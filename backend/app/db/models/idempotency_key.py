import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class IdempotencyKey(Base):
    """The stored result of a request made with an Idempotency-Key.

    A row is inserted in the same transaction as the business change it
    belongs to and committed only together with it and its response, so a
    committed row always holds the result of a committed operation. See
    app.api.idempotency.
    """

    __tablename__ = "idempotency_keys"

    __table_args__ = (
        # Keys are scoped to the user (and so to the restaurant).
        UniqueConstraint(
            "restaurant_id",
            "user_id",
            "idempotency_key",
            name="uq_idempotency_keys_scope_key",
        ),
        Index(
            "ix_idempotency_keys_expires_at",
            "expires_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("restaurants.id"),
        nullable=False,
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
    )

    idempotency_key: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    # e.g. "POST /api/v1/bills/{bill_id}/payments"
    operation: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    # SHA-256 of the operation, its path parameters and its body.
    request_fingerprint: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    response_status_code: Mapped[int | None] = mapped_column(
        Integer,
    )

    # JSON (not JSONB) keeps the body's key order for an exact replay.
    response_body: Mapped[dict | list | None] = mapped_column(
        JSON,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
