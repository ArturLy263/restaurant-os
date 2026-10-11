"""
Audit finding A3: cancelling an item after the bill exists must record a
negative bill adjustment instead of leaving the customer overcharged.
The original bill total is never rewritten.
"""

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

import app.services.order_service as order_service
from app.db.models.bill import BillAdjustment


# --- Before a bill exists ---


def test_cancel_before_bill_creates_no_adjustment(pos):
    order_id, burger_id, _ = pos.sent_order()

    response = pos.cancel(order_id, burger_id)

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert pos.db.query(BillAdjustment).count() == 0

    bill = pos.create_bill(order_id)
    assert bill.status_code == 201
    assert bill.json()["total"] == "2.50"
    assert bill.json()["adjustments"] == []
    assert bill.json()["amount_due"] == "2.50"
    pos.assert_bill_matches_items(bill.json()["id"])


# --- After a bill exists, no payment ---


def test_cancel_after_bill_records_negative_adjustment(pos):
    order_id, burger_id, _, bill_id = pos.billed_order()

    response = pos.cancel(order_id, burger_id, reason="Burger was cold")

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert pos.item(burger_id).status == "CANCELLED"
    assert pos.preparation(burger_id).status == "CANCELLED"

    [adjustment] = pos.adjustments(bill_id)
    assert adjustment.type == "ITEM_CANCELLATION"
    assert adjustment.status == "APPLIED"
    assert adjustment.amount == Decimal("-12.50")
    assert str(adjustment.order_item_id) == burger_id
    assert adjustment.restaurant_id == pos.restaurant.restaurant.id
    assert adjustment.user_id == pos.restaurant.user.id
    assert adjustment.reason == "Burger was cold"


def test_cancel_after_bill_preserves_original_total(pos):
    order_id, burger_id, _, bill_id = pos.billed_order()

    assert pos.cancel(order_id, burger_id).status_code == 200

    bill = pos.bill(bill_id)
    assert bill.subtotal == Decimal("15.00")
    assert bill.total == Decimal("15.00")
    assert bill.status == "OPEN"

    response = pos.get_bill(bill_id).json()
    assert response["subtotal"] == "15.00"
    assert response["total"] == "15.00"


def test_bill_response_exposes_adjusted_amounts(pos):
    order_id, burger_id, _, bill_id = pos.billed_order()

    assert pos.cancel(order_id, burger_id).status_code == 200

    response = pos.get_bill(bill_id)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == "15.00"
    assert body["adjustments_total"] == "-12.50"
    assert body["adjusted_total"] == "2.50"
    assert body["paid_total"] == "0.00"
    assert body["amount_due"] == "2.50"
    assert body["voided_at"] is None

    [adjustment] = body["adjustments"]
    assert adjustment["type"] == "ITEM_CANCELLATION"
    assert adjustment["amount"] == "-12.50"
    assert adjustment["status"] == "APPLIED"
    assert adjustment["order_item_id"] == burger_id
    assert adjustment["user_id"] == str(pos.restaurant.user.id)
    assert adjustment["reason"] == "Customer changed mind"


def test_repeated_bill_creation_returns_adjusted_bill(pos):
    order_id, burger_id, _, bill_id = pos.billed_order()

    assert pos.cancel(order_id, burger_id).status_code == 200

    response = pos.create_bill(order_id)

    assert response.status_code == 201
    assert response.json()["id"] == bill_id
    assert response.json()["total"] == "15.00"
    assert response.json()["amount_due"] == "2.50"


def test_cancel_one_item_from_multi_item_bill(pos):
    order_id, _, coke_id, bill_id = pos.billed_order()

    assert pos.cancel(order_id, coke_id).status_code == 200

    bill = pos.bill(bill_id)
    assert bill.adjustments_total == Decimal("-2.50")
    assert bill.adjusted_total == Decimal("12.50")
    assert bill.amount_due == Decimal("12.50")
    pos.assert_bill_matches_items(bill_id)


def test_cancel_every_item_after_bill_reaches_zero_due(pos):
    order_id, burger_id, coke_id, bill_id = pos.billed_order()

    assert pos.cancel(order_id, burger_id).status_code == 200
    assert pos.cancel(order_id, coke_id).status_code == 200

    bill = pos.bill(bill_id)
    assert len(bill.adjustments) == 2
    assert bill.total == Decimal("15.00")
    assert bill.adjusted_total == Decimal("0.00")
    assert bill.amount_due == Decimal("0.00")
    assert bill.status == "OPEN"
    pos.assert_bill_matches_items(bill_id)


def test_cancel_in_progress_item_after_bill_is_allowed(pos):
    order_id, burger_id, _, bill_id = pos.billed_order()
    pos.start_preparation(burger_id, "start")

    response = pos.cancel(order_id, burger_id)

    assert response.status_code == 200
    assert pos.preparation(burger_id).status == "CANCELLED"
    assert len(pos.adjustments(bill_id)) == 1
    pos.assert_bill_matches_items(bill_id)


