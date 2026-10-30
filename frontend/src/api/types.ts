// Mirrors of the backend response schemas (backend/app/schemas). UUIDs
// and timestamps arrive as strings; see parseApiTimestamp for the latter.

export interface TokenResponse {
  access_token: string;
  token_type: string;
}

export interface CurrentUserRole {
  id: string;
  name: string;
}

export interface CurrentUserStation {
  id: string;
  name: string;
  type: string;
}

// GET /api/v1/me
export interface CurrentUser {
  id: string;
  name: string;
  email: string;
  restaurant_id: string;
  // null if the user's role is not a role of their restaurant.
  role: CurrentUserRole | null;
  permissions: string[];
  // null means every station of the restaurant, [] none.
  stations: CurrentUserStation[] | null;
}

// GET /api/v1/stations
export interface Station {
  id: string;
  name: string;
  type: string;
  is_active: boolean;
}

// The queue only contains open preparations.
export type QueueStatus = "PENDING" | "IN_PROGRESS" | "READY";

export type PreparationStatus =
  | QueueStatus
  | "SERVED"
  | "CANCELLED";

export interface PreparationQueueModifier {
  modifier_id: string;
  name: string;
}

// GET /api/v1/stations/{station_id}/preparations
export interface PreparationQueueItem {
  id: string;
  order_item_id: string;
  station_id: string;
  status: QueueStatus;
  created_at: string;
  started_at: string | null;
  ready_at: string | null;
  order_id: string;
  table_number: number;
  table_name: string | null;
  product_name: string;
  quantity: number;
  modifiers: PreparationQueueModifier[];
  // The waiter's note for the station; null when there is none.
  notes: string | null;
  // CLOSED means fully paid; its unfinished preparations stay queued.
  order_status: string;
  // null before the order is billed.
  bill_status: string | null;
}

// POST /api/v1/preparations/{id}/start|ready|serve
export interface PreparationResponse {
  id: string;
  order_item_id: string;
  station_id: string;
  status: PreparationStatus;
  created_at: string;
  started_at: string | null;
  ready_at: string | null;
  served_at: string | null;
  cancelled_at: string | null;
}

// The error body every failing request returns (backend app/core/errors).
export interface ApiErrorBody {
  detail: unknown;
  error: {
    code: string;
    message: string;
    request_id: string;
  };
}

// --- Waiter POS ---

// A monetary amount as a decimal string ("12.50"; menu prices), or as a
// JSON number (order item prices, until the Stage 2 contract change; see
// docs/monetary-contract.md). Never do float arithmetic on it: see
// pos/money.ts.
export type Money = string | number;

export interface TableActiveOrder {
  id: string;
  status: OrderStatus;
  user_id: string;
  guest_count: number;
  created_at: string;
  unsent_items: number;
  sent_items: number;
}

// GET /api/v1/tables
export interface DiningTable {
  id: string;
  number: number;
  name: string | null;
  capacity: number;
  status: string;
  active_order: TableActiveOrder | null;
}

export interface MenuStation {
  id: string;
  name: string;
  type: string;
  is_active: boolean;
}

export interface MenuModifier {
  id: string;
  name: string;
  price_adjustment: string;
}

export interface MenuModifierGroup {
  id: string;
  name: string;
  min_selections: number;
  max_selections: number | null;
  is_required: boolean;
  modifiers: MenuModifier[];
}

export type UnavailableReason =
  | "PRODUCT_INACTIVE"
  | "PRODUCT_HAS_NO_STATION"
  | "STATION_INACTIVE";

export interface MenuProduct {
  id: string;
  name: string;
  description: string | null;
  price: string;
  station: MenuStation | null;
  is_available: boolean;
  unavailable_reason: UnavailableReason | null;
  modifier_groups: MenuModifierGroup[];
}

export interface MenuCategory {
  id: string;
  name: string;
  display_order: number;
  products: MenuProduct[];
}

// GET /api/v1/menu
export interface Menu {
  currency: string;
  categories: MenuCategory[];
}

export type OrderStatus = "OPEN" | "SENT" | "CLOSED" | "CANCELLED";
export type OrderItemStatus = "ADDED" | "SENT" | "CANCELLED";

export interface OrderItemModifier {
  id: string;
  modifier_id: string;
  name: string;
  price: Money;
}

export interface OrderItemPreparation {
  id: string;
  station_id: string;
  station_name: string;
  status: PreparationStatus;
}

export interface OrderItem {
  id: string;
  order_id: string;
  product_id: string;
  // null for items added before product name snapshots.
  product_name: string | null;
  quantity: number;
  // The product price without modifiers.
  unit_price: Money;
  // (unit_price + modifier prices) × quantity.
  total_price: Money;
  status: OrderItemStatus;
  created_at: string;
  cancelled_at: string | null;
  modifiers: OrderItemModifier[];
  notes: string | null;
  // null until the item is sent.
  preparation: OrderItemPreparation | null;
}

// POST /api/v1/orders
export interface Order {
  id: string;
  restaurant_id: string;
  table_id: string;
  user_id: string;
  guest_count: number;
  status: OrderStatus;
  created_at: string;
}

// GET /api/v1/orders/{id}, POST .../send
export interface OrderDetail extends Order {
  items: OrderItem[];
}

export const Permission = {
  PreparationsView: "preparations:view",
  PreparationsUpdate: "preparations:update",
  PreparationsServe: "preparations:serve",
  OrdersView: "orders:view",
  OrdersCreate: "orders:create",
  OrdersAddItems: "orders:add_items",
  OrdersSend: "orders:send",
  OrdersCancelItems: "orders:cancel_items",
  OrdersManageAll: "orders:manage_all",
} as const;
