import type {
  BillDetail,
  BusinessDayReport,
  CashierOrder,
  CashRegister,
  CashShift,
  MethodTotals,
  Payment,
} from "../api/cashierTypes";
import type { CurrentUser } from "../api/types";

const RESTAURANT_ID = "1f2e3d4c-0000-4000-8000-000000000001";

// The CASHIER role of backend app/core/permissions.py.
export const CASHIER: CurrentUser = {
  id: "c0000000-0000-4000-8000-0000000000c1",
  name: "Carla Cashier",
  email: "carla@example.com",
  restaurant_id: RESTAURANT_ID,
  role: { id: "aa000000-0000-4000-8000-0000000000c1", name: "CASHIER" },
  permissions: [
    "bills:create",
    "bills:view",
    "business_days:open",
    "cash_movements:create",
    "cash_shifts:close",
    "cash_shifts:open",
    "cash_shifts:view",
    "orders:view",
    "payments:create",
  ],
  stations: null,
};

export const MANAGER: CurrentUser = {
  id: "m0000000-0000-4000-8000-0000000000m1",
  name: "Max Manager",
  email: "max@example.com",
  restaurant_id: RESTAURANT_ID,
  role: { id: "aa000000-0000-4000-8000-0000000000m1", name: "MANAGER" },
  permissions: [
    ...CASHIER.permissions,
    "bills:adjust",
    "business_days:close",
    "business_days:open_past",
    "cash_drawer:view_expected",
    "cash_handovers:self_confirm",
    "cash_opening_balance:correct",
    "cash_reports:view",
    "payments:refund",
  ],
  stations: null,
};

export const BUSINESS_DAY = {
  id: "bd000000-0000-4000-8000-000000000001",
  business_date: "2026-11-08",
  status: "OPEN",
  opened_by_user_id: MANAGER.id,
  opened_at: "2026-11-08T08:00:00",
  closed_by_user_id: null,
  closed_at: null,
};

export function shift(overrides: Partial<CashShift> = {}): CashShift {
  return {
    id: "sh000000-0000-4000-8000-000000000001",
    cash_register_id: "cr000000-0000-4000-8000-000000000001",
    business_day_id: BUSINESS_DAY.id,
    status: "OPEN",
    currency: "EUR",
    opening_amount: "150.00",
    effective_opening_amount: "150.00",
    opened_by: { id: CASHIER.id, name: CASHIER.name },
    opened_at: "2026-11-08T08:05:00",
    closed_at: null,
    expected_cash: null,
    drawer: null,
    expected_visible: false,
    corrections: [],
    movements: [],
    handover: null,
    closing: null,
    ...overrides,
  };
}

/** What GET /cash/register returns to a cashier (blind: no expected cash). */
export const CASHIER_REGISTER: CashRegister = {
  register_id: "cr000000-0000-4000-8000-000000000001",
  business_day: BUSINESS_DAY,
  shift: shift(),
  previous_closing: {
    cash_shift_id: "sh000000-0000-4000-8000-000000000000",
    business_day_id: BUSINESS_DAY.id,
    expected_amount: null,
    counted_amount: null,
    closed_at: "2026-11-07T23:10:00",
    closed_by: { id: MANAGER.id, name: "Night Cashier" },
    handover_pending: false,
    explanation_required: false,
  },
};

/** What a manager gets: expected cash included. */
export const MANAGER_REGISTER: CashRegister = {
  ...CASHIER_REGISTER,
  shift: shift({
    expected_cash: "987.65",
    expected_visible: true,
    drawer: {
      opening_amount: "150.00",
      effective_opening_amount: "150.00",
      opening_corrections_count: 0,
      cash_sales: "837.65",
      cash_tips: "0.00",
      cash_in: "0.00",
      cash_out: "0.00",
      cash_refunds: "0.00",
      expected: "987.65",
    },
  }),
  previous_closing: {
    ...CASHIER_REGISTER.previous_closing!,
    expected_amount: "432.10",
    counted_amount: "430.00",
  },
};

