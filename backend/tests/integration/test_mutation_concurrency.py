"""Concurrent and failing critical mutations against real PostgreSQL.

Each request is its own transaction on its own connection and really
commits (see tests/concurrency.py for the synchronization). Data is created
by `committed_restaurants` and deleted afterwards.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.idempotency import IDEMPOTENCY_KEY_HEADER, REPLAYED_HEADER
import app.services.bill_service as bill_service
from app.main import app
from app.schemas.bill import PaymentResponse
from tests.concurrency import concurrency, results


class Live:
    """Sequential API calls and committed-state reads for one restaurant."""

    def __init__(self, engine, restaurant):
        self.engine = engine
        self.r = restaurant
        self.client = TestClient(app, raise_server_exceptions=False)

    def headers(self, key=None):
        if key is None:
            return self.r.headers

        return {**self.r.headers, IDEMPOTENCY_KEY_HEADER: key}

    def post(self, path, json=None, key=None):
        return self.client.post(path, json=json, headers=self.headers(key))

    def open_order(self):
        order = self.post(
            "/api/v1/orders",
            json={"table_id": str(self.r.table_id), "guest_count": 2},
        )
        assert order.status_code == 201, order.text
        order_id = order.json()["id"]

        items = []
        for product_id in (self.r.burger_id, self.r.coke_id):
            item = self.post(
                f"/api/v1/orders/{order_id}/items",
                json={"product_id": str(product_id), "quantity": 1},
            )
            assert item.status_code == 201, item.text
            items.append(item.json()["id"])

        return order_id, items

    def billed_order(self):
        order_id, items = self.open_order()
        assert self.post(f"/api/v1/orders/{order_id}/send").status_code == 200

        bill = self.post(f"/api/v1/bills/order/{order_id}")
        assert bill.status_code == 201, bill.text
        assert bill.json()["total"] == "15.00"

        return order_id, items, bill.json()["id"]

    def scalar(self, sql, **params):
        with self.engine.connect() as connection:
            return connection.scalar(text(sql), params)

    def count(self, table, where, **params):
        return self.scalar(f"SELECT count(*) FROM {table} WHERE {where}", **params)

    def events(self, event_type):
        return self.count(
            "operational_events",
            "restaurant_id = :r AND event_type = :t",
            r=self.r.restaurant_id,
            t=event_type,
        )

    def payments(self, bill_id):
        return self.count("payments", "bill_id = :b", b=bill_id)

    def outbox(self, event_type):
        with self.engine.connect() as connection:
            return connection.execute(
                text(
                    "SELECT * FROM outbox_events "
                    "WHERE restaurant_id = :r AND event_type = :t "
                    "ORDER BY created_at, id"
                ),
                {"r": self.r.restaurant_id, "t": event_type},
            ).mappings().all()

    def outbox_count(self, event_type):
        return len(self.outbox(event_type))

    def stored_keys(self):
        return self.count(
            "idempotency_keys",
            "restaurant_id = :r",
            r=self.r.restaurant_id,
        )


@pytest.fixture
def live(db_engine, committed_restaurants):
    return Live(db_engine, committed_restaurants())


def run_same_key_race(db_engine, gate_table, gate_row, method, path, **kwargs):
    """Two requests with the same key, the second arriving mid-flight.

    The first claims the key and blocks on the gate; the second blocks on
    the first one's uncommitted key. Then the gate opens.
    """

    with concurrency(db_engine) as run:
        gate = run.gate(gate_table, gate_row)

        first = run.start(method, path, **kwargs)
        first_pid = run.wait_for_new_backend_blocked_by(gate.pid, set())

        second = run.start(method, path, **kwargs)
        run.wait_for_new_backend_blocked_by(first_pid, {first_pid})

        gate.release()
        return results(first, second)


def run_parallel(db_engine, gate_table, gate_row, *requests):
    """Requests that all wait on the gate, then run concurrently."""

    with concurrency(db_engine) as run:
        gate = run.gate(gate_table, gate_row)
        futures, waiting = [], set()

        for method, path, kwargs in requests:
            futures.append(run.start(method, path, **kwargs))
            waiting.add(
                run.wait_for_new_backend_blocked_by({gate.pid, *waiting}, waiting)
            )

        gate.release()
        return results(*futures)


def assert_one_executed_one_replayed(first, second, status_code):
    assert first.status_code == status_code, first.text
    assert second.status_code == status_code, second.text
    assert first.json() == second.json()
    assert [
        response.headers.get(REPLAYED_HEADER)
        for response in (first, second)
    ] == [None, "true"]


# --- Same key, concurrent ---


def test_concurrent_payments_with_same_key_pay_once(db_engine, live):
    order_id, _, bill_id = live.billed_order()

    first, second = run_same_key_race(
        db_engine,
        "orders",
        order_id,
        "POST",
        f"/api/v1/bills/{bill_id}/payments",
        json={"amount": "5.00", "method": "CARD"},
        headers=live.headers("pay-race"),
    )

    assert_one_executed_one_replayed(first, second, 201)
    assert live.payments(bill_id) == 1
    assert live.events("payment.completed") == 1
    assert live.stored_keys() == 1


def test_concurrent_sends_with_same_key_send_once(db_engine, live):
    order_id, _ = live.open_order()

    first, second = run_same_key_race(
        db_engine,
        "orders",
        order_id,
        "POST",
        f"/api/v1/orders/{order_id}/send",
        headers=live.headers("send-race"),
    )

    assert_one_executed_one_replayed(first, second, 200)
    assert live.count(
        "preparations p JOIN order_items i ON i.id = p.order_item_id",
        "i.order_id = :o",
        o=order_id,
    ) == 2
    assert live.events("order.sent") == 1
    assert live.outbox_count("order.sent") == 1


def test_concurrent_cancels_with_same_key_adjust_once(db_engine, live):
    order_id, (burger_id, _), bill_id = live.billed_order()

    first, second = run_same_key_race(
        db_engine,
        "orders",
        order_id,
        "POST",
        f"/api/v1/bills/{bill_id}/items/{burger_id}/cancel",
        json={"reason": "Wrong dish"},
        headers=live.headers("cancel-race"),
    )

    assert_one_executed_one_replayed(first, second, 200)
    assert live.count("bill_adjustments", "bill_id = :b", b=bill_id) == 1
    assert live.events("bill.adjusted") == 1
    assert live.events("item.cancelled") == 1
    [cancelled] = live.outbox("preparation.status_changed")
    assert cancelled["payload"]["order_item_id"] == burger_id
    assert cancelled["payload"]["status"] == "CANCELLED"


def test_concurrent_order_creation_with_same_key_creates_one(db_engine, live):
    first, second = run_same_key_race(
        db_engine,
        "tables",
        live.r.table_id,
        "POST",
        "/api/v1/orders",
        json={"table_id": str(live.r.table_id), "guest_count": 2},
        headers=live.headers("open-race"),
    )

    assert_one_executed_one_replayed(first, second, 201)
    assert live.count("orders", "table_id = :t", t=live.r.table_id) == 1
    assert live.events("order.created") == 1


def test_concurrent_reuse_of_an_expired_key_runs_once(db_engine, live):
    """Two requests reuse an expired key at the same time.

    The first deletes the expired record and claims the key; the second
    blocks on that delete (a row lock, not the unique index), then finds
    the first one's new record and replays it.
    """

    order_id, _, bill_id = live.billed_order()
    path = f"/api/v1/bills/{bill_id}/payments"
    body = {"amount": "5.00", "method": "CASH"}

    original = live.post(path, json=body, key="reused")
    assert original.status_code == 201

    with db_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE idempotency_keys SET expires_at = now() "
                "WHERE restaurant_id = :r AND idempotency_key = 'reused'"
            ),
            {"r": live.r.restaurant_id},
        )

    first, second = run_same_key_race(
        db_engine,
        "orders",
        order_id,
        "POST",
        path,
        json=body,
        headers=live.headers("reused"),
    )

    assert_one_executed_one_replayed(first, second, 201)
    assert first.json()["id"] != original.json()["id"]
    assert live.payments(bill_id) == 2
    assert live.events("payment.completed") == 2
    assert live.stored_keys() == 1
    assert live.scalar(
        "SELECT expires_at > now() FROM idempotency_keys WHERE restaurant_id = :r",
        r=live.r.restaurant_id,
    )


# --- Different keys, same protected operation ---


def test_concurrent_full_payments_with_different_keys(db_engine, live):
    order_id, _, bill_id = live.billed_order()
    path = f"/api/v1/bills/{bill_id}/payments"
    body = {"amount": "15.00", "method": "CASH"}

    responses = run_parallel(
        db_engine,
        "orders",
        order_id,
        ("POST", path, {"json": body, "headers": live.headers("a")}),
        ("POST", path, {"json": body, "headers": live.headers("b")}),
    )

    assert sorted(r.status_code for r in responses) == [201, 409]
    [conflict] = [r for r in responses if r.status_code == 409]
    assert conflict.json()["error"]["code"] == "BILL_NOT_OPEN"
    assert conflict.json()["error"]["request_id"]

    assert live.payments(bill_id) == 1
    assert live.scalar("SELECT status FROM bills WHERE id = :b", b=bill_id) == "PAID"
    # Only the successful request's key is stored.
    assert live.stored_keys() == 1


def test_concurrent_sends_with_different_keys(db_engine, live):
    order_id, _ = live.open_order()
    path = f"/api/v1/orders/{order_id}/send"

    responses = run_parallel(
        db_engine,
        "orders",
        order_id,
        ("POST", path, {"headers": live.headers("a")}),
        ("POST", path, {"headers": live.headers("b")}),
    )

    assert sorted(r.status_code for r in responses) == [200, 409]
    [conflict] = [r for r in responses if r.status_code == 409]
    assert conflict.json()["error"]["code"] == "ORDER_HAS_NO_ITEMS_TO_SEND"
    assert live.events("order.sent") == 1
    assert live.outbox_count("order.sent") == 1


@pytest.mark.parametrize("keys", [(None, None), ("a", "b")], ids=["no keys", "different keys"])
def test_concurrent_order_creation_for_one_table(db_engine, live, keys):
    body = {"table_id": str(live.r.table_id), "guest_count": 2}

    responses = run_parallel(
        db_engine,
        "tables",
        live.r.table_id,
        *[
            ("POST", "/api/v1/orders", {"json": body, "headers": live.headers(key)})
            for key in keys
        ],
    )

    assert sorted(r.status_code for r in responses) == [201, 409]
    [conflict] = [r for r in responses if r.status_code == 409]
    assert conflict.json()["error"]["code"] == "TABLE_NOT_AVAILABLE"

    assert live.count(
        "orders",
        "table_id = :t AND status IN ('OPEN', 'SENT')",
        t=live.r.table_id,
    ) == 1
    assert live.scalar(
        "SELECT status FROM tables WHERE id = :t", t=live.r.table_id
    ) == "OCCUPIED"
    assert live.events("order.created") == 1


# --- Failure atomicity ---


def test_failure_after_the_mutation_commits_nothing_and_retry_runs(
    db_engine,
    live,
    monkeypatch,
):
    """The critical window: business change flushed, result not stored.

    A failure there must roll back the payment, its events and the key
    together; a retry with the same key then runs the payment once.
    """

    order_id, _, bill_id = live.billed_order()
    original_validate = PaymentResponse.model_validate

    def fail_once(*args, **kwargs):
        monkeypatch.setattr(PaymentResponse, "model_validate", original_validate)
        raise RuntimeError("simulated crash before the key's result is stored")

    monkeypatch.setattr(PaymentResponse, "model_validate", fail_once)

    body = {"amount": "15.00", "method": "CASH"}
    path = f"/api/v1/bills/{bill_id}/payments"

    crashed = live.post(path, json=body, key="final")

    assert crashed.status_code == 500
    assert crashed.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "simulated" not in crashed.text
    assert live.payments(bill_id) == 0
    assert live.events("payment.completed") == 0
    assert live.events("order.closed") == 0
    assert live.stored_keys() == 0
    assert live.scalar("SELECT status FROM bills WHERE id = :b", b=bill_id) == "OPEN"
    assert live.scalar("SELECT status FROM orders WHERE id = :o", o=order_id) == "SENT"

    retried = live.post(path, json=body, key="final")

    assert retried.status_code == 201
    assert REPLAYED_HEADER not in retried.headers
    assert live.payments(bill_id) == 1
    assert live.events("order.closed") == 1
    assert live.stored_keys() == 1

    replayed = live.post(path, json=body, key="final")
    assert replayed.json() == retried.json()
    assert replayed.headers[REPLAYED_HEADER] == "true"


def test_waiting_duplicate_runs_itself_when_the_first_request_fails(
    db_engine,
    live,
):
    """Same key in flight, but the first request fails.

    Nothing is stored for the failure, so the waiting duplicate is not
    given a replay: it runs the operation itself (and here fails the same
    way, since the state has not changed). A later retry, once the cause
    is fixed, succeeds.
    """

    order_id, _, bill_id = live.billed_order()
    path = f"/api/v1/bills/{bill_id}/payments"
    body = {"amount": "17.50", "method": "CASH"}

    # An unsent item makes the final payment fail with 409.
    unsent = live.post(
        f"/api/v1/orders/{order_id}/items",
        json={"product_id": str(live.r.coke_id), "quantity": 1},
    )
    assert unsent.status_code == 201

    first, second = run_same_key_race(
        db_engine,
        "orders",
        order_id,
        "POST",
        path,
        json=body,
        headers=live.headers("k"),
    )

    for response in (first, second):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "ORDER_HAS_UNSENT_ITEMS"
        assert REPLAYED_HEADER not in response.headers

    assert (
        first.json()["error"]["request_id"]
        != second.json()["error"]["request_id"]
    )
    assert live.payments(bill_id) == 0
    assert live.stored_keys() == 0

    assert live.post(f"/api/v1/orders/{order_id}/send").status_code == 200

    retried = live.post(path, json=body, key="k")
    assert retried.status_code == 201
    assert REPLAYED_HEADER not in retried.headers
    assert live.payments(bill_id) == 1
    assert live.stored_keys() == 1


def test_service_committing_directly_cannot_split_an_idempotent_request(
    live,
    monkeypatch,
):
    """Guard against a service that bypasses unit_of_work.commit(db).

    Simulates that regression in the payment service: with a key, the
    direct commit is refused, so neither the payment nor a key without its
    response is persisted; without a key, the same code commits as usual.
    """

    monkeypatch.setattr(bill_service, "commit", lambda db: db.commit())

    order_id, _, bill_id = live.billed_order()
    path = f"/api/v1/bills/{bill_id}/payments"
    body = {"amount": "5.00", "method": "CASH"}

    refused = live.post(path, json=body, key="direct-commit")

    assert refused.status_code == 500
    assert refused.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "OwnedTransaction" not in refused.text
    assert live.payments(bill_id) == 0
    assert live.events("payment.completed") == 0
    assert live.stored_keys() == 0

    plain = live.post(path, json=body)

    assert plain.status_code == 201
    assert live.payments(bill_id) == 1


def test_preparation_transition_waits_for_a_queued_cancellation(db_engine, live):
    """Transitions lock the order, so they cannot revive a cancelled dish.

    A cancellation and then a transition queue on the order lock; the
    transition runs second and sees the preparation already CANCELLED.
    """

    order_id, (burger_id, _) = live.open_order()
    assert live.post(f"/api/v1/orders/{order_id}/send").status_code == 200
    preparation_id = live.scalar(
        "SELECT id FROM preparations WHERE order_item_id = :i", i=burger_id
    )

    with concurrency(db_engine) as run:
        gate = run.gate("orders", order_id)

        cancel = run.start(
            "POST",
            f"/api/v1/orders/{order_id}/items/{burger_id}/cancel",
            json={"reason": "Out of stock"},
            headers=live.headers(),
        )
        cancel_pid = run.wait_for_new_backend_blocked_by(gate.pid, set())

        start = run.start(
            "POST",
            f"/api/v1/preparations/{preparation_id}/start",
            headers=live.headers(),
        )
        run.wait_for_new_backend_blocked_by({gate.pid, cancel_pid}, {cancel_pid})

        gate.release()
        cancelled, started = results(cancel, start)

    assert cancelled.status_code == 200
    assert started.status_code == 409
    assert started.json()["error"]["code"] == "INVALID_PREPARATION_TRANSITION"
    assert live.scalar(
        "SELECT status FROM preparations WHERE id = :p", p=preparation_id
    ) == "CANCELLED"
    assert live.events("preparation.started") == 0
    # Only the cancellation, which won the lock, has an event.
    [event] = live.outbox("preparation.status_changed")
    assert event["payload"]["previous_status"] == "PENDING"
    assert event["payload"]["status"] == "CANCELLED"
