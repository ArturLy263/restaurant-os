"""Outbox publisher that fans events out to open station event streams.

`StationEventPublisher` is an app.workers.outbox_worker Publisher: the
outbox delivery worker claims committed events in order and hands each to
`publish`, which parses it at this consumer boundary and offers it to the
station streams of this process (app.realtime.hub). Nothing bypasses the
outbox: an event reaches displays only after its transaction committed.

Routing (only the event's restaurant, never a broadcast):

- `preparation.status_changed`: to the preparation's station.
- `order.sent`: to each station with items in the round, each receiving
  an `order.sent` of the same version with only its own items.

What a stream receives, as JSON text:

    {"type": "event", "id": ..., "event_type": ..., "schema_version": ...,
     "aggregate_type": ..., "aggregate_id": ..., "created_at": ...,
     "payload": {...}}

    {"type": "resync_required", "reason": "UNSUPPORTED_EVENT",
     "event_id": ..., "event_type": ..., "schema_version": ...}

Delivery semantics:

- `publish` returning means the event was put in the buffers of the
  streams open in this process at that moment, which then marks it
  delivered in the outbox. It is not proof that any display received,
  processed or stored it: a display that is offline, reconnecting or
  disconnected as too slow misses it, and must reload its station queue
  over HTTP (GET /api/v1/stations/{id}/preparations) after every
  (re)connect, then apply events.
- At least once: an event can be sent again with the same `id` (a worker
  crash after publishing, a timed-out publish). Displays deduplicate on
  `id`.
- Order: the worker delivers one order's events in order, and each
  stream's buffer and sender are FIFO, so a stream receives one order's
  events in the order they were committed.
- An unsupported (event_type, schema_version) cannot be routed by this
  code, so it is not sent; the restaurant's open streams get a
  `resync_required` notice (no payload) and the event counts as
  delivered: no retry loop, no dead letter, no blocked order, and it is
  logged as a warning.
- A supported event whose payload breaks its contract raises: the worker
  retries it with backoff and finally dead-letters it (an operator
  issue; it holds back its order's later events, see the README). No
  stream receives it.
"""

import logging
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import Any

from app.realtime.hub import StationEventHub, StationSubscription
from app.schemas.outbox import (
    OrderSentV1,
    OutboxPayload,
    PreparationStatusChangedV1,
    PreparationStatusChangedV2,
    UnsupportedEventError,
    parse_payload,
)
from app.workers.outbox_worker import OutboxMessage


logger = logging.getLogger("app.realtime")


def _route_order_sent(payload: OrderSentV1):
    by_station = defaultdict(list)

    for item in payload.items:
        by_station[item.station_id].append(item)

    for station_id, items in by_station.items():
        yield station_id, OrderSentV1(
            order_id=payload.order_id,
            table_id=payload.table_id,
            initial=payload.initial,
            items=items,
        )


def _route_status_changed(
    payload: PreparationStatusChangedV1 | PreparationStatusChangedV2,
):
    yield payload.station_id, payload


# Every contract's routing: (station, the payload that station receives).
ROUTES: dict[
    type[OutboxPayload],
    Callable[[Any], Iterable[tuple[uuid.UUID, OutboxPayload]]],
] = {
    OrderSentV1: _route_order_sent,
    PreparationStatusChangedV1: _route_status_changed,
    PreparationStatusChangedV2: _route_status_changed,
}


def event_message(message: OutboxMessage, payload: OutboxPayload) -> dict[str, Any]:
    return {
        "type": "event",
        "id": str(message.id),
        "event_type": message.event_type,
        "schema_version": message.schema_version,
        "aggregate_type": message.aggregate_type,
        "aggregate_id": str(message.aggregate_id),
        "created_at": message.created_at.isoformat(),
        "payload": payload.model_dump(mode="json"),
    }


def resync_message(message: OutboxMessage) -> dict[str, Any]:
    return {
        "type": "resync_required",
        "reason": "UNSUPPORTED_EVENT",
        "event_id": str(message.id),
        "event_type": message.event_type,
        "schema_version": message.schema_version,
    }


class StationEventPublisher:

    def __init__(self, hub: StationEventHub):
        self.hub = hub

    def publish(self, message: OutboxMessage) -> None:
        try:
            # InvalidEventPayloadError propagates: a failed attempt.
            payload = parse_payload(
                message.event_type,
                message.schema_version,
                message.payload,
            )
            route = ROUTES[type(payload)]
        except (UnsupportedEventError, KeyError):
            self._resync(message)
            return

        for station_id, station_payload in route(payload):
            self._offer(
                self.hub.station_subscribers(message.restaurant_id, station_id),
                event_message(message, station_payload),
            )

    def _resync(self, message: OutboxMessage) -> None:
        subscribers = self.hub.restaurant_subscribers(message.restaurant_id)

        logger.warning(
            "Outbox event %s (%s v%d) is not supported by this version; "
            "sent resync_required to %d stream(s) of restaurant %s",
            message.id,
            message.event_type,
            message.schema_version,
            len(subscribers),
            message.restaurant_id,
        )
        self._offer(subscribers, resync_message(message))

    @staticmethod
    def _offer(
        subscribers: list[StationSubscription],
        wire: dict[str, Any],
    ) -> None:
        # A full buffer marks that stream overflowed (it is closed); the
        # others still get the event and the worker is never blocked.
        for subscription in subscribers:
            subscription.offer(wire)
