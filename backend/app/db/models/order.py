import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.states import ORDER, ORDER_ITEM, status_check
from app.db.database import Base
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.db.models.preparation import Preparation
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.db.models.bill import Bill

from app.db.models.modifier import OrderItemModifier

class Order(Base):
    __tablename__ = "orders"

    __table_args__ = (
        CheckConstraint(
            status_check(ORDER),
            name="ck_orders_status",
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

    table_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tables.id"),
        nullable=False,
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
    )

    guest_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(30),
        default="OPEN",
        nullable=False,
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

    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
    )

    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime,
    )

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
    )

    bill: Mapped["Bill | None"] = relationship(
    back_populates="order",
    uselist=False,
    cascade="all, delete-orphan",
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    __table_args__ = (
        CheckConstraint(
            status_check(ORDER_ITEM),
            name="ck_order_items_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id"),
        nullable=False,
    )

    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id"),
        nullable=False,
    )

    quantity: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    unit_price: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )

    # The product's name when the item was added, like unit_price for its
    # price. NULL for items added before migration a3d9e7c1f5b2.
    product_name_snapshot: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    # The waiter's note for the kitchen or bar; NULL when there is none.
    notes: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(30),
        default="ADDED",
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime,
    )

    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime,
    )

    preparations: Mapped[list["Preparation"]] = relationship(
        back_populates="order_item",
        cascade="all, delete-orphan",
    )

    modifiers: Mapped[list["OrderItemModifier"]] = relationship(
        back_populates="order_item",
        cascade="all, delete-orphan",
    )

    order: Mapped["Order"] = relationship(
        back_populates="items",
    )

    total_price: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )
    @property
    def preparation(self) -> "Preparation | None":
        """The item's preparation once sent (there is at most one)."""

        return self.preparations[0] if self.preparations else None
