"""C1.1 authorization rules: who may change which order, bill and dish.

1. Billed items are cancelled only through the bill (bills:adjust); the
   order endpoints never change an issued bill. Managers may override the
   creator-only order rule, within the state and financial rules.
2. Waiters may serve only READY preparations.
3. Waiters may view, but not modify, other waiters' orders.
4. Audit events record the acting user; the order's creator is kept
   separately as `order_created_by`.
"""

from decimal import Decimal

import pytest

from app.core.request_id import REQUEST_ID_HEADER
from app.db.models.table import Table
from tests.factories import (
    assign_station,
    auth_headers,
    create_restaurant,
    create_user,
)


def assert_error(response, status_code, code, message=None):
    assert response.status_code == status_code, response.text
    body = response.json()
    assert body["error"]["code"] == code
    assert body["error"]["request_id"] == response.headers[REQUEST_ID_HEADER]

    if message is not None:
        assert body["error"]["message"] == message


@pytest.fixture
def staff(db_session, restaurant):
    users = {
        role: create_user(db_session, restaurant, role.split("_")[0])
        for role in ("WAITER", "WAITER_2", "CASHIER", "MANAGER", "KITCHEN")
    }
    # The factory's products are prepared at its (KITCHEN) station.
    assign_station(db_session, users["KITCHEN"], restaurant.station)

    return users


class As:
    """Run Pos calls as a given user.

    Switches the one shared Pos, so call it again before each user's calls.
    """

    def __init__(self, pos, staff):
        self.pos = pos
        self.staff = staff

    def __call__(self, role):
        self.pos.headers = auth_headers(self.staff[role])
        return self.pos


@pytest.fixture
def as_(pos, staff):
    return As(pos, staff)


def waiters_sent_order(as_):
    """A SENT order (Burger 12.50, Coke 2.50) created by WAITER."""

    return as_("WAITER").sent_order()


def waiters_billed_order(as_):
    """WAITER's order with an OPEN 15.00 bill issued by CASHIER."""

    order_id, burger_id, coke_id = waiters_sent_order(as_)
    bill = as_("CASHIER").create_bill(order_id)
    assert bill.status_code == 201

    return order_id, burger_id, coke_id, bill.json()["id"]


def event(pos, event_type):
    [recorded] = pos.events(event_type)
    return recorded


# --- 1. Cancellation, voiding and the creator-only rule ---


def test_waiter_cannot_cancel_another_waiters_item(as_):
    order_id, burger_id, _ = waiters_sent_order(as_)

    response = as_("WAITER_2").cancel(order_id, burger_id)

    assert_error(response, 403, "ORDER_ACCESS_DENIED", "You cannot modify this order")
    assert as_.pos.item(burger_id).status == "SENT"


def test_waiter_cannot_void_another_waiters_order(as_):
    order_id, burger_id, coke_id = waiters_sent_order(as_)
    pos = as_("WAITER")
    assert pos.cancel(order_id, burger_id).status_code == 200
    assert pos.cancel(order_id, coke_id).status_code == 200

    response = as_("WAITER_2").void(order_id)

    assert_error(response, 403, "ORDER_ACCESS_DENIED")
    assert pos.order(order_id).status == "SENT"


def test_waiter_still_modifies_own_unbilled_order(as_):
    order_id, burger_id, coke_id = waiters_sent_order(as_)
    pos = as_("WAITER")

    assert pos.cancel(order_id, burger_id).status_code == 200
    assert pos.cancel(order_id, coke_id).status_code == 200
    assert pos.void(order_id).status_code == 200
    assert pos.order(order_id).status == "CANCELLED"


def test_manager_cancels_item_of_another_waiters_order(as_, staff):
    order_id, burger_id, _ = waiters_sent_order(as_)

    response = as_("MANAGER").cancel(order_id, burger_id, reason="Kitchen error")

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"

    cancelled = event(as_.pos, "item.cancelled")
    assert cancelled.user_id == staff["MANAGER"].id
    assert cancelled.event_metadata["order_created_by"] == str(staff["WAITER"].id)

    [preparation_event] = as_.pos.events("preparation.cancelled")
    assert preparation_event.user_id == staff["MANAGER"].id


def test_manager_voids_another_waiters_order(as_, staff):
    order_id, burger_id, coke_id = waiters_sent_order(as_)
    manager = as_("MANAGER")
    assert manager.cancel(order_id, burger_id).status_code == 200
    assert manager.cancel(order_id, coke_id).status_code == 200

    response = manager.void(order_id, reason="Guests left")

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert manager.table().status == "AVAILABLE"

    cancelled = event(manager, "order.cancelled")
    assert cancelled.user_id == staff["MANAGER"].id
    assert cancelled.event_metadata["order_created_by"] == str(staff["WAITER"].id)


