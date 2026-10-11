"""
A6: incremental order sending.

Items can be added to an order that was already sent and sent in further
rounds. Each send creates preparations only for items not yet sent, and an
item added after the bill exists is recorded as a positive ITEM_ADDITION
adjustment. The original bill total is never rewritten.
"""

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

import app.services.order_service as order_service
from app.db.models.preparation import Preparation
from app.db.models.product import Product
from app.db.models.station import Station
from app.db.models.user import User
from tests.factories import auth_headers, create_restaurant


@pytest.fixture
def bar(pos):
    station = Station(
        restaurant_id=pos.restaurant.restaurant.id,
        name="Bar",
        type="BAR",
    )
    pos.db.add(station)
    pos.db.flush()

    return station


@pytest.fixture
def beer(pos, bar):
    product = Product(
        category_id=pos.restaurant.category.id,
        name="Beer",
        price=Decimal("4.00"),
        station_id=bar.id,
    )
    pos.db.add(product)
    pos.db.flush()

    return product


def added(pos, order_id, product):
    response = pos.add_item(order_id, product)
    assert response.status_code == 201

    return response.json()["id"]


# --- Adding and sending further rounds ---


def test_add_item_to_sent_order(pos):
    order_id, _, _ = pos.sent_order()

    response = pos.add_item(order_id, pos.burger)

    assert response.status_code == 201
    assert response.json()["status"] == "ADDED"
    assert pos.order(order_id).status == "SENT"


def test_second_send_creates_preparations_only_for_new_items(pos):
    order_id, burger_id, coke_id = pos.sent_order()
    first_round = {
        preparation.id: preparation.order_item_id
        for preparation in pos.preparations(order_id)
    }

    new_id = added(pos, order_id, pos.burger)
    response = pos.send(order_id)

    assert response.status_code == 200
    assert response.json()["status"] == "SENT"

    preparations = pos.preparations(order_id)
    assert len(preparations) == 3

    # Earlier preparations are untouched; the new item has exactly one.
    assert {
        preparation.id: preparation.order_item_id
        for preparation in preparations
        if str(preparation.order_item_id) != new_id
    } == first_round
    assert pos.preparation(burger_id).status == "PENDING"
    assert pos.preparation(coke_id).status == "PENDING"
    assert pos.preparation(new_id).status == "PENDING"
    assert pos.item(new_id).status == "SENT"
    assert pos.item(new_id).sent_at is not None


def test_send_without_new_items_is_rejected(pos):
    order_id, _, _ = pos.sent_order()

    response = pos.send(order_id)

    assert response.status_code == 409
    assert response.json()["detail"] == "Order has no items ready to send"
    assert len(pos.preparations(order_id)) == 2
    assert len(pos.events("order.sent")) == 1


def test_resend_keeps_progress_of_earlier_preparations(pos):
    order_id, burger_id, _ = pos.sent_order()
    pos.start_preparation(burger_id, "start", "ready")

    new_id = added(pos, order_id, pos.burger)
    assert pos.send(order_id).status_code == 200

    assert pos.preparation(burger_id).status == "READY"
    assert pos.preparation(new_id).status == "PENDING"


def test_new_items_are_routed_to_their_product_station(pos, beer, bar):
    order_id, _, _ = pos.sent_order()

    beer_id = added(pos, order_id, beer)
    burger_id = added(pos, order_id, pos.burger)
    assert pos.send(order_id).status_code == 200

    assert pos.preparation(beer_id).station_id == bar.id
    assert pos.preparation(burger_id).station_id == pos.restaurant.station.id


def test_send_events_identify_each_round(pos):
    order_id, burger_id, coke_id = pos.sent_order()

    new_id = added(pos, order_id, pos.burger)
    assert pos.send(order_id).status_code == 200

    events = sorted(
        pos.events("order.sent"),
        key=lambda event: event.event_metadata["initial"],
        reverse=True,
    )
    assert len(events) == 2

    first, second = (event.event_metadata for event in events)
    assert first["initial"] is True
    assert first["items_count"] == 2
    assert sorted(first["item_ids"]) == sorted([burger_id, coke_id])
    assert second["initial"] is False
    assert second["items_count"] == 1
    assert second["item_ids"] == [new_id]


