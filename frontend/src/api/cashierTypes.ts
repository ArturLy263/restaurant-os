// Mirrors of the backend cashier schemas (backend/app/schemas/cash.py and
// bill.py). Amounts are decimal strings ("12.50"): convert with lib/money,
// never with parseFloat. Amounts the viewer may not see (expected cash
// without cash_drawer:view_expected) arrive as null.

/** A decimal amount string with two decimals, e.g. "12.50". */
export type Amount = string;

export interface UserRef {
  id: string;
  name: string | null;
}

// --- Business days ---

export type BusinessDayStatus = "OPEN" | "CLOSED";

export interface BusinessDay {
  id: string;
  business_date: string;
  // Typed loosely: a new backend status must not break the screen.
  status: BusinessDayStatus | (string & {});
  opened_by_user_id: string;
  opened_at: string;
  closed_by_user_id: string | null;
  closed_at: string | null;
}

export interface OpenBusinessDayRequest {
  // Restaurant-local date; omitted means today.
  business_date?: string | null;
}

export interface MethodTotals {
  CASH: Amount;
  CARD: Amount;
  total: Amount;
}

export interface DrawerReport {
  opening_amount: Amount;
  effective_opening_amount: Amount;
  opening_corrections_count: number;
  cash_sales: Amount;
  cash_tips: Amount;
  cash_in: Amount;
  cash_out: Amount;
  cash_refunds: Amount;
  expected: Amount;
}

export interface ClosingSummary {
  expected_amount: Amount;
  counted_amount: Amount;
  difference: Amount;
  explanation: string | null;
}

export interface DayShiftReport {
  sales: MethodTotals;
  refunds: MethodTotals;
  revenue: MethodTotals;
  tips: MethodTotals;
  refunded_tips: MethodTotals;
  net_tips: MethodTotals;
  payments_count: number;
  refunds_count: number;
  cash_shift_id: string;
  business_day_id: string;
  currency: string;
  drawer: DrawerReport;
  closing: ClosingSummary | null;
  status: string;
  opened_by: string | null;
  opened_at: string;
  closed_at: string | null;
}

export interface HandoverReport {
  id: string;
  from_shift_id: string;
  to_shift_id: string;
  business_day_id: string;
  previous_expected_amount: Amount;
  handed_over_amount: Amount;
  received_amount: Amount;
  difference: Amount;
  reason: string | null;
  outgoing_user: string | null;
  incoming_user: string | null;
  self_confirmed?: boolean | null;
  created_at: string;
}

export interface BusinessDayReport {
  sales: MethodTotals;
  refunds: MethodTotals;
  revenue: MethodTotals;
  tips: MethodTotals;
  refunded_tips: MethodTotals;
  net_tips: MethodTotals;
  payments_count: number;
  refunds_count: number;
  business_day_id: string;
  business_date: string;
  currency: string;
  shifts: DayShiftReport[];
  handovers: HandoverReport[];
  closing_difference_total: Amount;
  handover_difference_total: Amount;
}

// GET /business-days/{id}/report (cash_reports:view)
export interface BusinessDayReportResponse {
  business_day: BusinessDay;
  report: BusinessDayReport;
}

// --- Register and shifts ---

export type CashShiftStatus = "OPEN" | "CLOSED";
export type CashMovementType = "CASH_IN" | "CASH_OUT" | "CASH_REFUND" | "CASH_TIP";

export interface OpeningCorrection {
  id: string;
  cash_shift_id: string;
  previous_amount: Amount;
  new_amount: Amount;
  reason: string;
  user: UserRef;
  created_at: string;
}

export interface CashMovement {
  id: string;
  cash_shift_id: string;
  type: CashMovementType | (string & {});
  amount: Amount;
  reason: string | null;
  payment_id: string | null;
  refund_id: string | null;
  user: UserRef;
  created_at: string;
}

export interface Handover {
  id: string;
  cash_register_id: string;
  business_day_id: string;
  from_shift_id: string;
  to_shift_id: string;
  // null without cash_drawer:view_expected.
  previous_expected_amount: Amount | null;
  handed_over_amount: Amount;
  received_amount: Amount;
  difference: Amount;
  reason: string | null;
  outgoing_user: UserRef;
  incoming_user: UserRef;
  self_confirmed: boolean;
  created_at: string;
}

export interface ShiftClosing {
  // null without cash_drawer:view_expected.
  expected_amount: Amount | null;
  // null while the count is secret (blind handover still pending).
  counted_amount: Amount | null;
  difference: Amount | null;
  explanation: string | null;
  user: UserRef;
  created_at: string;
}

export interface CashShift {
  id: string;
  cash_register_id: string;
  business_day_id: string;
  status: CashShiftStatus | (string & {});
  currency: string;
  opening_amount: Amount;
  effective_opening_amount: Amount;
  opened_by: UserRef;
  opened_at: string;
  closed_at: string | null;
  // null unless expected_visible.
  expected_cash: Amount | null;
  drawer: DrawerReport | null;
  expected_visible: boolean;
  corrections: OpeningCorrection[];
  movements: CashMovement[];
  handover: Handover | null;
  closing: ShiftClosing | null;
}

