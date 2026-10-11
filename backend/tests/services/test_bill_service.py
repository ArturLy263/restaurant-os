import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.db.models.bill import Bill
from app.services.bill_service import BillService
from tests.factories import create_order
from tests.locking import assert_waits_for_order_lock


@pytest.fixture
def loaded_bill(db_session, restaurant):
    """An OPEN 12.50 bill whose state is already loaded in the session."""

    order = create_order(db_session, restaurant, status="SENT")
    bill = Bill(
        order_id=order.id,
        subtotal=Decimal("12.50"),
        total=Decimal("12.50"),
        status="OPEN",
    )
    db_session.add(bill)
    db_session.flush()

    assert bill.status == "OPEN"
    assert bill.payments == []

    return bill


def _pay(db_session, restaurant, bill, amount):
    return BillService.create_payment(
        db=db_session,
        restaurant_id=restaurant.restaurant.id,
        bill_id=bill.id,
        amount=Decimal(amount),
        method="CASH",
    )


def test_payment_rechecks_bill_status_after_taking_lock(
    db_session, restaurant, loaded_bill
):
    # Another transaction paid the bill while we waited for the lock.
    db_session.execute(
        text("UPDATE bills SET status = 'PAID' WHERE id = :id"),
        {"id": loaded_bill.id},
    )

    with pytest.raises(HTTPException) as exc_info:
        _pay(db_session, restaurant, loaded_bill, "1.00")

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Bill is not open"


def test_payment_rechecks_amount_due_after_taking_lock(
    db_session, restaurant, loaded_bill
):
    # Another transaction recorded a payment while we waited for the lock.
    db_session.execute(
        text(
            "INSERT INTO payments (id, bill_id, amount, method, status, "
            "created_at) VALUES (:id, :bill_id, 12.00, 'CASH', "
            "'COMPLETED', now())"
        ),
        {"id": uuid.uuid4(), "bill_id": loaded_bill.id},
    )

    with pytest.raises(HTTPException) as exc_info:
        _pay(db_session, restaurant, loaded_bill, "1.00")

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Payment exceeds remaining bill amount"

    # The remaining 0.50 is still accepted and closes the bill.
    _pay(db_session, restaurant, loaded_bill, "0.50")
    assert loaded_bill.status == "PAID"


@pytest.mark.parametrize(
    "call",
    [
        lambda db, ids: BillService.create_bill(
            db=db,
            restaurant_id=ids.restaurant_id,
            order_id=ids.order_id,
        ),
        lambda db, ids: BillService.create_payment(
            db=db,
            restaurant_id=ids.restaurant_id,
            bill_id=ids.bill_id,
            amount=Decimal("1.00"),
            method="CASH",
        ),
    ],
    ids=["create_bill", "create_payment"],
)
def test_bill_writes_wait_for_order_row_lock(
    db_engine,
    committed_bill,
    call,
):
    assert_waits_for_order_lock(
        db_engine,
        committed_bill.order_id,
        lambda session: call(session, committed_bill),
    )
