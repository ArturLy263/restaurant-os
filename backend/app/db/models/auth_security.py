"""Authentication state kept in PostgreSQL so every API process shares it:
revoked access tokens (by token id, never the token itself) and login
throttling counters (by a hash of the throttled key, never the raw email
or address)."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class RevokedToken(Base):
    """An access token ended before its expiry (sign-out). Kept until the
    token would have expired anyway; rows past `expires_at` are purged."""

    __tablename__ = "revoked_tokens"
    __table_args__ = (Index("ix_revoked_tokens_expires_at", "expires_at"),)

    # The token's "jti" claim.
    jti: Mapped[uuid.UUID] = mapped_column(primary_key=True)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
    )

    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )


class LoginThrottle(Base):
    """Failed sign-ins counted for one key (an email or a client address)."""

    __tablename__ = "login_throttles"
    __table_args__ = (
        CheckConstraint("failures >= 0", name="ck_login_throttles_failures_nonnegative"),
        Index("ix_login_throttles_updated_at", "updated_at"),
    )

    # sha256 hex of "<kind>:<value>"; the raw value is never stored.
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)

    failures: Mapped[int] = mapped_column(Integer, nullable=False)

    window_started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    blocked_until: Mapped[datetime | None] = mapped_column(DateTime)

    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
