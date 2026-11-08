import { Permission, type CurrentUser } from "../api/types";
import { meetsRequirement, type PermissionRequirement } from "../auth/permissions";

// The sections of the cashier workspace and the permissions each needs —
// the same permissions the backend requires for the section's endpoints,
// so a section is only offered when its requests can succeed.

export type CashierSectionId = "overview" | "orders" | "payments" | "register" | "reports" | "settings";

export interface CashierSection {
  id: CashierSectionId;
  label: string;
  requirement: PermissionRequirement;
}

export const CASHIER_SECTIONS: readonly CashierSection[] = [
  // GET /cash/register
  { id: "overview", label: "Overview", requirement: { all: [Permission.CashShiftsView] } },
  // GET /cashier/orders
  { id: "orders", label: "Orders", requirement: { all: [Permission.BillsView] } },
  // GET /bills/{id}, POST …/payments or …/refunds
  {
    id: "payments",
    label: "Payments",
    requirement: { all: [Permission.BillsView], any: [Permission.PaymentsCreate, Permission.PaymentsRefund] },
  },
  // GET /cash/register, /cash/shifts/{id}, /cash/handovers
  { id: "register", label: "Cash Register", requirement: { all: [Permission.CashShiftsView] } },
  // GET /business-days, /business-days/{id}/report
  { id: "reports", label: "Reports", requirement: { all: [Permission.CashReportsView] } },
  // Device preferences only.
  { id: "settings", label: "Settings", requirement: {} },
];

export function sectionById(id: CashierSectionId): CashierSection {
  return CASHIER_SECTIONS.find((section) => section.id === id)!;
}

export function canAccessSection(user: Pick<CurrentUser, "permissions">, id: CashierSectionId): boolean {
  return meetsRequirement(user, sectionById(id).requirement);
}

export function accessibleSections(user: Pick<CurrentUser, "permissions">): CashierSection[] {
  return CASHIER_SECTIONS.filter((section) => meetsRequirement(user, section.requirement));
}

/** The workspace is offered when a section other than Settings is usable. */
export function canUseCashierWorkspace(user: Pick<CurrentUser, "permissions">): boolean {
  return accessibleSections(user).some((section) => section.id !== "settings");
}

/** Expected cash and count differences (managers; cashiers count blind). */
export function canSeeExpectedCash(user: Pick<CurrentUser, "permissions">): boolean {
  return meetsRequirement(user, { all: [Permission.CashDrawerViewExpected] });
}

export function isSectionId(value: unknown): value is CashierSectionId {
  return CASHIER_SECTIONS.some((section) => section.id === value);
}
