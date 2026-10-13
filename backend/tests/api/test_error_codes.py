"""Stable error codes from the real endpoints, and table/order lifecycle."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.request_id import REQUEST_ID_HEADER
from app.db.models.order import Order
from app.main import app
from app.services.order_service import OrderService
from tests.factories import auth_headers, create_user


def assert_error(response, status_code, code, detail=None):
    assert response.status_code == status_code
    body = response.json()

    assert body["error"]["code"] == code
    assert body["error"]["request_id"] == response.headers[REQUEST_ID_HEADER]

    if detail is not None:
        # The pre-existing `detail` text is preserved.
        assert body["detail"] == detail
        assert body["error"]["message"] == detail


def test_order_not_found(pos):
    response = pos.client.get(
        f"/api/v1/orders/{uuid.uuid4()}",
        headers=pos.headers,
    )

    assert_error(response, 404, "ORDER_NOT_FOUND", "Order not found")


def test_table_not_found(pos):
    response = pos.client.post(
        "/api/v1/orders",
        json={"table_id": str(uuid.uuid4()), "guest_count": 2},
        headers=pos.headers,
    )

    assert_error(response, 404, "TABLE_NOT_FOUND", "Table not found")


def test_payment_exceeding_amount_due(pos):
    *_, bill_id = pos.billed_order()

    assert_error(
        pos.pay(bill_id, "15.01"),
        409,
        "PAYMENT_EXCEEDS_AMOUNT_DUE",
        "Payment exceeds remaining bill amount",
    )


def test_bill_not_open(pos):
    *_, bill_id = pos.billed_order()
    assert pos.pay(bill_id, "15.00").status_code == 201

    assert_error(pos.pay(bill_id, "1.00"), 409, "BILL_NOT_OPEN", "Bill is not open")


def test_payment_validation_error(pos):
    *_, bill_id = pos.billed_order()

    response = pos.pay(bill_id, "1.005")

    assert_error(response, 422, "VALIDATION_ERROR")
    assert response.json()["detail"][0]["loc"] == ["body", "amount"]


def test_cancel_twice(pos):
    order_id, burger_id, _ = pos.sent_order()
    assert pos.cancel(order_id, burger_id).status_code == 200

    assert_error(
        pos.cancel(order_id, burger_id),
        409,
        "ORDER_ITEM_ALREADY_CANCELLED",
        "Order item is already cancelled",
    )


def test_other_users_order(pos, db_session):
    order_id, *_ = pos.open_order()
    # Another waiter: the creator-only rule applies to them.
    colleague = create_user(db_session, pos.restaurant, "WAITER")

    response = pos.client.post(
        f"/api/v1/orders/{order_id}/send",
        headers=auth_headers(colleague),
    )

    assert_error(
        response,
        403,
        "ORDER_ACCESS_DENIED",
        "You cannot modify this order",
    )


def test_invalid_login(pos):
    response = pos.client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "nope"},
    )

    assert_error(
        response,
        401,
        "INVALID_CREDENTIALS",
        "Invalid email or password",
    )


def test_inactive_user(pos, db_session):
    pos.restaurant.user.is_active = False
    db_session.flush()

    response = pos.client.get(
        f"/api/v1/orders/{uuid.uuid4()}",
        headers=pos.headers,
    )

    assert_error(response, 403, "USER_INACTIVE", "User is inactive")


def test_unexpected_service_error_is_hidden(pos, monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("password=hunter2 at postgresql://db.internal")

    monkeypatch.setattr(OrderService, "get_order", broken)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get(f"/api/v1/orders/{uuid.uuid4()}", headers=pos.headers)

    assert_error(
        response,
        500,
        "INTERNAL_ERROR",
        "Internal server error",
    )
    assert "hunter2" not in response.text
    assert "postgresql" not in response.text


# --- Tables: one active order, freed by terminal states ---


def active_orders(pos):
    return (
        pos._fresh()
        .query(Order)
        .filter(
            Order.table_id == pos.restaurant.table.id,
            Order.status.in_(("OPEN", "SENT")),
        )
        .all()
    )


def test_occupied_table_rejects_a_second_order(pos):
    assert pos.create_order().status_code == 201

    assert_error(
        pos.create_order(),
        409,
        "TABLE_NOT_AVAILABLE",
        "Table is not available",
    )
    assert len(active_orders(pos)) == 1


def test_table_with_active_order_is_rejected_even_if_marked_available(pos):
    """Inconsistent legacy data: the active order wins over the status."""

    assert pos.create_order().status_code == 201
    pos.table().status = "AVAILABLE"
    pos.db.flush()

    assert_error(
        pos.create_order(),
        409,
        "TABLE_HAS_ACTIVE_ORDER",
        "Table already has an active order",
    )
    assert len(active_orders(pos)) == 1


def test_new_order_after_previous_order_was_paid(pos):
    order_id, *_, bill_id = pos.billed_order()
    assert pos.pay(bill_id, "15.00").status_code == 201
    assert pos.order(order_id).status == "CLOSED"
    assert pos.table().status == "AVAILABLE"

    response = pos.create_order()

    assert response.status_code == 201
    assert pos.table().status == "OCCUPIED"
    assert [str(order.id) for order in active_orders(pos)] == [
        response.json()["id"]
    ]


def test_new_order_after_previous_order_was_voided(pos):
    order_id, burger_id, coke_id = pos.sent_order()
    assert pos.cancel(order_id, burger_id).status_code == 200
    assert pos.cancel(order_id, coke_id).status_code == 200
    assert pos.void(order_id).status_code == 200
    assert pos.table().status == "AVAILABLE"

    response = pos.create_order()

    assert response.status_code == 201
    assert len(active_orders(pos)) == 1


@pytest.mark.parametrize("terminal", ["CLOSED", "CANCELLED"])
def test_terminal_orders_do_not_block_the_table(pos, terminal):
    response = pos.create_order()
    order = pos.order(response.json()["id"])
    table = pos.table()  # expires the session: read before changing
    order.status = terminal
    table.status = "AVAILABLE"
    pos.db.flush()

    assert pos.create_order().status_code == 201
