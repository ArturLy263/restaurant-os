"""Outbox event contracts (Epic D3): versions, validation, compatibility.

The golden payloads below are as written since each version was added
(v1 since D1, preparation.status_changed v2 for discards). They must keep
parsing to the same model and dumping to the same JSON: a change that
breaks them is a new version, not an edit (see app.schemas.outbox).
"""

import copy
import uuid

import pytest

from app.schemas.outbox import (
    CONTRACTS,
    ORDER_AGGREGATE,
    PREPARATION_TRANSITIONS_V1,
    PREPARATION_TRANSITIONS_V2,
    EventContractError,
    InvalidEventPayloadError,
    OrderSentV1,
    OutboxPayload,
    PreparationStatusChangedV1,
    PreparationStatusChangedV2,
    SentOrderItem,
    UnsupportedEventError,
    dump_payload,
    parse_payload,
    preparation_status_changed,
)


ORDER = "0b6a3c4e-7f0d-4b8e-9a51-2c7d1e3f4a50"
TABLE = "1c7b4d5f-8a1e-4c9f-8b62-3d8e2f4a5b61"
ITEM_1 = "2d8c5e6a-9b2f-4dab-9c73-4e9f3a5b6c72"
ITEM_2 = "3e9d6f7b-ac3a-4ebc-8d84-5fa04b6c7d83"
PREP_1 = "4fae7a8c-bd4b-4fcd-9e95-6ab15c7d8e94"
PREP_2 = "5abf8b9d-ce5c-4ade-8fa6-7bc26d8e9fa5"
STATION = "6bca9cae-df6d-4bef-9ab7-8cd37e9fa0b6"
PRODUCT = "7cdbadbf-ea7e-4cfa-8bc8-9de48fa0b1c7"

GOLDEN = {
    ("order.sent", 1): {
        "order_id": ORDER,
        "table_id": TABLE,
        "initial": True,
        "items": [
            {
                "order_item_id": ITEM_1,
                "preparation_id": PREP_1,
                "station_id": STATION,
                "product_id": PRODUCT,
                "quantity": 2,
            },
            {
                "order_item_id": ITEM_2,
                "preparation_id": PREP_2,
                "station_id": STATION,
                "product_id": PRODUCT,
                "quantity": 1,
            },
        ],
    },
    ("preparation.status_changed", 1): {
        "preparation_id": PREP_1,
        "order_id": ORDER,
        "order_item_id": ITEM_1,
        "station_id": STATION,
        "previous_status": "PENDING",
        "status": "IN_PROGRESS",
    },
    ("preparation.status_changed", 2): {
        "preparation_id": PREP_1,
        "order_id": ORDER,
        "order_item_id": ITEM_1,
        "station_id": STATION,
        "previous_status": "READY",
        "status": "CANCELLED",
    },
}


def golden(key):
    return copy.deepcopy(GOLDEN[key])


def sent(**changes):
    payload = golden(("order.sent", 1))
    payload.update(changes)
    return payload


def sent_item(**changes):
    payload = sent()
    payload["items"][0].update(changes)
    return payload


def changed(**changes):
    payload = golden(("preparation.status_changed", 1))
    payload.update(changes)
    return payload


# --- Registry ---


def test_registry_lists_exactly_the_supported_contracts():
    assert dict(CONTRACTS) == {
        ("order.sent", 1): OrderSentV1,
        ("preparation.status_changed", 1): PreparationStatusChangedV1,
        ("preparation.status_changed", 2): PreparationStatusChangedV2,
    }

    # Every payload model is registered under its own type and version.
    assert set(CONTRACTS.values()) == set(OutboxPayload.__subclasses__())
    for (event_type, version), contract in CONTRACTS.items():
        assert (contract.event_type, contract.schema_version) == (event_type, version)
        assert contract.aggregate_type == ORDER_AGGREGATE


def test_registry_is_read_only():
    with pytest.raises(TypeError):
        CONTRACTS[("order.sent", 2)] = OrderSentV1


def test_every_contract_has_a_golden_payload():
    assert set(GOLDEN) == set(CONTRACTS)


# --- Supported and unsupported events ---


