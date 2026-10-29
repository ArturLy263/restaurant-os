"""
Discarding an unfinished preparation of a fully paid order.

Payment is not service: a CLOSED order's preparations stay in the queue
until served, or until a manager discards them (e.g. the guest left).
Discarding cancels only the preparation; the order item and the paid bill
are unchanged. The reason is audit history, never part of outbox events.
"""

import uuid
from decimal import Decimal

import pytest

from app.api.idempotency import IDEMPOTENCY_KEY_HEADER, REPLAYED_HEADER
from app.db.models.outbox_event import OutboxEvent
from tests.factories import auth_headers, create_restaurant, create_user


REASON = "Guest left before the dish was served"


def discard(pos, preparation_id, reason=REASON, headers=None, key=None):
    headers = dict(headers or pos.headers)
    if key is not None:
        headers[IDEMPOTENCY_KEY_HEADER] = key

    return pos.client.post(
        f"/api/v1/preparations/{preparation_id}/discard",
        json={"reason": reason},
        headers=headers,
    )


def status_changes(pos):
    pos.db.expire_all()
    return (
        pos.db.query(OutboxEvent)
        .filter(
            OutboxEvent.restaurant_id == pos.restaurant.restaurant.id,
            OutboxEvent.event_type == "preparation.status_changed",
        )
        .order_by(OutboxEvent.created_at, OutboxEvent.id)
        .all()
    )


def paid_order(pos, *burger_actions):
    """A CLOSED, fully paid order: Burger after `burger_actions`, Coke
    PENDING."""

    order_id, burger_id, coke_id, bill_id = pos.billed_order()
    if burger_actions:
        pos.start_preparation(burger_id, *burger_actions)
    assert pos.pay(bill_id, "15.00").status_code == 201
    assert pos.order(order_id).status == "CLOSED"

    return order_id, burger_id, coke_id, bill_id


