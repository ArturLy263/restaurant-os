import { apiRequest } from "./client";
import type {
  CurrentUser,
  DiningTable,
  Menu,
  Order,
  OrderDetail,
  OrderItem,
  PreparationQueueItem,
  PreparationResponse,
  Station,
  TokenResponse,
} from "./types";

const BASE = "/api/v1";

export function login(email: string, password: string): Promise<TokenResponse> {
  return apiRequest<TokenResponse>(`${BASE}/auth/login`, {
    method: "POST",
    body: { email, password },
  });
}

export function getMe(token: string, signal?: AbortSignal): Promise<CurrentUser> {
  return apiRequest<CurrentUser>(`${BASE}/me`, { token, signal });
}

export function getStations(token: string, signal?: AbortSignal): Promise<Station[]> {
  return apiRequest<Station[]>(`${BASE}/stations`, { token, signal });
}

export function getStationQueue(
  token: string,
  stationId: string,
  signal?: AbortSignal,
): Promise<PreparationQueueItem[]> {
  return apiRequest<PreparationQueueItem[]>(
    `${BASE}/stations/${encodeURIComponent(stationId)}/preparations`,
    { token, signal },
  );
}

export type PreparationAction = "start" | "ready" | "serve";

export function updatePreparation(
  token: string,
  preparationId: string,
  action: PreparationAction,
): Promise<PreparationResponse> {
  return apiRequest<PreparationResponse>(
    `${BASE}/preparations/${encodeURIComponent(preparationId)}/${action}`,
    { method: "POST", token },
  );
}

// --- Waiter POS ---

export function getTables(token: string, signal?: AbortSignal): Promise<DiningTable[]> {
  return apiRequest<DiningTable[]>(`${BASE}/tables`, { token, signal });
}

export function getMenu(token: string, signal?: AbortSignal): Promise<Menu> {
  return apiRequest<Menu>(`${BASE}/menu`, { token, signal });
}

export function createOrder(
  token: string,
  tableId: string,
  guestCount: number,
  idempotencyKey: string,
): Promise<Order> {
  return apiRequest<Order>(`${BASE}/orders`, {
    method: "POST",
    token,
    idempotencyKey,
    body: { table_id: tableId, guest_count: guestCount },
  });
}

export function getOrder(token: string, orderId: string, signal?: AbortSignal): Promise<OrderDetail> {
  return apiRequest<OrderDetail>(`${BASE}/orders/${encodeURIComponent(orderId)}`, { token, signal });
}

export interface NewOrderItem {
  product_id: string;
  quantity: number;
  modifier_ids: string[];
  notes: string | null;
}

export function addOrderItem(
  token: string,
  orderId: string,
  item: NewOrderItem,
  idempotencyKey: string,
): Promise<OrderItem> {
  return apiRequest<OrderItem>(`${BASE}/orders/${encodeURIComponent(orderId)}/items`, {
    method: "POST",
    token,
    idempotencyKey,
    body: item,
  });
}

export interface OrderItemChanges {
  quantity?: number;
  // null removes the note.
  notes?: string | null;
}

export function updateOrderItem(
  token: string,
  orderId: string,
  itemId: string,
  changes: OrderItemChanges,
  idempotencyKey: string,
): Promise<OrderItem> {
  return apiRequest<OrderItem>(
    `${BASE}/orders/${encodeURIComponent(orderId)}/items/${encodeURIComponent(itemId)}`,
    { method: "PATCH", token, idempotencyKey, body: changes },
  );
}

export function cancelOrderItem(
  token: string,
  orderId: string,
  itemId: string,
  reason: string,
  idempotencyKey: string,
): Promise<OrderItem> {
  return apiRequest<OrderItem>(
    `${BASE}/orders/${encodeURIComponent(orderId)}/items/${encodeURIComponent(itemId)}/cancel`,
    { method: "POST", token, idempotencyKey, body: { reason } },
  );
}

export function sendOrder(token: string, orderId: string, idempotencyKey: string): Promise<OrderDetail> {
  return apiRequest<OrderDetail>(`${BASE}/orders/${encodeURIComponent(orderId)}/send`, {
    method: "POST",
    token,
    idempotencyKey,
  });
}