@pytest.mark.parametrize("key", sorted(GOLDEN))
def test_golden_payloads_parse_and_dump_unchanged(key):
    event_type, version = key

    model = parse_payload(event_type, version, golden(key))

    assert type(model) is CONTRACTS[key]
    assert str(model.aggregate_id) == ORDER
    assert dump_payload(model) == GOLDEN[key]


@pytest.mark.parametrize(
    "event_type, version",
    [
        ("order.sent", 2),
        ("order.sent", 0),
        ("preparation.status_changed", 3),
        ("order.closed", 1),
        ("ORDER.SENT", 1),
        ("", 1),
    ],
)
def test_unknown_type_or_version_is_unsupported_not_invalid(event_type, version):
    with pytest.raises(UnsupportedEventError) as raised:
        parse_payload(event_type, version, golden(("order.sent", 1)))

    assert not isinstance(raised.value, InvalidEventPayloadError)
    assert (raised.value.event_type, raised.value.schema_version) == (
        event_type,
        version,
    )


def test_a_versions_payload_is_not_read_as_another_type():
    with pytest.raises(InvalidEventPayloadError):
        parse_payload("preparation.status_changed", 1, golden(("order.sent", 1)))


# --- Malformed payloads ---


MALFORMED_SENT = {
    "missing field": {k: v for k, v in sent().items() if k != "table_id"},
    "unknown field": sent(user_id=TABLE),
    "null field": sent(table_id=None),
    "not a uuid": sent(order_id="order-1"),
    "uuid as int": sent(order_id=123),
    "upper-case uuid": sent(order_id=ORDER.upper()),
    "uuid without hyphens": sent(order_id=ORDER.replace("-", "")),
    "bool as int": sent(initial=1),
    "bool as string": sent(initial="true"),
    "items not a list": sent(items={}),
    "no items": sent(items=[]),
    "repeated item": sent(items=[golden(("order.sent", 1))["items"][0]] * 2),
    "repeated preparation": sent_item(preparation_id=PREP_2),
    "item unknown field": sent_item(price="9.50"),
    "item missing field": {
        **sent(),
        "items": [{k: v for k, v in sent()["items"][0].items() if k != "station_id"}],
    },
    "quantity as string": sent_item(quantity="2"),
    "quantity as float": sent_item(quantity=2.0),
    "quantity as bool": sent_item(quantity=True),
    "zero quantity": sent_item(quantity=0),
    "negative quantity": sent_item(quantity=-1),
}

MALFORMED_CHANGED = {
    "unknown status": changed(status="COOKED"),
    "lower-case status": changed(status="in_progress"),
    "status as null": changed(previous_status=None),
    "no change": changed(previous_status="READY", status="READY"),
    "backwards": changed(previous_status="SERVED", status="PENDING"),
    "skips a step": changed(previous_status="PENDING", status="READY"),
    "cancels a served preparation": changed(
        previous_status="SERVED", status="CANCELLED"
    ),
    "unknown field": changed(reason="allergy"),
}


@pytest.mark.parametrize("name", sorted(MALFORMED_SENT))
def test_malformed_order_sent_is_rejected(name):
    with pytest.raises(InvalidEventPayloadError):
        parse_payload("order.sent", 1, MALFORMED_SENT[name])


@pytest.mark.parametrize("name", sorted(MALFORMED_CHANGED))
def test_malformed_status_change_is_rejected(name):
    with pytest.raises(InvalidEventPayloadError):
        parse_payload("preparation.status_changed", 1, MALFORMED_CHANGED[name])


@pytest.mark.parametrize("payload", [[], "x", None, {"order_id": {1, 2}}, {"q": float("nan")}])
def test_non_object_or_non_json_payload_is_rejected(payload):
    with pytest.raises(InvalidEventPayloadError):
        parse_payload("order.sent", 1, payload)


def test_errors_name_fields_but_never_values():
    secret = "Guest Jane Doe"

    with pytest.raises(InvalidEventPayloadError) as raised:
        parse_payload("order.sent", 1, sent(table_id=secret, initial=secret))

    message = str(raised.value)
    assert "table_id" in message and "initial" in message
    assert "Jane" not in message
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