@pytest.mark.parametrize(
    ("actions", "prep_status"),
    [
        (("start", "ready"), "READY"),
        (("start", "ready", "serve"), "SERVED"),
    ],
)
def test_cancel_ready_or_served_item_after_bill_is_rejected(
    pos, actions, prep_status
):
    order_id, burger_id, _, bill_id = pos.billed_order()
    pos.start_preparation(burger_id, *actions)

    response = pos.cancel(order_id, burger_id)

    assert response.status_code == 409
    assert prep_status in response.json()["detail"]
    assert pos.item(burger_id).status == "SENT"
    assert pos.adjustments(bill_id) == []


# --- Rejected after payment or on terminal states ---


def test_cancel_after_partial_payment_is_rejected(pos):
    order_id, burger_id, _, bill_id = pos.billed_order()
    assert pos.pay(bill_id, "10.00").status_code == 201

    response = pos.cancel(order_id, burger_id)

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Cannot cancel an order item after payment has been made"
    )

    assert pos.item(burger_id).status == "SENT"
    assert pos.item(burger_id).cancelled_at is None
    assert pos.preparation(burger_id).status == "PENDING"
    assert pos.adjustments(bill_id) == []
    assert pos.events("item.cancelled") == []

    bill = pos.bill(bill_id)
    assert bill.status == "OPEN"
    assert bill.paid_total == Decimal("10.00")
    assert bill.amount_due == Decimal("5.00")


def test_cancel_after_full_payment_is_rejected(pos):
    order_id, burger_id, _, bill_id = pos.billed_order()
    assert pos.pay(bill_id, "15.00").status_code == 201

    response = pos.cancel(order_id, burger_id)

    assert response.status_code == 409
    assert response.json()["detail"] == "Cannot cancel items in a closed order"
    assert pos.item(burger_id).status == "SENT"
    assert pos.adjustments(bill_id) == []
    assert pos.bill(bill_id).status == "PAID"


def test_cancel_on_closed_order_is_rejected(pos):
    order_id, burger_id, coke_id, bill_id = pos.billed_order()
    assert pos.cancel(order_id, burger_id).status_code == 200
    assert pos.pay(bill_id, "2.50").status_code == 201
    assert pos.order(order_id).status == "CLOSED"

    response = pos.cancel(order_id, coke_id)

    assert response.status_code == 409
    assert response.json()["detail"] == "Cannot cancel items in a closed order"
    assert pos.item(coke_id).status == "SENT"
    assert len(pos.adjustments(bill_id)) == 1


def test_cancel_on_void_bill_is_rejected(pos):
    order_id, burger_id, coke_id, bill_id = pos.billed_order()
    assert pos.cancel(order_id, burger_id).status_code == 200
    assert pos.cancel(order_id, coke_id).status_code == 200
    assert pos.void(order_id).status_code == 200
    assert pos.bill(bill_id).status == "VOID"

    response = pos.cancel(order_id, burger_id)

    assert response.status_code == 409
    assert len(pos.adjustments(bill_id)) == 2


def test_duplicate_cancellation_creates_one_adjustment(pos):
    order_id, burger_id, _, bill_id = pos.billed_order()

    first = pos.cancel(order_id, burger_id)
    second = pos.cancel(order_id, burger_id)

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"] == "Order item is already cancelled"
    assert len(pos.adjustments(bill_id)) == 1
    assert pos.bill(bill_id).amount_due == Decimal("2.50")


# --- Payment after adjustment ---


def test_paying_adjusted_amount_closes_bill_order_and_table(pos):
    order_id, burger_id, _, bill_id = pos.billed_order()
    assert pos.cancel(order_id, burger_id).status_code == 200

    response = pos.pay(bill_id, "2.50")

    assert response.status_code == 201

    bill = pos.bill(bill_id)
    assert bill.status == "PAID"
    assert bill.paid_at is not None
    assert bill.total == Decimal("15.00")
    assert bill.paid_total == Decimal("2.50")
    assert bill.amount_due == Decimal("0.00")

    order = pos.order(order_id)
    assert order.status == "CLOSED"
    assert order.closed_at is not None
    assert order.cancelled_at is None

    assert pos.table().status == "AVAILABLE"
    pos.assert_bill_matches_items(bill_id)


@pytest.mark.parametrize("amount", ["2.51", "15.00"])
def test_payment_cannot_exceed_adjusted_amount_due(pos, amount):
    order_id, burger_id, _, bill_id = pos.billed_order()
    assert pos.cancel(order_id, burger_id).status_code == 200

    response = pos.pay(bill_id, amount)

    assert response.status_code == 409
    assert response.json()["detail"] == "Payment exceeds remaining bill amount"
    assert pos.bill(bill_id).paid_total == Decimal("0.00")
    assert pos.bill(bill_id).status == "OPEN"


