"""
Regression tests for audit finding A4: payment amounts must have at most
2 decimal places, matching the Numeric(10, 2) money columns.
"""

import pytest

from app.db.models.bill import Bill, Payment
from app.db.models.order import Order
from tests.factories import auth_headers


@pytest.fixture
def headers(restaurant):
    return auth_headers(restaurant.user)


@pytest.fixture
def bill(db_client, restaurant, headers):
    """An OPEN bill for one Burger (total 12.50)."""

    order = db_client.post(
        "/api/v1/orders",
        json={
            "table_id": str(restaurant.table.id),
            "guest_count": 2,
        },
        headers=headers,
    )
    assert order.status_code == 201
    order_id = order.json()["id"]

    item = db_client.post(
        f"/api/v1/orders/{order_id}/items",
        json={
            "product_id": str(restaurant.product.id),
            "quantity": 1,
        },
        headers=headers,
    )
    assert item.status_code == 201

    sent = db_client.post(
        f"/api/v1/orders/{order_id}/send",
        headers=headers,
    )
    assert sent.status_code == 200

    bill = db_client.post(
        f"/api/v1/bills/order/{order_id}",
        headers=headers,
    )
    assert bill.status_code == 201
    assert bill.json()["total"] == "12.50"

    return bill.json()


@pytest.fixture
def pay(db_client, bill, headers):
    def _pay(amount, method="CASH"):
        return db_client.post(
            f"/api/v1/bills/{bill['id']}/payments",
            json={"amount": amount, "method": method},
            headers=headers,
        )

    return _pay


def payments_for(db_session, bill_id):
    return (
        db_session.query(Payment)
        .filter(Payment.bill_id == bill_id)
        .all()
    )


# --- Bug reproduction ---


@pytest.mark.parametrize(
    "amount",
    [
        "5.001",
        "0.001",
        # 12.495 would be stored as 12.50 while the service compares the
        # unrounded value, leaving the bill OPEN with nothing left to pay.
        "12.495",
        # JSON numbers must be rejected too, not only strings.
        5.001,
    ],
)
def test_amount_with_more_than_two_decimals_is_rejected(
    db_session, bill, pay, amount
):
    response = pay(amount)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "amount"]
    assert payments_for(db_session, bill["id"]) == []
    assert db_session.get(Bill, bill["id"]).status == "OPEN"


def test_amount_exceeding_money_column_digits_is_rejected(
    db_session, bill, pay
):
    response = pay("100000000.00")

    assert response.status_code == 422
    assert payments_for(db_session, bill["id"]) == []


# --- Valid behavior ---


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("10.00", "10.00"),
        ("10.50", "10.50"),
        ("10.55", "10.55"),
        (10.55, "10.55"),
        (10, "10.00"),
    ],
)
def test_two_decimal_amounts_are_accepted(
    db_session, bill, pay, amount, expected
):
    response = pay(amount)

    assert response.status_code == 201
    assert response.json()["amount"] == expected

    [payment] = payments_for(db_session, bill["id"])
    assert str(payment.amount) == expected


def test_split_payments_close_bill_order_and_table(
    db_client, db_session, restaurant, bill, pay, headers
):
    first = pay("10.55", method="CASH")
    assert first.status_code == 201

    after_first = db_client.get(
        f"/api/v1/bills/{bill['id']}",
        headers=headers,
    )
    assert after_first.json()["status"] == "OPEN"

    second = pay("1.95", method="CARD")
    assert second.status_code == 201

    paid = db_client.get(
        f"/api/v1/bills/{bill['id']}",
        headers=headers,
    )
    assert paid.json()["status"] == "PAID"
    assert paid.json()["paid_at"] is not None

    assert db_session.get(Order, bill["order_id"]).status == "CLOSED"

    db_session.refresh(restaurant.table)
    assert restaurant.table.status == "AVAILABLE"


def test_underpayment_leaves_bill_open(db_session, bill, pay):
    response = pay("10.00")

    assert response.status_code == 201
    assert db_session.get(Bill, bill["id"]).status == "OPEN"
    assert db_session.get(Order, bill["order_id"]).status == "SENT"


def test_overpayment_is_rejected(db_session, bill, pay):
    response = pay("12.51")

    assert response.status_code == 409
    assert response.json()["detail"] == "Payment exceeds remaining bill amount"
    assert payments_for(db_session, bill["id"]) == []


def test_payment_exceeding_remaining_amount_is_rejected(db_session, bill, pay):
    assert pay("10.00").status_code == 201

    response = pay("2.51")

    assert response.status_code == 409
    assert len(payments_for(db_session, bill["id"])) == 1


def test_payment_on_paid_bill_is_rejected(bill, pay):
    assert pay("12.50").status_code == 201

    response = pay("0.01")

    assert response.status_code == 409
    assert response.json()["detail"] == "Bill is not open"


@pytest.mark.parametrize("amount", ["0", "0.00", "-1.00"])
def test_non_positive_amount_is_rejected(db_session, bill, pay, amount):
    response = pay(amount)

    assert response.status_code == 422
    assert payments_for(db_session, bill["id"]) == []
