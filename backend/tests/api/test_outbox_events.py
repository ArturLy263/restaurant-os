"""Transactional outbox (Epic D1): which mutations write which events.

Runs through the API on the rolled-back test session. Atomicity against
real commits is covered in tests/integration/test_outbox_atomicity.py.
"""

import uuid

import pytest
from pydantic import ValidationError

from app.api.idempotency import IDEMPOTENCY_KEY_HEADER, REPLAYED_HEADER
from app.db.models.outbox_event import OutboxEvent
from app.schemas.outbox import (
    OrderSentV1,
    OutboxPayload,
    PreparationStatusChangedV1,
)
from tests.factories import (
    assign_station,
    auth_headers,
    create_restaurant,
    create_user,
)


SENT = "order.sent"
CHANGED = "preparation.status_changed"

ORDER_SENT_KEYS = {"order_id", "table_id", "initial", "items"}
SENT_ITEM_KEYS = {
    "order_item_id",
    "preparation_id",
    "station_id",
    "product_id",
    "quantity",
}
STATUS_CHANGED_KEYS = {
    "preparation_id",
    "order_id",
    "order_item_id",
    "station_id",
    "previous_status",
    "status",
}


def outbox(db, restaurant, event_type=None):
    db.expire_all()
    query = db.query(OutboxEvent).filter(
        OutboxEvent.restaurant_id == restaurant.restaurant.id
    )

    if event_type is not None:
        query = query.filter(OutboxEvent.event_type == event_type)

    return query.order_by(OutboxEvent.created_at, OutboxEvent.id).all()


def transition(pos, preparation_id, action, headers=None):
    return pos.client.post(
        f"/api/v1/preparations/{preparation_id}/{action}",
        headers=headers or pos.headers,
    )


# --- order.sent ---


def test_send_writes_one_order_sent_event(pos, db_session, restaurant):
    order_id, burger_id, coke_id = pos.open_order()

    assert pos.send(order_id).status_code == 200

    [event] = outbox(db_session, restaurant)
    burger, coke = pos.preparation(burger_id), pos.preparation(coke_id)

    assert isinstance(event.id, uuid.UUID)
    assert event.event_type == "order.sent"
    assert event.schema_version == 1
    assert event.restaurant_id == restaurant.restaurant.id
    assert event.aggregate_type == "order"
    assert str(event.aggregate_id) == order_id
    assert event.created_at is not None
    assert event.published_at is None
    assert event.payload == {
        "order_id": order_id,
        "table_id": str(restaurant.table.id),
        "initial": True,
        "items": [
            {
                "order_item_id": burger_id,
                "preparation_id": str(burger.id),
                "station_id": str(burger.station_id),
                "product_id": str(pos.burger.id),
                "quantity": 1,
            },
            {
                "order_item_id": coke_id,
                "preparation_id": str(coke.id),
                "station_id": str(coke.station_id),
                "product_id": str(pos.coke.id),
                "quantity": 1,
            },
        ],
    }


def test_each_send_round_lists_only_its_own_items(pos, db_session, restaurant):
    order_id, _, _ = pos.sent_order()
    water = pos.add_item(order_id, pos.coke).json()["id"]

    assert pos.send(order_id).status_code == 200

    first, second = outbox(db_session, restaurant, SENT)
    assert first.payload["initial"] is True
    assert len(first.payload["items"]) == 2
    assert second.payload["initial"] is False
    assert [i["order_item_id"] for i in second.payload["items"]] == [water]
    assert second.aggregate_id == first.aggregate_id


def test_rejected_sends_write_no_event(pos, db_session, restaurant):
    order_id = pos.create_order().json()["id"]

    # Nothing to send yet, then nothing new to send.
    assert pos.send(order_id).status_code == 400
    pos.add_item(order_id, pos.burger)
    assert pos.send(order_id).status_code == 200
    assert pos.send(order_id).status_code == 409

    assert len(outbox(db_session, restaurant)) == 1


def test_replayed_send_writes_no_second_event(pos, db_session, restaurant):
    order_id, _, _ = pos.open_order()
    headers = {**pos.headers, IDEMPOTENCY_KEY_HEADER: "send-once"}
    path = f"/api/v1/orders/{order_id}/send"

    first = pos.client.post(path, headers=headers)
    replay = pos.client.post(path, headers=headers)

    assert first.status_code == replay.status_code == 200
    assert replay.headers[REPLAYED_HEADER] == "true"
    assert replay.json() == first.json()
    assert len(outbox(db_session, restaurant)) == 1


# --- preparation.status_changed ---


