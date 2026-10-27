// The station event stream, /api/v1/stations/{station_id}/events
// (backend app/realtime/stream.py and publisher.py; README "Station event
// streams"). The first client message authenticates; the server answers
// `subscribed` or closes with one of the codes below.

import type { PreparationStatus } from "./types";

export const CloseCode = {
  NotAuthenticated: 4401,
  Forbidden: 4403,
  StationNotFound: 4404,
  SlowConsumer: 4429,
  TryAgainLater: 1013,
} as const;

export interface AuthenticateMessage {
  type: "authenticate";
  token: string;
}

export interface SentOrderItem {
  order_item_id: string;
  preparation_id: string;
  station_id: string;
  product_id: string;
  quantity: number;
}

// order.sent v1: only this station's items of the round.
export interface OrderSentEvent {
  id: string;
  event_type: "order.sent";
  schema_version: 1;
  created_at: string;
  payload: {
    order_id: string;
    table_id: string;
    initial: boolean;
    items: SentOrderItem[];
  };
}

// preparation.status_changed v1 and v2 (v2 also allows READY -> CANCELLED).
export interface PreparationStatusChangedEvent {
  id: string;
  event_type: "preparation.status_changed";
  schema_version: 1 | 2;
  created_at: string;
  payload: {
    preparation_id: string;
    order_id: string;
    order_item_id: string;
    station_id: string;
    previous_status: PreparationStatus;
    status: PreparationStatus;
  };
}

export type StationEvent = OrderSentEvent | PreparationStatusChangedEvent;

export type ServerMessage =
  | { type: "subscribed"; stationId: string }
  | { type: "event"; event: StationEvent }
  // resync_required, and anything this client cannot read: an unknown
  // message, event type or version, or a payload it cannot trust. Never
  // guess its fields; reload the queue instead.
  | { type: "resync"; eventId: string | null };

const STATUSES: ReadonlySet<string> = new Set([
  "PENDING",
  "IN_PROGRESS",
  "READY",
  "SERVED",
  "CANCELLED",
]);

type Json = Record<string, unknown>;

function isObject(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function isStatus(value: unknown): value is PreparationStatus {
  return typeof value === "string" && STATUSES.has(value);
}

function isSentItem(value: unknown): value is SentOrderItem {
  return (
    isObject(value) &&
    isString(value.order_item_id) &&
    isString(value.preparation_id) &&
    isString(value.station_id) &&
    isString(value.product_id) &&
    Number.isInteger(value.quantity)
  );
}

function readEvent(message: Json): StationEvent | null {
  const { id, event_type, schema_version, created_at, payload } = message;
  if (!isString(id) || !isString(created_at) || !isObject(payload)) return null;

  if (event_type === "order.sent" && schema_version === 1) {
    const { order_id, table_id, initial, items } = payload;
    if (
      isString(order_id) &&
      isString(table_id) &&
      typeof initial === "boolean" &&
      Array.isArray(items) &&
      items.every(isSentItem)
    ) {
      return {
        id,
        event_type,
        schema_version,
        created_at,
        payload: { order_id, table_id, initial, items },
      };
    }
    return null;
  }

  if (event_type === "preparation.status_changed" && (schema_version === 1 || schema_version === 2)) {
    const { preparation_id, order_id, order_item_id, station_id, previous_status, status } = payload;
    if (
      isString(preparation_id) &&
      isString(order_id) &&
      isString(order_item_id) &&
      isString(station_id) &&
      isStatus(previous_status) &&
      isStatus(status)
    ) {
      return {
        id,
        event_type,
        schema_version,
        created_at,
        payload: { preparation_id, order_id, order_item_id, station_id, previous_status, status },
      };
    }
    return null;
  }

  return null;
}

/** Reads one text frame from the server; never throws. */
export function parseServerMessage(data: unknown): ServerMessage {
  let message: unknown;
  try {
    message = typeof data === "string" ? JSON.parse(data) : undefined;
  } catch {
    message = undefined;
  }
  if (!isObject(message)) return { type: "resync", eventId: null };

  if (message.type === "subscribed" && isString(message.station_id)) {
    return { type: "subscribed", stationId: message.station_id };
  }
  if (message.type === "event") {
    const event = readEvent(message);
    if (event) return { type: "event", event };
  }
  // resync_required names the event it could not route.
  const eventId = message.event_id ?? message.id;
  return { type: "resync", eventId: isString(eventId) ? eventId : null };
}

/** Same origin as the page: the dev server proxies /api WebSockets too. */
export function stationEventsUrl(stationId: string, location: Location = window.location): string {
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${location.host}/api/v1/stations/${encodeURIComponent(stationId)}/events`;
}