def test_manager_adds_and_sends_on_another_waiters_order(as_, staff):
    order_id, *_ = waiters_sent_order(as_)
    manager = as_("MANAGER")

    assert manager.add_item(order_id, manager.coke).status_code == 201
    assert manager.send(order_id).status_code == 200

    sends = manager.events("order.sent")
    assert [e.user_id for e in sends].count(staff["MANAGER"].id) == 1


def test_manager_override_does_not_bypass_state_rules(as_):
    order_id, burger_id, _ = waiters_sent_order(as_)
    as_("KITCHEN").start_preparation(burger_id, "start", "ready")
    manager = as_("MANAGER")

    response = manager.cancel(order_id, burger_id)
    assert_error(response, 409, "ORDER_ITEM_NOT_CANCELLABLE")

    response = manager.void(order_id)
    assert_error(response, 409, "ORDER_HAS_ACTIVE_ITEMS")
    assert manager.order(order_id).status == "SENT"


def test_manager_override_does_not_bypass_financial_rules(as_):
    order_id, burger_id, coke_id, bill_id = waiters_billed_order(as_)
    assert as_("CASHIER").pay(bill_id, "1.00").status_code == 201
    manager = as_("MANAGER")

    assert_error(
        manager.cancel_billed(bill_id, burger_id),
        409,
        "BILL_HAS_PAYMENTS",
    )
    assert_error(manager.void(order_id), 409, "ORDER_HAS_ACTIVE_ITEMS")
    assert manager.adjustments(bill_id) == []


# --- 1. No cancellation around an issued bill ---


@pytest.mark.parametrize("role", ["WAITER", "MANAGER"])
def test_order_endpoint_never_cancels_a_billed_item(as_, role):
    """Even the creator, even a manager: billed items go through the bill."""

    order_id, burger_id, _, bill_id = waiters_billed_order(as_)

    response = as_(role).cancel(order_id, burger_id)

    assert_error(
        response,
        409,
        "ORDER_BILLED",
        "Order has a bill; cancel billed items through the bill",
    )
    pos = as_.pos
    assert pos.item(burger_id).status == "SENT"
    assert pos.preparation(burger_id).status == "PENDING"
    assert pos.adjustments(bill_id) == []
    assert pos.events("item.cancelled") == []
    assert pos.bill(bill_id).amount_due == Decimal("15.00")


def test_waiter_cannot_use_the_financial_cancellation(as_):
    _, burger_id, _, bill_id = waiters_billed_order(as_)

    response = as_("WAITER").cancel_billed(bill_id, burger_id)

    assert_error(response, 403, "PERMISSION_DENIED", "Missing permission: bills:adjust")
    assert as_.pos.adjustments(bill_id) == []


def test_cashier_cancels_billed_item_through_bill_adjustment(as_, staff):
    order_id, burger_id, _, bill_id = waiters_billed_order(as_)
    cashier = as_("CASHIER")

    response = cashier.cancel_billed(bill_id, burger_id, reason="Cold burger")

    assert response.status_code == 200
    bill = response.json()
    assert bill["id"] == bill_id
    assert bill["total"] == "15.00"
    assert bill["adjustments_total"] == "-12.50"
    assert bill["amount_due"] == "2.50"
    assert [a["type"] for a in bill["adjustments"]] == ["ITEM_CANCELLATION"]

    [adjustment] = cashier.adjustments(bill_id)
    assert adjustment.user_id == staff["CASHIER"].id
    assert adjustment.reason == "Cold burger"
    assert cashier.item(burger_id).status == "CANCELLED"
    assert cashier.preparation(burger_id).status == "CANCELLED"
    cashier.assert_bill_matches_items(bill_id)

    adjusted = event(cashier, "bill.adjusted")
    assert adjusted.user_id == staff["CASHIER"].id
    assert adjusted.event_metadata["order_created_by"] == str(staff["WAITER"].id)
    assert event(cashier, "item.cancelled").user_id == staff["CASHIER"].id


def test_financial_cancellation_keeps_bill_rules(as_):
    _, burger_id, coke_id, bill_id = waiters_billed_order(as_)
    as_("KITCHEN").start_preparation(coke_id, "start", "ready")
    cashier = as_("CASHIER")

    assert_error(cashier.cancel_billed(bill_id, coke_id), 409, "ORDER_ITEM_NOT_CANCELLABLE")

    assert cashier.pay(bill_id, "1.00").status_code == 201
    assert_error(
        cashier.cancel_billed(bill_id, burger_id),
        409,
        "BILL_HAS_PAYMENTS",
        "Cannot cancel an order item after payment has been made",
    )
    assert cashier.adjustments(bill_id) == []