def test_partial_payment_of_adjusted_amount_keeps_bill_open(pos):
    order_id, _, coke_id, bill_id = pos.billed_order()
    assert pos.cancel(order_id, coke_id).status_code == 200

    assert pos.pay(bill_id, "10.00").status_code == 201

    bill = pos.bill(bill_id)
    assert bill.status == "OPEN"
    assert bill.amount_due == Decimal("2.50")

    assert pos.pay(bill_id, "2.50").status_code == 201
    assert pos.bill(bill_id).status == "PAID"


# --- Events ---


def test_cancellation_after_bill_records_events(pos):
    order_id, burger_id, _, bill_id = pos.billed_order()

    assert pos.cancel(order_id, burger_id, reason="Wrong order").status_code == 200

    [adjustment] = pos.adjustments(bill_id)

    [bill_event] = pos.events("bill.adjusted")
    assert str(bill_event.entity_id) == bill_id
    assert bill_event.user_id == pos.restaurant.user.id
    assert bill_event.event_metadata == {
        "adjustment_id": str(adjustment.id),
        "order_id": order_id,
        "order_item_id": burger_id,
        "type": "ITEM_CANCELLATION",
        "amount": "-12.50",
        "reason": "Wrong order",
        "original_total": "15.00",
        "adjusted_total": "2.50",
        "amount_due": "2.50",
    }

    [item_event] = pos.events("item.cancelled")
    assert item_event.event_metadata["bill_id"] == bill_id
    assert item_event.event_metadata["adjustment_id"] == str(adjustment.id)
    assert item_event.event_metadata["total_price"] == "12.50"

    assert len(pos.events("preparation.cancelled")) == 1


def test_cancellation_before_bill_records_no_bill_event(pos):
    order_id, burger_id, _ = pos.sent_order()

    assert pos.cancel(order_id, burger_id).status_code == 200

    assert pos.events("bill.adjusted") == []

    [item_event] = pos.events("item.cancelled")
    assert "bill_id" not in item_event.event_metadata


# --- Atomicity ---


def test_failed_adjustment_does_not_commit_cancellation(pos, monkeypatch):
    order_id, burger_id, _, bill_id = pos.billed_order()

    def fail(*args, **kwargs):
        raise RuntimeError("adjustment creation failed")

    monkeypatch.setattr(order_service, "BillAdjustment", fail)

    with pytest.raises(RuntimeError, match="adjustment creation failed"):
        pos.cancel(order_id, burger_id)

    # The request never committed; its session is discarded on error,
    # just as the real get_db dependency closes (rolls back) the session.
    pos.db.rollback()

    assert pos.item(burger_id).status == "SENT"
    assert pos.item(burger_id).cancelled_at is None
    assert pos.preparation(burger_id).status == "PENDING"
    assert pos.adjustments(bill_id) == []
    assert pos.events("item.cancelled") == []
    assert pos.events("preparation.cancelled") == []
    assert pos.bill(bill_id).amount_due == Decimal("15.00")


def test_unique_index_backstop_rolls_back_cancellation(pos):
    order_id, burger_id, _, bill_id = pos.billed_order()

    # Simulate a concurrent request that already recorded the adjustment.
    pos.db.add(
        BillAdjustment(
            restaurant_id=pos.restaurant.restaurant.id,
            bill_id=bill_id,
            order_item_id=burger_id,
            type="ITEM_CANCELLATION",
            amount=Decimal("-12.50"),
            status="APPLIED",
            reason="Concurrent cancellation",
            user_id=pos.restaurant.user.id,
        )
    )
    pos.db.commit()

    response = pos.cancel(order_id, burger_id)

    assert response.status_code == 409
    assert response.json()["detail"] == "Order item is already cancelled"
    assert pos.item(burger_id).status == "SENT"
    assert pos.preparation(burger_id).status == "PENDING"
    assert len(pos.adjustments(bill_id)) == 1
    assert pos.events("item.cancelled") == []


def test_adjustment_amount_cannot_be_zero(pos):
    order_id, burger_id, _, bill_id = pos.billed_order()

    pos.db.add(
        BillAdjustment(
            restaurant_id=pos.restaurant.restaurant.id,
            bill_id=bill_id,
            order_item_id=burger_id,
            type="ITEM_CANCELLATION",
            amount=Decimal("0.00"),
            status="APPLIED",
            reason="Invalid",
        )
    )

    with pytest.raises(
        IntegrityError,
        match="ck_bill_adjustments_amount_nonzero",
    ):
        pos.db.flush()

    pos.db.rollback()


