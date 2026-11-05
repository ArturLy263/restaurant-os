import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import ApiError, ErrorCode
from app.core.states import BILL, ORDER, TABLE
from app.db.models.bill import Bill, BillAdjustment, Payment, PaymentAllocation, Refund
from app.db.models.cash import BusinessDay, CashMovement, CashShift
from app.db.models.order import Order, OrderItem
from app.db.models.restaurant import Restaurant
from app.db.models.table import Table
from app.db.unit_of_work import commit
from app.payments.providers import confirmer_for
from app.repositories.bill_repository import BillRepository
from app.repositories.cash_repository import CashRepository
from app.repositories.order_repository import OrderRepository
from app.schemas.outbox import BillCreatedV1, PaymentCompletedV1, PaymentRefundedV1
from app.services.cash_service import CashService
from app.services.event_service import EventService
from app.services.outbox_service import OutboxService

ZERO = Decimal("0.00")
# Adjustments that charge or credit exactly one item's price: a bill with
# only these can be paid by items (each item's total_price is its share).
ITEM_ADJUSTMENT_TYPES = frozenset({"ITEM_ADDITION", "ITEM_CANCELLATION"})
# Equal splits: at most this many shares.
MAX_SPLIT_PARTS = 50


class BillService:

    @staticmethod
    def create_bill(
        db: Session,
        restaurant_id: uuid.UUID,
        user_id: uuid.UUID,
        order_id: uuid.UUID,
    ) -> Bill:

        order = db.get(Order, order_id)

        # Another restaurant's order is reported exactly like a missing one,
        # so its existence is not revealed.
        if order is None or order.restaurant_id != restaurant_id:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.ORDER_NOT_FOUND,
                message="Order not found",
            )

        # Lock the order so items cannot be added or cancelled while the
        # bill total is computed, and concurrent requests cannot both
        # create a bill. State read below is current as of the lock.
        order = OrderRepository(db).get_for_restaurant(
            order.id,
            restaurant_id,
            for_update=True,
        )

        if order.status != "SENT":
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.ORDER_NOT_READY_FOR_BILLING,
                message="Order is not ready for billing",
            )

        existing_bill = BillRepository(db).get_for_order(order.id)

        if existing_bill is not None:
            return existing_bill

        if all(item.status == "CANCELLED" for item in order.items):
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.ORDER_HAS_NO_BILLABLE_ITEMS,
                message="Order has no billable items; void the order instead",
            )

        subtotal = sum(
            (
                item.total_price
                for item in order.items
                if item.status != "CANCELLED"
            ),
            Decimal("0.00"),
)

        bill = Bill(
            order_id=order.id,
            subtotal=subtotal,
            total=subtotal,
            status="OPEN",
            created_at=datetime.utcnow(),
        )

        db.add(bill)
        db.flush()

        OutboxService.record(
            db=db,
            restaurant_id=restaurant_id,
            payload=BillCreatedV1(bill_id=bill.id, order_id=order.id),
        )

        EventService.record(
            db=db,
            restaurant_id=restaurant_id,
            user_id=user_id,
            event_type="bill.created",
            entity_type="bill",
            entity_id=bill.id,
            metadata={
                "order_id": str(order.id),
                "order_created_by": str(order.user_id),
                "subtotal": str(subtotal),
                "total": str(subtotal),
            },
        )

        commit(db)
        db.refresh(bill)

        return bill

    @staticmethod
    def create_payment(
        db: Session,
        restaurant_id: uuid.UUID,
        user_id: uuid.UUID,
        bill_id: uuid.UUID,
        amount: Decimal,
        method: str,
        *,
        tip_amount: Decimal = ZERO,
        order_item_ids: list[uuid.UUID] | None = None,
        external_reference: str | None = None,
        card_confirmed: bool = False,
    ) -> Payment:
        """Record money taken for a bill in the open cash shift.

        `amount` pays the bill (never more than is due); `tip_amount` is
        on top and owed nothing. With `order_item_ids` the payment pays
        exactly those items (splitting by items): `amount` must equal
        their total and each item can be paid only once. A card payment
        needs the cashier's confirmation that the external terminal
        approved it (`card_confirmed`).
        """

        bill = db.get(Bill, bill_id)

        if bill is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.BILL_NOT_FOUND,
                message="Bill not found",
            )

        order = db.get(Order, bill.order_id)

        if order is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.ORDER_NOT_FOUND,
                message="Order not found",
            )

        # Another restaurant's bill is reported exactly like a missing one,
        # so its existence is not revealed.
        if order.restaurant_id != restaurant_id:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.BILL_NOT_FOUND,
                message="Bill not found",
            )

        # Lock the order (and so its bill) before checking the balance, so
        # concurrent payments or item changes cannot act on a stale amount
        # due. Re-read both under the lock.
        order = OrderRepository(db).get_for_restaurant(
            order.id,
            restaurant_id,
            for_update=True,
        )
        bill = BillRepository(db).get_for_order(order.id)

        if bill.status != "OPEN":
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.BILL_NOT_OPEN,
                message="Bill is not open",
            )

        if amount <= Decimal("0.00"):
            raise ApiError(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                code=ErrorCode.INVALID_PAYMENT_AMOUNT,
                message="Payment amount must be greater than zero",
            )

        # Remaining balance accounts for adjustments (e.g. items cancelled
        # after the bill was created); bill.total is the original amount.
        paid_amount = bill.paid_total
        adjusted_total = bill.adjusted_total
        remaining_amount = bill.amount_due

        items = BillService._items_to_pay(db, order, bill, amount, order_item_ids)

        if amount > remaining_amount:
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.PAYMENT_EXCEEDS_AMOUNT_DUE,
                message="Payment exceeds remaining bill amount",
            )

        # The final payment closes the order, after which unsent items can
        # no longer be sent or cancelled. Checked under the order lock, so
        # no item can be added between this check and the payment.
        if amount == remaining_amount:
            has_unsent_items = db.query(
                db.query(OrderItem)
                .filter(
                    OrderItem.order_id == order.id,
                    OrderItem.status == "ADDED",
                )
                .exists()
            ).scalar()

            if has_unsent_items:
                raise ApiError(
                    status_code=status.HTTP_409_CONFLICT,
                    code=ErrorCode.ORDER_HAS_UNSENT_ITEMS,
                    message=(
                        "Order has unsent items; send or cancel them "
                        "before the final payment"
                    ),
                )

        confirmation = confirmer_for(method).confirm(
            cashier_confirmed=card_confirmed,
            external_reference=external_reference,
        )

        # After the order lock: the day can't close and the shift can't
        # close or hand over until this payment has committed.
        day, shift = BillService._lock_open_day_and_shift(db, restaurant_id)

        payment = Payment(
            restaurant_id=restaurant_id,
            bill_id=bill.id,
            amount=amount,
            tip_amount=tip_amount,
            currency=shift.currency,
            method=method,
            status="COMPLETED",
            user_id=user_id,
            cash_shift_id=shift.id,
            business_day_id=day.id,
            confirmation_method=confirmation.method,
            confirmed_at=confirmation.confirmed_at,
            external_reference=confirmation.external_reference,
            created_at=datetime.utcnow(),
        )

        db.add(payment)
        db.flush()

        for item in items:
            db.add(
                PaymentAllocation(
                    restaurant_id=restaurant_id,
                    payment_id=payment.id,
                    order_item_id=item.id,
                    amount=item.total_price,
                    created_at=payment.created_at,
                )
            )

        # A cash tip goes into the drawer: its own line, never revenue.
        if method == "CASH" and tip_amount > ZERO:
            db.add(
                CashMovement(
                    restaurant_id=restaurant_id,
                    cash_shift_id=shift.id,
                    type="CASH_TIP",
                    amount=tip_amount,
                    payment_id=payment.id,
                    user_id=user_id,
                    created_at=payment.created_at,
                )
            )

        db.flush()

        EventService.record(
            db=db,
            restaurant_id=restaurant_id,
            user_id=user_id,
            event_type="payment.completed",
            entity_type="payment",
            entity_id=payment.id,
            metadata={
                "bill_id": str(bill.id),
                "order_id": str(order.id),
                "order_created_by": str(order.user_id),
                "amount": str(amount),
                "tip_amount": str(tip_amount),
                "currency": payment.currency,
                "method": method,
                "confirmation_method": confirmation.method,
                "external_reference": confirmation.external_reference,
                "cash_shift_id": str(shift.id),
                "business_day_id": str(day.id),
                "order_item_ids": [str(item.id) for item in items],
            },
        )

        new_paid_amount = paid_amount + amount

        if new_paid_amount == adjusted_total:
            now = datetime.utcnow()

            BILL.apply(bill, "PAID")
            bill.paid_at = now

            ORDER.apply(order, "CLOSED")
            order.closed_at = now

            table = db.get(Table, order.table_id)

            if table is not None:
                TABLE.apply(table, "AVAILABLE")

            EventService.record(
                db=db,
                restaurant_id=restaurant_id,
                user_id=user_id,
                event_type="order.closed",
                entity_type="order",
                entity_id=order.id,
                metadata={
                    "bill_id": str(bill.id),
                    "order_created_by": str(order.user_id),
                    "total_paid": str(new_paid_amount),
                },
            )

        OutboxService.record(
            db=db,
            restaurant_id=restaurant_id,
            payload=PaymentCompletedV1(
                payment_id=payment.id,
                bill_id=bill.id,
                order_id=order.id,
                business_day_id=day.id,
                cash_shift_id=shift.id,
                method=method,
                bill_status=bill.status,
            ),
        )

        commit(db)
        db.refresh(payment)

        return payment

    @staticmethod
    def _lock_open_day_and_shift(
        db: Session,
        restaurant_id: uuid.UUID,
    ) -> tuple[BusinessDay, CashShift]:
        repository = CashRepository(db)
        day = repository.lock_open_business_day(restaurant_id)
        if day is None:
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.NO_OPEN_BUSINESS_DAY,
                message="No business day is open; open the day first",
            )
        shift = repository.lock_open_shift(restaurant_id)
        if shift is None:
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.NO_OPEN_CASH_SHIFT,
                message="The register has no open cash shift; open one first",
            )
        # A day can't close while a shift is open, and a shift opens only
        # in the open day, so the open shift is always in the open day.
        if shift.business_day_id != day.id:
            raise RuntimeError("The open cash shift is not in the open business day")
        return day, shift

    @staticmethod
    def _items_to_pay(
        db: Session,
        order: Order,
        bill: Bill,
        amount: Decimal,
        order_item_ids: list[uuid.UUID] | None,
    ) -> list[OrderItem]:
        """The items a split-by-items payment pays, checked under the
        order lock; the unique allocation per item is the backstop.

        Each item is paid at its own total_price, to the cent; nothing is
        prorated. That is exact only while every applied adjustment of the
        bill is an item's own price added or cancelled (so the adjusted
        total is the sum of the uncancelled items). Any other adjustment
        (none exists yet, e.g. a future bill-level discount) can't be
        assigned to items without rounding choices, so paying by items is
        then refused (ITEM_SPLIT_UNAVAILABLE) rather than guessed: pay by
        amount instead."""

        if not order_item_ids:
            return []
        BillService._ensure_item_split_available(db, order, bill)
        ids = set(order_item_ids)
        items = db.scalars(
            select(OrderItem).where(
                OrderItem.order_id == order.id,
                OrderItem.id.in_(ids),
            )
        ).all()
        if len(items) != len(ids) or any(item.status == "CANCELLED" for item in items):
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.ORDER_ITEM_NOT_PAYABLE,
                message="Every item must be an uncancelled item of this order",
            )
        paid = db.scalars(
            select(PaymentAllocation.order_item_id).where(
                PaymentAllocation.order_item_id.in_(ids)
            )
        ).all()
        if paid:
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.ORDER_ITEM_ALREADY_PAID,
                message=f"{len(paid)} of the items are already paid",
            )
        total = sum((item.total_price for item in items), ZERO)
        if total != amount:
            raise ApiError(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code=ErrorCode.PAYMENT_AMOUNT_MISMATCH,
                message=f"The selected items total {total:.2f}, not {amount:.2f}",
            )
        return sorted(items, key=lambda item: (item.created_at, item.id))

    @staticmethod
    def _ensure_item_split_available(db: Session, order: Order, bill: Bill) -> None:
        unallocatable = db.scalar(
            select(BillAdjustment.id).where(
                BillAdjustment.bill_id == bill.id,
                BillAdjustment.status == "APPLIED",
                (BillAdjustment.order_item_id.is_(None))
                | BillAdjustment.type.not_in(ITEM_ADJUSTMENT_TYPES),
            ).limit(1)
        )
        items_total = sum(
            (item.total_price for item in order.items if item.status != "CANCELLED"),
            ZERO,
        )
        if unallocatable is not None or items_total != bill.adjusted_total:
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.ITEM_SPLIT_UNAVAILABLE,
                message=(
                    "This bill has an adjustment that belongs to no single item; "
                    "pay it by amount instead of by items"
                ),
            )

    @staticmethod
    def refund_payment(
        db: Session,
        restaurant_id: uuid.UUID,
        user_id: uuid.UUID,
        payment_id: uuid.UUID,
        amount: Decimal,
        tip_amount: Decimal,
        reason: str,
        *,
        external_reference: str | None = None,
        card_confirmed: bool = False,
    ) -> Refund:
        """Return money for a payment, the way it was paid, in the open
        shift and business day. The payment and the bill are kept as they
        are (a refund does not reopen a bill); `amount` reduces revenue,
        `tip_amount` reduces tips only; together they never exceed what
        the payment took.

        Only a settled (PAID) bill's payments are refunded. While a bill is
        OPEN its amount due is total - payments, so a refund there would
        let the bill close as paid with less money kept than its total;
        a mistaken payment on an open bill is settled first (pay the rest),
        then refunded."""

        payment = db.scalar(
            select(Payment).where(
                Payment.id == payment_id,
                Payment.restaurant_id == restaurant_id,
            )
        )
        if payment is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.PAYMENT_NOT_FOUND,
                message="Payment not found",
            )
        bill = db.get(Bill, payment.bill_id)
        # Same lock as payments: refunds of one payment are serialized.
        order = OrderRepository(db).get_for_restaurant(
            bill.order_id,
            restaurant_id,
            for_update=True,
        )
        payment = db.scalar(
            select(Payment)
            .where(Payment.id == payment_id)
            .options(selectinload(Payment.refunds))
            .execution_options(populate_existing=True)
        )
        # Re-read under the order lock: the bill can't change meanwhile.
        bill = BillRepository(db).get_for_order(order.id)
        if bill.status != "PAID":
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.BILL_NOT_SETTLED,
                message=(
                    "Only payments of a settled (paid) bill can be refunded; "
                    f"this bill is {bill.status.lower()}"
                ),
            )

        if amount + tip_amount <= ZERO:
            raise ApiError(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code=ErrorCode.VALIDATION_ERROR,
                message="Refund a positive amount, tip or both",
            )
        refundable = payment.amount - payment.refunded_amount
        refundable_tip = payment.tip_amount - payment.refunded_tip_amount
        if amount > refundable or tip_amount > refundable_tip:
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.REFUND_EXCEEDS_REFUNDABLE,
                message=(
                    f"At most {refundable:.2f} of the payment and "
                    f"{refundable_tip:.2f} of its tip can still be refunded"
                ),
            )

        confirmation = confirmer_for(payment.method).confirm(
            cashier_confirmed=card_confirmed,
            external_reference=external_reference,
        )
        day, shift = BillService._lock_open_day_and_shift(db, restaurant_id)
        if payment.method == "CASH":
            CashService.ensure_cash_available(db, shift, amount + tip_amount)

        now = datetime.utcnow()
        refund = Refund(
            restaurant_id=restaurant_id,
            payment_id=payment.id,
            cash_shift_id=shift.id,
            business_day_id=day.id,
            amount=amount,
            tip_amount=tip_amount,
            currency=payment.currency,
            method=payment.method,
            confirmation_method=confirmation.method,
            external_reference=confirmation.external_reference,
            reason=reason,
            user_id=user_id,
            created_at=now,
        )
        db.add(refund)
        db.flush()

        if payment.method == "CASH":
            db.add(
                CashMovement(
                    restaurant_id=restaurant_id,
                    cash_shift_id=shift.id,
                    type="CASH_REFUND",
                    amount=amount + tip_amount,
                    refund_id=refund.id,
                    user_id=user_id,
                    created_at=now,
                )
            )

        EventService.record(
            db=db,
            restaurant_id=restaurant_id,
            user_id=user_id,
            event_type="payment.refunded",
            entity_type="refund",
            entity_id=refund.id,
            metadata={
                "payment_id": str(payment.id),
                "bill_id": str(bill.id),
                "order_id": str(order.id),
                "amount": str(amount),
                "tip_amount": str(tip_amount),
                "method": payment.method,
                "confirmation_method": confirmation.method,
                "external_reference": confirmation.external_reference,
                "reason": reason,
                "cash_shift_id": str(shift.id),
                "business_day_id": str(day.id),
                "payment_cash_shift_id": (
                    str(payment.cash_shift_id) if payment.cash_shift_id else None
                ),
            },
        )
        OutboxService.record(
            db=db,
            restaurant_id=restaurant_id,
            payload=PaymentRefundedV1(
                refund_id=refund.id,
                payment_id=payment.id,
                bill_id=bill.id,
                order_id=order.id,
                business_day_id=day.id,
                cash_shift_id=shift.id,
                method=payment.method,
            ),
        )
        commit(db)
        db.refresh(refund)
        return refund

    @staticmethod
    def equal_split(
        db: Session,
        restaurant_id: uuid.UUID,
        bill_id: uuid.UUID,
        parts: int,
    ) -> list[Decimal]:
        """The amount due in `parts` shares, to the cent: every share is
        the amount divided down to the cent, and the cents left over go
        one each to the first shares, so the shares always add up to the
        amount due exactly. A proposal: each share is then an ordinary
        payment, checked against the amount due when it is made."""

        bill = BillService.get_bill(db, restaurant_id, bill_id)
        due_cents = int(bill.amount_due * 100)
        if bill.status != "OPEN" or due_cents <= 0:
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.BILL_NOT_OPEN,
                message="Nothing is due on this bill",
            )
        if not 2 <= parts <= min(MAX_SPLIT_PARTS, due_cents):
            raise ApiError(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code=ErrorCode.INVALID_SPLIT,
                message=(
                    f"Split into 2 to {min(MAX_SPLIT_PARTS, due_cents)} shares"
                ),
            )
        share, remainder = divmod(due_cents, parts)
        return [
            Decimal(share + (1 if index < remainder else 0)) / 100
            for index in range(parts)
        ]

    @staticmethod
    def get_bill(
        db: Session,
        restaurant_id: uuid.UUID,
        bill_id: uuid.UUID,
    ) -> Bill:

        bill = db.get(Bill, bill_id)

        if bill is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.BILL_NOT_FOUND,
                message="Bill not found",
            )

        order = db.get(Order, bill.order_id)

        if order is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.ORDER_NOT_FOUND,
                message="Order not found",
            )

        # Another restaurant's bill is reported exactly like a missing one,
        # so its existence is not revealed.
        if order.restaurant_id != restaurant_id:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.BILL_NOT_FOUND,
                message="Bill not found",
            )

        return bill