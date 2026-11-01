"""Totals of the cash ledger: one shift's drawer, one business day's report.

Everything is summed from the append-only rows (payments, refunds, cash
movements, opening corrections, closings, handovers) with Decimal; nothing
is stored as a running balance. The formulas:

    revenue      = payments.amount - refunds.amount          (never tips)
    tips         = payments.tip_amount - refunds.tip_amount
    expected cash = effective opening + cash payments.amount
                  + CASH_TIP + CASH_IN - CASH_OUT - CASH_REFUND

A cash tip enters the drawer as its CASH_TIP movement and a cash refund
(amount + tip) leaves it as its CASH_REFUND movement, so the drawer counts
each exactly once. Payments and refunds are attributed to the shift and
business day they were made in. Handover differences belong to handovers
and appear in no shift's expected cash.

Report dictionaries hold amounts as two-decimal strings, so they can be
stored as JSON (closings, closed days) and returned unchanged.
"""

import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.bill import Payment, Refund
from app.db.models.cash import (
    MOVEMENT_SIGNS,
    BusinessDay,
    CashMovement,
    CashOpeningCorrection,
    CashShift,
    CashShiftClosing,
    CashShiftHandover,
)
from app.db.models.user import User

ZERO = Decimal("0.00")
METHODS = ("CASH", "CARD")


def money(value: Decimal) -> str:
    return f"{value:.2f}"


@dataclass
class Flows:
    """Payments and refunds of a period, per method."""

    sales: dict[str, Decimal] = field(default_factory=lambda: dict.fromkeys(METHODS, ZERO))
    tips: dict[str, Decimal] = field(default_factory=lambda: dict.fromkeys(METHODS, ZERO))
    refunds: dict[str, Decimal] = field(default_factory=lambda: dict.fromkeys(METHODS, ZERO))
    refunded_tips: dict[str, Decimal] = field(default_factory=lambda: dict.fromkeys(METHODS, ZERO))
    payments_count: int = 0
    refunds_count: int = 0

    def as_report(self) -> dict:
        def by_method(values):
            return {**{m: money(values[m]) for m in METHODS}, "total": money(sum(values.values(), ZERO))}

        revenue = {m: self.sales[m] - self.refunds[m] for m in METHODS}
        net_tips = {m: self.tips[m] - self.refunded_tips[m] for m in METHODS}
        return {
            "sales": by_method(self.sales),
            "refunds": by_method(self.refunds),
            "revenue": by_method(revenue),
            "tips": by_method(self.tips),
            "refunded_tips": by_method(self.refunded_tips),
            "net_tips": by_method(net_tips),
            "payments_count": self.payments_count,
            "refunds_count": self.refunds_count,
        }


def flows(db: Session, *, cash_shift_id=None, business_day_id=None) -> Flows:
    """Payments and refunds made in a shift or a business day."""

    result = Flows()
    payment_filter = (
        Payment.cash_shift_id == cash_shift_id
        if cash_shift_id is not None
        else Payment.business_day_id == business_day_id
    )
    refund_filter = (
        Refund.cash_shift_id == cash_shift_id
        if cash_shift_id is not None
        else Refund.business_day_id == business_day_id
    )
    for method, amount, tip, count in db.execute(
        select(
            Payment.method,
            func.coalesce(func.sum(Payment.amount), ZERO),
            func.coalesce(func.sum(Payment.tip_amount), ZERO),
            func.count(),
        )
        .where(payment_filter, Payment.status == "COMPLETED")
        .group_by(Payment.method)
    ):
        result.sales[method] = amount
        result.tips[method] = tip
        result.payments_count += count
    for method, amount, tip, count in db.execute(
        select(
            Refund.method,
            func.coalesce(func.sum(Refund.amount), ZERO),
            func.coalesce(func.sum(Refund.tip_amount), ZERO),
            func.count(),
        )
        .where(refund_filter)
        .group_by(Refund.method)
    ):
        result.refunds[method] = amount
        result.refunded_tips[method] = tip
        result.refunds_count += count
    return result


@dataclass
class Drawer:
    opening_amount: Decimal
    effective_opening_amount: Decimal
    corrections_count: int
    cash_sales: Decimal
    movements: dict[str, Decimal]

    @property
    def expected(self) -> Decimal:
        total = self.effective_opening_amount + self.cash_sales
        for movement_type, amount in self.movements.items():
            total += MOVEMENT_SIGNS[movement_type] * amount
        return total

    def as_report(self) -> dict:
        return {
            "opening_amount": money(self.opening_amount),
            "effective_opening_amount": money(self.effective_opening_amount),
            "opening_corrections_count": self.corrections_count,
            "cash_sales": money(self.cash_sales),
            "cash_tips": money(self.movements["CASH_TIP"]),
            "cash_in": money(self.movements["CASH_IN"]),
            "cash_out": money(self.movements["CASH_OUT"]),
            "cash_refunds": money(self.movements["CASH_REFUND"]),
            "expected": money(self.expected),
        }