export interface PreviousClosing {
  cash_shift_id: string;
  business_day_id: string;
  expected_amount: Amount | null;
  counted_amount: Amount | null;
  closed_at: string;
  closed_by: UserRef;
  handover_pending: boolean;
}

// GET /cash/register (cash_shifts:view)
export interface CashRegister {
  // null before the restaurant's register exists (it is created with the
  // first shift).
  register_id: string | null;
  business_day: BusinessDay | null;
  shift: CashShift | null;
  previous_closing: PreviousClosing | null;
}

export interface OpenShiftRequest {
  opening_amount: Amount;
  handover_reason?: string | null;
}

export interface CloseShiftRequest {
  counted_amount: Amount;
  explanation?: string | null;
}

export interface HandoverRequest {
  counted_amount: Amount;
  closing_explanation?: string | null;
}

export interface OpeningCorrectionRequest {
  opening_amount: Amount;
  reason: string;
}

export interface CashMovementRequest {
  type: "CASH_IN" | "CASH_OUT";
  amount: Amount;
  reason: string;
}

// --- Cashier orders and bills ---

export type CashierOrdersView = "payable" | "settled";

// GET /cashier/orders (bills:view)
export interface CashierOrder {
  order_id: string;
  order_status: string;
  table_number: number;
  table_name: string | null;
  guest_count: number;
  waiter_name: string | null;
  created_at: string;
  items_count: number;
  items_total: Amount;
  bill_id: string | null;
  bill_status: string | null;
  paid_total: Amount;
  amount_due: Amount;
}

export interface BillAdjustment {
  id: string;
  order_item_id: string | null;
  type: string;
  amount: Amount;
  status: string;
  reason: string;
  user_id: string | null;
  created_at: string;
}

// POST /bills/{id}/items/{item_id}/cancel
export interface Bill {
  id: string;
  order_id: string;
  subtotal: Amount;
  total: Amount;
  status: string;
  created_at: string;
  paid_at: string | null;
  voided_at: string | null;
  adjustments_total: Amount;
  adjusted_total: Amount;
  paid_total: Amount;
  amount_due: Amount;
  adjustments: BillAdjustment[];
}

// POST /bills/order/{order_id} (has no paid_at)
export type CreatedBill = Omit<Bill, "paid_at">;

export interface BillOrderSummary {
  id: string;
  status: string;
  table_number: number;
  table_name: string | null;
  guest_count: number;
  waiter_name: string | null;
}

export interface BillLineModifier {
  name: string;
  price: Amount;
}

export interface BillLine {
  order_item_id: string;
  product_name: string | null;
  quantity: number;
  unit_price: Amount;
  total_price: Amount;
  status: string;
  notes: string | null;
  modifiers: BillLineModifier[];
  paid_by_payment_id: string | null;
}

export interface BillRefund {
  id: string;
  amount: Amount;
  tip_amount: Amount;
  method: string;
  reason: string;
  user_name: string | null;
  created_at: string;
}

export interface BillPayment {
  id: string;
  amount: Amount;
  tip_amount: Amount;
  method: string;
  confirmation_method: string | null;
  external_reference: string | null;
  user_name: string | null;
  created_at: string;
  refunded_amount: Amount;
  refunded_tip_amount: Amount;
  refunds: BillRefund[];
}

// GET /bills/{id}
export interface BillDetail extends Bill {
  currency: string;
  order: BillOrderSummary;
  lines: BillLine[];
  payments: BillPayment[];
  tips_total: Amount;
  refunded_total: Amount;
  refunded_tips_total: Amount;
}

// GET /bills/{id}/split?parts=
export interface EqualSplit {
  bill_id: string;
  amount_due: Amount;
  shares: Amount[];
}

// --- Payments and refunds ---

export type PaymentMethod = "CASH" | "CARD";

export interface CreatePaymentRequest {
  amount: Amount;
  method: PaymentMethod;
  tip_amount?: Amount;
  // Pay for these lines (a split by items); omitted pays an amount.
  order_item_ids?: string[];
  // The card terminal's receipt reference, never card data.
  external_reference?: string | null;
  // The cashier confirmed the terminal approved the card payment.
  card_confirmed?: boolean;
}

// POST /bills/{id}/payments
export interface Payment {
  id: string;
  bill_id: string;
  amount: Amount;
  method: string;
  status: string;
  created_at: string;
  tip_amount: Amount;
  currency: string;
  user_id: string | null;
  cash_shift_id: string | null;
  business_day_id: string | null;
  confirmation_method: string | null;
  confirmed_at: string | null;
  external_reference: string | null;
}

export interface RefundRequest {
  amount?: Amount;
  tip_amount?: Amount;
  reason: string;
  external_reference?: string | null;
  card_confirmed?: boolean;
}

// POST /payments/{id}/refunds (payments:refund)
export interface Refund {
  id: string;
  payment_id: string;
  cash_shift_id: string;
  business_day_id: string;
  amount: Amount;
  tip_amount: Amount;
  currency: string;
  method: string;
  confirmation_method: string;
  external_reference: string | null;
  reason: string;
  user_id: string;
  created_at: string;
}
