import { vi } from "vitest";

import type { CurrentUser, PreparationQueueItem, Station } from "../api/types";

export const STATION: Station = {
  id: "5b0a3c1e-0000-4000-8000-000000000001",
  name: "Kitchen",
  type: "KITCHEN",
  is_active: true,
};

export const BAR: Station = {
  id: "5b0a3c1e-0000-4000-8000-000000000002",
  name: "Bar",
  type: "BAR",
  is_active: true,
};

export const KITCHEN_USER: CurrentUser = {
  id: "9d6f0d8e-0000-4000-8000-000000000001",
  name: "Kim Cook",
  email: "kim@example.com",
  restaurant_id: "1f2e3d4c-0000-4000-8000-000000000001",
  role: { id: "aa000000-0000-4000-8000-000000000001", name: "KITCHEN" },
  permissions: ["preparations:serve", "preparations:update", "preparations:view"],
  stations: [{ id: STATION.id, name: STATION.name, type: STATION.type }],
};

let sequence = 0;

export function queueItem(overrides: Partial<PreparationQueueItem> = {}): PreparationQueueItem {
  sequence += 1;
  const n = String(sequence).padStart(12, "0");
  return {
    id: `prep0000-0000-4000-8000-${n}`,
    order_item_id: `item0000-0000-4000-8000-${n}`,
    station_id: STATION.id,
    status: "PENDING",
    created_at: "2026-10-22T18:00:00.000000",
    started_at: null,
    ready_at: null,
    order_id: "order000-0000-4000-8000-000000000001",
    table_number: 12,
    table_name: "Terrace",
    product_name: "Burger",
    quantity: 1,
    modifiers: [],
    order_status: "SENT",
    bill_status: null,
    ...overrides,
  };
}

export function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

export function errorResponse(status: number, code: string, message: string): Response {
  return jsonResponse(
    { detail: message, error: { code, message, request_id: "req-123" } },
    status,
    { "X-Request-ID": "req-123" },
  );
}

type Handler = (init: RequestInit | undefined) => Response | Promise<Response>;

/**
 * Replaces fetch with a router keyed by "METHOD /path"; each key holds a
 * list of responses consumed in order (the last one repeats).
 */
export function mockApi(routes: Record<string, Array<Response | Handler>>) {
  const calls: Array<{ key: string; init: RequestInit | undefined }> = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${String(input)}`;
    calls.push({ key, init });
    const queue = routes[key];
    if (!queue || queue.length === 0) {
      throw new Error(`Unexpected request: ${key}`);
    }
    const next = queue.length > 1 ? queue.shift()! : queue[0];
    const response = typeof next === "function" ? await next(init) : next.clone();
    return response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, calls };
}

/** A JWT-shaped token with the given payload (unsigned; tests only). */
export function fakeToken(payload: Record<string, unknown>): string {
  const encode = (value: unknown) =>
    btoa(JSON.stringify(value)).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
  return `${encode({ alg: "HS256", typ: "JWT" })}.${encode(payload)}.signature`;
}