def test_each_preparation_transition_writes_one_event(
    pos, db_session, restaurant
):
    order_id, burger_id, _ = pos.sent_order()
    preparation = pos.preparation(burger_id)

    for action in ("start", "ready", "serve"):
        assert transition(pos, preparation.id, action).status_code == 200

    events = outbox(db_session, restaurant, CHANGED)

    assert [(e.payload["previous_status"], e.payload["status"]) for e in events] == [
        ("PENDING", "IN_PROGRESS"),
        ("IN_PROGRESS", "READY"),
        ("READY", "SERVED"),
    ]
    for event in events:
        assert event.schema_version == 1
        assert event.restaurant_id == restaurant.restaurant.id
        assert event.aggregate_type == "order"
        assert str(event.aggregate_id) == order_id
        assert event.payload == {
            "preparation_id": str(preparation.id),
            "order_id": order_id,
            "order_item_id": burger_id,
            "station_id": str(preparation.station_id),
            "previous_status": event.payload["previous_status"],
            "status": event.payload["status"],
        }

    # Events are distinct, ordered occurrences.
    assert len({e.id for e in events}) == 3
    assert [e.created_at for e in events] == sorted(e.created_at for e in events)


def test_repeated_and_invalid_transitions_write_no_event(
    pos, db_session, restaurant
):
    _, burger_id, coke_id = pos.sent_order()
    burger, coke = pos.preparation(burger_id), pos.preparation(coke_id)

    assert transition(pos, burger.id, "start").status_code == 200
    assert transition(pos, burger.id, "start").status_code == 409
    assert transition(pos, burger.id, "start").status_code == 409
    assert transition(pos, coke.id, "serve").status_code == 409
    assert transition(pos, uuid.uuid4(), "start").status_code == 404

    [event] = outbox(db_session, restaurant, CHANGED)
    assert event.payload["preparation_id"] == str(burger.id)
    assert event.payload["status"] == "IN_PROGRESS"


@pytest.mark.parametrize("started", [False, True])
def test_cancelling_a_sent_item_writes_a_cancelled_event(
    pos, db_session, restaurant, started
):
    order_id, burger_id, _ = pos.sent_order()
    preparation = pos.preparation(burger_id)
    if started:
        pos.start_preparation(burger_id, "start")

    assert pos.cancel(order_id, burger_id).status_code == 200

    event = outbox(db_session, restaurant, CHANGED)[-1]
    assert event.payload == {
        "preparation_id": str(preparation.id),
        "order_id": order_id,
        "order_item_id": burger_id,
        "station_id": str(preparation.station_id),
        "previous_status": "IN_PROGRESS" if started else "PENDING",
        "status": "CANCELLED",
    }


def test_cancelling_a_billed_item_writes_a_cancelled_event(
    pos, db_session, restaurant
):
    _, burger_id, _, bill_id = pos.billed_order()

    assert pos.cancel_billed(bill_id, burger_id).status_code == 200

    [event] = outbox(db_session, restaurant, CHANGED)
    assert event.payload["order_item_id"] == burger_id
    assert event.payload["status"] == "CANCELLED"


def test_cancelling_an_unsent_item_writes_no_event(pos, db_session, restaurant):
    order_id, burger_id, _ = pos.open_order()

    assert pos.cancel(order_id, burger_id).status_code == 200

    assert outbox(db_session, restaurant) == []


def test_rejected_cancellation_writes_no_event(pos, db_session, restaurant):
    order_id, burger_id, _ = pos.sent_order()
    pos.start_preparation(burger_id, "start", "ready")
    before = len(outbox(db_session, restaurant))

    response = pos.cancel(order_id, burger_id)

    assert response.status_code == 409
    assert len(outbox(db_session, restaurant)) == before


def test_replayed_cancellation_writes_no_second_event(
    pos, db_session, restaurant
):
    order_id, burger_id, _ = pos.sent_order()
    headers = {**pos.headers, IDEMPOTENCY_KEY_HEADER: "cancel-once"}
    path = f"/api/v1/orders/{order_id}/items/{burger_id}/cancel"
    body = {"reason": "Customer changed mind"}

    first = pos.client.post(path, json=body, headers=headers)
    replay = pos.client.post(path, json=body, headers=headers)

    assert first.status_code == replay.status_code == 200
    assert replay.headers[REPLAYED_HEADER] == "true"
    assert len(outbox(db_session, restaurant, CHANGED)) == 1


# --- Scope ---


