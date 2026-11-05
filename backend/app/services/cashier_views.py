"""Read models of the cashier screens: the order list, a bill with its
lines and payments, a cash shift with its drawer.

Read only; amounts are Decimal (serialized as Money strings by the
response models). Every query is scoped by the restaurant.

Blind count: a cash shift's figures depend on who looks (`CashViewer`).
Without cash_drawer:view_expected (cashiers) the expected cash, the
drawer breakdown it is summed from, closing differences and handovers'
previous expected amounts are null; a closing's count is null too while
its handover is pending, except for the employee who counted it, so the
incoming cashier can't learn what to "find" before counting. Managers
see everything.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db.models.bill import Bill, Payment, PaymentAllocation
from app.db.models.cash import (
    BusinessDay,
    CashShift,
    CashShiftClosing,
    CashShiftHandover,
)
from app.db.models.order import Order, OrderItem
from app.db.models.restaurant import Restaurant
from app.db.models.table import Table
from app.db.models.user import User
from app.repositories.cash_repository import CashRepository
from app.services import cash_ledger
from app.services.bill_service import BillService
from app.services.order_service import ACTIVE_ORDER_STATUSES

ZERO = Decimal("0.00")
SETTLED_LIMIT = 50


@dataclass(frozen=True)
class CashViewer:
    """Who looks at cash figures: the user and whether they may see
    expected cash (Permission.CASH_DRAWER_VIEW_EXPECTED)."""

    user_id: uuid.UUID
    sees_expected: bool


def _user(names: dict, user_id) -> dict | None:
    if user_id is None:
        return None
    return {"id": user_id, "name": names.get(user_id)}


def cashier_orders(db: Session, restaurant_id: uuid.UUID, view: str) -> list[dict]:
    """`payable`: active orders (OPEN or SENT), oldest first, with their
    bill if issued. `settled`: the latest paid bills, newest first, for
    refunds."""

    items = (
        select(
            OrderItem.order_id,
            func.count().label("count"),
            func.coalesce(func.sum(OrderItem.total_price), ZERO).label("total"),
        )
        .where(OrderItem.status != "CANCELLED")
        .group_by(OrderItem.order_id)
        .subquery()
    )
    query = (
        select(Order, Table, User.name, Bill, items.c.count, items.c.total)
        .join(Table, Table.id == Order.table_id)
        .outerjoin(User, User.id == Order.user_id)
        .outerjoin(Bill, Bill.order_id == Order.id)
        .outerjoin(items, items.c.order_id == Order.id)
        .where(Order.restaurant_id == restaurant_id)
    )
    if view == "payable":
        query = query.where(Order.status.in_(ACTIVE_ORDER_STATUSES)).order_by(
            Order.created_at, Order.id
        )
    else:
        query = (
            query.where(Bill.status == "PAID")
            .order_by(Bill.paid_at.desc().nulls_last(), Bill.id.desc())
            .limit(SETTLED_LIMIT)
        )
    result = []
    for order, table, waiter, bill, count, total in db.execute(query):
        total = total if total is not None else ZERO
        result.append({
            "order_id": order.id,
            "order_status": order.status,
            "table_number": table.number,
            "table_name": table.name,
            "guest_count": order.guest_count,
            "waiter_name": waiter,
            "created_at": order.created_at,
            "items_count": count or 0,
            "items_total": total,
            "bill_id": bill.id if bill else None,
            "bill_status": bill.status if bill else None,
            "paid_total": bill.paid_total if bill else ZERO,
            "amount_due": bill.amount_due if bill else total,
        })
    return result


def bill_detail(db: Session, restaurant_id: uuid.UUID, bill_id: uuid.UUID) -> dict:
    bill = BillService.get_bill(db, restaurant_id, bill_id)
    order = db.get(Order, bill.order_id)
    table = db.get(Table, order.table_id)
    restaurant = db.get(Restaurant, restaurant_id)
    payments = db.scalars(
        select(Payment)
        .where(Payment.bill_id == bill.id)
        .options(selectinload(Payment.refunds))
        .order_by(Payment.created_at, Payment.id)
        .execution_options(populate_existing=True)
    ).all()
    allocations = dict(
        db.execute(
            select(PaymentAllocation.order_item_id, PaymentAllocation.payment_id).where(
                PaymentAllocation.payment_id.in_([p.id for p in payments])
            )
        ).all()
    ) if payments else {}
    names = cash_ledger.user_names(
        db,
        [order.user_id]
        + [p.user_id for p in payments]
        + [r.user_id for p in payments for r in p.refunds],
    )
    items = db.scalars(
        select(OrderItem)
        .where(OrderItem.order_id == order.id)
        .options(selectinload(OrderItem.modifiers))
        .order_by(OrderItem.created_at, OrderItem.id)
    ).all()
    return {
        **{field: getattr(bill, field) for field in (
            "id", "order_id", "subtotal", "total", "status", "created_at", "paid_at",
            "voided_at", "adjustments_total", "adjusted_total", "paid_total", "amount_due",
            "adjustments",
        )},
        "currency": restaurant.currency,
        "order": {
            "id": order.id,
            "status": order.status,
            "table_number": table.number,
            "table_name": table.name,
            "guest_count": order.guest_count,
            "waiter_name": names.get(order.user_id),
        },
        "lines": [
            {
                "order_item_id": item.id,
                "product_name": item.product_name_snapshot,
                "quantity": item.quantity,
                "unit_price": item.unit_price,
                "total_price": item.total_price,
                "status": item.status,
                "notes": item.notes,
                "modifiers": [
                    {"name": m.name_snapshot, "price": m.price_snapshot} for m in item.modifiers
                ],
                "paid_by_payment_id": allocations.get(item.id),
            }
            for item in items
        ],
        "payments": [
            {
                "id": p.id,
                "amount": p.amount,
                "tip_amount": p.tip_amount,
                "method": p.method,
                "confirmation_method": p.confirmation_method,
                "external_reference": p.external_reference,
                "user_name": names.get(p.user_id),
                "created_at": p.created_at,
                "refunded_amount": p.refunded_amount,
                "refunded_tip_amount": p.refunded_tip_amount,
                "refunds": [
                    {
                        "id": r.id,
                        "amount": r.amount,
                        "tip_amount": r.tip_amount,
                        "method": r.method,
                        "reason": r.reason,
                        "user_name": names.get(r.user_id),
                        "created_at": r.created_at,
                    }
                    for r in p.refunds
                ],
            }
            for p in payments
        ],
        "tips_total": sum((p.tip_amount for p in payments), ZERO),
        "refunded_total": sum((p.refunded_amount for p in payments), ZERO),
        "refunded_tips_total": sum((p.refunded_tip_amount for p in payments), ZERO),
    }


def _handover(handover: CashShiftHandover, names: dict, viewer: CashViewer) -> dict:
    # A handover exists only once the incoming cashier has counted, so its
    # counts are no longer secret; the previous expected cash still is.
    return {
        **{field: getattr(handover, field) for field in (
            "id", "cash_register_id", "business_day_id", "from_shift_id", "to_shift_id",
            "handed_over_amount", "received_amount", "difference", "reason", "created_at",
        )},
        "previous_expected_amount": (
            handover.previous_expected_amount if viewer.sees_expected else None
        ),
        "outgoing_user": _user(names, handover.outgoing_user_id),
        "incoming_user": _user(names, handover.incoming_user_id),
        "self_confirmed": handover.outgoing_user_id == handover.incoming_user_id,
    }


def handovers(db: Session, rows: list[CashShiftHandover], viewer: CashViewer) -> list[dict]:
    names = cash_ledger.user_names(
        db, [h.outgoing_user_id for h in rows] + [h.incoming_user_id for h in rows]
    )
    return [_handover(h, names, viewer) for h in rows]


def _handover_pending(db: Session, shift_id: uuid.UUID) -> bool:
    """Whether the closed shift's cash still waits for the next count."""

    return db.scalar(
        select(CashShiftHandover.id).where(CashShiftHandover.from_shift_id == shift_id)
    ) is None