def test_both_errors_share_one_base():
    assert issubclass(UnsupportedEventError, EventContractError)
    assert issubclass(InvalidEventPayloadError, EventContractError)


@pytest.mark.parametrize("transition", sorted(PREPARATION_TRANSITIONS_V1))
def test_every_v1_transition_is_accepted(transition):
    previous_status, status = transition

    model = parse_payload(
        "preparation.status_changed",
        1,
        changed(previous_status=previous_status, status=status),
    )

    assert (model.previous_status, model.status) == transition


def test_v1_does_not_describe_ready_to_cancelled():
    with pytest.raises(InvalidEventPayloadError):
        parse_payload(
            "preparation.status_changed",
            1,
            changed(previous_status="READY", status="CANCELLED"),
        )


def test_v2_is_v1_plus_ready_to_cancelled():
    assert PREPARATION_TRANSITIONS_V2 == PREPARATION_TRANSITIONS_V1 | {
        ("READY", "CANCELLED")
    }


@pytest.mark.parametrize("transition", sorted(PREPARATION_TRANSITIONS_V2))
def test_every_v2_transition_is_accepted(transition):
    previous_status, status = transition

    model = parse_payload(
        "preparation.status_changed",
        2,
        changed(previous_status=previous_status, status=status),
    )

    assert type(model) is PreparationStatusChangedV2
    assert (model.previous_status, model.status) == transition


@pytest.mark.parametrize(
    "transition",
    [("SERVED", "CANCELLED"), ("CANCELLED", "PENDING"), ("PENDING", "READY")],
)
def test_v2_rejects_other_transitions(transition):
    previous_status, status = transition

    with pytest.raises(InvalidEventPayloadError):
        parse_payload(
            "preparation.status_changed",
            2,
            changed(previous_status=previous_status, status=status),
        )


@pytest.mark.parametrize(
    "transition, version",
    [
        (("PENDING", "CANCELLED"), 1),
        (("IN_PROGRESS", "CANCELLED"), 1),
        (("READY", "SERVED"), 1),
        (("READY", "CANCELLED"), 2),
    ],
)
def test_producers_write_the_lowest_version_describing_a_change(transition, version):
    previous_status, status = transition

    model = preparation_status_changed(
        preparation_id=PREP_1,
        order_id=ORDER,
        order_item_id=ITEM_1,
        station_id=STATION,
        previous_status=previous_status,
        status=status,
    )

    assert model.schema_version == version
    assert parse_payload(model.event_type, version, dump_payload(model)) == model


def test_validation_is_deterministic():
    payload = golden(("order.sent", 1))

    assert parse_payload("order.sent", 1, payload) == parse_payload(
        "order.sent", 1, payload
    )
    # Parsing never changes its input.
    assert payload == GOLDEN[("order.sent", 1)]


# --- Producer side ---


def test_dump_rejects_an_unregistered_model():
    class OrderSentV2(OrderSentV1):
        schema_version = 2

    model = OrderSentV2.model_validate(golden(("order.sent", 1)))

    with pytest.raises(UnsupportedEventError):
        dump_payload(model)


def test_dump_rejects_a_model_built_without_validation():
    model = PreparationStatusChangedV1.model_construct(
        **{**changed(), "previous_status": "SERVED", "status": "PENDING"}
    )

    with pytest.raises(InvalidEventPayloadError):
        dump_payload(model)


def test_producers_may_pass_uuid_objects_or_strings():
    ids = {
        "preparation_id": uuid.UUID(PREP_1),
        "order_id": ORDER,
        "order_item_id": uuid.UUID(ITEM_1),
        "station_id": STATION,
    }

    model = PreparationStatusChangedV1(
        **ids, previous_status="PENDING", status="IN_PROGRESS"
    )

    assert dump_payload(model) == GOLDEN[("preparation.status_changed", 1)]
    assert model.aggregate_id == uuid.UUID(ORDER)


def test_sent_item_quantity_must_be_positive_when_built():
    with pytest.raises(ValueError):
        SentOrderItem(
            order_item_id=ITEM_1,
            preparation_id=PREP_1,
            station_id=STATION,
            product_id=PRODUCT,
            quantity=0,
        )
