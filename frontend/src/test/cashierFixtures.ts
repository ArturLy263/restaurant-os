import type { CashRegister, CashShift } from "../api/cashierTypes";
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
