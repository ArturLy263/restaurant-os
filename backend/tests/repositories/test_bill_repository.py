import uuid
from decimal import Decimal

from sqlalchemy import text

from app.db.models.bill import Bill
from app.repositories.bill_repository import BillRepository
from tests.factories import create_order


def _create_bill(db, order, total="15.00"):
    bill = Bill(
        order_id=order.id,
        subtotal=Decimal(total),
        total=Decimal(total),
        status="OPEN",
    )
    db.add(bill)
    db.flush()

    return bill


def test_returns_bill_of_order(db_session, restaurant):
    order = create_order(db_session, restaurant, status="SENT")
    bill = _create_bill(db_session, order)
    db_session.expunge_all()

    found = BillRepository(db_session).get_for_order(order.id)

    assert found is not None
    assert found.id == bill.id
    assert found.payments == []
    assert found.adjustments == []


def test_returns_none_when_order_has_no_bill(db_session, restaurant):
    order = create_order(db_session, restaurant)

    assert BillRepository(db_session).get_for_order(order.id) is None
    assert BillRepository(db_session).get_for_order(uuid.uuid4()) is None


def test_reloads_bill_and_payments_already_in_session(db_session, restaurant):
    order = create_order(db_session, restaurant, status="SENT")
    bill = _create_bill(db_session, order)
    assert bill.payments == []

    # Changes committed by another transaction while we waited for the
    # order lock must be visible, not the copies already in the session.
    db_session.execute(
        text("UPDATE bills SET status = 'PAID' WHERE id = :id"),
        {"id": bill.id},
    )
    db_session.execute(
        text(
            "INSERT INTO payments (id, bill_id, amount, method, status, "
            "created_at) VALUES (:id, :bill_id, 15.00, 'CASH', "
            "'COMPLETED', now())"
        ),
        {"id": uuid.uuid4(), "bill_id": bill.id},
    )

    found = BillRepository(db_session).get_for_order(order.id)

    assert found is bill
    assert found.status == "PAID"
    assert [payment.amount for payment in found.payments] == [
        Decimal("15.00")
    ]
    assert found.amount_due == Decimal("0.00")