def test_financial_cancellation_checks_the_item_belongs_to_the_bill(as_):
    _, _, _, bill_id = waiters_billed_order(as_)
    _, other_item_id = _second_table_order(as_)

    response = as_("CASHIER").cancel_billed(bill_id, other_item_id)

    assert_error(response, 404, "ORDER_ITEM_NOT_FOUND")
    assert as_.pos.item(other_item_id).status == "SENT"


def _second_table_order(as_):
    """A SENT order on a second table, by WAITER: (order_id, item_id)."""

    pos = as_("WAITER")
    table = Table(
        restaurant_id=pos.restaurant.restaurant.id,
        number=2,
        capacity=2,
    )
    pos.db.add(table)
    pos.db.flush()

    order = pos.client.post(
        "/api/v1/orders",
        json={"table_id": str(table.id), "guest_count": 1},
        headers=pos.headers,
    )
    assert order.status_code == 201
    order_id = order.json()["id"]
    item = pos.add_item(order_id, pos.coke)
    assert pos.send(order_id).status_code == 200

    return order_id, item.json()["id"]


def test_financial_cancellation_of_another_restaurants_bill_looks_missing(
    as_, db_session
):
    _, burger_id, _, bill_id = waiters_billed_order(as_)
    other = create_restaurant(db_session, name="Other Restaurant")
    foreign_cashier = create_user(db_session, other, "CASHIER")
    client = as_.pos.client

    foreign = client.post(
        f"/api/v1/bills/{bill_id}/items/{burger_id}/cancel",
        json={"reason": "x"},
        headers=auth_headers(foreign_cashier),
    )
    missing = client.post(
        f"/api/v1/bills/{burger_id}/items/{burger_id}/cancel",
        json={"reason": "x"},
        headers=auth_headers(foreign_cashier),
    )

    assert_error(foreign, 404, "BILL_NOT_FOUND", "Bill not found")
    assert foreign.json()["detail"] == missing.json()["detail"]
    assert missing.status_code == 404
    assert as_.pos.adjustments(bill_id) == []


def test_voiding_a_billed_order_needs_bills_adjust(as_, staff):
    order_id, burger_id, coke_id, bill_id = waiters_billed_order(as_)
    cashier = as_("CASHIER")
    assert cashier.cancel_billed(bill_id, burger_id).status_code == 200
    assert cashier.cancel_billed(bill_id, coke_id).status_code == 200
    assert cashier.bill(bill_id).amount_due == Decimal("0.00")

    # The creator may not void the financial document...
    response = as_("WAITER").void(order_id)
    assert_error(response, 403, "PERMISSION_DENIED", "Missing permission: bills:adjust")
    assert as_.pos.bill(bill_id).status == "OPEN"

    # ...and the cashier has no order-management powers.
    response = as_("CASHIER").void(order_id)
    assert_error(response, 403, "PERMISSION_DENIED", "Missing permission: orders:void")

    # A manager may, since the bill is open, unpaid and fully adjusted.
    manager = as_("MANAGER")
    assert manager.void(order_id).status_code == 200
    assert manager.bill(bill_id).status == "VOID"
    assert event(manager, "bill.voided").user_id == staff["MANAGER"].id


# --- 1. Cashier has no order-management powers ---


def test_cashier_cannot_manage_orders(as_):
    order_id, burger_id, _ = waiters_sent_order(as_)
    cashier = as_("CASHIER")

    assert_error(cashier.create_order(), 403, "PERMISSION_DENIED")
    assert_error(cashier.add_item(order_id, cashier.coke), 403, "PERMISSION_DENIED")
    assert_error(cashier.send(order_id), 403, "PERMISSION_DENIED")
    assert_error(cashier.cancel(order_id, burger_id), 403, "PERMISSION_DENIED")
    assert_error(cashier.void(order_id), 403, "PERMISSION_DENIED")
    assert cashier.item(burger_id).status == "SENT"


# --- 2. Serving preparations ---


