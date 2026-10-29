import type { StationEvent } from "../api/stationEvents";
import type { PreparationQueueItem, QueueStatus } from "../api/types";

export interface LiveResult {
  items: PreparationQueueItem[];
  // The event cannot be shown from what the queue holds: reload it.
  reload: boolean;
}

const QUEUE_STATUSES: ReadonlySet<string> = new Set(["PENDING", "IN_PROGRESS", "READY"]);

/**
 * Applies one stream event to the loaded queue, following the backend's
 * client synchronization rules: everything is keyed by preparation id, an
 * event that does not continue from the status shown is already reflected
 * (or its preparation left the queue) and is ignored, and what the event
 * cannot describe is reloaded. Returns `items` itself when nothing changes.
 */
export function applyStationEvent(
  items: PreparationQueueItem[],
  event: StationEvent,
  stationId: string,
): LiveResult {
  if (event.event_type === "order.sent") {
    // Ids and quantities only: product, table and modifiers come from a
    // reload, which also picks up items already present.
    const present = new Set(items.map((item) => item.id));
    const missing = event.payload.items.some(
      (sent) => sent.station_id === stationId && !present.has(sent.preparation_id),
    );
    return { items, reload: missing };
  }

  const { preparation_id, station_id, previous_status, status } = event.payload;
  if (station_id !== stationId) return { items, reload: false };

  const index = items.findIndex((item) => item.id === preparation_id);
  if (index === -1 || items[index].status !== previous_status) {
    return { items, reload: false };
  }

  if (status === "SERVED" || status === "CANCELLED") {
    return { items: items.filter((_, i) => i !== index), reload: false };
  }
  if (!QUEUE_STATUSES.has(status)) return { items, reload: true };

  const current = items[index];
  const next: PreparationQueueItem = {
    ...current,
    status: status as QueueStatus,
    // The event's commit time stands in for the transition's timestamp
    // until the next reload.
    started_at: status === "IN_PROGRESS" ? (current.started_at ?? event.created_at) : current.started_at,
    ready_at: status === "READY" ? (current.ready_at ?? event.created_at) : current.ready_at,
  };
  return { items: items.map((item, i) => (i === index ? next : item)), reload: false };
}