export const EMPTY_REGISTER: CashRegister = {
  register_id: null,
  business_day: null,
  shift: null,
  previous_closing: null,
};

// --- Phase 2 sections ---

/** A second cashier, for the two-person handover. */
export const CASHIER_TWO: CurrentUser = {
  ...CASHIER,
  id: "c0000000-0000-4000-8000-0000000000c2",
  name: "Chris Cashier",
  email: "chris@example.com",
};

/** The day is open, no shift has ever been opened (first shift of the register). */
export const DAY_WITHOUT_SHIFT: CashRegister = {
  register_id: null,
  business_day: BUSINESS_DAY,
  shift: null,
  previous_closing: null,
};

/** Carla closed (handed over) the last shift; the next cashier must receive it. */
export const HANDOVER_PENDING: CashRegister = {
  register_id: "cr000000-0000-4000-8000-000000000001",
  business_day: BUSINESS_DAY,
  shift: null,
  previous_closing: {
    cash_shift_id: "sh000000-0000-4000-8000-000000000001",
    business_day_id: BUSINESS_DAY.id,
    expected_amount: null,
    counted_amount: null,
    closed_at: "2026-11-08T15:00:00",
    closed_by: { id: CASHIER.id, name: CASHIER.name },
    handover_pending: true,
    explanation_required: false,
  },
};

export function closedShift(overrides: Partial<CashShift> = {}): CashShift {
  return shift({
    status: "CLOSED",
    closed_at: "2026-11-08T15:00:00",
    closing: {
      expected_amount: null,
      counted_amount: "200.00",
      difference: null,
      explanation: null,
      explanation_required: false,
      user: { id: CASHIER.id, name: CASHIER.name },
      created_at: "2026-11-08T15:00:00",
    },
    ...overrides,
  });
}

export function cashierOrder(overrides: Partial<CashierOrder> = {}): CashierOrder {
  return {
    order_id: "or000000-0000-4000-8000-000000000003",
    order_status: "SENT",
    table_number: 3,
    table_name: null,
    guest_count: 2,
    waiter_name: "Walt Waiter",
    created_at: "2026-11-08T12:30:00",
    items_count: 4,
    items_total: "40.50",
    bill_id: null,
    bill_status: null,
    paid_total: "0.00",
    amount_due: "40.50",
    ...overrides,
  };
}

export const BILL_ID = "b1000000-0000-4000-8000-000000000001";

export function billDetail(overrides: Partial<BillDetail> = {}): BillDetail {
  return {
    id: BILL_ID,
    order_id: "or000000-0000-4000-8000-000000000003",
    subtotal: "40.50",
    total: "40.50",
    status: "OPEN",
    created_at: "2026-11-08T13:00:00",
    paid_at: null,
    voided_at: null,
    adjustments_total: "0.00",
    adjusted_total: "40.50",
    paid_total: "0.00",
    amount_due: "40.50",
    adjustments: [],
    currency: "EUR",
    order: {
      id: "or000000-0000-4000-8000-000000000003",
      status: "SENT",
      table_number: 3,
      table_name: null,
      guest_count: 2,
      waiter_name: "Walt Waiter",
    },
    lines: [
      {
        order_item_id: "it000000-0000-4000-8000-000000000001",
        product_name: "Classic burger",
        quantity: 1,
        unit_price: "14.50",
        total_price: "14.50",
        status: "SENT",
        notes: null,
        modifiers: [{ name: "Cheese", price: "1.00" }],
        paid_by_payment_id: null,
      },
      {
        order_item_id: "it000000-0000-4000-8000-000000000002",
        product_name: "Pizza Margherita",
        quantity: 2,
        unit_price: "11.00",
        total_price: "22.00",
        status: "SENT",
        notes: null,
        modifiers: [],
        paid_by_payment_id: null,
      },
      {
        order_item_id: "it000000-0000-4000-8000-000000000003",
        product_name: "Coca-Cola",
        quantity: 1,
        unit_price: "4.00",
        total_price: "4.00",
        status: "SENT",
        notes: null,
        modifiers: [],
        paid_by_payment_id: null,
      },
    ],
    payments: [],
    tips_total: "0.00",
    refunded_total: "0.00",
    refunded_tips_total: "0.00",
    ...overrides,
  };
}

