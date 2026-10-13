"""Idempotency-Key behavior of the critical mutations.

Sequential retries run here in the rolled-back test session. Concurrent
requests and failure atomicity need real commits and separate connections;
they are in tests/integration/test_mutation_concurrency.py.
"""

import uuid

import pytest
from sqlalchemy import update

from app.api.idempotency import IDEMPOTENCY_KEY_HEADER, REPLAYED_HEADER
from app.db.models.bill import Payment
from app.db.models.idempotency_key import IdempotencyKey
from app.db.models.order import Order
from app.db.models.user import User
from app.core.security import hash_password
from tests.factories import auth_headers, create_restaurant


def keyed(pos, key, headers=None):
    return {**(headers or pos.headers), IDEMPOTENCY_KEY_HEADER: key}


def post(pos, path, key, json=None, headers=None):
    return pos.client.post(path, json=json, headers=keyed(pos, key, headers))


def pay(pos, bill_id, key, amount="5.00", method="CASH", headers=None):
    return post(
        pos,
        f"/api/v1/bills/{bill_id}/payments",
        key,
        json={"amount": amount, "method": method},
        headers=headers,
    )


def payments(pos, bill_id):
    return (
        pos._fresh()
        .query(Payment)
        .filter(Payment.bill_id == bill_id)
        .all()
    )


def stored_keys(pos):
    return (
        pos._fresh()
        .query(IdempotencyKey)
        .filter(IdempotencyKey.restaurant_id == pos.restaurant.restaurant.id)
        .all()
    )


def assert_replay(first, again):
    assert again.status_code == first.status_code
    assert again.json() == first.json()
    assert again.headers[REPLAYED_HEADER] == "true"
    assert REPLAYED_HEADER not in first.headers


# --- Payments ---


def test_first_request_with_a_key_runs_and_stores_the_result(pos):
    *_, bill_id = pos.billed_order()

    response = pay(pos, bill_id, "pay-1")

    assert response.status_code == 201
    assert response.json()["amount"] == "5.00"
    assert REPLAYED_HEADER not in response.headers

    [record] = stored_keys(pos)
    assert record.idempotency_key == "pay-1"
    assert record.user_id == pos.restaurant.user.id
    assert record.operation == "POST /api/v1/bills/{bill_id}/payments"
    assert record.response_status_code == 201
    assert record.response_body == response.json()


def test_payment_retry_replays_without_a_second_payment(pos):
    *_, bill_id = pos.billed_order()

    first = pay(pos, bill_id, "pay-1")
    again = pay(pos, bill_id, "pay-1")

    assert_replay(first, again)
    assert len(payments(pos, bill_id)) == 1
    assert len(pos.events("payment.completed")) == 1
    assert pos.bill(bill_id).paid_total == 5


def test_equivalent_amount_formatting_is_the_same_request(pos):
    *_, bill_id = pos.billed_order()

    first = pay(pos, bill_id, "pay-1", amount="5.00")
    again = pay(pos, bill_id, "pay-1", amount=5)

    assert_replay(first, again)
    assert len(payments(pos, bill_id)) == 1


def test_final_payment_retry_keeps_order_closed_once(pos):
    order_id, *_, bill_id = pos.billed_order()

    first = pay(pos, bill_id, "final", amount="15.00")
    again = pay(pos, bill_id, "final", amount="15.00")

    assert first.status_code == 201
    assert_replay(first, again)
    assert pos.order(order_id).status == "CLOSED"
    assert len(payments(pos, bill_id)) == 1
    assert len(pos.events("order.closed")) == 1


def test_same_key_with_different_payload_is_rejected(pos):
    *_, bill_id = pos.billed_order()

    assert pay(pos, bill_id, "pay-1", amount="5.00").status_code == 201
    response = pay(pos, bill_id, "pay-1", amount="6.00")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert response.json()["error"]["request_id"]
    assert len(payments(pos, bill_id)) == 1

    # Deterministic: asking again gives the same answer.
    assert pay(pos, bill_id, "pay-1", amount="6.00").status_code == 422


