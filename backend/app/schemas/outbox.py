"""Integration event contracts written to the transactional outbox.

An event is an envelope plus a payload. The envelope is the columns of
outbox_events (not repeated in the payload): `id` (stable event id, for
deduplication), `event_type`, `schema_version`, `restaurant_id` (tenant),
`aggregate_type` + `aggregate_id`, `created_at`.

Each payload model below is the contract of one (event_type,
schema_version) pair and is listed in `CONTRACTS`. `event_type` names
what happened and never changes meaning; `schema_version` numbers the
payload's shape and rules for that type. A payload is valid only if it
is exactly the canonical JSON of its model: strict JSON types (no "2"
for 2, no 1 for true), lowercase hyphenated UUIDs, no unknown or missing
fields, and the model's rules (e.g. which transitions v1 describes).

Compatibility rules for future changes:

- Compatible (same version): documentation only, or a new optional
  field that consumers of this version may ignore. Because payloads
  forbid unknown fields, a new field still needs every consumer of the
  version updated first; when in doubt, use a new version.
- Everything else is a new version with its own model, added next to
  the old one: renaming, removing, retyping or making a field required,
  changing a field's meaning, and widening or narrowing allowed values
  (a new status, a new transition).
- A version, once written, is never changed or removed while events of
  it may still be undelivered or stored. Existing rows are never
  rewritten.
- A new meaning is a new event_type, not a new version.

Consumers look up (event_type, schema_version) with `parse_payload`: an
unknown pair raises `UnsupportedEventError` (not malformed, just not
understood: skip or park it, do not guess), a known pair with a payload
that breaks its contract raises `InvalidEventPayloadError`.

Every event below uses the order as its aggregate (`aggregate_type`
"order", `aggregate_id` the payload's `order_id`): every change they
describe is made under that order's row lock, so one order's events are
created in the order they were committed.

Payloads hold identifiers and states only. They carry no actor, names,
prices, notes or free-text reasons: who did what is audit history
(operational_events), and consumers that need more read it through the
API with their own permissions.
"""

import json
import uuid
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


ORDER_AGGREGATE = "order"

PreparationStatus = Literal[
    "PENDING",
    "IN_PROGRESS",
    "READY",
    "SERVED",
    "CANCELLED",
]


class EventContractError(Exception):
    """Base of the errors raised by `parse_payload` and `dump_payload`."""


class UnsupportedEventError(EventContractError):
    """No contract for this (event_type, schema_version) here."""

    def __init__(self, event_type: str, schema_version: int):
        self.event_type = event_type
        self.schema_version = schema_version
        super().__init__(f"Unsupported event {event_type!r} v{schema_version}")


class InvalidEventPayloadError(EventContractError):
    """A supported event whose payload breaks its contract.

    The message names the contract and the failing fields, never values.
    """


