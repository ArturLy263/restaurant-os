import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import status
from sqlalchemy.orm import Session

from app.core.errors import ApiError, ErrorCode
from app.db.models.bill import Bill, Payment
from app.db.models.order import Order, OrderItem
from app.db.models.table import Table
from app.db.unit_of_work import commit
from app.repositories.bill_repository import BillRepository
from app.repositories.order_repository import OrderRepository
from app.services.event_service import EventService


class BillService:

    @staticmethod
    def create_bill(
        db: Session,
        restaurant_id: uuid.UUID,
        order_id: uuid.UUID,
    ) -> Bill:

        order = db.get(Order, order_id)

        if order is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.ORDER_NOT_FOUND,
                message="Order not found",
            )

        if order.restaurant_id != restaurant_id:
            raise ApiError(
                status_code=status.HTTP_403_FORBIDDEN,
                code=ErrorCode.TENANT_ACCESS_DENIED,
                message="Order does not belong to this restaurant",
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

        EventService.record(
            db=db,
            restaurant_id=restaurant_id,
            user_id=order.user_id,
            event_type="bill.created",
            entity_type="bill",
            entity_id=bill.id,
            metadata={
                "order_id": str(order.id),
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
        bill_id: uuid.UUID,
        amount: Decimal,
        method: str,
    ) -> Payment:

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

        if order.restaurant_id != restaurant_id:
            raise ApiError(
                status_code=status.HTTP_403_FORBIDDEN,
                code=ErrorCode.TENANT_ACCESS_DENIED,
                message="Bill does not belong to this restaurant",
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

        payment = Payment(
            bill_id=bill.id,
            amount=amount,
            method=method,
            status="COMPLETED",
            created_at=datetime.utcnow(),
        )

        db.add(payment)
        db.flush()

        EventService.record(
            db=db,
            restaurant_id=restaurant_id,
            user_id=order.user_id,
            event_type="payment.completed",
            entity_type="payment",
            entity_id=payment.id,
            metadata={
                "bill_id": str(bill.id),
                "order_id": str(order.id),
                "amount": str(amount),
                "method": method,
            },
        )

        new_paid_amount = paid_amount + amount

        if new_paid_amount == adjusted_total:
            now = datetime.utcnow()

            bill.status = "PAID"
            bill.paid_at = now

            order.status = "CLOSED"
            order.closed_at = now

            table = db.get(Table, order.table_id)

            if table is not None:
                table.status = "AVAILABLE"

            EventService.record(
                db=db,
                restaurant_id=restaurant_id,
                user_id=order.user_id,
                event_type="order.closed",
                entity_type="order",
                entity_id=order.id,
                metadata={
                    "bill_id": str(bill.id),
                    "total_paid": str(new_paid_amount),
                },
            )

        commit(db)
        db.refresh(payment)

        return payment

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

        if order.restaurant_id != restaurant_id:
            raise ApiError(
                status_code=status.HTTP_403_FORBIDDEN,
                code=ErrorCode.TENANT_ACCESS_DENIED,
                message="Bill does not belong to this restaurant",
            )

        return bill