def test_unsent_item_on_sent_order_can_be_cancelled(pos):
    order_id, _, _ = pos.sent_order()
    new_id = added(pos, order_id, pos.burger)

    assert pos.cancel(order_id, new_id).status_code == 200

    assert pos.item(new_id).status == "CANCELLED"
    assert new_id not in {
        str(preparation.order_item_id)
        for preparation in pos.preparations(order_id)
    }
    assert pos.send(order_id).status_code == 409


def test_failed_send_commits_nothing_from_the_round(pos, beer):
    order_id, _, _ = pos.sent_order()
    burger_id = added(pos, order_id, pos.burger)
    beer_id = added(pos, order_id, beer)

    beer.is_active = False
    pos.db.flush()

    response = pos.send(order_id)

    assert response.status_code == 409
    assert response.json()["detail"] == f"Product is not active: {beer.id}"

    # The real get_db discards the failed request's session.
    pos.db.rollback()

    assert pos.item(burger_id).status == "ADDED"
    assert pos.item(beer_id).status == "ADDED"
    assert len(pos.preparations(order_id)) == 2
    assert len(pos.events("order.sent")) == 1


def test_preparation_constraint_backstop_rolls_back_send(pos):
    order_id, _, _ = pos.sent_order()
    new_id = added(pos, order_id, pos.burger)

    # Simulate a concurrent send that already prepared the item.
    pos.db.add(
        Preparation(
            order_item_id=new_id,
            station_id=pos.restaurant.station.id,
            status="PENDING",
        )
    )
    pos.db.commit()

    response = pos.send(order_id)

    assert response.status_code == 409
    assert response.json()["detail"] == "Order item has already been sent"
    assert pos.item(new_id).status == "ADDED"
    assert len(pos.preparations(order_id)) == 3
    assert len(pos.events("order.sent")) == 1


def test_order_item_cannot_have_two_preparations(pos):
    _, burger_id, _ = pos.sent_order()

    pos.db.add(
        Preparation(
            order_item_id=burger_id,
            station_id=pos.restaurant.station.id,
            status="PENDING",
        )
    )

    with pytest.raises(IntegrityError, match="uq_preparations_order_item_id"):
        pos.db.flush()

    pos.db.rollback()


# --- Items added after the bill ---


def test_item_added_after_bill_records_positive_adjustment(pos):
    order_id, _, _, bill_id = pos.billed_order()

    new_id = added(pos, order_id, pos.burger)

    [adjustment] = pos.adjustments(bill_id)
    assert adjustment.type == "ITEM_ADDITION"
    assert adjustment.amount == Decimal("12.50")
    assert adjustment.status == "APPLIED"
    assert str(adjustment.order_item_id) == new_id
    assert adjustment.user_id == pos.restaurant.user.id
    assert adjustment.reason == "Item added after bill was created"

    bill = pos.bill(bill_id)
    assert bill.total == Decimal("15.00")
    assert bill.subtotal == Decimal("15.00")
    assert bill.adjusted_total == Decimal("27.50")
    assert bill.amount_due == Decimal("27.50")
    pos.assert_bill_matches_items(bill_id)


def test_bill_response_exposes_item_addition(pos):
    order_id, _, _, bill_id = pos.billed_order()
    new_id = added(pos, order_id, pos.burger)

    bill = pos.get_bill(bill_id).json()

    assert bill["total"] == "15.00"
    assert bill["adjustments_total"] == "12.50"
    assert bill["adjusted_total"] == "27.50"
    assert bill["amount_due"] == "27.50"
    assert [
        (adjustment["type"], adjustment["amount"], adjustment["order_item_id"])
        for adjustment in bill["adjustments"]
    ] == [("ITEM_ADDITION", "12.50", new_id)]


def test_repeated_bill_creation_returns_bill_with_addition(pos):
    order_id, _, _, bill_id = pos.billed_order()
    added(pos, order_id, pos.burger)

    response = pos.create_bill(order_id)

    assert response.json()["id"] == bill_id
    assert response.json()["total"] == "15.00"
    assert response.json()["adjusted_total"] == "27.50"


