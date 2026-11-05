"""Business days and the shared cash register (docs/stage-3-cashier-plan.md).

A business day is independent of cash shifts: shifts open, close and hand
over the register within it while orders and payments go on. Every
operation records the employee who made it; locks follow the order in
CashRepository. Services end with `commit(db)`, as elsewhere.
"""

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ApiError, ErrorCode
from app.core.states import BUSINESS_DAY, CASH_SHIFT
from app.db.models.bill import Bill
from app.db.models.cash import (
    BusinessDay,
    CashMovement,
    CashOpeningCorrection,
    CashRegister,
    CashShift,
    CashShiftClosing,
    CashShiftHandover,
)
from app.db.models.order import Order, OrderItem
from app.db.models.restaurant import Restaurant
from app.db.models.table import Table
from app.db.unit_of_work import commit
from app.repositories.cash_repository import CashRepository
from app.schemas.outbox import (
    BusinessDayClosedV1,
    BusinessDayOpenedV1,
    CashMovementCreatedV1,
    CashOpeningBalanceCorrectedV1,
    CashShiftClosedV1,
    CashShiftHandoverRecordedV1,
    CashShiftOpenedV1,
)
from app.services import cash_ledger
from app.services.event_service import EventService
from app.services.order_service import ACTIVE_ORDER_STATUSES
from app.services.outbox_service import OutboxService

ZERO = Decimal("0.00")


def _conflict(code: ErrorCode, message: str, details: dict | None = None) -> ApiError:
    return ApiError(
        status_code=status.HTTP_409_CONFLICT,
        code=code,
        message=message,
        details=details,
    )


def _forbidden(permission: str) -> ApiError:
    return ApiError(
        status_code=status.HTTP_403_FORBIDDEN,
        code=ErrorCode.PERMISSION_DENIED,
        message=f"Missing permission: {permission}",
    )


def _required(code: ErrorCode, message: str) -> ApiError:
    return ApiError(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        code=code,
        message=message,
    )


def _shift_not_found() -> ApiError:
    return ApiError(
        status_code=status.HTTP_404_NOT_FOUND,
        code=ErrorCode.CASH_SHIFT_NOT_FOUND,
        message="Cash shift not found",
    )


def _day_not_found() -> ApiError:
    return ApiError(
        status_code=status.HTTP_404_NOT_FOUND,
        code=ErrorCode.BUSINESS_DAY_NOT_FOUND,
        message="Business day not found",
    )


