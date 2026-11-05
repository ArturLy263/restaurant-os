import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.states import BILL, BILL_ADJUSTMENT, PAYMENT, status_check
from app.db.database import Base

# How the payment's money was confirmed. MANUAL_CARD_TERMINAL means the
# cashier ran the card on an external terminal and confirmed the result
# by hand: RestOS has not verified it with the bank.
PAYMENT_METHODS = ("CASH", "CARD")
CONFIRMATION_METHODS = ("CASH_RECEIVED", "MANUAL_CARD_TERMINAL")

if TYPE_CHECKING:
    from app.db.models.cash import CashShift
    from app.db.models.order import Order, OrderItem
    from app.db.models.restaurant import Restaurant
    from app.db.models.user import User


class Bill(Base):
    __tablename__ = "bills"

    __table_args__ = (
        CheckConstraint(
            status_check(BILL),
            name="ck_bills_status",
        ),
    )

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

    # No delete cascade: payments are financial history (append-only, see
    # migration f2a8c6e4b1d9); a bill with payments is never deleted.
    payments: Mapped[list["Payment"]] = relationship(
        back_populates="bill",
        passive_deletes="all",
    )

    # No delete cascade: adjustments are financial history.
    adjustments: Mapped[list["BillAdjustment"]] = relationship(
        back_populates="bill",
        order_by="BillAdjustment.created_at",
    )

    # subtotal/total are the original document amounts and never change.
    # Everything below is derived from adjustments and payments. Tips and
    # refunds never change what is owed: a refund returns money for a
    # settled bill, it does not reopen it.

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
    """Money received for a bill.

    `amount` pays the bill; `tip_amount` is the guest's tip on top of it,
    never part of what is owed or of revenue. Rows written before the
    cashier module (Stage 3) have no user, shift or confirmation; every
    later row has them, and its business day (ck_payments_recorded_in_shift,
    NOT VALID).

    Payments are append-only (never updated or deleted, migration
    f2a8c6e4b1d9) and can be written only into an OPEN shift of an OPEN
    business day: database triggers refuse anything else.
    """

    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint(
            status_check(PAYMENT),
            name="ck_payments_status",
        ),
        CheckConstraint(
            "method IN ('CASH', 'CARD')",
            name="ck_payments_method",
        ),
        CheckConstraint(
            "amount > 0",
            name="ck_payments_amount_positive",
        ),
        CheckConstraint(
            "tip_amount >= 0",
            name="ck_payments_tip_amount_nonnegative",
        ),
        CheckConstraint(
            "external_reference IS NULL OR method = 'CARD'",
            name="ck_payments_external_reference_card_only",
        ),
        UniqueConstraint(
            "id",
            "restaurant_id",
            name="uq_payments_id_restaurant_id",
        ),
        ForeignKeyConstraint(
            ["cash_shift_id", "restaurant_id"],
            ["cash_shifts.id", "cash_shifts.restaurant_id"],
            name="fk_payments_cash_shift_restaurant",
        ),
        # The payment's business day is its shift's.
        ForeignKeyConstraint(
            ["cash_shift_id", "business_day_id"],
            ["cash_shifts.id", "cash_shifts.business_day_id"],
            name="fk_payments_cash_shift_business_day",
        ),
        Index("ix_payments_bill_id", "bill_id"),
        Index("ix_payments_cash_shift_id", "cash_shift_id"),
        Index("ix_payments_business_day_id", "business_day_id"),
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

    amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )

    tip_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("0.00"),
        server_default=text("0"),
    )

    currency: Mapped[str] = mapped_column(
        String(3),
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

    # The employee who took the payment.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"),
    )

    cash_shift_id: Mapped[uuid.UUID | None] = mapped_column()

    business_day_id: Mapped[uuid.UUID | None] = mapped_column()

    confirmation_method: Mapped[str | None] = mapped_column(
        String(30),
    )

    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
    )

    # A card terminal's receipt or transaction number, as typed by the
    # cashier; never card data.
    external_reference: Mapped[str | None] = mapped_column(
        String(100),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    bill: Mapped["Bill"] = relationship(
        back_populates="payments",
    )

    refunds: Mapped[list["Refund"]] = relationship(
        back_populates="payment",
        order_by="(Refund.created_at, Refund.id)",
    )

    allocations: Mapped[list["PaymentAllocation"]] = relationship(
        back_populates="payment",
        order_by="(PaymentAllocation.created_at, PaymentAllocation.id)",
    )

    user: Mapped["User | None"] = relationship()

    @property
    def refunded_amount(self) -> Decimal:
        return sum((refund.amount for refund in self.refunds), Decimal("0.00"))

    @property
    def refunded_tip_amount(self) -> Decimal:
        return sum((refund.tip_amount for refund in self.refunds), Decimal("0.00"))


class PaymentAllocation(Base):
    """An order item paid by a payment (splitting a bill by items).

    An item is paid at most once (uq_payment_allocations_order_item_id),
    so concurrent split payments can never charge the same item twice.
    """

    __tablename__ = "payment_allocations"
    __table_args__ = (
        CheckConstraint(
            "amount > 0",
            name="ck_payment_allocations_amount_positive",
        ),
        ForeignKeyConstraint(
            ["payment_id", "restaurant_id"],
            ["payments.id", "payments.restaurant_id"],
            name="fk_payment_allocations_payment_restaurant",
        ),
        Index("ix_payment_allocations_payment_id", "payment_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("restaurants.id"),
        nullable=False,
    )

    payment_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    order_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("order_items.id"),
        nullable=False,
        unique=True,
    )

    amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    payment: Mapped["Payment"] = relationship(
        back_populates="allocations",
    )


class Refund(Base):
    """Money returned to a guest for a payment; the payment is kept.

    `amount` is returned bill money (it reduces revenue), `tip_amount`
    returned tip. Together they never exceed what the payment took
    (checked under the order lock). The refund goes back the way the
    payment came: a cash refund leaves the drawer of the shift it was
    made in (a CASH_REFUND movement); a card refund is run on the external
    terminal and confirmed by hand.
    """

    __tablename__ = "refunds"
    __table_args__ = (
        CheckConstraint(
            "amount >= 0 AND tip_amount >= 0 AND amount + tip_amount > 0",
            name="ck_refunds_amounts",
        ),
        CheckConstraint(
            "method IN ('CASH', 'CARD')",
            name="ck_refunds_method",
        ),
        CheckConstraint(
            "external_reference IS NULL OR method = 'CARD'",
            name="ck_refunds_external_reference_card_only",
        ),
        CheckConstraint(
            "confirmation_method IN ('CASH_RECEIVED', 'MANUAL_CARD_TERMINAL') "
            "AND (confirmation_method = 'CASH_RECEIVED') = (method = 'CASH')",
            name="ck_refunds_confirmation_method",
        ),
        UniqueConstraint(
            "id",
            "restaurant_id",
            name="uq_refunds_id_restaurant_id",
        ),
        ForeignKeyConstraint(
            ["payment_id", "restaurant_id"],
            ["payments.id", "payments.restaurant_id"],
            name="fk_refunds_payment_restaurant",
        ),
        ForeignKeyConstraint(
            ["cash_shift_id", "restaurant_id"],
            ["cash_shifts.id", "cash_shifts.restaurant_id"],
            name="fk_refunds_cash_shift_restaurant",
        ),
        ForeignKeyConstraint(
            ["cash_shift_id", "business_day_id"],
            ["cash_shifts.id", "cash_shifts.business_day_id"],
            name="fk_refunds_cash_shift_business_day",
        ),
        Index("ix_refunds_payment_id", "payment_id"),
        Index("ix_refunds_cash_shift_id", "cash_shift_id"),
        Index("ix_refunds_business_day_id", "business_day_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("restaurants.id"),
        nullable=False,
    )

    payment_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    # The shift and day in which the refund was made (not the payment's).
    cash_shift_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    business_day_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    # Refunded bill money (reduces revenue) and refunded tip (reduces
    # tips only), kept apart.
    amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )

    tip_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )

    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
    )

    method: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    confirmation_method: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    external_reference: Mapped[str | None] = mapped_column(
        String(100),
    )

    reason: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    payment: Mapped["Payment"] = relationship(
        back_populates="refunds",
    )

    user: Mapped["User"] = relationship()


class BillAdjustment(Base):
    """
    A signed change to the amount owed on a bill.

    Negative amounts reduce the amount due (e.g. ITEM_CANCELLATION),
    positive amounts increase it. Only APPLIED adjustments count.
    """

    __tablename__ = "bill_adjustments"

    __table_args__ = (
        CheckConstraint(
            status_check(BILL_ADJUSTMENT),
            name="ck_bill_adjustments_status",
        ),
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