def _count_visible(db: Session, closing: CashShiftClosing, viewer: CashViewer) -> bool:
    return (
        viewer.sees_expected
        or closing.user_id == viewer.user_id
        or not _handover_pending(db, closing.cash_shift_id)
    )


def shift_detail(db: Session, shift: CashShift, viewer: CashViewer) -> dict:
    drawer = cash_ledger.drawer(db, shift)
    closing = db.scalar(select(CashShiftClosing).where(CashShiftClosing.cash_shift_id == shift.id))
    handover = db.scalar(select(CashShiftHandover).where(CashShiftHandover.to_shift_id == shift.id))
    expected = viewer.sees_expected
    names = cash_ledger.user_names(
        db,
        [shift.opened_by_user_id]
        + [c.user_id for c in shift.corrections]
        + [m.user_id for m in shift.movements]
        + ([closing.user_id] if closing else [])
        + ([handover.outgoing_user_id, handover.incoming_user_id] if handover else []),
    )
    return {
        "id": shift.id,
        "cash_register_id": shift.cash_register_id,
        "business_day_id": shift.business_day_id,
        "status": shift.status,
        "currency": shift.currency,
        "opening_amount": shift.opening_amount,
        "effective_opening_amount": drawer.effective_opening_amount,
        "opened_by": _user(names, shift.opened_by_user_id),
        "opened_at": shift.opened_at,
        "closed_at": shift.closed_at,
        "expected_cash": (
            (closing.expected_amount if closing else drawer.expected) if expected else None
        ),
        "drawer": (
            (closing.summary["drawer"] if closing else drawer.as_report()) if expected else None
        ),
        "expected_visible": expected,
        "corrections": [
            {
                "id": c.id,
                "cash_shift_id": c.cash_shift_id,
                "previous_amount": c.previous_amount,
                "new_amount": c.new_amount,
                "reason": c.reason,
                "user": _user(names, c.user_id),
                "created_at": c.created_at,
            }
            for c in shift.corrections
        ],
        "movements": [
            {
                "id": m.id,
                "cash_shift_id": m.cash_shift_id,
                "type": m.type,
                "amount": m.amount,
                "reason": m.reason,
                "payment_id": m.payment_id,
                "refund_id": m.refund_id,
                "user": _user(names, m.user_id),
                "created_at": m.created_at,
            }
            for m in shift.movements
        ],
        "handover": _handover(handover, names, viewer) if handover else None,
        "closing": (
            {
                "expected_amount": closing.expected_amount if expected else None,
                "counted_amount": (
                    closing.counted_amount if _count_visible(db, closing, viewer) else None
                ),
                "difference": closing.difference if expected else None,
                "explanation": closing.explanation,
                "user": _user(names, closing.user_id),
                "created_at": closing.created_at,
            }
            if closing
            else None
        ),
    }


