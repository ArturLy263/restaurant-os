"""
Audit finding A5: an order with nothing left to bill must be closable
through an explicit void, instead of staying SENT/OPEN with the table
OCCUPIED forever. Nothing financial is deleted.
"""

from decimal import Decimal

import pytest

from app.db.models.bill import Bill, Payment


def cancel_all(pos, order_id, *item_ids, bill_id=None):
    """Cancel items; through the bill once one has been issued."""

    for item_id in item_ids:
        if bill_id is None:
            response = pos.cancel(order_id, item_id)
        else:
            response = pos.cancel_billed(bill_id, item_id)

        assert response.status_code == 200


# --- Bill creation with nothing billable ---


def test_bill_creation_rejected_when_all_items_cancelled(pos):
    order_id, burger_id, coke_id = pos.sent_order()
    cancel_all(pos, order_id, burger_id, coke_id)

    response = pos.create_bill(order_id)

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Order has no billable items; void the order instead"
    )
    assert pos.db.query(Bill).filter(Bill.order_id == order_id).count() == 0
    assert pos.order(order_id).status == "SENT"
    assert pos.table().status == "OCCUPIED"


# --- Void without a bill ---


def test_void_sent_order_without_bill(pos):
    order_id, burger_id, coke_id = pos.sent_order()
    cancel_all(pos, order_id, burger_id, coke_id)

    response = pos.void(order_id, reason="Guest left")

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"

    order = pos.order(order_id)
    assert order.status == "CANCELLED"
    assert order.cancelled_at is not None
    assert order.closed_at is None
    assert pos.table().status == "AVAILABLE"


def test_void_open_order_with_cancelled_items(pos):
    order_id, burger_id, coke_id = pos.open_order()
    cancel_all(pos, order_id, burger_id, coke_id)

    response = pos.void(order_id)

    assert response.status_code == 200
    assert pos.order(order_id).status == "CANCELLED"
    assert pos.table().status == "AVAILABLE"


def test_void_order_without_items(pos):
    created = pos.create_order()
    order_id = created.json()["id"]

    response = pos.void(order_id, reason="Table opened by mistake")

    assert response.status_code == 200
    assert pos.order(order_id).status == "CANCELLED"
    assert pos.table().status == "AVAILABLE"


def test_table_can_be_reused_after_void(pos):
    order_id, burger_id, coke_id = pos.sent_order()
    cancel_all(pos, order_id, burger_id, coke_id)
    assert pos.void(order_id).status_code == 200

    response = pos.create_order()

    assert response.status_code == 201
    assert pos.table().status == "OCCUPIED"


# --- Void with an OPEN zero-balance bill ---


def test_void_order_with_zero_balance_bill(pos):
    order_id, burger_id, coke_id, bill_id = pos.billed_order()
    cancel_all(pos, order_id, burger_id, coke_id, bill_id=bill_id)

    response = pos.void(order_id, reason="Guest left")

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"

    bill = pos.bill(bill_id)
    assert bill.status == "VOID"
    assert bill.voided_at is not None
    assert bill.paid_at is None

    order = pos.order(order_id)
    assert order.status == "CANCELLED"
    assert order.cancelled_at is not None
    assert order.closed_at is None

    assert pos.table().status == "AVAILABLE"


def test_void_preserves_bill_history(pos):
    order_id, burger_id, coke_id, bill_id = pos.billed_order()
    cancel_all(pos, order_id, burger_id, coke_id, bill_id=bill_id)

    assert pos.void(order_id).status_code == 200

    bill = pos.bill(bill_id)
    assert bill.subtotal == Decimal("15.00")
    assert bill.total == Decimal("15.00")
    assert len(pos.adjustments(bill_id)) == 2
    assert bill.adjustments_total == Decimal("-15.00")
    assert bill.amount_due == Decimal("0.00")
    assert len(pos.order(order_id).items) == 2

    response = pos.get_bill(bill_id)
    assert response.status_code == 200
    assert response.json()["status"] == "VOID"
    assert response.json()["total"] == "15.00"
    assert response.json()["voided_at"] is not None
    assert len(response.json()["adjustments"]) == 2


def test_void_bill_cannot_be_paid(pos):
    order_id, burger_id, coke_id, bill_id = pos.billed_order()
    cancel_all(pos, order_id, burger_id, coke_id, bill_id=bill_id)
    assert pos.void(order_id).status_code == 200

    response = pos.pay(bill_id, "0.01")

    assert response.status_code == 409
    assert response.json()["detail"] == "Bill is not open"
    assert pos.db.query(Payment).filter(Payment.bill_id == bill_id).count() == 0


# --- Rejections ---


