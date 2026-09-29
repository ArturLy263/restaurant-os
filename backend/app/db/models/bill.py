import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base

if TYPE_CHECKING:
    from app.db.models.order import Order, OrderItem
    from app.db.models.restaurant import Restaurant
    from app.db.models.user import User


class Bill(Base):
    __tablename__ = "bills"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id"),
        nullable=False,
        unique=True,
    )

    subtotal: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )

    total: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
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

    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime,
    )

    voided_at: Mapped[datetime | None] = mapped_column(
        DateTime,
    )

    order: Mapped["Order"] = relationship(
        back_populates="bill",
    )

    payments: Mapped[list["Payment"]] = relationship(
        back_populates="bill",
        cascade="all, delete-orphan",
    )

    # No delete cascade: adjustments are financial history.
    adjustments: Mapped[list["BillAdjustment"]] = relationship(
        back_populates="bill",
        order_by="BillAdjustment.created_at",
    )

    # subtotal/total are the original document amounts and never change.
    # Everything below is derived from adjustments and payments.

    @property
    def adjustments_total(self) -> Decimal:
        return sum(
            (
                adjustment.amount
                for adjustment in self.adjustments
                if adjustment.status == "APPLIED"
            ),
            Decimal("0.00"),
        )

    @property
    def adjusted_total(self) -> Decimal:
        return self.total + self.adjustments_total

    @property
    def paid_total(self) -> Decimal:
        return sum(
            (
                payment.amount
                for payment in self.payments
                if payment.status == "COMPLETED"
            ),
            Decimal("0.00"),
        )

    @property
    def amount_due(self) -> Decimal:
        return self.adjusted_total - self.paid_total


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    bill_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bills.id"),
        nullable=False,
    )

    amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )

    method: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(30),
        default="COMPLETED",
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    bill: Mapped["Bill"] = relationship(
        back_populates="payments",
    )


class BillAdjustment(Base):
    """
    A signed change to the amount owed on a bill.

    Negative amounts reduce the amount due (e.g. ITEM_CANCELLATION),
    positive amounts increase it. Only APPLIED adjustments count.
    """

    __tablename__ = "bill_adjustments"

    __table_args__ = (
        CheckConstraint(
            "amount <> 0",
            name="ck_bill_adjustments_amount_nonzero",
        ),
        Index(
            "ix_bill_adjustments_bill_id",
            "bill_id",
        ),
        Index(
            "ix_bill_adjustments_restaurant_id_created_at",
            "restaurant_id",
            "created_at",
        ),
        Index(
            "uq_bill_adjustments_applied_order_item_type",
            "order_item_id",
            "type",
            unique=True,
            postgresql_where=text("status = 'APPLIED'"),
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

    bill_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bills.id"),
        nullable=False,
    )

    order_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("order_items.id"),
    )

    type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(30),
        default="APPLIED",
        nullable=False,
    )

    reason: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    bill: Mapped["Bill"] = relationship(
        back_populates="adjustments",
    )

    order_item: Mapped["OrderItem | None"] = relationship()

    user: Mapped["User | None"] = relationship()

    restaurant: Mapped["Restaurant"] = relationship()