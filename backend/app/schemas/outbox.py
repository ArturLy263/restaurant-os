"""Integration event contracts written to the transactional outbox.

Each payload model is one (event_type, schema_version) pair. A payload's
fields are its contract: a compatible addition keeps the version, any
other change (rename, removal, new meaning) is a new version with its own
model, and consumers must check `schema_version` before reading a payload.

Envelope (columns of outbox_events, not repeated in the payload):
`id` (stable event id, for deduplication), `event_type`, `schema_version`,
`restaurant_id` (tenant), `aggregate_type` + `aggregate_id`, `created_at`.

All events below use the order as their aggregate (`aggregate_type`
"order"): every change they describe is made under that order's row lock,
so one order's events are created in the order they were committed.

Payloads hold identifiers and states only. They carry no actor, names,
prices, notes or free-text reasons: who did what is audit history
(operational_events), and consumers that need more read it through the
API with their own permissions.
"""

import uuid
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict


ORDER_AGGREGATE = "order"

PreparationStatus = Literal[
    "PENDING",
    "IN_PROGRESS",
    "READY",
    "SERVED",
    "CANCELLED",
]


class OutboxPayload(BaseModel):
    """Base of every event payload; subclasses set the two class vars."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: ClassVar[str]
    schema_version: ClassVar[int]


class SentOrderItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    order_item_id: uuid.UUID
    preparation_id: uuid.UUID
    station_id: uuid.UUID
    product_id: uuid.UUID
    quantity: int


class OrderSentV1(OutboxPayload):
    """`order.sent` v1: a round of an order's items was sent to stations.

    One event per successful send. `items` are exactly the items sent in
    this round, each with the PENDING preparation created for it at its
    station; `initial` is true for the order's first send.
    """

    event_type: ClassVar[str] = "order.sent"
    schema_version: ClassVar[int] = 1

    order_id: uuid.UUID
    table_id: uuid.UUID
    initial: bool
    items: list[SentOrderItem]


class PreparationStatusChangedV1(OutboxPayload):
    """`preparation.status_changed` v1: a preparation changed status.

    One event per successful change: PENDING -> IN_PROGRESS -> READY ->
    SERVED through the preparation endpoints, and PENDING/IN_PROGRESS ->
    CANCELLED when its order item is cancelled. Rejected transitions
    create no event. Preparations are created PENDING by `order.sent`.
    """

    event_type: ClassVar[str] = "preparation.status_changed"
    schema_version: ClassVar[int] = 1

    preparation_id: uuid.UUID
    order_id: uuid.UUID
    order_item_id: uuid.UUID
    station_id: uuid.UUID
    previous_status: PreparationStatus
    status: PreparationStatus
