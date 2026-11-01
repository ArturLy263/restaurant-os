"""Business days, the cash register, its shifts and its cash ledger.

A business day is the restaurant's trading period (opened before service,
closed by a manager when nothing is left open). It is independent of cash
shifts: a day has any number of shifts, one after another, and the drawer's
cash is handed over between them (cash_shift_handovers), also across days.

One shared register per restaurant in this version (one active register,
uq_cash_registers_one_active); a shift belongs to the register, never to a
cashier, and any authorized employee works in it: every row records who
did it. At most one OPEN shift per register (uq_cash_shifts_one_open).

Money in the drawer is a ledger, never an editable balance:

    expected cash = effective opening amount
                  + cash payments (payments.amount, method CASH)
                  + CASH_TIP + CASH_IN
                  - CASH_OUT - CASH_REFUND

The effective opening amount is the latest opening correction's
`new_amount`, else the shift's `opening_amount` (which keeps what was
first entered). Corrections are not movements: no cash moved.

A handover difference (cash received by the incoming cashier minus what
the outgoing cashier counted) belongs to the handover only: it changes
neither shift's expected cash.

Closed days, closed shifts, closings, handovers and the ledgers are
append-only; database triggers refuse to change or delete them
(migration d9e4b2a7c6f1). Every row carries its restaurant, held
consistent with its day and shift by composite foreign keys.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.states import BUSINESS_DAY, CASH_SHIFT, status_check
from app.db.database import Base

if TYPE_CHECKING:
    from app.db.models.user import User

# Movements an employee records by hand; the others are written by the
# payment (CASH_TIP) and refund (CASH_REFUND) they belong to.
MANUAL_MOVEMENT_TYPES = ("CASH_IN", "CASH_OUT")
MOVEMENT_TYPES = ("CASH_IN", "CASH_OUT", "CASH_REFUND", "CASH_TIP")
# The sign of each movement in expected cash.
MOVEMENT_SIGNS = {"CASH_IN": 1, "CASH_TIP": 1, "CASH_OUT": -1, "CASH_REFUND": -1}


def _shift_fk(name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["cash_shift_id", "restaurant_id"],
        ["cash_shifts.id", "cash_shifts.restaurant_id"],
        name=name,
    )


class BusinessDay(Base):
    __tablename__ = "business_days"
    __table_args__ = (
        CheckConstraint(
            status_check(BUSINESS_DAY),
            name="ck_business_days_status",
        ),
        CheckConstraint(
            "(status = 'CLOSED') = (closed_at IS NOT NULL "
            "AND closed_by_user_id IS NOT NULL AND closing_summary IS NOT NULL)",
            name="ck_business_days_closed_fields",
        ),
        UniqueConstraint(
            "restaurant_id",
            "business_date",
            name="uq_business_days_restaurant_date",
        ),
        UniqueConstraint(
            "id",
            "restaurant_id",
            name="uq_business_days_id_restaurant_id",
        ),
        Index(
            "uq_business_days_one_open",
            "restaurant_id",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
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

    # The restaurant's local date when the day was opened.
    business_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(30),
        default="OPEN",
        nullable=False,
    )

    opened_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
    )

    opened_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    closed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"),
    )

    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
    )

    # The day report frozen at closing (decimal strings).
    closing_summary: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
    )


class CashRegister(Base):
    __tablename__ = "cash_registers"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "restaurant_id",
            name="uq_cash_registers_id_restaurant_id",
        ),
        # One shared register per restaurant for now; dropping this index
        # is what allows several.
        Index(
            "uq_cash_registers_one_active",
            "restaurant_id",
            unique=True,
            postgresql_where=text("is_active"),
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

    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )


class CashShift(Base):
    __tablename__ = "cash_shifts"
    __table_args__ = (
        CheckConstraint(
            status_check(CASH_SHIFT),
            name="ck_cash_shifts_status",
        ),
        CheckConstraint(
            "opening_amount >= 0",
            name="ck_cash_shifts_opening_amount_nonnegative",
        ),
        CheckConstraint(
            "(status = 'CLOSED') = (closed_at IS NOT NULL)",
            name="ck_cash_shifts_closed_at",
        ),
        UniqueConstraint(
            "id",
            "restaurant_id",
            name="uq_cash_shifts_id_restaurant_id",
        ),
        # Targets of the composite keys that tie payments and refunds to
        # the shift's day, and handovers to the shift's register.
        UniqueConstraint(
            "id",
            "business_day_id",
            name="uq_cash_shifts_id_business_day_id",
        ),
        UniqueConstraint(
            "id",
            "cash_register_id",
            name="uq_cash_shifts_id_cash_register_id",
        ),
        ForeignKeyConstraint(
            ["cash_register_id", "restaurant_id"],
            ["cash_registers.id", "cash_registers.restaurant_id"],
            name="fk_cash_shifts_register_restaurant",
        ),
        ForeignKeyConstraint(
            ["business_day_id", "restaurant_id"],
            ["business_days.id", "business_days.restaurant_id"],
            name="fk_cash_shifts_business_day_restaurant",
        ),
        Index("ix_cash_shifts_business_day_id", "business_day_id"),
        Index(
            "uq_cash_shifts_one_open",
            "cash_register_id",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
        ),
        Index(
            "ix_cash_shifts_register_opened_at",
            "cash_register_id",
            "opened_at",
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

    cash_register_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    business_day_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    status: Mapped[str] = mapped_column(
        String(30),
        default="OPEN",
        nullable=False,
    )

    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
    )

    # The cash counted by the cashier who opened the shift (for a
    # handover, the amount received); corrections never change it.
    opening_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )

    opened_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
    )

    opened_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
    )

    business_day: Mapped["BusinessDay"] = relationship(
        foreign_keys=[business_day_id],
        primaryjoin="CashShift.business_day_id == BusinessDay.id",
    )

    register: Mapped["CashRegister"] = relationship(
        foreign_keys=[cash_register_id],
        primaryjoin="CashShift.cash_register_id == CashRegister.id",
    )

    opened_by: Mapped["User"] = relationship(
        foreign_keys=[opened_by_user_id],
    )

    corrections: Mapped[list["CashOpeningCorrection"]] = relationship(
        primaryjoin="CashShift.id == foreign(CashOpeningCorrection.cash_shift_id)",
        order_by="(CashOpeningCorrection.created_at, CashOpeningCorrection.id)",
        viewonly=True,
    )

    movements: Mapped[list["CashMovement"]] = relationship(
        primaryjoin="CashShift.id == foreign(CashMovement.cash_shift_id)",
        order_by="(CashMovement.created_at, CashMovement.id)",
        viewonly=True,
    )

    closing: Mapped["CashShiftClosing | None"] = relationship(
        primaryjoin="CashShift.id == foreign(CashShiftClosing.cash_shift_id)",
        uselist=False,
        viewonly=True,
    )

    @property
    def effective_opening_amount(self) -> Decimal:
        if self.corrections:
            return self.corrections[-1].new_amount
        return self.opening_amount


class CashShiftHandover(Base):
    """The drawer passing from a register's previous closed shift to the
    next one, possibly from an earlier business day.

    `difference` (received - handed over) is the handover's own
    discrepancy, explained by `reason`; it is not a cash movement and
    changes neither shift's expected cash. The record belongs to the
    incoming shift's business day.
    """

    __tablename__ = "cash_shift_handovers"
    __table_args__ = (
        CheckConstraint(
            "previous_expected_amount >= 0 AND handed_over_amount >= 0 "
            "AND received_amount >= 0",
            name="ck_cash_shift_handovers_amounts_nonnegative",
        ),
        CheckConstraint(
            "difference = received_amount - handed_over_amount",
            name="ck_cash_shift_handovers_difference",
        ),
        CheckConstraint(
            "difference = 0 OR reason IS NOT NULL",
            name="ck_cash_shift_handovers_reason",
        ),
        CheckConstraint(
            "from_shift_id <> to_shift_id",
            name="ck_cash_shift_handovers_distinct_shifts",
        ),
        # Both shifts on the record's register, the incoming one in the
        # record's day, the day in the record's restaurant.
        ForeignKeyConstraint(
            ["from_shift_id", "cash_register_id"],
            ["cash_shifts.id", "cash_shifts.cash_register_id"],
            name="fk_cash_shift_handovers_from_shift_register",
        ),
        ForeignKeyConstraint(
            ["to_shift_id", "cash_register_id"],
            ["cash_shifts.id", "cash_shifts.cash_register_id"],
            name="fk_cash_shift_handovers_to_shift_register",
        ),
        ForeignKeyConstraint(
            ["to_shift_id", "business_day_id"],
            ["cash_shifts.id", "cash_shifts.business_day_id"],
            name="fk_cash_shift_handovers_to_shift_business_day",
        ),
        ForeignKeyConstraint(
            ["business_day_id", "restaurant_id"],
            ["business_days.id", "business_days.restaurant_id"],
            name="fk_cash_shift_handovers_business_day_restaurant",
        ),
        ForeignKeyConstraint(
            ["cash_register_id", "restaurant_id"],
            ["cash_registers.id", "cash_registers.restaurant_id"],
            name="fk_cash_shift_handovers_register_restaurant",
        ),
        Index("ix_cash_shift_handovers_business_day_id", "business_day_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("restaurants.id"),
        nullable=False,
    )

    cash_register_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    # The incoming shift's business day.
    business_day_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    from_shift_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        unique=True,
    )

    to_shift_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        unique=True,
    )

    # From the outgoing shift's closing: what the ledger expected and
    # what its cashier counted (and so handed over).
    previous_expected_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )

    handed_over_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )

    # What the incoming cashier counted: the new shift's opening amount.
    received_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )

    difference: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )

    reason: Mapped[str | None] = mapped_column(
        String(500),
    )

    outgoing_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
    )

    incoming_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )


class CashOpeningCorrection(Base):
    """A manager's correction of a shift's opening amount (a typing
    mistake when opening), kept forever: no cash moved."""

    __tablename__ = "cash_opening_corrections"
    __table_args__ = (
        CheckConstraint(
            "previous_amount >= 0 AND new_amount >= 0 "
            "AND new_amount <> previous_amount",
            name="ck_cash_opening_corrections_amounts",
        ),
        _shift_fk("fk_cash_opening_corrections_shift_restaurant"),
        Index("ix_cash_opening_corrections_shift", "cash_shift_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("restaurants.id"),
        nullable=False,
    )

    cash_shift_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    # The effective opening amount before and after this correction.
    previous_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )

    new_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
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

    user: Mapped["User"] = relationship()


class CashMovement(Base):
    """Cash that physically entered or left the drawer, other than the
    cash taken for bills (read from payments)."""

    __tablename__ = "cash_movements"
    __table_args__ = (
        CheckConstraint(
            "type IN ('CASH_IN', 'CASH_OUT', 'CASH_REFUND', 'CASH_TIP')",
            name="ck_cash_movements_type",
        ),
        CheckConstraint(
            "amount > 0",
            name="ck_cash_movements_amount_positive",
        ),
        # Manual movements need a reason and belong to nothing else; the
        # automatic ones belong to their payment or refund.
        CheckConstraint(
            "(type IN ('CASH_IN', 'CASH_OUT') AND reason IS NOT NULL "
            "AND payment_id IS NULL AND refund_id IS NULL) "
            "OR (type = 'CASH_TIP' AND payment_id IS NOT NULL "
            "AND refund_id IS NULL) "
            "OR (type = 'CASH_REFUND' AND refund_id IS NOT NULL "
            "AND payment_id IS NULL)",
            name="ck_cash_movements_links",
        ),
        _shift_fk("fk_cash_movements_shift_restaurant"),
        ForeignKeyConstraint(
            ["payment_id", "restaurant_id"],
            ["payments.id", "payments.restaurant_id"],
            name="fk_cash_movements_payment_restaurant",
        ),
        ForeignKeyConstraint(
            ["refund_id", "restaurant_id"],
            ["refunds.id", "refunds.restaurant_id"],
            name="fk_cash_movements_refund_restaurant",
        ),
        # At most one tip movement per payment and one per refund.
        Index(
            "uq_cash_movements_payment_tip",
            "payment_id",
            unique=True,
            postgresql_where=text("payment_id IS NOT NULL"),
        ),
        Index(
            "uq_cash_movements_refund",
            "refund_id",
            unique=True,
            postgresql_where=text("refund_id IS NOT NULL"),
        ),
        Index("ix_cash_movements_shift", "cash_shift_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("restaurants.id"),
        nullable=False,
    )

    cash_shift_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    # Always positive; MOVEMENT_SIGNS gives the direction.
    amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )

    reason: Mapped[str | None] = mapped_column(
        String(500),
    )

    payment_id: Mapped[uuid.UUID | None] = mapped_column()

    refund_id: Mapped[uuid.UUID | None] = mapped_column()

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    user: Mapped["User"] = relationship()


class CashShiftClosing(Base):
    """The reconciliation recorded when a shift closes: what the ledger
    expected, what was counted, and the breakdown at that moment."""

    __tablename__ = "cash_shift_closings"
    __table_args__ = (
        CheckConstraint(
            "counted_amount >= 0",
            name="ck_cash_shift_closings_counted_nonnegative",
        ),
        CheckConstraint(
            "difference = counted_amount - expected_amount",
            name="ck_cash_shift_closings_difference",
        ),
        CheckConstraint(
            "difference = 0 OR explanation IS NOT NULL",
            name="ck_cash_shift_closings_explained",
        ),
        _shift_fk("fk_cash_shift_closings_shift_restaurant"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("restaurants.id"),
        nullable=False,
    )

    cash_shift_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        unique=True,
    )

    expected_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )

    counted_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )

    # counted - expected: negative is a shortage, positive an overage.
    difference: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )

    explanation: Mapped[str | None] = mapped_column(
        String(500),
    )

    # The shift report at closing time (decimal strings).
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
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

    user: Mapped["User"] = relationship()