def test_item_added_after_bill_records_events(pos):
    order_id, _, _, bill_id = pos.billed_order()
    new_id = added(pos, order_id, pos.burger)
    [adjustment] = pos.adjustments(bill_id)

    [bill_event] = pos.events("bill.adjusted")
    assert str(bill_event.entity_id) == bill_id
    assert bill_event.user_id == pos.restaurant.user.id
    assert bill_event.event_metadata == {
        "adjustment_id": str(adjustment.id),
        "order_id": order_id,
        "order_item_id": new_id,
        "type": "ITEM_ADDITION",
        "amount": "12.50",
        "reason": "Item added after bill was created",
        "original_total": "15.00",
        "adjusted_total": "27.50",
        "amount_due": "27.50",
    }

    [item_event] = [
        event
        for event in pos.events("item.added")
        if str(event.entity_id) == new_id
    ]
    assert item_event.event_metadata["bill_id"] == bill_id
    assert item_event.event_metadata["adjustment_id"] == str(adjustment.id)


def test_item_added_before_bill_records_no_bill_event(pos):
    order_id, _, _ = pos.sent_order()
    new_id = added(pos, order_id, pos.burger)

    assert pos.events("bill.adjusted") == []

    [item_event] = [
        event
        for event in pos.events("item.added")
        if str(event.entity_id) == new_id
    ]
    assert "bill_id" not in item_event.event_metadata
    assert "adjustment_id" not in item_event.event_metadata


def test_unsent_item_added_before_bill_is_in_original_total(pos):
    order_id, _, _ = pos.sent_order()
    new_id = added(pos, order_id, pos.burger)

    response = pos.create_bill(order_id)

    assert response.status_code == 201
    assert response.json()["total"] == "27.50"
    assert response.json()["adjustments"] == []
    assert pos.item(new_id).status == "ADDED"
    pos.assert_bill_matches_items(response.json()["id"])


def test_zero_priced_item_after_bill_creates_no_adjustment(pos, water):
    order_id, _, _, bill_id = pos.billed_order()

    water_id = added(pos, order_id, water)

    assert pos.adjustments(bill_id) == []
    assert pos.events("bill.adjusted") == []
    assert pos.bill(bill_id).amount_due == Decimal("15.00")

    [item_event] = [
        event
        for event in pos.events("item.added")
        if str(event.entity_id) == water_id
    ]
    assert item_event.event_metadata["bill_id"] == bill_id
    assert "adjustment_id" not in item_event.event_metadata
    pos.assert_bill_matches_items(bill_id)


def test_cancelling_added_item_offsets_addition(pos):
    order_id, _, _, bill_id = pos.billed_order()
    new_id = added(pos, order_id, pos.burger)

    assert pos.cancel(order_id, new_id).status_code == 200

    assert sorted(
        (adjustment.type, adjustment.amount)
        for adjustment in pos.adjustments(bill_id)
    ) == [
        ("ITEM_ADDITION", Decimal("12.50")),
        ("ITEM_CANCELLATION", Decimal("-12.50")),
    ]
    assert pos.bill(bill_id).adjusted_total == Decimal("15.00")
    assert pos.bill(bill_id).total == Decimal("15.00")
    pos.assert_bill_matches_items(bill_id)


# --- Partial payments and closed orders ---


def test_item_added_after_partial_payment_increases_amount_due(pos):
    order_id, _, _, bill_id = pos.billed_order()
    assert pos.pay(bill_id, "10.00").status_code == 201

    added(pos, order_id, pos.burger)

    bill = pos.bill(bill_id)
    assert [payment.amount for payment in bill.payments] == [Decimal("10.00")]
    assert bill.paid_total == Decimal("10.00")
    assert bill.adjusted_total == Decimal("27.50")
    assert bill.amount_due == Decimal("17.50")
    assert bill.status == "OPEN"
    pos.assert_bill_matches_items(bill_id)


def test_new_amount_due_cannot_be_overpaid(pos):
    order_id, _, _, bill_id = pos.billed_order()
    assert pos.pay(bill_id, "10.00").status_code == 201
    added(pos, order_id, pos.burger)

    response = pos.pay(bill_id, "17.51")

    assert response.status_code == 409
    assert response.json()["detail"] == "Payment exceeds remaining bill amount"
    assert pos.bill(bill_id).paid_total == Decimal("10.00")