@pytest.mark.parametrize(
    "burger_actions, previous_status, version",
    [
        ((), "PENDING", 1),
        (("start",), "IN_PROGRESS", 1),
        (("start", "ready"), "READY", 2),
    ],
)
def test_manager_discards_an_unfinished_preparation_of_a_paid_order(
    pos, burger_actions, previous_status, version
):
    order_id, burger_id, coke_id, bill_id = paid_order(pos, *burger_actions)
    preparation = pos.preparation(burger_id)
    changes_before = len(status_changes(pos))
    bill_before = pos.bill(bill_id)
    paid_total, adjusted_total = bill_before.paid_total, bill_before.adjusted_total

    response = discard(pos, preparation.id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(preparation.id)
    assert body["status"] == "CANCELLED"
    assert body["cancelled_at"] is not None
    assert body["served_at"] is None

    # Only the preparation changed: no refund, no bill adjustment.
    assert pos.preparation(burger_id).status == "CANCELLED"
    assert pos.item(burger_id).status == "SENT"
    assert pos.order(order_id).status == "CLOSED"
    bill = pos.bill(bill_id)
    assert bill.status == "PAID"
    assert (bill.paid_total, bill.adjusted_total) == (paid_total, adjusted_total)
    assert pos.adjustments(bill_id) == []
    assert pos.preparation(coke_id).status == "PENDING"

    [event] = pos.events("preparation.discarded")
    assert event.user_id == pos.restaurant.user.id
    assert event.entity_id == preparation.id
    assert event.event_metadata == {
        "status": "CANCELLED",
        "previous_status": previous_status,
        "reason": REASON,
        "order_item_id": burger_id,
        "order_id": order_id,
        "station_id": str(pos.restaurant.station.id),
        "order_created_by": str(pos.restaurant.user.id),
    }

    changes = status_changes(pos)
    assert len(changes) == changes_before + 1
    outbox_event = changes[-1]
    assert outbox_event.schema_version == version
    assert outbox_event.aggregate_id == uuid.UUID(order_id)
    assert outbox_event.payload == {
        "preparation_id": str(preparation.id),
        "order_id": order_id,
        "order_item_id": burger_id,
        "station_id": str(pos.restaurant.station.id),
        "previous_status": previous_status,
        "status": "CANCELLED",
    }
    assert REASON not in str(outbox_event.payload)


def test_discarded_preparation_leaves_the_queue_and_cannot_be_served(pos):
    _, burger_id, coke_id, _ = paid_order(pos, "start", "ready")
    preparation = pos.preparation(burger_id)

    assert discard(pos, preparation.id).status_code == 200

    queue = pos.client.get(
        f"/api/v1/stations/{pos.restaurant.station.id}/preparations",
        headers=pos.headers,
    ).json()
    assert [entry["order_item_id"] for entry in queue] == [coke_id]

    response = pos.client.post(
        f"/api/v1/preparations/{preparation.id}/serve", headers=pos.headers
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_PREPARATION_TRANSITION"


def test_manager_role_may_discard(pos, db_session):
    _, burger_id, _, _ = paid_order(pos)
    manager = create_user(db_session, pos.restaurant, "MANAGER")

    response = discard(
        pos, pos.preparation(burger_id).id, headers=auth_headers(manager)
    )

    assert response.status_code == 200
    [event] = pos.events("preparation.discarded")
    assert event.user_id == manager.id


@pytest.mark.parametrize("role", ["KITCHEN", "BAR", "WAITER", "CASHIER"])
def test_other_roles_may_not_discard(pos, db_session, role):
    _, burger_id, _, _ = paid_order(pos)
    user = create_user(db_session, pos.restaurant, role)

    response = discard(pos, pos.preparation(burger_id).id, headers=auth_headers(user))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"
    assert pos.preparation(burger_id).status == "PENDING"
    assert pos.events("preparation.discarded") == []


def test_unpaid_order_is_not_discardable(pos):
    order_id, burger_id, _ = pos.sent_order()
    changes_before = len(status_changes(pos))

    response = discard(pos, pos.preparation(burger_id).id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ORDER_NOT_CLOSED"
    assert pos.preparation(burger_id).status == "PENDING"
    assert pos.events("preparation.discarded") == []
    assert len(status_changes(pos)) == changes_before


def test_partially_paid_order_is_not_discardable(pos):
    _, burger_id, _, bill_id = pos.billed_order()
    assert pos.pay(bill_id, "5.00").status_code == 201

    response = discard(pos, pos.preparation(burger_id).id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ORDER_NOT_CLOSED"


def test_served_preparation_is_not_discardable(pos):
    _, burger_id, _, _ = paid_order(pos, "start", "ready", "serve")

    response = discard(pos, pos.preparation(burger_id).id)

    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "INVALID_PREPARATION_TRANSITION",
        "message": "Invalid preparation transition: SERVED -> CANCELLED",
        "request_id": response.headers["X-Request-ID"],
    }
    assert pos.preparation(burger_id).status == "SERVED"


def test_discarding_twice_is_an_invalid_transition(pos):
    _, burger_id, _, _ = paid_order(pos)
    preparation_id = pos.preparation(burger_id).id
    assert discard(pos, preparation_id).status_code == 200

    response = discard(pos, preparation_id)

    assert response.status_code == 409
    assert response.json()["error"]["message"] == (
        "Invalid preparation transition: CANCELLED -> CANCELLED"
    )
    assert len(pos.events("preparation.discarded")) == 1


def test_another_restaurants_preparation_looks_missing(pos, db_session):
    _, burger_id, _, _ = paid_order(pos)
    other = create_restaurant(db_session, name="Other Restaurant")

    response = discard(
        pos, pos.preparation(burger_id).id, headers=auth_headers(other.user)
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PREPARATION_NOT_FOUND"
    assert pos.preparation(burger_id).status == "PENDING"


def test_unknown_preparation_is_not_found(pos):
    response = discard(pos, uuid.uuid4())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PREPARATION_NOT_FOUND"


@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "x" * 501}])
def test_a_reason_is_required(pos, body):
    _, burger_id, _, _ = paid_order(pos)

    response = pos.client.post(
        f"/api/v1/preparations/{pos.preparation(burger_id).id}/discard",
        json=body,
        headers=pos.headers,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert pos.preparation(burger_id).status == "PENDING"


def test_repeated_request_with_the_same_key_is_replayed(pos):
    _, burger_id, _, _ = paid_order(pos, "start", "ready")
    preparation_id = pos.preparation(burger_id).id
    key = f"discard-{uuid.uuid4()}"
    changes_before = len(status_changes(pos))

    first = discard(pos, preparation_id, key=key)
    second = discard(pos, preparation_id, key=key)

    assert first.status_code == second.status_code == 200
    assert second.json() == first.json()
    assert second.headers[REPLAYED_HEADER] == "true"
    assert len(pos.events("preparation.discarded")) == 1
    assert len(status_changes(pos)) == changes_before + 1


def test_final_payment_is_allowed_with_unfinished_preparations(pos):
    """Unchanged rule: paying never waits for the kitchen."""

    order_id, burger_id, coke_id, bill_id = pos.billed_order()
    pos.start_preparation(burger_id, "start")

    response = pos.pay(bill_id, "15.00")

    assert response.status_code == 201
    assert pos.order(order_id).status == "CLOSED"
    assert pos.bill(bill_id).amount_due == Decimal("0.00")
    assert pos.preparation(burger_id).status == "IN_PROGRESS"
    assert pos.preparation(coke_id).status == "PENDING"
