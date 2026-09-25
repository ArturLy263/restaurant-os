import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.db.models.bill import Bill, Payment
from app.db.models.order import Order
from app.db.models.table import Table
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
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Order not found",
            )

        if order.restaurant_id != restaurant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Order does not belong to this restaurant",
            )

        if order.status != "SENT":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Order is not ready for billing",
            )

        existing_bill = (
            db.query(Bill)
            .filter(Bill.order_id == order.id)
            .first()
        )

        if existing_bill is not None:
            return existing_bill

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

        db.commit()
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
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Bill not found",
            )

        order = db.get(Order, bill.order_id)

        if order is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Order not found",
            )

        if order.restaurant_id != restaurant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Bill does not belong to this restaurant",
            )

        if bill.status != "OPEN":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Bill is not open",
            )

        if amount <= Decimal("0.00"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Payment amount must be greater than zero",
            )

        paid_amount = sum(
            (
                payment.amount
                for payment in bill.payments
                if payment.status == "COMPLETED"
            ),
            Decimal("0.00"),
        )

        remaining_amount = bill.total - paid_amount

        if amount > remaining_amount:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Payment exceeds remaining bill amount",
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

        if new_paid_amount == bill.total:
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

        db.commit()
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
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Bill not found",
            )

        order = db.get(Order, bill.order_id)

        if order is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Order not found",
            )

        if order.restaurant_id != restaurant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Bill does not belong to this restaurant",
            )

        return bill