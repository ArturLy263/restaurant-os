import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base

if TYPE_CHECKING:
    from app.db.models.order import OrderItem


class Preparation(Base):
    __tablename__ = "preparations"

    # An order item is sent at most once, so it has at most one preparation.
    __table_args__ = (
        UniqueConstraint(
            "order_item_id",
            name="uq_preparations_order_item_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    order_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("order_items.id"),
        nullable=False,
    )

    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id"),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(30),
        default="PENDING",
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime,
    )

    ready_at: Mapped[datetime | None] = mapped_column(
        DateTime,
    )

    served_at: Mapped[datetime | None] = mapped_column(
        DateTime,
    )

    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime,
    )

    order_item: Mapped["OrderItem"] = relationship(
        back_populates="preparations",
    )