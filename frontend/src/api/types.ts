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

export const Permission = {
  PreparationsView: "preparations:view",
  PreparationsUpdate: "preparations:update",
  PreparationsServe: "preparations:serve",
} as const;
