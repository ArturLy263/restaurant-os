import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class User(Base):
    __tablename__ = "users"
    # Server-side defaults (token_version) are loaded when first read, not
    # returned by every INSERT.
    __mapper_args__ = {"eager_defaults": False}

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("restaurants.id"),
        nullable=False,
    )

    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("roles.id"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
    )

    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
    )

    # Carried in every access token ("ver"); a token whose version differs
    # is refused. The database bumps it whenever password_hash changes
    # (trigger users_bump_token_version), so a new password ends every
    # session issued with the old one.
    # Database default only (no Python default), so INSERTs leave it to
    # PostgreSQL and the ORM loads it when first read.
    token_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime,
    )