def test_void_with_active_items_is_rejected(pos):
    order_id, burger_id, _ = pos.sent_order()
    assert pos.cancel(order_id, burger_id).status_code == 200

    response = pos.void(order_id)

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Order still has 1 active item(s); cancel them before voiding the order"
    )
    assert pos.order(order_id).status == "SENT"
    assert pos.table().status == "OCCUPIED"


def test_void_with_partial_payment_is_rejected(pos):
    order_id, _, _, bill_id = pos.billed_order()
    assert pos.pay(bill_id, "10.00").status_code == 201

    response = pos.void(order_id)

    assert response.status_code == 409
    assert pos.order(order_id).status == "SENT"
    assert pos.bill(bill_id).status == "OPEN"
    assert pos.table().status == "OCCUPIED"


def test_void_with_paid_bill_is_rejected(pos):
    order_id, _, _, bill_id = pos.billed_order()
    assert pos.pay(bill_id, "15.00").status_code == 201

    response = pos.void(order_id)

    assert response.status_code == 409
    assert response.json()["detail"] == "Order is already closed"
    assert pos.bill(bill_id).status == "PAID"


def test_void_closed_order_is_rejected(pos):
    order_id, burger_id, _, bill_id = pos.billed_order()
    assert pos.cancel_billed(bill_id, burger_id).status_code == 200
    assert pos.pay(bill_id, "2.50").status_code == 201

    response = pos.void(order_id)

    assert response.status_code == 409
    assert response.json()["detail"] == "Order is already closed"
    assert pos.order(order_id).status == "CLOSED"
    assert pos.order(order_id).cancelled_at is None


def test_void_twice_is_rejected(pos):
    order_id, burger_id, coke_id, bill_id = pos.billed_order()
    cancel_all(pos, order_id, burger_id, coke_id, bill_id=bill_id)
    assert pos.void(order_id).status_code == 200

    response = pos.void(order_id)

    assert response.status_code == 409
    assert response.json()["detail"] == "Order is already cancelled"
    assert len(pos.events("order.cancelled")) == 1
    assert len(pos.events("bill.voided")) == 1


@pytest.mark.parametrize("reason", ["", "x" * 501])
def test_void_requires_reason(pos, reason):
    order_id, burger_id, coke_id = pos.sent_order()
    cancel_all(pos, order_id, burger_id, coke_id)

    response = pos.void(order_id, reason=reason)

    assert response.status_code == 422
    assert pos.order(order_id).status == "SENT"


def test_cancelled_order_rejects_further_changes(pos):
    order_id, burger_id, coke_id = pos.sent_order()
    cancel_all(pos, order_id, burger_id, coke_id)
    assert pos.void(order_id).status_code == 200

    assert pos.add_item(order_id, pos.burger).status_code == 409
    assert pos.send(order_id).status_code == 409
    assert pos.create_bill(order_id).status_code == 409

    response = pos.cancel(order_id, burger_id)
    assert response.status_code == 409
    assert response.json()["detail"] == "Cannot cancel items in a cancelled order"


# --- Events ---


def test_void_records_events(pos):
    order_id, burger_id, coke_id, bill_id = pos.billed_order()
    cancel_all(pos, order_id, burger_id, coke_id, bill_id=bill_id)

    assert pos.void(order_id, reason="Guest left").status_code == 200

    [bill_event] = pos.events("bill.voided")
    assert str(bill_event.entity_id) == bill_id
    assert bill_event.user_id == pos.restaurant.user.id
    assert bill_event.event_metadata == {
        "order_id": order_id,
        "order_created_by": str(pos.restaurant.user.id),
        "original_total": "15.00",
        "adjustments_total": "-15.00",
        "adjusted_total": "0.00",
        "reason": "Guest left",
    }

    [order_event] = pos.events("order.cancelled")
    assert str(order_event.entity_id) == order_id
    assert order_event.user_id == pos.restaurant.user.id
    assert order_event.event_metadata == {
        "order_created_by": str(pos.restaurant.user.id),
        "previous_status": "SENT",
        "bill_id": bill_id,
        "table_id": str(pos.restaurant.table.id),
        "reason": "Guest left",
    }

    assert len(pos.events("bill.adjusted")) == 2


def test_void_without_bill_records_no_bill_event(pos):
    order_id, burger_id, coke_id = pos.open_order()
    cancel_all(pos, order_id, burger_id, coke_id)

    assert pos.void(order_id).status_code == 200

    assert pos.events("bill.voided") == []

    [order_event] = pos.events("order.cancelled")
    assert order_event.event_metadata["previous_status"] == "OPEN"
    assert order_event.event_metadata["bill_id"] is None
