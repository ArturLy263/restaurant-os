import { describe, expect, it } from "vitest";

import { Permission } from "../api/types";
import { hasAllPermissions, hasAnyPermission, hasPermission, meetsRequirement } from "./permissions";

const user = { permissions: [Permission.CashShiftsView, Permission.BillsView] };

describe("permission helpers", () => {
  it("checks single permissions", () => {
    expect(hasPermission(user, Permission.CashShiftsView)).toBe(true);
    expect(hasPermission(user, Permission.CashDrawerViewExpected)).toBe(false);
  });

  it("checks all and any", () => {
    expect(hasAllPermissions(user, [Permission.CashShiftsView, Permission.BillsView])).toBe(true);
    expect(hasAllPermissions(user, [Permission.CashShiftsView, Permission.CashReportsView])).toBe(false);
    expect(hasAllPermissions(user, [])).toBe(true);
    expect(hasAnyPermission(user, [Permission.CashReportsView, Permission.BillsView])).toBe(true);
    expect(hasAnyPermission(user, [Permission.CashReportsView])).toBe(false);
    expect(hasAnyPermission(user, [])).toBe(false);
  });

  it("combines all and any in a requirement", () => {
    expect(meetsRequirement(user, { all: [Permission.BillsView] })).toBe(true);
    expect(meetsRequirement(user, { any: [Permission.PaymentsCreate, Permission.PaymentsRefund] })).toBe(false);
    expect(
      meetsRequirement(user, { all: [Permission.BillsView], any: [Permission.CashShiftsView, Permission.PaymentsRefund] }),
    ).toBe(true);
    expect(meetsRequirement(user, {})).toBe(true);
  });

  it("does not match permission names loosely", () => {
    expect(hasPermission({ permissions: ["cash_shifts:view_all"] }, Permission.CashShiftsView)).toBe(false);
    expect(hasPermission({ permissions: [] }, Permission.CashShiftsView)).toBe(false);
  });
});