def test_unrelated_integrity_error_is_not_reported_as_duplicate(
    pos, monkeypatch
):
    order_id, burger_id, _, bill_id = pos.billed_order()

    real_adjustment = order_service.BillAdjustment

    def zero_amount_adjustment(**kwargs):
        # Violates ck_bill_adjustments_amount_nonzero, not the
        # duplicate-cancellation unique index.
        return real_adjustment(**{**kwargs, "amount": Decimal("0.00")})

    monkeypatch.setattr(
        order_service,
        "BillAdjustment",
        zero_amount_adjustment,
    )

    with pytest.raises(
        IntegrityError,
        match="ck_bill_adjustments_amount_nonzero",
    ):
        pos.cancel(order_id, burger_id)

    # The service rolled back before re-raising.
    assert pos.item(burger_id).status == "SENT"
    assert pos.preparation(burger_id).status == "PENDING"
    assert pos.adjustments(bill_id) == []
    assert pos.events("item.cancelled") == []


# --- Zero-priced items ---


@pytest.fixture
def billed_burger_and_water(pos, water):
    """SENT order with a Burger (12.50) and free water; OPEN 12.50 bill."""

    order_id = pos.create_order().json()["id"]

    burger = pos.add_item(order_id, pos.burger)
    free_water = pos.add_item(order_id, water)
    assert burger.status_code == 201
    assert free_water.status_code == 201
    assert free_water.json()["total_price"] == 0

    assert pos.send(order_id).status_code == 200

    bill = pos.create_bill(order_id)
    assert bill.status_code == 201
    assert bill.json()["total"] == "12.50"

    return (
        order_id,
        burger.json()["id"],
        free_water.json()["id"],
        bill.json()["id"],
    )


def test_cancel_zero_priced_item_after_bill(pos, billed_burger_and_water):
    order_id, _, water_id, bill_id = billed_burger_and_water

    response = pos.cancel(order_id, water_id)

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert pos.item(water_id).status == "CANCELLED"
    assert pos.item(water_id).cancelled_at is not None
    assert pos.preparation(water_id).status == "CANCELLED"

    assert pos.adjustments(bill_id) == []
    assert pos.events("bill.adjusted") == []

    [item_event] = pos.events("item.cancelled")
    assert item_event.event_metadata["bill_id"] == bill_id
    assert "adjustment_id" not in item_event.event_metadata

    bill = pos.bill(bill_id)
    assert bill.status == "OPEN"
    assert bill.subtotal == Decimal("12.50")
    assert bill.total == Decimal("12.50")
    assert bill.adjustments_total == Decimal("0.00")
    assert bill.adjusted_total == Decimal("12.50")
    assert bill.amount_due == Decimal("12.50")
    pos.assert_bill_matches_items(bill_id)


def test_order_with_cancelled_zero_priced_item_can_be_voided(
    pos, billed_burger_and_water
):
    order_id, burger_id, water_id, bill_id = billed_burger_and_water

    assert pos.cancel(order_id, water_id).status_code == 200
    assert pos.cancel(order_id, burger_id).status_code == 200

    [adjustment] = pos.adjustments(bill_id)
    assert adjustment.amount == Decimal("-12.50")
    assert pos.bill(bill_id).amount_due == Decimal("0.00")
    pos.assert_bill_matches_items(bill_id)

    response = pos.void(order_id)

    assert response.status_code == 200
    assert pos.order(order_id).status == "CANCELLED"
    assert pos.bill(bill_id).status == "VOID"
    assert pos.bill(bill_id).total == Decimal("12.50")
    assert pos.table().status == "AVAILABLE"


def test_paying_after_cancelling_zero_priced_item(
    pos, billed_burger_and_water
):
    order_id, _, water_id, bill_id = billed_burger_and_water

    assert pos.cancel(order_id, water_id).status_code == 200

    assert pos.pay(bill_id, "12.50").status_code == 201
    assert pos.bill(bill_id).status == "PAID"
    assert pos.order(order_id).status == "CLOSED"


# --- Regression ---


def test_normal_order_send_bill_pay_flow_is_unchanged(pos):
    order_id, _, _, bill_id = pos.billed_order()

    bill = pos.get_bill(bill_id).json()
    assert bill["total"] == "15.00"
    assert bill["adjustments_total"] == "0.00"
    assert bill["adjusted_total"] == "15.00"
    assert bill["amount_due"] == "15.00"
    assert bill["adjustments"] == []

    assert pos.pay(bill_id, "10.00").status_code == 201
    assert pos.pay(bill_id, "5.00").status_code == 201

    assert pos.bill(bill_id).status == "PAID"
    assert pos.order(order_id).status == "CLOSED"
    assert pos.table().status == "AVAILABLE"
    pos.assert_bill_matches_items(bill_id)
