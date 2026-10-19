"""The outbox is written in the business change's own transaction.

Requests go through the real application with its own sessions and real
commits; state is read back on separate connections. Failures are real:
a crash between the flushed change and the commit, and an outbox insert
that PostgreSQL itself rejects (a CHECK constraint), not mocked sessions.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.unit_of_work import owned_transaction
from app.schemas.order import OrderDetailResponse
from app.schemas.outbox import (
    OrderSentV1,
    PreparationStatusChangedV1,
)
from app.services.order_service import OrderService
from app.services.outbox_service import OutboxService
from tests.integration.test_mutation_concurrency import Live


@pytest.fixture
def live(db_engine, committed_restaurants):
    return Live(db_engine, committed_restaurants())


def send_state(live, order_id):
    """Everything a send changes, as committed."""

    return {
        "order": live.scalar("SELECT status FROM orders WHERE id = :o", o=order_id),
        "sent_items": live.count(
            "order_items", "order_id = :o AND status = 'SENT'", o=order_id
        ),
        "preparations": live.count(
            "preparations p JOIN order_items i ON i.id = p.order_item_id",
            "i.order_id = :o",
            o=order_id,
        ),
        "audit": live.events("order.sent"),
        "outbox": live.outbox_count("order.sent"),
    }


UNSENT = {"order": "OPEN", "sent_items": 0, "preparations": 0, "audit": 0, "outbox": 0}
SENT = {"order": "SENT", "sent_items": 2, "preparations": 2, "audit": 1, "outbox": 1}


def preparation_state(live, preparation_id):
    return {
        "status": live.scalar(
            "SELECT status FROM preparations WHERE id = :p", p=preparation_id
        ),
        "audit": live.events("preparation.started"),
        "outbox": live.outbox_count("preparation.status_changed"),
    }


def sent_burger(live):
    order_id, (burger_id, _) = live.open_order()
    assert live.post(f"/api/v1/orders/{order_id}/send").status_code == 200
    preparation_id = live.scalar(
        "SELECT id FROM preparations WHERE order_item_id = :i", i=burger_id
    )
    return order_id, burger_id, preparation_id


def test_committed_send_and_its_event_are_visible_together(live):
    order_id, _ = live.open_order()

    response = live.post(f"/api/v1/orders/{order_id}/send", key="send")

    assert response.status_code == 200
    assert send_state(live, order_id) == SENT
    [event] = live.outbox("order.sent")
    assert str(event["aggregate_id"]) == order_id
    assert event["restaurant_id"] == live.r.restaurant_id
    assert event["published_at"] is None


def test_crash_before_commit_leaves_neither_send_nor_event(live, monkeypatch):
    """Fails after the send and its event are flushed, before the commit;
    the retry with the same key then sends once, with one event."""

    order_id, _ = live.open_order()
    original_validate = OrderDetailResponse.model_validate

    def fail_once(*args, **kwargs):
        monkeypatch.setattr(OrderDetailResponse, "model_validate", original_validate)
        raise RuntimeError("simulated crash before commit")

    monkeypatch.setattr(OrderDetailResponse, "model_validate", fail_once)
    path = f"/api/v1/orders/{order_id}/send"

    crashed = live.post(path, key="send")

    assert crashed.status_code == 500
    assert send_state(live, order_id) == UNSENT
    assert live.stored_keys() == 0

    retried = live.post(path, key="send")

    assert retried.status_code == 200
    assert send_state(live, order_id) == SENT


@pytest.mark.parametrize("key", [None, "send"])
def test_rejected_outbox_insert_rolls_back_the_send(live, monkeypatch, key):
    """PostgreSQL refuses the event (CHECK schema_version >= 1): the send
    must not commit without it, with or without an Idempotency-Key."""

    order_id, _ = live.open_order()
    monkeypatch.setattr(OrderSentV1, "schema_version", 0)

    failed = live.post(f"/api/v1/orders/{order_id}/send", key=key)

    assert failed.status_code == 500
    assert send_state(live, order_id) == UNSENT
    assert live.stored_keys() == 0

    monkeypatch.undo()
    assert live.post(f"/api/v1/orders/{order_id}/send", key=key).status_code == 200
    assert send_state(live, order_id) == SENT


def test_rejected_outbox_insert_rolls_back_the_transition(live, monkeypatch):
    _, _, preparation_id = sent_burger(live)
    monkeypatch.setattr(PreparationStatusChangedV1, "schema_version", 0)
    path = f"/api/v1/preparations/{preparation_id}/start"

    failed = live.post(path)

    assert failed.status_code == 500
    assert preparation_state(live, preparation_id) == {
        "status": "PENDING",
        "audit": 0,
        "outbox": 0,
    }

    monkeypatch.undo()
    assert live.post(path).status_code == 200
    assert preparation_state(live, preparation_id) == {
        "status": "IN_PROGRESS",
        "audit": 1,
        "outbox": 1,
    }


def test_rejected_outbox_insert_rolls_back_the_cancellation(live, monkeypatch):
    order_id, burger_id, preparation_id = sent_burger(live)
    monkeypatch.setattr(PreparationStatusChangedV1, "schema_version", 0)
    path = f"/api/v1/orders/{order_id}/items/{burger_id}/cancel"
    body = {"reason": "Out of stock"}

    failed = live.post(path, json=body, key="cancel")

    assert failed.status_code == 500
    assert live.scalar(
        "SELECT status FROM order_items WHERE id = :i", i=burger_id
    ) == "SENT"
    assert live.scalar(
        "SELECT status FROM preparations WHERE id = :p", p=preparation_id
    ) == "PENDING"
    assert live.events("item.cancelled") == 0
    assert live.events("preparation.cancelled") == 0
    assert live.outbox_count("preparation.status_changed") == 0
    assert live.stored_keys() == 0


def test_rolled_back_service_transaction_leaves_no_event(db_engine, live):
    """The owner of the transaction rolls back after the service's unit of
    work: the transition and its event disappear together, and recording
    the event did not commit anything on its own."""

    _, _, preparation_id = sent_burger(live)

    with Session(db_engine) as db, owned_transaction(db):
        OrderService.update_preparation_status(
            db=db,
            restaurant_id=live.r.restaurant_id,
            user_id=live.r.user_id,
            preparation_id=preparation_id,
            new_status="IN_PROGRESS",
            station_scope=None,
        )
        # Flushed in this transaction, invisible to other connections.
        assert db.scalar(
            text("SELECT count(*) FROM outbox_events WHERE aggregate_type = 'order'"
                 " AND payload->>'preparation_id' = :p"),
            {"p": str(preparation_id)},
        ) == 1
        assert live.outbox_count("preparation.status_changed") == 0
        db.rollback()

    assert preparation_state(live, preparation_id) == {
        "status": "PENDING",
        "audit": 0,
        "outbox": 0,
    }


def test_recording_an_event_neither_flushes_nor_commits(db_engine, live):
    order_id, _, preparation_id = sent_burger(live)

    with Session(db_engine) as db:
        event = OutboxService.record(
            db=db,
            restaurant_id=live.r.restaurant_id,
            payload=PreparationStatusChangedV1(
                preparation_id=preparation_id,
                order_id=order_id,
                order_item_id=preparation_id,
                station_id=preparation_id,
                previous_status="PENDING",
                status="IN_PROGRESS",
            ),
        )

        # Pending in the session only: nothing was sent to PostgreSQL.
        assert event in db.new
        assert live.outbox_count("preparation.status_changed") == 0

    # Closed without a commit by its caller: never persisted.
    assert live.outbox_count("preparation.status_changed") == 0