def register_view(db: Session, restaurant_id: uuid.UUID, viewer: CashViewer) -> dict:
    repository = CashRepository(db)
    register = repository.active_register(restaurant_id)
    day = db.scalar(
        select(BusinessDay).where(
            BusinessDay.restaurant_id == restaurant_id,
            BusinessDay.status == "OPEN",
        )
    )
    shift = repository.open_shift(restaurant_id)
    if shift is not None:
        shift = repository.get_shift(shift.id, restaurant_id)
    previous = repository.last_closed_shift(register.id) if register else None
    previous_closing = None
    if previous is not None:
        closing = db.scalar(select(CashShiftClosing).where(CashShiftClosing.cash_shift_id == previous.id))
        names = cash_ledger.user_names(db, [closing.user_id])
        previous_closing = {
            "cash_shift_id": previous.id,
            "business_day_id": previous.business_day_id,
            "expected_amount": closing.expected_amount if viewer.sees_expected else None,
            "counted_amount": (
                closing.counted_amount if _count_visible(db, closing, viewer) else None
            ),
            "closed_at": previous.closed_at,
            "closed_by": _user(names, closing.user_id),
            "handover_pending": _handover_pending(db, previous.id),
        }
    return {
        "register_id": register.id if register else None,
        "business_day": day,
        "shift": shift_detail(db, shift, viewer) if shift else None,
        "previous_closing": previous_closing,
    }