def test_sending_unsent_item_allows_final_payment(pos):
    order_id, _, _, bill_id = pos.billed_order()
    assert pos.pay(bill_id, "10.00").status_code == 201
    added(pos, order_id, pos.burger)
    assert pos.pay(bill_id, "17.50").status_code == 409

    assert pos.send(order_id).status_code == 200
    assert pos.pay(bill_id, "17.50").status_code == 201

    assert pos.bill(bill_id).status == "PAID"
    assert pos.order(order_id).status == "CLOSED"
    assert pos.table().status == "AVAILABLE"
    pos.assert_bill_matches_items(bill_id)


def test_added_item_cannot_be_cancelled_after_partial_payment(pos):
    order_id, _, _, bill_id = pos.billed_order()
    new_id = added(pos, order_id, pos.burger)
    assert pos.pay(bill_id, "5.00").status_code == 201

    response = pos.cancel(order_id, new_id)

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Cannot cancel an order item after payment has been made"
    )
    assert pos.item(new_id).status == "ADDED"
    assert len(pos.adjustments(bill_id)) == 1


# --- Final payment with unsent items ---


UNSENT_ITEMS_DETAIL = (
    "Order has unsent items; send or cancel them before the final payment"
)


def assert_final_payment_rejected(pos, order_id, bill_id, amount):
    """Paying off the bill is refused and nothing changes."""

    payments_before = sorted(
        (payment.id, payment.amount) for payment in pos.bill(bill_id).payments
    )
    payment_events_before = len(pos.events("payment.completed"))

    response = pos.pay(bill_id, amount)

    assert response.status_code == 409
    assert response.json()["detail"] == UNSENT_ITEMS_DETAIL

    bill = pos.bill(bill_id)
    assert bill.status == "OPEN"
    assert bill.paid_at is None
    assert sorted(
        (payment.id, payment.amount) for payment in bill.payments
    ) == payments_before
    assert bill.amount_due == Decimal(amount)

    order = pos.order(order_id)
    assert order.status == "SENT"
    assert order.closed_at is None
    assert pos.table().status == "OCCUPIED"

    assert len(pos.events("payment.completed")) == payment_events_before
    assert pos.events("order.closed") == []


def test_final_payment_rejected_while_item_added_after_bill_is_unsent(pos):
    order_id, _, _, bill_id = pos.billed_order()
    new_id = added(pos, order_id, pos.burger)
    assert pos.pay(bill_id, "10.00").status_code == 201

    assert_final_payment_rejected(pos, order_id, bill_id, "17.50")
    assert pos.item(new_id).status == "ADDED"


def test_final_payment_rejected_while_billed_item_is_unsent(pos):
    order_id, _, _ = pos.sent_order()
    added(pos, order_id, pos.burger)

    bill = pos.create_bill(order_id)
    assert bill.json()["total"] == "27.50"

    assert_final_payment_rejected(pos, order_id, bill.json()["id"], "27.50")


def test_zero_priced_unsent_item_blocks_final_payment(pos, water):
    order_id, _, _, bill_id = pos.billed_order()
    added(pos, order_id, water)

    assert_final_payment_rejected(pos, order_id, bill_id, "15.00")


def test_partial_payment_allowed_while_item_is_unsent(pos):
    order_id, _, _, bill_id = pos.billed_order()
    new_id = added(pos, order_id, pos.burger)

    assert pos.pay(bill_id, "27.49").status_code == 201

    bill = pos.bill(bill_id)
    assert bill.status == "OPEN"
    assert bill.amount_due == Decimal("0.01")
    assert pos.order(order_id).status == "SENT"
    assert pos.item(new_id).status == "ADDED"


def test_cancelling_unsent_item_allows_final_payment(pos):
    order_id, _, _, bill_id = pos.billed_order()
    new_id = added(pos, order_id, pos.burger)

    assert pos.cancel(order_id, new_id).status_code == 200
    assert pos.pay(bill_id, "15.00").status_code == 201

    assert pos.bill(bill_id).status == "PAID"
    assert pos.order(order_id).status == "CLOSED"
    assert pos.table().status == "AVAILABLE"
    pos.assert_bill_matches_items(bill_id)


def test_cannot_add_item_to_closed_order(pos):
    order_id, _, _, bill_id = pos.billed_order()
    assert pos.pay(bill_id, "15.00").status_code == 201

    response = pos.add_item(order_id, pos.burger)

    assert response.status_code == 409
    assert response.json()["detail"] == "Order is not open"
    assert len(pos.items(order_id)) == 2
    assert pos.adjustments(bill_id) == []
    assert pos.send(order_id).status_code == 409


