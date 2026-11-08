import { apiRequest } from "./client";
import type {
  Bill,
  BillDetail,
  BusinessDay,
  BusinessDayReportResponse,
  CashierOrder,
  CashierOrdersView,
  CashMovement,
  CashMovementRequest,
  CashRegister,
  CashShift,
  CloseShiftRequest,
  CreatedBill,
  CreatePaymentRequest,
  EqualSplit,
  Handover,
  HandoverRequest,
  OpenBusinessDayRequest,
  OpeningCorrection,
  OpeningCorrectionRequest,
  OpenShiftRequest,
  Payment,
  Refund,
  RefundRequest,
} from "./cashierTypes";
import type { OrderDetail } from "./types";

// The cashier endpoints of the backend (app/api/routes/cash.py, cashier.py,
// bills.py, payments.py, orders.py). Nothing here invents an endpoint.

const BASE = "/api/v1";
const id = encodeURIComponent;

// --- Reads ---

export function getRegister(token: string, signal?: AbortSignal): Promise<CashRegister> {
  return apiRequest<CashRegister>(`${BASE}/cash/register`, { token, signal });
}

export function getShift(token: string, shiftId: string, signal?: AbortSignal): Promise<CashShift> {
  return apiRequest<CashShift>(`${BASE}/cash/shifts/${id(shiftId)}`, { token, signal });
}

export function listHandovers(token: string, limit?: number, signal?: AbortSignal): Promise<Handover[]> {
  return apiRequest<Handover[]>(withQuery(`${BASE}/cash/handovers`, { limit }), { token, signal });
}

export function listBusinessDays(token: string, limit?: number, signal?: AbortSignal): Promise<BusinessDay[]> {
  return apiRequest<BusinessDay[]>(withQuery(`${BASE}/business-days`, { limit }), { token, signal });
}

export function getBusinessDayReport(
  token: string,
  businessDayId: string,
  signal?: AbortSignal,
): Promise<BusinessDayReportResponse> {
  return apiRequest<BusinessDayReportResponse>(`${BASE}/business-days/${id(businessDayId)}/report`, {
    token,
    signal,
  });
}

export function listCashierOrders(
  token: string,
  view: CashierOrdersView = "payable",
  signal?: AbortSignal,
): Promise<CashierOrder[]> {
  return apiRequest<CashierOrder[]>(withQuery(`${BASE}/cashier/orders`, { view }), { token, signal });
}

export function getBill(token: string, billId: string, signal?: AbortSignal): Promise<BillDetail> {
  return apiRequest<BillDetail>(`${BASE}/bills/${id(billId)}`, { token, signal });
}

export function splitBillEqually(
  token: string,
  billId: string,
  parts: number,
  signal?: AbortSignal,
): Promise<EqualSplit> {
  return apiRequest<EqualSplit>(withQuery(`${BASE}/bills/${id(billId)}/split`, { parts }), { token, signal });
}

// --- Mutations ---

/**
 * One mutation as data: what pending-operation storage keeps so that a
 * retry sends exactly the same method, path and body with the same key.
 * `Result` only carries the response type.
 */
export interface MutationRequest<Result = unknown> {
  type: CashierOperationType;
  method: "POST";
  path: string;
  body: unknown;
  // The route refuses a request without a key (428 IDEMPOTENCY_KEY_REQUIRED).
  // Every cashier mutation is sent with a key anyway.
  requiresIdempotencyKey: boolean;
  readonly __result?: Result;
}

export type CashierOperationType =
  | "business_day.open"
  | "business_day.close"
  | "cash_shift.open"
  | "cash_shift.close"
  | "cash_shift.handover"
  | "cash_shift.opening_correction"
  | "cash_movement.create"
  | "bill.create"
  | "bill.cancel_item"
  | "order.void"
  | "payment.create"
  | "payment.refund";

function mutation<Result>(
  type: CashierOperationType,
  path: string,
  body: unknown,
  requiresIdempotencyKey: boolean,
): MutationRequest<Result> {
  return { type, method: "POST", path, body, requiresIdempotencyKey };
}

export const cashierMutations = {
  openBusinessDay: (request: OpenBusinessDayRequest = {}) =>
    mutation<BusinessDay>("business_day.open", `${BASE}/business-days`, request, false),

  closeBusinessDay: (businessDayId: string) =>
    mutation<BusinessDay>("business_day.close", `${BASE}/business-days/${id(businessDayId)}/close`, undefined, false),

  openShift: (request: OpenShiftRequest) =>
    mutation<CashShift>("cash_shift.open", `${BASE}/cash/shifts`, request, true),

  closeShift: (shiftId: string, request: CloseShiftRequest) =>
    mutation<CashShift>("cash_shift.close", `${BASE}/cash/shifts/${id(shiftId)}/close`, request, true),

  handOverShift: (shiftId: string, request: HandoverRequest) =>
    mutation<CashShift>("cash_shift.handover", `${BASE}/cash/shifts/${id(shiftId)}/handover`, request, true),

  correctOpeningAmount: (shiftId: string, request: OpeningCorrectionRequest) =>
    mutation<OpeningCorrection>(
      "cash_shift.opening_correction",
      `${BASE}/cash/shifts/${id(shiftId)}/opening-corrections`,
      request,
      true,
    ),

  recordCashMovement: (shiftId: string, request: CashMovementRequest) =>
    mutation<CashMovement>("cash_movement.create", `${BASE}/cash/shifts/${id(shiftId)}/movements`, request, true),

  createBill: (orderId: string) =>
    mutation<CreatedBill>("bill.create", `${BASE}/bills/order/${id(orderId)}`, undefined, false),

  cancelBilledItem: (billId: string, itemId: string, reason: string) =>
    mutation<Bill>("bill.cancel_item", `${BASE}/bills/${id(billId)}/items/${id(itemId)}/cancel`, { reason }, false),

  voidOrder: (orderId: string, reason: string) =>
    mutation<OrderDetail>("order.void", `${BASE}/orders/${id(orderId)}/void`, { reason }, false),

  createPayment: (billId: string, request: CreatePaymentRequest) =>
    mutation<Payment>("payment.create", `${BASE}/bills/${id(billId)}/payments`, request, true),

  refundPayment: (paymentId: string, request: RefundRequest) =>
    mutation<Refund>("payment.refund", `${BASE}/payments/${id(paymentId)}/refunds`, request, true),
} satisfies Record<string, (...args: never[]) => MutationRequest>;

export const MISSING_IDEMPOTENCY_KEY = "Every cashier mutation needs an Idempotency-Key.";

/**
 * Sends a mutation with its Idempotency-Key. Money and shift changes
 * should go through pending-operation storage (cashier/pendingOperations),
 * which keeps the key and body for a retry; this is the transport.
 */
export function sendMutation<Result>(
  token: string,
  request: MutationRequest<Result>,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<Result> {
  if (!idempotencyKey) return Promise.reject(new Error(MISSING_IDEMPOTENCY_KEY));
  return apiRequest<Result>(request.path, {
    method: request.method,
    token,
    idempotencyKey,
    body: request.body,
    signal,
  });
}

function withQuery(path: string, params: Record<string, string | number | undefined>): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) query.set(key, String(value));
  }
  const text = query.toString();
  return text ? `${path}?${text}` : path;
}