def test_events_are_isolated_by_restaurant(pos, db_session, restaurant):
    other = create_restaurant(db_session, name="Other Restaurant")
    other_headers = auth_headers(other.user)

    _, burger_id, _ = pos.sent_order()
    burger = pos.preparation(burger_id)

    other_order = pos.client.post(
        "/api/v1/orders",
        json={"table_id": str(other.table.id), "guest_count": 1},
        headers=other_headers,
    ).json()["id"]
    pos.client.post(
        f"/api/v1/orders/{other_order}/items",
        json={"product_id": str(other.product.id), "quantity": 1},
        headers=other_headers,
    )
    assert pos.client.post(
        f"/api/v1/orders/{other_order}/send", headers=other_headers
    ).status_code == 200

    # Another restaurant's preparation is not found and writes nothing.
    assert transition(pos, burger.id, "start", other_headers).status_code == 404

    [own] = outbox(db_session, restaurant)
    [foreign] = outbox(db_session, other)
    assert own.restaurant_id == restaurant.restaurant.id
    assert foreign.restaurant_id == other.restaurant.id
    assert foreign.payload["order_id"] == other_order
    assert {i["product_id"] for i in foreign.payload["items"]} == {
        str(other.product.id)
    }


def test_station_scope_applies_before_any_event(pos, db_session, restaurant):
    _, burger_id, _ = pos.sent_order()
    preparation = pos.preparation(burger_id)

    assigned = create_user(db_session, restaurant, "KITCHEN")
    unassigned = create_user(db_session, restaurant, "KITCHEN")
    assign_station(db_session, assigned, restaurant.station)

    denied = transition(pos, preparation.id, "start", auth_headers(unassigned))
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "STATION_ACCESS_DENIED"
    assert outbox(db_session, restaurant, CHANGED) == []

    allowed = transition(pos, preparation.id, "start", auth_headers(assigned))
    assert allowed.status_code == 200
    assert len(outbox(db_session, restaurant, CHANGED)) == 1


# --- Audit history is unchanged and separate ---


def test_audit_events_and_attribution_are_unchanged(
    pos, db_session, restaurant
):
    cook = create_user(db_session, restaurant, "KITCHEN")
    assign_station(db_session, cook, restaurant.station)

    order_id, burger_id, coke_id = pos.sent_order()
    preparation = pos.preparation(burger_id)
    assert transition(
        pos, preparation.id, "start", auth_headers(cook)
    ).status_code == 200

    [sent] = pos.events("order.sent")
    assert sent.user_id == restaurant.user.id
    assert sent.entity_type == "order"
    assert str(sent.entity_id) == order_id
    assert sent.event_metadata == {
        "order_created_by": str(restaurant.user.id),
        "items_count": 2,
        "item_ids": [burger_id, coke_id],
        "initial": True,
    }

    [started] = pos.events("preparation.started")
    assert started.user_id == cook.id
    assert started.entity_type == "preparation"
    assert started.entity_id == preparation.id
    assert started.event_metadata == {
        "status": "IN_PROGRESS",
        "order_item_id": burger_id,
        "order_id": order_id,
        "order_created_by": str(restaurant.user.id),
    }


def test_payloads_carry_no_actor_or_free_text(pos, db_session, restaurant):
    order_id, burger_id, coke_id = pos.sent_order()
    pos.start_preparation(coke_id, "start")
    pos.cancel(order_id, burger_id, reason="Guest Jane Doe is allergic")

    events = outbox(db_session, restaurant)
    assert [e.event_type for e in events] == [SENT, CHANGED, CHANGED]

    sent, *changes = events
    assert set(sent.payload) == ORDER_SENT_KEYS
    assert all(set(i) == SENT_ITEM_KEYS for i in sent.payload["items"])
    assert all(set(e.payload) == STATUS_CHANGED_KEYS for e in changes)

    text = str([e.payload for e in events])
    assert str(restaurant.user.id) not in text
    assert "Jane" not in text


# --- Contracts ---


def test_event_types_and_versions_are_unique():
    contracts = [
        (model.event_type, model.schema_version)
        for model in OutboxPayload.__subclasses__()
    ]

    assert sorted(contracts) == [
        ("order.sent", 1),
        ("preparation.status_changed", 1),
    ]
    assert len(set(contracts)) == len(contracts)


def test_payloads_reject_unknown_fields_and_statuses():
    ids = {name: uuid.uuid4() for name in STATUS_CHANGED_KEYS - {"previous_status", "status"}}

    with pytest.raises(ValidationError):
        PreparationStatusChangedV1(**ids, previous_status="PENDING", status="COOKED")

    with pytest.raises(ValidationError):
        OrderSentV1(
            order_id=uuid.uuid4(),
            table_id=uuid.uuid4(),
            initial=True,
            items=[],
            user_id=uuid.uuid4(),
        )