def test_same_key_on_a_different_operation_is_rejected(pos):
    order_id, burger_id, _, bill_id = pos.billed_order()

    assert pay(pos, bill_id, "shared").status_code == 201

    response = post(
        pos,
        f"/api/v1/orders/{order_id}/items/{burger_id}/cancel",
        "shared",
        json={"reason": "Wrong dish"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert pos.item(burger_id).status == "SENT"


def test_failed_request_stores_nothing_and_retry_runs_again(pos):
    """A business error is not stored; the retry sees the current state."""

    order_id, *_, bill_id = pos.billed_order()
    late_item = pos.add_item(order_id, pos.coke)
    assert late_item.status_code == 201

    # The final payment is refused while an item is unsent.
    refused = pay(pos, bill_id, "final", amount="17.50")
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "ORDER_HAS_UNSENT_ITEMS"
    assert stored_keys(pos) == []
    assert payments(pos, bill_id) == []

    assert pos.send(order_id).status_code == 200

    retried = pay(pos, bill_id, "final", amount="17.50")
    assert retried.status_code == 201
    assert REPLAYED_HEADER not in retried.headers
    assert len(payments(pos, bill_id)) == 1
    assert pos.order(order_id).status == "CLOSED"


def test_payment_without_a_key_is_not_deduplicated(pos):
    """Missing key: the request runs exactly as before, every time."""

    *_, bill_id = pos.billed_order()

    assert pos.pay(bill_id, "5.00").status_code == 201
    assert pos.pay(bill_id, "5.00").status_code == 201

    assert len(payments(pos, bill_id)) == 2
    assert stored_keys(pos) == []


@pytest.mark.parametrize(
    "key",
    ["", "has space", "x" * 256, "naïve"],
    ids=["empty", "space", "too long", "non-ascii"],
)
def test_invalid_key_is_rejected_before_running(pos, key):
    *_, bill_id = pos.billed_order()

    response = pos.client.post(
        f"/api/v1/bills/{bill_id}/payments",
        json={"amount": "5.00", "method": "CASH"},
        headers={**pos.headers, IDEMPOTENCY_KEY_HEADER: key.encode()},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_INVALID"
    assert payments(pos, bill_id) == []


def test_expired_key_is_a_new_key(pos):
    *_, bill_id = pos.billed_order()

    assert pay(pos, bill_id, "pay-1").status_code == 201

    pos.db.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.idempotency_key == "pay-1")
        .values(expires_at=IdempotencyKey.created_at)
    )

    again = pay(pos, bill_id, "pay-1")

    assert again.status_code == 201
    assert REPLAYED_HEADER not in again.headers
    assert len(payments(pos, bill_id)) == 2
    assert len(stored_keys(pos)) == 1


# --- Tenant and user scope ---


def test_keys_are_scoped_per_restaurant(pos, db_session, db_client):
    other = create_restaurant(db_session, name="Other Restaurant")
    *_, bill_id = pos.billed_order()

    assert pay(pos, bill_id, "same-key").status_code == 201

    # Another tenant's user cannot reach the bill, nor A's stored result.
    response = pay(
        pos,
        bill_id,
        "same-key",
        headers=auth_headers(other.user),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "TENANT_ACCESS_DENIED"
    assert REPLAYED_HEADER not in response.headers
    assert len(payments(pos, bill_id)) == 1

    # The same key works independently in the other restaurant.
    response = db_client.post(
        "/api/v1/orders",
        json={"table_id": str(other.table.id), "guest_count": 1},
        headers={
            **auth_headers(other.user),
            IDEMPOTENCY_KEY_HEADER: "same-key",
        },
    )
    assert response.status_code == 201
    assert response.json()["restaurant_id"] == str(other.restaurant.id)


def test_keys_are_scoped_per_user(pos, db_session):
    *_, bill_id = pos.billed_order()

    colleague = User(
        restaurant_id=pos.restaurant.restaurant.id,
        role_id=pos.restaurant.role.id,
        name="Colleague",
        email=f"colleague-{uuid.uuid4()}@example.com",
        password_hash=hash_password("colleague-password"),
    )
    db_session.add(colleague)
    db_session.flush()

    assert pay(pos, bill_id, "same-key").status_code == 201
    response = pay(
        pos,
        bill_id,
        "same-key",
        headers=auth_headers(colleague),
    )

    # A different user's key: a separate, new operation.
    assert response.status_code == 201
    assert REPLAYED_HEADER not in response.headers
    assert len(payments(pos, bill_id)) == 2


# --- Sending ---


def test_send_retry_creates_no_duplicate_preparations_or_events(pos):
    order_id, *_ = pos.open_order()

    first = post(pos, f"/api/v1/orders/{order_id}/send", "send-1")
    again = post(pos, f"/api/v1/orders/{order_id}/send", "send-1")

    assert first.status_code == 200
    assert_replay(first, again)
    assert len(pos.preparations(order_id)) == 2
    assert len(pos.events("order.sent")) == 1


def test_send_replay_does_not_send_items_added_later(pos):
    """A key identifies one send; the next round needs a new key."""

    order_id, *_ = pos.open_order()

    first = post(pos, f"/api/v1/orders/{order_id}/send", "round-1")
    assert pos.add_item(order_id, pos.coke).status_code == 201

    again = post(pos, f"/api/v1/orders/{order_id}/send", "round-1")
    assert_replay(first, again)
    assert len(pos.preparations(order_id)) == 2

    second_round = post(pos, f"/api/v1/orders/{order_id}/send", "round-2")
    assert second_round.status_code == 200
    assert REPLAYED_HEADER not in second_round.headers
    assert len(pos.preparations(order_id)) == 3
    assert len(pos.events("order.sent")) == 2


def test_send_without_key_retry_gets_a_conflict(pos):
    """Without a key, a retry of a successful send is not a replay."""

    order_id, *_ = pos.open_order()

    assert pos.send(order_id).status_code == 200
    response = pos.send(order_id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ORDER_HAS_NO_ITEMS_TO_SEND"


# --- Cancellation ---


def test_cancel_retry_applies_one_financial_adjustment(pos):
    order_id, burger_id, _, bill_id = pos.billed_order()
    path = f"/api/v1/orders/{order_id}/items/{burger_id}/cancel"

    first = post(pos, path, "cancel-1", json={"reason": "Wrong dish"})
    again = post(pos, path, "cancel-1", json={"reason": "Wrong dish"})

    assert first.status_code == 200
    assert first.json()["status"] == "CANCELLED"
    assert_replay(first, again)

    [adjustment] = pos.adjustments(bill_id)
    assert adjustment.amount == -12.50
    assert len(pos.events("bill.adjusted")) == 1
    assert len(pos.events("item.cancelled")) == 1
    assert pos.bill(bill_id).amount_due == 2.50
    pos.assert_bill_matches_items(bill_id)


def test_cancel_with_different_reason_is_a_different_request(pos):
    order_id, burger_id, _, _ = pos.billed_order()
    path = f"/api/v1/orders/{order_id}/items/{burger_id}/cancel"

    assert post(pos, path, "c", json={"reason": "Wrong dish"}).status_code == 200
    response = post(pos, path, "c", json={"reason": "Too slow"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_void_retry_replays_the_cancelled_order(pos):
    order_id, burger_id, coke_id = pos.sent_order()
    assert pos.cancel(order_id, burger_id).status_code == 200
    assert pos.cancel(order_id, coke_id).status_code == 200

    path = f"/api/v1/orders/{order_id}/void"
    first = post(pos, path, "void-1", json={"reason": "Guests left"})
    again = post(pos, path, "void-1", json={"reason": "Guests left"})

    assert first.status_code == 200
    assert first.json()["status"] == "CANCELLED"
    assert_replay(first, again)
    assert len(pos.events("order.cancelled")) == 1


# --- Order creation ---


def test_create_order_retry_returns_the_same_order(pos):
    body = {"table_id": str(pos.restaurant.table.id), "guest_count": 2}

    first = post(pos, "/api/v1/orders", "open-1", json=body)
    again = post(pos, "/api/v1/orders", "open-1", json=body)

    assert first.status_code == 201
    assert_replay(first, again)

    orders = (
        pos._fresh()
        .query(Order)
        .filter(Order.table_id == pos.restaurant.table.id)
        .all()
    )
    assert [order.id for order in orders] == [uuid.UUID(first.json()["id"])]
    assert len(pos.events("order.created")) == 1


def test_create_order_retry_without_key_conflicts(pos):
    assert pos.create_order().status_code == 201

    response = pos.create_order()

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TABLE_NOT_AVAILABLE"
    assert response.json()["detail"] == "Table is not available"
