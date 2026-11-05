"""Cashier operations racing each other on real PostgreSQL connections.

A payment and a shift close (or a handover) never interleave: whichever
takes the shift row lock first finishes, the other sees its result. A
payment is therefore either fully in the old shift (and in its closing)
or refused; never lost, duplicated or in no shift. See tests/concurrency.py
for how requests are ordered with a gate lock instead of sleeps.

Money and shift requests carry a fresh Idempotency-Key (mandatory), from
`key()` unless a test races the same key on purpose.
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.api.idempotency import IDEMPOTENCY_KEY_HEADER, REPLAYED_HEADER
from tests.concurrency import concurrency, results
from tests.factories import auth_headers, create_user
from tests.integration.test_mutation_concurrency import Live, run_same_key_race


@pytest.fixture
def live(db_engine, committed_restaurants):
    return Live(db_engine, committed_restaurants())


def key():
    return str(uuid.uuid4())


def committed_cashier(db_engine, live):
    """Request headers of another (CASHIER) employee of the restaurant,
    committed (deleted with the restaurant)."""

    with Session(db_engine) as setup:
        user = create_user(setup, SimpleNamespace(restaurant=SimpleNamespace(id=live.r.restaurant_id)), "CASHIER")
        headers = auth_headers(user)
        setup.commit()
    return headers


def keyed(headers):
    return {**headers, IDEMPOTENCY_KEY_HEADER: key()}


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
        ("POST", f"/api/v1/bills/{bill_id}/payments", {"json": {"amount": "15.00", "method": "CASH"}, "headers": live.headers(key())}),
        ("POST", f"/api/v1/cash/shifts/{shift_id}/close", {"json": {"counted_amount": "115.00"}, "headers": live.headers(key())}),
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
        ("POST", f"/api/v1/cash/shifts/{shift_id}/close", {"json": {"counted_amount": "100.00"}, "headers": live.headers(key())}),
        ("POST", f"/api/v1/bills/{bill_id}/payments", {"json": {"amount": "15.00", "method": "CASH"}, "headers": live.headers(key())}),
    )

    assert close.status_code == 200, close.text
    assert payment.status_code == 409, payment.text
    assert payment.json()["error"]["code"] == "NO_OPEN_CASH_SHIFT"
    assert live.payments(bill_id) == 0
    assert shift_column(live, "expected_amount", shift_id) == Decimal("100.00")
    assert live.count("outbox_events", "event_type = 'payment.completed' AND restaurant_id = :r", r=live.r.restaurant_id) == 0


def test_a_payment_during_a_handover_lands_in_exactly_one_shift(db_engine, live):
    """Step 1 of the handover (the outgoing count) races a payment; while
    the incoming cashier has not confirmed, payments are refused and
    nothing is recorded; once confirmed, they go into the new shift."""

    _, _, bill_id = live.billed_order()
    shift_id = live.r.cash_shift_id
    incoming = committed_cashier(db_engine, live)

    handover, payment = ordered(
        db_engine, "cash_shifts", shift_id,
        ("POST", f"/api/v1/cash/shifts/{shift_id}/handover",
         {"json": {"counted_amount": "100.00"}, "headers": live.headers(key())}),
        ("POST", f"/api/v1/bills/{bill_id}/payments", {"json": {"amount": "15.00", "method": "CASH"}, "headers": live.headers(key())}),
    )

    assert handover.status_code == 200, handover.text
    assert handover.json()["status"] == "CLOSED"
    # The payment waited for the old shift's lock, then found it closed.
    # It is refused rather than put into either shift ambiguously.
    assert payment.status_code == 409, payment.text
    assert payment.json()["error"]["code"] == "NO_OPEN_CASH_SHIFT"
    assert live.payments(bill_id) == 0
    # The pending handover lasts until the incoming cashier confirms.
    opened = live.client.post("/api/v1/cash/shifts", json={"opening_amount": "100.00"}, headers=keyed(incoming))
    assert opened.status_code == 201, opened.text
    retry = live.post(f"/api/v1/bills/{bill_id}/payments", json={"amount": "15.00", "method": "CASH"}, key=key())
    assert retry.status_code == 201, retry.text
    assert retry.json()["cash_shift_id"] == opened.json()["id"]
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
    closed = live.post(f"/api/v1/cash/shifts/{shift_id}/close", json={"counted_amount": "100.00"}, key=key())
    assert closed.status_code == 200, closed.text
    register_id = live.scalar("SELECT cash_register_id FROM cash_shifts WHERE id = :s", s=shift_id)
    # Two incoming cashiers confirm the same pending handover at once.
    one, other = committed_cashier(db_engine, live), committed_cashier(db_engine, live)

    first, second = ordered(
        db_engine, "cash_registers", register_id,
        ("POST", "/api/v1/cash/shifts", {"json": {"opening_amount": "100.00"}, "headers": keyed(one)}),
        ("POST", "/api/v1/cash/shifts", {"json": {"opening_amount": "100.00"}, "headers": keyed(other)}),
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
        ("POST", f"/api/v1/cash/shifts/{shift_id}/close", {"json": {"counted_amount": "100.00"}, "headers": live.headers(key())}),
        ("POST", f"/api/v1/cash/shifts/{shift_id}/movements",
         {"json": {"type": "CASH_OUT", "amount": "50.00", "reason": "Safe drop"}, "headers": live.headers(key())}),
    )

    assert close.status_code == 200, close.text
    assert cash_out.status_code == 409, cash_out.text
    assert cash_out.json()["error"]["code"] == "CASH_SHIFT_NOT_OPEN"
    assert live.count("cash_movements", "cash_shift_id = :s", s=shift_id) == 0


def test_concurrent_refunds_of_one_payment_never_exceed_it(db_engine, live):
    order_id, _, bill_id = live.billed_order()
    paid = live.post(f"/api/v1/bills/{bill_id}/payments", json={"amount": "15.00", "method": "CASH"}, key=key())
    payment_id = paid.json()["id"]
    path = f"/api/v1/payments/{payment_id}/refunds"

    first, second = ordered(
        db_engine, "orders", order_id,
        ("POST", path, {"json": {"amount": "10.00", "reason": "Cold"}, "headers": live.headers(key())}),
        ("POST", path, {"json": {"amount": "10.00", "reason": "Cold"}, "headers": live.headers(key())}),
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "REFUND_EXCEEDS_REFUNDABLE"
    assert live.count("refunds", "payment_id = :p", p=payment_id) == 1
    assert live.count("cash_movements", "type = 'CASH_REFUND' AND restaurant_id = :r", r=live.r.restaurant_id) == 1


def test_a_refund_sent_twice_at_once_with_one_key_refunds_once(db_engine, live):
    order_id, _, bill_id = live.billed_order()
    paid = live.post(f"/api/v1/bills/{bill_id}/payments", json={"amount": "15.00", "method": "CASH"}, key=key())
    payment_id = paid.json()["id"]

    first, second = run_same_key_race(
        db_engine, "orders", order_id,
        "POST", f"/api/v1/payments/{payment_id}/refunds",
        json={"amount": "5.00", "reason": "Cold"}, headers=live.headers("refund-once"),
    )

    assert (first.status_code, second.status_code) == (201, 201), (first.text, second.text)
    assert first.json() == second.json()
    assert [r.headers.get(REPLAYED_HEADER) for r in (first, second)] == [None, "true"]
    assert live.count("refunds", "payment_id = :p", p=payment_id) == 1


def test_a_cash_movement_sent_twice_at_once_with_one_key_is_recorded_once(db_engine, live):
    shift_id = live.r.cash_shift_id

    first, second = run_same_key_race(
        db_engine, "cash_shifts", shift_id,
        "POST", f"/api/v1/cash/shifts/{shift_id}/movements",
        json={"type": "CASH_OUT", "amount": "40.00", "reason": "Safe drop"}, headers=live.headers("drop-once"),
    )

    assert (first.status_code, second.status_code) == (201, 201), (first.text, second.text)
    assert second.headers.get(REPLAYED_HEADER) == "true"
    assert live.count("cash_movements", "cash_shift_id = :s", s=shift_id) == 1


def test_closing_the_day_while_a_payment_runs_never_loses_it(db_engine, live):
    """A payment holds the open day FOR SHARE until it commits; the day
    close (FOR UPDATE) waits for it, then finds the payment's shift still
    open and is refused. The payment is recorded in the day, which stays
    open (a day closes only once its shifts are counted)."""

    _, _, bill_id = live.billed_order()
    day_id = live.r.business_day_id

    # The payment takes the day FOR SHARE, then waits at the gate on the
    # shift; the close queues behind it on the day.
    paid, closed = ordered(
        db_engine, "cash_shifts", live.r.cash_shift_id,
        ("POST", f"/api/v1/bills/{bill_id}/payments",
         {"json": {"amount": "15.00", "method": "CASH"}, "headers": live.headers(key())}),
        ("POST", f"/api/v1/business-days/{day_id}/close", {"headers": live.headers()}),
    )

    assert paid.status_code == 201, paid.text
    assert closed.status_code == 409, closed.text
    assert closed.json()["error"]["code"] == "CASH_SHIFT_STILL_OPEN"
    assert live.scalar("SELECT status FROM business_days WHERE id = :d", d=day_id) == "OPEN"
    assert live.count("payments", "business_day_id = :d", d=day_id) == 1


def test_concurrent_business_day_openings_open_one(db_engine, live):
    shift_id, day_id = live.r.cash_shift_id, live.r.business_day_id
    assert live.post(f"/api/v1/cash/shifts/{shift_id}/close", json={"counted_amount": "100.00"}, key=key()).status_code == 200
    assert live.post(f"/api/v1/business-days/{day_id}/close").status_code == 200

    first, second = ordered(
        db_engine, "restaurants", live.r.restaurant_id,
        ("POST", "/api/v1/business-days", {"json": {}, "headers": live.headers()}),
        ("POST", "/api/v1/business-days", {"json": {}, "headers": live.headers()}),
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "BUSINESS_DAY_ALREADY_OPEN"
    assert live.count("business_days", "restaurant_id = :r AND status = 'OPEN'", r=live.r.restaurant_id) == 1


def test_an_opening_correction_before_the_close_is_in_the_closing(db_engine, live):
    shift_id = live.r.cash_shift_id

    correction, close = ordered(
        db_engine, "cash_shifts", shift_id,
        ("POST", f"/api/v1/cash/shifts/{shift_id}/opening-corrections",
         {"json": {"opening_amount": "120.00", "reason": "Typo"}, "headers": live.headers(key())}),
        ("POST", f"/api/v1/cash/shifts/{shift_id}/close", {"json": {"counted_amount": "120.00"}, "headers": live.headers(key())}),
    )

    assert correction.status_code == 201, correction.text
    assert close.status_code == 200, close.text
    assert shift_column(live, "expected_amount", shift_id) == Decimal("120.00")
    assert shift_column(live, "difference", shift_id) == Decimal("0.00")


def test_an_opening_correction_after_the_close_is_refused(db_engine, live):
    shift_id = live.r.cash_shift_id

    close, correction = ordered(
        db_engine, "cash_shifts", shift_id,
        ("POST", f"/api/v1/cash/shifts/{shift_id}/close", {"json": {"counted_amount": "100.00"}, "headers": live.headers(key())}),
        ("POST", f"/api/v1/cash/shifts/{shift_id}/opening-corrections",
         {"json": {"opening_amount": "120.00", "reason": "Typo"}, "headers": live.headers(key())}),
    )

    assert close.status_code == 200, close.text
    assert correction.status_code == 409, correction.text
    assert correction.json()["error"]["code"] == "CASH_SHIFT_NOT_OPEN"
    assert live.count("cash_opening_corrections", "cash_shift_id = :s", s=shift_id) == 0
    assert shift_column(live, "expected_amount", shift_id) == Decimal("100.00")