class CashService:

    # --- Business days ---

    @staticmethod
    def open_business_day(
        db: Session,
        restaurant_id: uuid.UUID,
        user_id: uuid.UUID,
        business_date: date | None = None,
        *,
        may_open_past: bool = False,
    ) -> BusinessDay:
        """Open the restaurant's next business day.

        A business day is a trading period, not a calendar date: it is
        named after the restaurant-local date it trades for, which may be
        yesterday (a day opened after midnight for the night's service).
        The date defaults to today (restaurant time); it may never be in
        the future, and every new day is dated after the latest existing
        one. A date before yesterday needs business_days:open_past
        (management), and the opening is recorded as backdated.
        """

        # Serializes opening per restaurant: the existence checks below
        # then can't race (the unique indexes are the backstop).
        restaurant = db.scalar(
            select(Restaurant)
            .where(Restaurant.id == restaurant_id)
            .with_for_update()
        )
        today = datetime.now(ZoneInfo(restaurant.timezone)).date()
        if business_date is None:
            business_date = today

        repository = CashRepository(db)
        if repository.lock_open_business_day(restaurant_id) is not None:
            raise _conflict(
                ErrorCode.BUSINESS_DAY_ALREADY_OPEN,
                "A business day is already open; close it first",
            )
        if db.scalar(
            select(BusinessDay.id).where(
                BusinessDay.restaurant_id == restaurant_id,
                BusinessDay.business_date == business_date,
            )
        ):
            raise _conflict(
                ErrorCode.BUSINESS_DAY_EXISTS,
                f"The business day {business_date.isoformat()} already exists; "
                "a closed day is never reopened",
            )
        if business_date > today:
            raise _required(
                ErrorCode.INVALID_BUSINESS_DATE,
                f"A business day can't be dated in the future (today is {today.isoformat()})",
            )
        latest = db.scalar(
            select(func.max(BusinessDay.business_date)).where(
                BusinessDay.restaurant_id == restaurant_id
            )
        )
        if latest is not None and business_date < latest:
            raise _required(
                ErrorCode.INVALID_BUSINESS_DATE,
                f"A new business day must be dated after the latest one ({latest.isoformat()})",
            )
        backdated = business_date < today - timedelta(days=1)
        if backdated and not may_open_past:
            raise _forbidden("business_days:open_past")

        day = BusinessDay(
            restaurant_id=restaurant_id,
            business_date=business_date,
            status=BUSINESS_DAY.initial,
            opened_by_user_id=user_id,
            opened_at=datetime.utcnow(),
        )
        db.add(day)
        db.flush()
        EventService.record(
            db=db,
            restaurant_id=restaurant_id,
            user_id=user_id,
            event_type="business_day.opened",
            entity_type="business_day",
            entity_id=day.id,
            metadata={
                "business_date": business_date.isoformat(),
                "restaurant_date": today.isoformat(),
                "backdated": backdated,
            },
        )
        OutboxService.record(
            db=db,
            restaurant_id=restaurant_id,
            payload=BusinessDayOpenedV1(business_day_id=day.id),
        )
        commit(db)
        db.refresh(day)
        return day

    @staticmethod
    def close_business_day(
        db: Session,
        restaurant_id: uuid.UUID,
        user_id: uuid.UUID,
        business_day_id: uuid.UUID,
    ) -> BusinessDay:
        """Close the day when nothing is left open; freeze its report.

        Refused while a cash shift of the day is open or any order of the
        restaurant is still active (decision E). Nothing else is changed:
        the manager resolves each blocking item first.
        """

        repository = CashRepository(db)
        day = repository.lock_business_day(business_day_id, restaurant_id)
        if day is None:
            raise _day_not_found()
        if day.status != "OPEN":
            raise _conflict(ErrorCode.BUSINESS_DAY_NOT_OPEN, "The business day is already closed")

        open_shifts = db.scalars(
            select(CashShift.id).where(
                CashShift.business_day_id == day.id,
                CashShift.status == "OPEN",
            )
        ).all()
        if open_shifts:
            raise _conflict(
                ErrorCode.CASH_SHIFT_STILL_OPEN,
                "Close the open cash shift before closing the business day",
                details={"cash_shift_ids": [str(i) for i in open_shifts]},
            )

        blocking = CashService.blocking_orders(db, restaurant_id)
        if blocking:
            raise _conflict(
                ErrorCode.BUSINESS_DAY_HAS_OPEN_ORDERS,
                f"{len(blocking)} order(s) are still open; settle, close or void "
                "each before closing the business day",
                details={"orders": blocking},
            )

        restaurant = db.get(Restaurant, restaurant_id)
        BUSINESS_DAY.apply(day, "CLOSED")
        day.closed_by_user_id = user_id
        day.closed_at = datetime.utcnow()
        day.closing_summary = cash_ledger.day_report(db, day, restaurant.currency)
        EventService.record(
            db=db,
            restaurant_id=restaurant_id,
            user_id=user_id,
            event_type="business_day.closed",
            entity_type="business_day",
            entity_id=day.id,
            metadata={
                "business_date": day.business_date.isoformat(),
                "revenue_total": day.closing_summary["revenue"]["total"],
                "net_tips_total": day.closing_summary["net_tips"]["total"],
            },
        )
        OutboxService.record(
            db=db,
            restaurant_id=restaurant_id,
            payload=BusinessDayClosedV1(business_day_id=day.id),
        )
        commit(db)
        db.refresh(day)
        return day

    @staticmethod
    def blocking_orders(db: Session, restaurant_id: uuid.UUID) -> list[dict]:
        """Every active order with its table and what is still owed."""

        rows = db.execute(
            select(Order, Table.number, Table.name, Bill)
            .join(Table, Table.id == Order.table_id)
            .outerjoin(Bill, Bill.order_id == Order.id)
            .where(
                Order.restaurant_id == restaurant_id,
                Order.status.in_(ACTIVE_ORDER_STATUSES),
            )
            .order_by(Order.created_at, Order.id)
        ).all()
        result = []
        for order, table_number, table_name, bill in rows:
            if bill is not None:
                outstanding = bill.amount_due
            else:
                outstanding = db.scalar(
                    select(func.coalesce(func.sum(OrderItem.total_price), ZERO)).where(
                        OrderItem.order_id == order.id,
                        OrderItem.status != "CANCELLED",
                    )
                )
            result.append({
                "order_id": str(order.id),
                "order_status": order.status,
                "table_number": table_number,
                "table_name": table_name,
                "bill_id": str(bill.id) if bill else None,
                "bill_status": bill.status if bill else None,
                "outstanding_amount": cash_ledger.money(outstanding),
            })
        return result

    @staticmethod
    def get_business_day(
        db: Session,
        restaurant_id: uuid.UUID,
        business_day_id: uuid.UUID,
    ) -> BusinessDay:
        day = db.scalar(
            select(BusinessDay).where(
                BusinessDay.id == business_day_id,
                BusinessDay.restaurant_id == restaurant_id,
            )
        )
        if day is None:
            raise _day_not_found()
        return day

    @staticmethod
    def business_day_report(db: Session, restaurant_id: uuid.UUID, day: BusinessDay) -> dict:
        """A closed day's frozen report, or an open day's report so far."""

        if day.status == "CLOSED":
            return day.closing_summary
        restaurant = db.get(Restaurant, restaurant_id)
        return cash_ledger.day_report(db, day, restaurant.currency)

    # --- Shifts ---

    @staticmethod
    def open_shift(
        db: Session,
        restaurant_id: uuid.UUID,
        user_id: uuid.UUID,
        opening_amount: Decimal,
        handover_reason: str | None = None,
        *,
        may_self_confirm: bool = False,
    ) -> CashShift:
        """Open the register's next shift with the cash the opening
        cashier counted (blind), reconciled with the previous shift's count.

        When the register's previous shift is closed, this is the second
        step of the handover: the incoming cashier confirms the cash
        physically received, which becomes the opening amount, and a
        handover is recorded between the two shifts. The incoming cashier
        must be another person than the one who closed the previous shift,
        unless they hold cash_handovers:self_confirm (management); such a
        handover shows the same user on both sides.
        """

        repository = CashRepository(db)
        day = repository.lock_open_business_day(restaurant_id)
        if day is None:
            raise _conflict(ErrorCode.NO_OPEN_BUSINESS_DAY, "Open the business day first")
        register = repository.lock_or_create_active_register(restaurant_id)
        if repository.open_shift(restaurant_id) is not None:
            raise _conflict(
                ErrorCode.CASH_SHIFT_ALREADY_OPEN,
                "The register already has an open shift; close it or hand it over",
            )
        shift = CashService._open(
            db, day, register, user_id, opening_amount, handover_reason, may_self_confirm
        )
        commit(db)
        db.refresh(shift)
        return shift

    @staticmethod
    def close_shift(
        db: Session,
        restaurant_id: uuid.UUID,
        user_id: uuid.UUID,
        shift_id: uuid.UUID,
        counted_amount: Decimal,
        explanation: str | None = None,
    ) -> CashShift:
        shift = CashService._lock_open_shift(db, restaurant_id, shift_id)
        CashService._close(db, shift, user_id, counted_amount, explanation)
        commit(db)
        db.refresh(shift)
        return shift

    @staticmethod
    def hand_over(
        db: Session,
        restaurant_id: uuid.UUID,
        user_id: uuid.UUID,
        shift_id: uuid.UUID,
        counted_amount: Decimal,
        closing_explanation: str | None,
    ) -> CashShift:
        """First step of a two-party handover: the outgoing cashier counts
        the drawer and closes their shift under their own name.

        The drawer then waits for the incoming cashier, who confirms the
        cash physically received by opening the next shift (open_shift),
        under their own name. Between the two steps the register has no
        open shift: payments, refunds and cash movements are refused and
        nothing is recorded, so no money can land outside a shift. (This
        replaces the one-step handover, which recorded the caller as both
        cashiers.)
        """

        return CashService.close_shift(
            db, restaurant_id, user_id, shift_id, counted_amount, closing_explanation
        )

    @staticmethod
    def correct_opening_amount(
        db: Session,
        restaurant_id: uuid.UUID,
        user_id: uuid.UUID,
        shift_id: uuid.UUID,
        new_amount: Decimal,
        reason: str,
    ) -> CashOpeningCorrection:
        """A manager fixes a mistyped opening amount of an open shift: the
        expected cash uses the new amount; no cash moved, so no movement
        is written. Every correction is kept."""

        shift = CashService._lock_open_shift(db, restaurant_id, shift_id)
        previous = cash_ledger.drawer(db, shift).effective_opening_amount
        if new_amount == previous:
            raise _conflict(
                ErrorCode.OPENING_AMOUNT_UNCHANGED,
                "The opening amount is already this amount",
            )
        correction = CashOpeningCorrection(
            restaurant_id=restaurant_id,
            cash_shift_id=shift.id,
            previous_amount=previous,
            new_amount=new_amount,
            reason=reason,
            user_id=user_id,
            created_at=datetime.utcnow(),
        )
        db.add(correction)
        db.flush()
        EventService.record(
            db=db,
            restaurant_id=restaurant_id,
            user_id=user_id,
            event_type="cash_opening_balance.corrected",
            entity_type="cash_shift",
            entity_id=shift.id,
            metadata={
                "correction_id": str(correction.id),
                "original_amount": cash_ledger.money(shift.opening_amount),
                "previous_amount": cash_ledger.money(previous),
                "new_amount": cash_ledger.money(new_amount),
                "reason": reason,
            },
        )
        OutboxService.record(
            db=db,
            restaurant_id=restaurant_id,
            payload=CashOpeningBalanceCorrectedV1(
                cash_shift_id=shift.id,
                correction_id=correction.id,
            ),
        )
        commit(db)
        db.refresh(correction)
        return correction

    @staticmethod
    def record_movement(
        db: Session,
        restaurant_id: uuid.UUID,
        user_id: uuid.UUID,
        shift_id: uuid.UUID,
        movement_type: str,
        amount: Decimal,
        reason: str,
    ) -> CashMovement:
        """Cash put into (CASH_IN) or taken out of (CASH_OUT) the drawer,
        e.g. change, a safe drop or a tip payout. A CASH_OUT can't take
        more than the drawer should hold."""

        shift = CashService._lock_open_shift(db, restaurant_id, shift_id)
        if movement_type == "CASH_OUT":
            CashService.ensure_cash_available(db, shift, amount)
        movement = CashMovement(
            restaurant_id=restaurant_id,
            cash_shift_id=shift.id,
            type=movement_type,
            amount=amount,
            reason=reason,
            user_id=user_id,
            created_at=datetime.utcnow(),
        )
        db.add(movement)
        db.flush()
        EventService.record(
            db=db,
            restaurant_id=restaurant_id,
            user_id=user_id,
            event_type="cash_movement.created",
            entity_type="cash_movement",
            entity_id=movement.id,
            metadata={
                "cash_shift_id": str(shift.id),
                "type": movement_type,
                "amount": cash_ledger.money(amount),
                "reason": reason,
            },
        )
        OutboxService.record(
            db=db,
            restaurant_id=restaurant_id,
            payload=CashMovementCreatedV1(
                cash_shift_id=shift.id,
                cash_movement_id=movement.id,
                type=movement_type,
            ),
        )
        commit(db)
        db.refresh(movement)
        return movement

    @staticmethod
    def ensure_cash_available(db: Session, shift: CashShift, amount: Decimal) -> None:
        """Refuse taking out more than the (locked) shift's drawer holds."""

        available = cash_ledger.drawer(db, shift).expected
        if amount > available:
            # The amount available is not in the message (blind count).
            raise _conflict(
                ErrorCode.INSUFFICIENT_CASH,
                "The drawer should not hold that much cash",
            )

    @staticmethod
    def get_shift(db: Session, restaurant_id: uuid.UUID, shift_id: uuid.UUID) -> CashShift:
        shift = CashRepository(db).get_shift(shift_id, restaurant_id)
        if shift is None:
            raise _shift_not_found()
        return shift

    @staticmethod
    def handovers(db: Session, restaurant_id: uuid.UUID, limit: int) -> list[CashShiftHandover]:
        return db.scalars(
            select(CashShiftHandover)
            .where(CashShiftHandover.restaurant_id == restaurant_id)
            .order_by(CashShiftHandover.created_at.desc(), CashShiftHandover.id.desc())
            .limit(limit)
        ).all()

    # --- Internals (caller holds the locks and commits) ---

    @staticmethod
    def _lock_open_shift(
        db: Session,
        restaurant_id: uuid.UUID,
        shift_id: uuid.UUID,
    ) -> CashShift:
        shift = CashRepository(db).lock_shift(shift_id, restaurant_id)
        if shift is None:
            raise _shift_not_found()
        if shift.status != "OPEN":
            raise _conflict(ErrorCode.CASH_SHIFT_NOT_OPEN, "The cash shift is already closed")
        return shift

    @staticmethod
    def _open(
        db: Session,
        day: BusinessDay,
        register: CashRegister,
        user_id: uuid.UUID,
        opening_amount: Decimal,
        handover_reason: str | None,
        may_self_confirm: bool,
    ) -> CashShift:
        restaurant = db.get(Restaurant, day.restaurant_id)
        previous = CashRepository(db).last_closed_shift(register.id)
        previous_closing = (
            db.scalar(select(CashShiftClosing).where(CashShiftClosing.cash_shift_id == previous.id))
            if previous is not None
            else None
        )
        if (
            previous_closing is not None
            and previous_closing.user_id == user_id
            and not may_self_confirm
        ):
            raise _conflict(
                ErrorCode.HANDOVER_SAME_USER,
                "You closed the previous shift; the cash must be received and "
                "counted by another employee",
            )
        difference = (
            opening_amount - previous_closing.counted_amount
            if previous_closing is not None
            else ZERO
        )
        # Blind count: the amounts are never in the message.
        if difference != ZERO and not handover_reason:
            raise _required(
                ErrorCode.HANDOVER_REASON_REQUIRED,
                "The cash received differs from what the previous shift handed "
                "over; recount, or give a reason",
            )

        now = datetime.utcnow()
        shift = CashShift(
            restaurant_id=day.restaurant_id,
            cash_register_id=register.id,
            business_day_id=day.id,
            status=CASH_SHIFT.initial,
            currency=restaurant.currency,
            opening_amount=opening_amount,
            opened_by_user_id=user_id,
            opened_at=now,
        )
        db.add(shift)
        db.flush()

        handover = None
        if previous_closing is not None:
            handover = CashShiftHandover(
                restaurant_id=day.restaurant_id,
                cash_register_id=register.id,
                business_day_id=day.id,
                from_shift_id=previous.id,
                to_shift_id=shift.id,
                previous_expected_amount=previous_closing.expected_amount,
                handed_over_amount=previous_closing.counted_amount,
                received_amount=opening_amount,
                difference=difference,
                reason=handover_reason or None,
                outgoing_user_id=previous_closing.user_id,
                incoming_user_id=user_id,
                created_at=now,
            )
            db.add(handover)
            db.flush()

        EventService.record(
            db=db,
            restaurant_id=day.restaurant_id,
            user_id=user_id,
            event_type="cash_shift.opened",
            entity_type="cash_shift",
            entity_id=shift.id,
            metadata={
                "business_day_id": str(day.id),
                "cash_register_id": str(register.id),
                "opening_amount": cash_ledger.money(opening_amount),
                "handover_id": str(handover.id) if handover else None,
            },
        )
        OutboxService.record(
            db=db,
            restaurant_id=day.restaurant_id,
            payload=CashShiftOpenedV1(
                cash_shift_id=shift.id,
                business_day_id=day.id,
                cash_register_id=register.id,
                handover_id=handover.id if handover else None,
            ),
        )
        if handover is not None:
            EventService.record(
                db=db,
                restaurant_id=day.restaurant_id,
                user_id=user_id,
                event_type="cash_shift.handover_recorded",
                entity_type="cash_shift_handover",
                entity_id=handover.id,
                metadata={
                    "from_shift_id": str(previous.id),
                    "to_shift_id": str(shift.id),
                    "business_day_id": str(day.id),
                    "previous_business_day_id": str(previous.business_day_id),
                    "previous_expected_amount": cash_ledger.money(handover.previous_expected_amount),
                    "handed_over_amount": cash_ledger.money(handover.handed_over_amount),
                    "received_amount": cash_ledger.money(handover.received_amount),
                    "difference": cash_ledger.money(handover.difference),
                    "reason": handover.reason,
                    "outgoing_user_id": str(handover.outgoing_user_id),
                    "self_confirmed": handover.outgoing_user_id == handover.incoming_user_id,
                },
            )
            OutboxService.record(
                db=db,
                restaurant_id=day.restaurant_id,
                payload=CashShiftHandoverRecordedV1(
                    cash_shift_id=shift.id,
                    handover_id=handover.id,
                    from_shift_id=previous.id,
                    business_day_id=day.id,
                ),
            )
        return shift

    @staticmethod
    def _close(
        db: Session,
        shift: CashShift,
        user_id: uuid.UUID,
        counted_amount: Decimal,
        explanation: str | None,
    ) -> CashShiftClosing:
        # Under the shift lock: every payment, refund and movement of the
        # shift has committed (they hold the same lock), none can follow.
        report = cash_ledger.shift_report(db, shift)
        expected = Decimal(report["drawer"]["expected"])
        difference = counted_amount - expected
        # Blind count: the expected cash is never in the message.
        if difference != ZERO and not explanation:
            raise _required(
                ErrorCode.CLOSING_EXPLANATION_REQUIRED,
                "The count differs from the expected cash; recount, or explain "
                "the difference",
            )
        now = datetime.utcnow()
        closing = CashShiftClosing(
            restaurant_id=shift.restaurant_id,
            cash_shift_id=shift.id,
            expected_amount=expected,
            counted_amount=counted_amount,
            difference=difference,
            explanation=explanation or None,
            summary=report,
            user_id=user_id,
            created_at=now,
        )
        db.add(closing)
        # The closing is written while the shift is still OPEN (the
        # database accepts ledger rows only into open shifts).
        db.flush()
        CASH_SHIFT.apply(shift, "CLOSED")
        shift.closed_at = now
        db.flush()
        EventService.record(
            db=db,
            restaurant_id=shift.restaurant_id,
            user_id=user_id,
            event_type="cash_shift.closed",
            entity_type="cash_shift",
            entity_id=shift.id,
            metadata={
                "business_day_id": str(shift.business_day_id),
                "expected_amount": cash_ledger.money(expected),
                "counted_amount": cash_ledger.money(counted_amount),
                "difference": cash_ledger.money(difference),
                "explanation": explanation or None,
            },
        )
        OutboxService.record(
            db=db,
            restaurant_id=shift.restaurant_id,
            payload=CashShiftClosedV1(
                cash_shift_id=shift.id,
                business_day_id=shift.business_day_id,
            ),
        )
        return closing