class OutboxPayload(BaseModel):
    """Base of every event payload; subclasses set the class vars and
    `aggregate_id`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: ClassVar[str]
    schema_version: ClassVar[int]
    aggregate_type: ClassVar[str]

    @property
    def aggregate_id(self) -> uuid.UUID:
        raise NotImplementedError


class SentOrderItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    order_item_id: uuid.UUID
    preparation_id: uuid.UUID
    station_id: uuid.UUID
    product_id: uuid.UUID
    quantity: int = Field(ge=1)


class OrderSentV1(OutboxPayload):
    """`order.sent` v1: a round of an order's items was sent to stations.

    One event per successful send. `items` are exactly the items sent in
    this round (at least one, each once), each with the PENDING
    preparation created for it at its station; `initial` is true for the
    order's first send.
    """

    event_type: ClassVar[str] = "order.sent"
    schema_version: ClassVar[int] = 1
    aggregate_type: ClassVar[str] = ORDER_AGGREGATE

    order_id: uuid.UUID
    table_id: uuid.UUID
    initial: bool
    items: list[SentOrderItem] = Field(min_length=1)

    @property
    def aggregate_id(self) -> uuid.UUID:
        return self.order_id

    @model_validator(mode="after")
    def _items_are_distinct(self):
        for field in ("order_item_id", "preparation_id"):
            ids = [getattr(item, field) for item in self.items]

            if len(set(ids)) != len(ids):
                raise ValueError(f"items repeat a {field}")

        return self


# The transitions v1 describes; any other pair is not a v1 payload.
PREPARATION_TRANSITIONS_V1 = frozenset({
    ("PENDING", "IN_PROGRESS"),
    ("IN_PROGRESS", "READY"),
    ("READY", "SERVED"),
    ("PENDING", "CANCELLED"),
    ("IN_PROGRESS", "CANCELLED"),
})


class PreparationStatusChangedV1(OutboxPayload):
    """`preparation.status_changed` v1: a preparation changed status.

    One event per successful change: PENDING -> IN_PROGRESS -> READY ->
    SERVED through the preparation endpoints, and PENDING/IN_PROGRESS ->
    CANCELLED when its order item is cancelled or the preparation of a
    fully paid order is discarded (READY -> CANCELLED is v2). Rejected
    transitions create no event. Preparations are created PENDING by
    `order.sent`.
    """

    event_type: ClassVar[str] = "preparation.status_changed"
    schema_version: ClassVar[int] = 1
    aggregate_type: ClassVar[str] = ORDER_AGGREGATE

    preparation_id: uuid.UUID
    order_id: uuid.UUID
    order_item_id: uuid.UUID
    station_id: uuid.UUID
    previous_status: PreparationStatus
    status: PreparationStatus

    @property
    def aggregate_id(self) -> uuid.UUID:
        return self.order_id

    @model_validator(mode="after")
    def _is_a_v1_transition(self):
        if (self.previous_status, self.status) not in PREPARATION_TRANSITIONS_V1:
            raise ValueError("not a v1 preparation transition")

        return self


# v2 adds one transition: READY -> CANCELLED, when a fully paid order's
# ready preparation is discarded.
PREPARATION_TRANSITIONS_V2 = PREPARATION_TRANSITIONS_V1 | {
    ("READY", "CANCELLED"),
}


class PreparationStatusChangedV2(OutboxPayload):
    """`preparation.status_changed` v2: v1 plus READY -> CANCELLED.

    Same fields and meaning as v1. Producers write v1 whenever v1 describes
    the transition, so only READY -> CANCELLED (discarding a ready
    preparation of a fully paid order) is written as v2; consumers that
    know only v1 are unaffected by every other change.
    """

    event_type: ClassVar[str] = "preparation.status_changed"
    schema_version: ClassVar[int] = 2
    aggregate_type: ClassVar[str] = ORDER_AGGREGATE

    preparation_id: uuid.UUID
    order_id: uuid.UUID
    order_item_id: uuid.UUID
    station_id: uuid.UUID
    previous_status: PreparationStatus
    status: PreparationStatus

    @property
    def aggregate_id(self) -> uuid.UUID:
        return self.order_id

    @model_validator(mode="after")
    def _is_a_v2_transition(self):
        if (self.previous_status, self.status) not in PREPARATION_TRANSITIONS_V2:
            raise ValueError("not a v2 preparation transition")

        return self


def preparation_status_changed(**fields) -> OutboxPayload:
    """The lowest version of `preparation.status_changed` that describes
    the transition (v1, or v2 for READY -> CANCELLED)."""

    if (fields["previous_status"], fields["status"]) in PREPARATION_TRANSITIONS_V1:
        return PreparationStatusChangedV1(**fields)

    return PreparationStatusChangedV2(**fields)


def _register(*contracts: type[OutboxPayload]):
    registry: dict[tuple[str, int], type[OutboxPayload]] = {}

    for contract in contracts:
        key = (contract.event_type, contract.schema_version)

        if key in registry:
            raise RuntimeError(f"Duplicate event contract {key}")

        registry[key] = contract

    return MappingProxyType(registry)


# Every supported (event_type, schema_version) and its payload model.
CONTRACTS: Mapping[tuple[str, int], type[OutboxPayload]] = _register(
    OrderSentV1,
    PreparationStatusChangedV1,
    PreparationStatusChangedV2,
)


def parse_payload(
    event_type: str,
    schema_version: int,
    payload: Mapping[str, Any],
) -> OutboxPayload:
    """The payload as its contract's model; see the module docstring."""

    contract = CONTRACTS.get((event_type, schema_version))

    if contract is None:
        raise UnsupportedEventError(event_type, schema_version)

    return _validate(contract, payload)


def dump_payload(payload: OutboxPayload) -> dict[str, Any]:
    """The JSON object to store for `payload`, checked to parse back.

    Rejects models that are not registered contracts and models built
    without validation (`model_construct`) that break their contract.
    """

    contract = type(payload)

    # By class, not by its event_type/schema_version: those are written
    # as they are, and PostgreSQL checks the version.
    if contract not in CONTRACTS.values():
        raise UnsupportedEventError(contract.event_type, contract.schema_version)

    data = payload.model_dump(mode="json")

    if _validate(contract, data) != payload:
        raise InvalidEventPayloadError(f"{contract.__name__}: does not round-trip")

    return data


def _validate(contract: type[OutboxPayload], payload: Mapping[str, Any]):
    name = contract.__name__

    # Validated as JSON text in strict mode: UUIDs are read from strings,
    # as stored, and nothing else is coerced. (Building a model in Python
    # stays lax; what is stored is its canonical dump.)
    try:
        text = json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError):
        raise InvalidEventPayloadError(f"{name}: not a JSON value") from None

    try:
        model = contract.model_validate_json(text, strict=True)
    except ValidationError as exc:
        # Field locations only: input values could be anything.
        fields = sorted({
            ".".join(str(part) for part in error["loc"]) or "<payload>"
            for error in exc.errors(include_input=False)
        })
        raise InvalidEventPayloadError(f"{name}: invalid {', '.join(fields)}") from None

    # One representation per payload (e.g. no upper-case UUIDs).
    if model.model_dump(mode="json") != json.loads(text):
        raise InvalidEventPayloadError(f"{name}: not in canonical form")

    return model
