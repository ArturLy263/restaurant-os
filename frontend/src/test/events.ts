import type { PreparationQueueItem, PreparationStatus } from "../api/types";
import { STATION } from "./fixtures";

// Server messages as backend app/realtime/publisher.py sends them.

export function statusChanged(
  id: string,
  item: PreparationQueueItem,
  previous: PreparationStatus,
  status: PreparationStatus,
  options: { version?: number; stationId?: string; createdAt?: string } = {},
) {
  return {
    type: "event",
    id,
    event_type: "preparation.status_changed",
    schema_version: options.version ?? 1,
    aggregate_type: "order",
    aggregate_id: item.order_id,
    created_at: options.createdAt ?? "2026-10-22T18:10:00.000000+00:00",
    payload: {
      preparation_id: item.id,
      order_id: item.order_id,
      order_item_id: item.order_item_id,
      station_id: options.stationId ?? STATION.id,
      previous_status: previous,
      status,
    },
  };
}

export function orderSent(id: string, items: Array<Pick<PreparationQueueItem, "id" | "order_item_id">>) {
  return {
    type: "event",
    id,
    event_type: "order.sent",
    schema_version: 1,
    aggregate_type: "order",
    aggregate_id: "order000-0000-4000-8000-000000000009",
    created_at: "2026-10-22T18:10:00.000000+00:00",
    payload: {
      order_id: "order000-0000-4000-8000-000000000009",
      table_id: "table000-0000-4000-8000-000000000001",
      initial: true,
      items: items.map((item) => ({
        order_item_id: item.order_item_id,
        preparation_id: item.id,
        station_id: STATION.id,
        product_id: "prod0000-0000-4000-8000-000000000001",
        quantity: 1,
      })),
    },
  };
}

export function resyncRequired(eventId = "evt-unsupported") {
  return {
    type: "resync_required",
    reason: "UNSUPPORTED_EVENT",
    event_id: eventId,
    event_type: "order.sent",
    schema_version: 3,
  };
}