def drawer(db: Session, shift: CashShift) -> Drawer:
    """The shift's drawer: what should be in it now."""

    latest_correction = db.scalar(
        select(CashOpeningCorrection.new_amount)
        .where(CashOpeningCorrection.cash_shift_id == shift.id)
        .order_by(CashOpeningCorrection.created_at.desc(), CashOpeningCorrection.id.desc())
        .limit(1)
    )
    corrections_count = db.scalar(
        select(func.count()).where(CashOpeningCorrection.cash_shift_id == shift.id)
    )
    cash_sales = db.scalar(
        select(func.coalesce(func.sum(Payment.amount), ZERO)).where(
            Payment.cash_shift_id == shift.id,
            Payment.method == "CASH",
            Payment.status == "COMPLETED",
        )
    )
    movements = dict.fromkeys(MOVEMENT_SIGNS, ZERO)
    for movement_type, amount in db.execute(
        select(CashMovement.type, func.sum(CashMovement.amount))
        .where(CashMovement.cash_shift_id == shift.id)
        .group_by(CashMovement.type)
    ):
        movements[movement_type] = amount
    return Drawer(
        opening_amount=shift.opening_amount,
        effective_opening_amount=(
            latest_correction if latest_correction is not None else shift.opening_amount
        ),
        corrections_count=corrections_count,
        cash_sales=cash_sales,
        movements=movements,
    )


def shift_report(db: Session, shift: CashShift) -> dict:
    """The shift's money: payments, refunds, tips and its drawer; with
    its closing once closed."""

    report = {
        "cash_shift_id": str(shift.id),
        "business_day_id": str(shift.business_day_id),
        "currency": shift.currency,
        **flows(db, cash_shift_id=shift.id).as_report(),
        "drawer": drawer(db, shift).as_report(),
    }
    closing = db.scalar(select(CashShiftClosing).where(CashShiftClosing.cash_shift_id == shift.id))
    report["closing"] = (
        None
        if closing is None
        else {
            "expected_amount": money(closing.expected_amount),
            "counted_amount": money(closing.counted_amount),
            "difference": money(closing.difference),
            "explanation": closing.explanation,
        }
    )
    return report


def handover_report(handover: CashShiftHandover, names: dict[uuid.UUID, str]) -> dict:
    return {
        "id": str(handover.id),
        "from_shift_id": str(handover.from_shift_id),
        "to_shift_id": str(handover.to_shift_id),
        "business_day_id": str(handover.business_day_id),
        "previous_expected_amount": money(handover.previous_expected_amount),
        "handed_over_amount": money(handover.handed_over_amount),
        "received_amount": money(handover.received_amount),
        "difference": money(handover.difference),
        "reason": handover.reason,
        "outgoing_user": names.get(handover.outgoing_user_id),
        "incoming_user": names.get(handover.incoming_user_id),
        "created_at": handover.created_at.isoformat(),
    }


def user_names(db: Session, ids) -> dict[uuid.UUID, str]:
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    return dict(db.execute(select(User.id, User.name).where(User.id.in_(ids))).all())


def day_report(db: Session, day: BusinessDay, currency: str) -> dict:
    """The business day: its payments and refunds (all shifts), each
    shift's drawer and closing, and the handovers into its shifts."""

    shifts = db.scalars(
        select(CashShift)
        .where(CashShift.business_day_id == day.id)
        .order_by(CashShift.opened_at, CashShift.id)
    ).all()
    handovers = db.scalars(
        select(CashShiftHandover)
        .where(CashShiftHandover.business_day_id == day.id)
        .order_by(CashShiftHandover.created_at, CashShiftHandover.id)
    ).all()
    names = user_names(
        db,
        [s.opened_by_user_id for s in shifts]
        + [h.outgoing_user_id for h in handovers]
        + [h.incoming_user_id for h in handovers],
    )
    shift_reports = []
    closing_difference = ZERO
    for shift in shifts:
        report = shift_report(db, shift)
        report["status"] = shift.status
        report["opened_by"] = names.get(shift.opened_by_user_id)
        report["opened_at"] = shift.opened_at.isoformat()
        report["closed_at"] = shift.closed_at.isoformat() if shift.closed_at else None
        if report["closing"] is not None:
            closing_difference += Decimal(report["closing"]["difference"])
        shift_reports.append(report)
    return {
        "business_day_id": str(day.id),
        "business_date": day.business_date.isoformat(),
        "currency": currency,
        **flows(db, business_day_id=day.id).as_report(),
        "shifts": shift_reports,
        "handovers": [handover_report(h, names) for h in handovers],
        "closing_difference_total": money(closing_difference),
        "handover_difference_total": money(sum((h.difference for h in handovers), ZERO)),
    }