def test_waiter_serves_a_ready_preparation(as_, staff):
    order_id, burger_id, _ = waiters_sent_order(as_)
    preparation_id = as_.pos.preparation(burger_id).id
    kitchen = as_("KITCHEN")

    for action in ("start", "ready"):
        response = kitchen.client.post(
            f"/api/v1/preparations/{preparation_id}/{action}",
            headers=kitchen.headers,
        )
        assert response.status_code == 200

    waiter = as_("WAITER_2")  # any waiter may serve, not only the creator
    response = waiter.client.post(
        f"/api/v1/preparations/{preparation_id}/serve",
        headers=waiter.headers,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "SERVED"

    assert event(waiter, "preparation.started").user_id == staff["KITCHEN"].id
    assert event(waiter, "preparation.ready").user_id == staff["KITCHEN"].id
    served = event(waiter, "preparation.served")
    assert served.user_id == staff["WAITER_2"].id
    assert served.event_metadata["order_id"] == order_id
    assert served.event_metadata["order_created_by"] == str(staff["WAITER"].id)


@pytest.mark.parametrize(
    ("actions", "status"),
    [((), "PENDING"), (("start",), "IN_PROGRESS")],
)
def test_waiter_cannot_serve_an_unready_preparation(as_, actions, status):
    _, burger_id, _ = waiters_sent_order(as_)
    as_("KITCHEN").start_preparation(burger_id, *actions)
    preparation_id = as_.pos.preparation(burger_id).id
    waiter = as_("WAITER")

    response = waiter.client.post(
        f"/api/v1/preparations/{preparation_id}/serve",
        headers=waiter.headers,
    )

    assert_error(
        response,
        409,
        "INVALID_PREPARATION_TRANSITION",
        f"Invalid preparation transition: {status} -> SERVED",
    )
    assert waiter.preparation(burger_id).status == status
    assert waiter.events("preparation.served") == []


@pytest.mark.parametrize("action", ["start", "ready"])
def test_waiter_cannot_drive_kitchen_transitions(as_, action):
    _, burger_id, _ = waiters_sent_order(as_)
    preparation_id = as_.pos.preparation(burger_id).id
    waiter = as_("WAITER")

    response = waiter.client.post(
        f"/api/v1/preparations/{preparation_id}/{action}",
        headers=waiter.headers,
    )

    assert_error(
        response,
        403,
        "PERMISSION_DENIED",
        "Missing permission: preparations:update",
    )
    assert waiter.preparation(burger_id).status == "PENDING"


def test_cancelled_preparation_cannot_be_started_or_served(as_):
    order_id, burger_id, _ = waiters_sent_order(as_)
    assert as_("WAITER").cancel(order_id, burger_id).status_code == 200
    preparation_id = as_.pos.preparation(burger_id).id
    kitchen = as_("KITCHEN")

    for action in ("start", "serve"):
        response = kitchen.client.post(
            f"/api/v1/preparations/{preparation_id}/{action}",
            headers=kitchen.headers,
        )
        assert_error(response, 409, "INVALID_PREPARATION_TRANSITION")

    assert kitchen.preparation(burger_id).status == "CANCELLED"


# --- 3. Viewing other waiters' orders ---


def test_waiter_views_but_cannot_modify_another_waiters_order(as_):
    order_id, burger_id, _ = waiters_sent_order(as_)
    other = as_("WAITER_2")

    response = other.client.get(f"/api/v1/orders/{order_id}", headers=other.headers)
    assert response.status_code == 200
    assert response.json()["id"] == order_id
    assert len(response.json()["items"]) == 2

    assert_error(other.add_item(order_id, other.coke), 403, "ORDER_ACCESS_DENIED")
    assert_error(other.send(order_id), 403, "ORDER_ACCESS_DENIED")
    assert_error(other.cancel(order_id, burger_id), 403, "ORDER_ACCESS_DENIED")
    assert len(other.items(order_id)) == 2


def test_waiter_cannot_view_another_restaurants_order(as_, db_session):
    order_id, *_ = waiters_sent_order(as_)
    other = create_restaurant(db_session, name="Other Restaurant")
    foreign_waiter = create_user(db_session, other, "WAITER")

    response = as_.pos.client.get(
        f"/api/v1/orders/{order_id}",
        headers=auth_headers(foreign_waiter),
    )

    assert_error(response, 404, "ORDER_NOT_FOUND", "Order not found")


# --- 4. Audit attribution of bills and payments ---


def test_bill_and_payment_events_record_the_cashier(as_, staff):
    order_id, _, _, bill_id = waiters_billed_order(as_)
    cashier = as_("CASHIER")

    assert cashier.pay(bill_id, "15.00").status_code == 201

    for event_type in ("bill.created", "payment.completed", "order.closed"):
        recorded = event(cashier, event_type)
        assert recorded.user_id == staff["CASHIER"].id, event_type
        assert recorded.event_metadata["order_created_by"] == str(
            staff["WAITER"].id
        ), event_type

    # The order's own creator is unchanged.
    assert str(cashier.order(order_id).user_id) == str(staff["WAITER"].id)