def test_cannot_add_item_to_cancelled_order_with_void_bill(pos):
    order_id, burger_id, coke_id, bill_id = pos.billed_order()
    assert pos.cancel(order_id, burger_id).status_code == 200
    assert pos.cancel(order_id, coke_id).status_code == 200
    assert pos.void(order_id).status_code == 200

    response = pos.add_item(order_id, pos.burger)

    assert response.status_code == 409
    assert response.json()["detail"] == "Order is not open"
    assert len(pos.items(order_id)) == 2
    assert {
        adjustment.type for adjustment in pos.adjustments(bill_id)
    } == {"ITEM_CANCELLATION"}


def test_multi_round_order_through_payment(pos, beer):
    order_id, _, _ = pos.sent_order()

    added(pos, order_id, beer)
    assert pos.send(order_id).status_code == 200

    bill = pos.create_bill(order_id)
    assert bill.status_code == 201
    bill_id = bill.json()["id"]
    assert bill.json()["total"] == "19.00"

    added(pos, order_id, pos.burger)
    assert pos.send(order_id).status_code == 200

    preparations = pos.preparations(order_id)
    assert len(preparations) == 4
    assert len({preparation.order_item_id for preparation in preparations}) == 4
    pos.assert_bill_matches_items(bill_id)

    assert pos.bill(bill_id).amount_due == Decimal("31.50")
    assert pos.pay(bill_id, "31.50").status_code == 201
    assert pos.order(order_id).status == "CLOSED"
    assert pos.table().status == "AVAILABLE"


# --- Tenant isolation and ownership ---


def test_other_restaurant_cannot_add_or_send(pos, db_session):
    order_id, _, _ = pos.sent_order()
    other = create_restaurant(db_session, name="Other Restaurant")
    headers = auth_headers(other.user)

    add = pos.client.post(
        f"/api/v1/orders/{order_id}/items",
        json={"product_id": str(other.product.id), "quantity": 1},
        headers=headers,
    )
    send = pos.client.post(f"/api/v1/orders/{order_id}/send", headers=headers)

    assert add.status_code == 404
    assert send.status_code == 404
    assert len(pos.items(order_id)) == 2


def test_other_user_cannot_add_to_sent_order(pos, db_session):
    order_id, _, _ = pos.sent_order()
    colleague = User(
        restaurant_id=pos.restaurant.restaurant.id,
        role_id=pos.restaurant.role.id,
        name="Colleague",
        email=f"colleague-{pos.restaurant.restaurant.id}@example.com",
        password_hash="unused",
    )
    db_session.add(colleague)
    db_session.flush()

    response = pos.client.post(
        f"/api/v1/orders/{order_id}/items",
        json={"product_id": str(pos.burger.id), "quantity": 1},
        headers=auth_headers(colleague),
    )

    assert response.status_code == 403
    assert len(pos.items(order_id)) == 2


# --- Atomicity ---


def test_failed_addition_adjustment_does_not_commit_item(pos, monkeypatch):
    order_id, _, _, bill_id = pos.billed_order()

    def fail(*args, **kwargs):
        raise RuntimeError("adjustment creation failed")

    monkeypatch.setattr(order_service, "BillAdjustment", fail)

    with pytest.raises(RuntimeError, match="adjustment creation failed"):
        pos.add_item(order_id, pos.burger)

    # The real get_db discards the failed request's session.
    pos.db.rollback()

    assert len(pos.items(order_id)) == 2
    assert pos.adjustments(bill_id) == []
    assert len(pos.events("item.added")) == 2
    assert pos.events("bill.adjusted") == []
    assert pos.bill(bill_id).amount_due == Decimal("15.00")


def test_rejected_addition_to_non_open_bill_writes_nothing(pos):
    order_id, _, _, bill_id = pos.billed_order()

    # Defensive check: an order that is still SENT must not take items onto
    # a bill that is no longer OPEN.
    pos.bill(bill_id).status = "PAID"
    pos.db.flush()

    response = pos.add_item(order_id, pos.burger)

    assert response.status_code == 409
    assert response.json()["detail"] == "Cannot add an item to a paid bill"
    assert len(pos.items(order_id)) == 2
    assert pos.adjustments(bill_id) == []
