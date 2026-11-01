"""Cashier operations racing each other on real PostgreSQL connections.

A payment and a shift close (or a handover) never interleave: whichever
takes the shift row lock first finishes, the other sees its result. A
payment is therefore either fully in the old shift (and in its closing)
or refused; never lost, duplicated or in no shift. See tests/concurrency.py
for how requests are ordered with a gate lock instead of sleeps.
"""

from decimal import Decimal

import pytest

from tests.concurrency import concurrency, results
from tests.integration.test_mutation_concurrency import Live


@pytest.fixture
def live(db_engine, committed_restaurants):
    return Live(db_engine, committed_restaurants())


def ordered(db_engine, gate_table, gate_row, *requests):
    """Start `requests` one after another, each queued behind the gate (and
    the requests before it) on the same row lock, then open the gate: they
    get the lock in the order they were started."""

    with concurrency(db_engine) as run:
        gate = run.gate(gate_table, gate_row)
        futures, waiting = [], set()
        for method, path, kwargs in requests:
            futures.append(run.start(method, path, **kwargs))
            waiting.add(run.wait_for_new_backend_blocked_by({gate.pid, *waiting}, waiting))
        gate.release()
        return results(*futures)


def shift_column(live, column, shift_id):
    return live.scalar(f"SELECT {column} FROM cash_shift_closings WHERE cash_shift_id = :s", s=shift_id)


def test_a_payment_before_the_close_is_in_the_closing(db_engine, live):
    _, _, bill_id = live.billed_order()
    shift_id = live.r.cash_shift_id

    payment, close = ordered(
        db_engine, "cash_shifts", shift_id,
        ("POST", f"/api/v1/bills/{bill_id}/payments", {"json": {"amount": "15.00", "method": "CASH"}, "headers": live.headers()}),
        ("POST", f"/api/v1/cash/shifts/{shift_id}/close", {"json": {"counted_amount": "115.00"}, "headers": live.headers()}),
    )

    assert (payment.status_code, close.status_code) == (201, 200), (payment.text, close.text)
    assert payment.json()["cash_shift_id"] == str(shift_id)
    assert shift_column(live, "expected_amount", shift_id) == Decimal("115.00")
    assert shift_column(live, "difference", shift_id) == Decimal("0.00")


def test_a_payment_after_the_close_is_refused_and_not_recorded(db_engine, live):
    _, _, bill_id = live.billed_order()
    shift_id = live.r.cash_shift_id

    close, payment = ordered(
        db_engine, "cash_shifts", shift_id,
        ("POST", f"/api/v1/cash/shifts/{shift_id}/close", {"json": {"counted_amount": "100.00"}, "headers": live.headers()}),
        ("POST", f"/api/v1/bills/{bill_id}/payments", {"json": {"amount": "15.00", "method": "CASH"}, "headers": live.headers()}),
    )

    assert close.status_code == 200, close.text
    assert payment.status_code == 409, payment.text
    assert payment.json()["error"]["code"] == "NO_OPEN_CASH_SHIFT"
    assert live.payments(bill_id) == 0
    assert shift_column(live, "expected_amount", shift_id) == Decimal("100.00")
    assert live.count("outbox_events", "event_type = 'payment.completed' AND restaurant_id = :r", r=live.r.restaurant_id) == 0


def test_a_payment_during_a_handover_lands_in_exactly_one_shift(db_engine, live):
    _, _, bill_id = live.billed_order()
    shift_id = live.r.cash_shift_id

    handover, payment = ordered(
        db_engine, "cash_shifts", shift_id,
        ("POST", f"/api/v1/cash/shifts/{shift_id}/handover",
         {"json": {"counted_amount": "100.00", "received_amount": "100.00"}, "headers": live.headers()}),
        ("POST", f"/api/v1/bills/{bill_id}/payments", {"json": {"amount": "15.00", "method": "CASH"}, "headers": live.headers()}),
    )

    assert handover.status_code == 201, handover.text
    # The payment waited for the old shift's lock, then found it closed.
    # It is refused rather than put into either shift ambiguously.
    assert payment.status_code == 409, payment.text
    assert payment.json()["error"]["code"] == "NO_OPEN_CASH_SHIFT"
    retry = live.post(f"/api/v1/bills/{bill_id}/payments", json={"amount": "15.00", "method": "CASH"})
    assert retry.status_code == 201, retry.text
    assert retry.json()["cash_shift_id"] == handover.json()["id"]
    assert live.payments(bill_id) == 1


def test_one_item_is_paid_once_by_concurrent_split_payments(db_engine, live):
    order_id, (burger_id, _), bill_id = live.billed_order()
    body = {"amount": "12.50", "method": "CASH", "order_item_ids": [str(burger_id)]}

    first, second = ordered(
        db_engine, "orders", order_id,
        ("POST", f"/api/v1/bills/{bill_id}/payments", {"json": body, "headers": live.headers("a")}),
        ("POST", f"/api/v1/bills/{bill_id}/payments", {"json": {**body, "method": "CARD", "card_confirmed": True},
                                                        "headers": live.headers("b")}),
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "ORDER_ITEM_ALREADY_PAID"
    assert live.payments(bill_id) == 1
    assert live.count("payment_allocations", "order_item_id = :i", i=burger_id) == 1


def test_concurrent_shift_openings_open_one(db_engine, live):
    shift_id = live.r.cash_shift_id
    closed = live.post(f"/api/v1/cash/shifts/{shift_id}/close", json={"counted_amount": "100.00"})
    assert closed.status_code == 200, closed.text
    register_id = live.scalar("SELECT cash_register_id FROM cash_shifts WHERE id = :s", s=shift_id)

    first, second = ordered(
        db_engine, "cash_registers", register_id,
        ("POST", "/api/v1/cash/shifts", {"json": {"opening_amount": "100.00"}, "headers": live.headers("a")}),
        ("POST", "/api/v1/cash/shifts", {"json": {"opening_amount": "100.00"}, "headers": live.headers("b")}),
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "CASH_SHIFT_ALREADY_OPEN"
    assert live.count("cash_shifts", "restaurant_id = :r AND status = 'OPEN'", r=live.r.restaurant_id) == 1
    assert live.count("cash_shift_handovers", "restaurant_id = :r", r=live.r.restaurant_id) == 1


def test_a_cash_out_after_the_close_is_refused(db_engine, live):
    shift_id = live.r.cash_shift_id

    close, cash_out = ordered(
        db_engine, "cash_shifts", shift_id,
        ("POST", f"/api/v1/cash/shifts/{shift_id}/close", {"json": {"counted_amount": "100.00"}, "headers": live.headers()}),
        ("POST", f"/api/v1/cash/shifts/{shift_id}/movements",
         {"json": {"type": "CASH_OUT", "amount": "50.00", "reason": "Safe drop"}, "headers": live.headers()}),
    )

    assert close.status_code == 200, close.text
    assert cash_out.status_code == 409, cash_out.text
    assert cash_out.json()["error"]["code"] == "CASH_SHIFT_NOT_OPEN"
    assert live.count("cash_movements", "cash_shift_id = :s", s=shift_id) == 0