export function payment(overrides: Partial<Payment> = {}): Payment {
  return {
    id: "pa000000-0000-4000-8000-000000000001",
    bill_id: BILL_ID,
    amount: "40.50",
    method: "CASH",
    status: "COMPLETED",
    created_at: "2026-11-08T13:05:00",
    tip_amount: "0.00",
    currency: "EUR",
    user_id: CASHIER.id,
    cash_shift_id: "sh000000-0000-4000-8000-000000000001",
    business_day_id: BUSINESS_DAY.id,
    confirmation_method: null,
    confirmed_at: null,
    external_reference: null,
    ...overrides,
  };
}

const TOTALS = (cash: string, card: string, total: string): MethodTotals => ({ CASH: cash, CARD: card, total });

export function dayReport(overrides: Partial<BusinessDayReport> = {}): BusinessDayReport {
  return {
    sales: TOTALS("120.00", "80.50", "200.50"),
    refunds: TOTALS("5.00", "0.00", "5.00"),
    revenue: TOTALS("115.00", "80.50", "195.50"),
    tips: TOTALS("3.00", "2.00", "5.00"),
    refunded_tips: TOTALS("0.00", "0.00", "0.00"),
    net_tips: TOTALS("3.00", "2.00", "5.00"),
    payments_count: 6,
    refunds_count: 1,
    business_day_id: BUSINESS_DAY.id,
    business_date: BUSINESS_DAY.business_date,
    currency: "EUR",
    shifts: [
      {
        sales: TOTALS("120.00", "80.50", "200.50"),
        refunds: TOTALS("5.00", "0.00", "5.00"),
        revenue: TOTALS("115.00", "80.50", "195.50"),
        tips: TOTALS("3.00", "2.00", "5.00"),
        refunded_tips: TOTALS("0.00", "0.00", "0.00"),
        net_tips: TOTALS("3.00", "2.00", "5.00"),
        payments_count: 6,
        refunds_count: 1,
        cash_shift_id: "sh000000-0000-4000-8000-000000000001",
        business_day_id: BUSINESS_DAY.id,
        currency: "EUR",
        drawer: {
          opening_amount: "150.00",
          effective_opening_amount: "150.00",
          opening_corrections_count: 0,
          cash_sales: "120.00",
          cash_tips: "3.00",
          cash_in: "20.00",
          cash_out: "10.00",
          cash_refunds: "5.00",
          expected: "278.00",
        },
        closing: { expected_amount: "278.00", counted_amount: "275.00", difference: "-3.00", explanation: "Coins miscounted" },
        status: "CLOSED",
        opened_by: CASHIER.name,
        opened_at: "2026-11-08T08:05:00",
        closed_at: "2026-11-08T15:00:00",
      },
    ],
    handovers: [
      {
        id: "ho000000-0000-4000-8000-000000000001",
        from_shift_id: "sh000000-0000-4000-8000-000000000001",
        to_shift_id: "sh000000-0000-4000-8000-000000000002",
        business_day_id: BUSINESS_DAY.id,
        previous_expected_amount: "278.00",
        handed_over_amount: "275.00",
        received_amount: "275.00",
        difference: "0.00",
        reason: null,
        outgoing_user: CASHIER.name,
        incoming_user: CASHIER_TWO.name,
        self_confirmed: false,
        created_at: "2026-11-08T15:05:00",
      },
    ],
    closing_difference_total: "-3.00",
    handover_difference_total: "0.00",
    ...overrides,
  };
}
