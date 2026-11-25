import { fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BusinessDay } from "../api/cashierTypes";
import { resetServerClock } from "../api/serverClock";
import type { CurrentUser } from "../api/types";
import { formatCents } from "../lib/money";
import { BUSINESS_DAY, CASHIER, dayReport, MANAGER } from "../test/cashierFixtures";
import { openCashierSection, sent } from "../test/cashierTesting";
import { jsonResponse } from "../test/fixtures";
import { resetStoreForTests } from "../test/pendingOperationsTesting";
import { pendingOperations } from "./pendingOperations";

const DAYS = "GET /api/v1/business-days?limit=30";
const REPORT = `GET /api/v1/business-days/${BUSINESS_DAY.id}/report`;
const CLOSE = `POST /api/v1/business-days/${BUSINESS_DAY.id}/close`;
const REGISTER = "GET /api/v1/cash/register";
const eur = (cents: number) => formatCents(cents, "EUR");

const CLOSED_DAY: BusinessDay = {
  ...BUSINESS_DAY,
  status: "CLOSED",
  closed_by_user_id: MANAGER.id,
  closed_at: "2026-11-08T23:30:00",
};
const EARLIER_DAY: BusinessDay = {
  ...CLOSED_DAY,
  id: "bd000000-0000-4000-8000-000000000000",
  business_date: "2026-11-07",
};

beforeEach(() => {
  window.localStorage.clear();
  resetStoreForTests(pendingOperations);
});

afterEach(() => {
  window.localStorage.clear();
  resetStoreForTests(pendingOperations);
  resetServerClock();
});

function open(user: CurrentUser, routes: Record<string, Response[]>) {
  return openCashierSection(user, "reports", {
    [DAYS]: [jsonResponse([BUSINESS_DAY, EARLIER_DAY])],
    [REPORT]: [jsonResponse({ business_day: BUSINESS_DAY, report: dayReport() })],
    ...routes,
  });
}

describe("business day report", () => {
  it("lists the days and shows the newest day's report", async () => {
    open(MANAGER, {});
    const days = await screen.findByRole("navigation", { name: "Business days" });
    expect(within(days).getAllByRole("button")).toHaveLength(2);
    expect(within(days).getAllByRole("button")[0].getAttribute("aria-current")).toBe("true");

    const revenue = await screen.findByRole("article", { name: "Revenue" });
    expect(within(revenue).getByText(eur(19550))).toBeTruthy();
    expect(within(revenue).getByText(eur(11500))).toBeTruthy();
    expect(within(revenue).getByText(eur(8050))).toBeTruthy();
    expect(within(screen.getByRole("article", { name: "Tips" })).getByText(eur(500))).toBeTruthy();
    expect(within(screen.getByRole("article", { name: "Refunds" })).getAllByText(eur(500)).length).toBeGreaterThan(0);
    expect(screen.getByText(/Open · figures so far/)).toBeTruthy();
  });

  it("shows each shift's drawer, count and difference, and the handovers, to a manager", async () => {
    open(MANAGER, {});
    const shifts = await screen.findByRole("article", { name: "Cash shifts" });
    const row = within(shifts).getAllByRole("row")[1];
    expect(row.textContent).toContain("Carla Cashier");
    expect(row.textContent).toContain("Coins miscounted");
    expect(row.textContent).toContain(eur(27800));
    expect(row.textContent).toContain(eur(27500));
    expect(row.textContent).toContain(eur(-300));

    const handovers = screen.getByRole("article", { name: "Handovers" });
    expect(handovers.textContent).toContain("Carla Cashier → Chris Cashier");
  });

  it("leaves expected cash and differences out without cash_drawer:view_expected", async () => {
    const auditor = { ...MANAGER, permissions: ["cash_reports:view", "cash_shifts:view"] };
    open(auditor, {});
    const shifts = await screen.findByRole("article", { name: "Cash shifts" });
    expect(within(shifts).queryByText("Expected")).toBeNull();
    expect(within(shifts).queryByText("Difference")).toBeNull();
    expect(shifts.textContent).not.toContain(eur(27800));
    expect(screen.queryByRole("button", { name: "Close business day" })).toBeNull();
  });

  it("switches to an earlier, frozen report", async () => {
    const earlier = `GET /api/v1/business-days/${EARLIER_DAY.id}/report`;
    open(MANAGER, {
      [earlier]: [jsonResponse({ business_day: EARLIER_DAY, report: dayReport({ business_date: "2026-11-07", shifts: [], handovers: [] }) })],
    });
    const days = await screen.findByRole("navigation", { name: "Business days" });
    fireEvent.click(within(days).getAllByRole("button")[1]);
    expect(await screen.findByText(/Closed · frozen report/)).toBeTruthy();
    expect(screen.getByText("No shift was opened on this day.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Close business day" })).toBeNull();
  });

  it("explains when there is no business day yet", async () => {
    open(MANAGER, { [DAYS]: [jsonResponse([])] });
    expect(await screen.findByText("No business days yet")).toBeTruthy();
  });

  it("is not offered to cashiers", async () => {
    window.sessionStorage.setItem("restaurantos.cashier.section", "reports");
    openCashierSection(CASHIER, "reports", { [REGISTER]: [jsonResponse({ register_id: null, business_day: null, shift: null, previous_closing: null })] });
    const nav = await screen.findByRole("navigation", { name: "Cashier sections" });
    expect(within(nav).queryByRole("button", { name: "Reports" })).toBeNull();
    expect(await screen.findByRole("heading", { level: 2, name: "Overview" })).toBeTruthy();
  });
});

describe("closing the business day", () => {
  it("closes the day after a confirmation and shows the frozen report", async () => {
    const { calls } = open(MANAGER, {
      [DAYS]: [jsonResponse([BUSINESS_DAY, EARLIER_DAY]), jsonResponse([CLOSED_DAY, EARLIER_DAY])],
      [REPORT]: [
        jsonResponse({ business_day: BUSINESS_DAY, report: dayReport() }),
        jsonResponse({ business_day: CLOSED_DAY, report: dayReport() }),
      ],
      [CLOSE]: [jsonResponse(CLOSED_DAY)],
    });
    fireEvent.click(await screen.findByRole("button", { name: "Close business day" }));
    expect(sent(calls, CLOSE)).toHaveLength(0);
    expect(screen.getByText(/This cannot be undone/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Confirm close day" }));

    expect(await screen.findByText(/closed\. Its report is now frozen\./)).toBeTruthy();
    await vi.waitFor(() => expect(screen.getByText(/Closed · frozen report/)).toBeTruthy());
    expect(screen.queryByRole("button", { name: "Close business day" })).toBeNull();
    expect(sent(calls, CLOSE)[0].key).toBeTruthy();
  });

  it("lists the orders that keep the day from closing", async () => {
    open(MANAGER, {
      [CLOSE]: [
        jsonResponse(
          {
            detail: "1 order(s) are still open; settle, close or void each before closing the business day",
            error: {
              code: "BUSINESS_DAY_HAS_OPEN_ORDERS",
              message: "1 order(s) are still open; settle, close or void each before closing the business day",
              request_id: "req-9",
              details: {
                orders: [
                  {
                    order_id: "or1",
                    order_status: "SENT",
                    table_number: 4,
                    table_name: null,
                    bill_id: "b9",
                    bill_status: "OPEN",
                    outstanding_amount: "18.20",
                  },
                ],
              },
            },
          },
          409,
        ),
      ],
    });
    fireEvent.click(await screen.findByRole("button", { name: "Close business day" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm close day" }));

    expect(await screen.findByText(/The business day was not closed: 1 order\(s\) are still open/)).toBeTruthy();
    const blocking = await screen.findByRole("article", { name: /Still open/ });
    expect(blocking.textContent).toContain("Table 4");
    expect(blocking.textContent).toContain("Bill open");
    expect(blocking.textContent).toContain(`${eur(1820)} due`);
    expect(pendingOperations.getSnapshot().operations).toHaveLength(0);
  });

  it("lists unexplained counted differences and lets the manager explain them", async () => {
    const shiftId = "sh000000-0000-4000-8000-000000000002";
    const explain = `POST /api/v1/cash/shifts/${shiftId}/closing-explanation`;
    const { calls } = open(MANAGER, {
      [CLOSE]: [
        jsonResponse(
          {
            detail: "1 counted cash difference(s) have no explanation; explain each in the cash register before closing the business day",
            error: {
              code: "CASH_DIFFERENCE_UNEXPLAINED",
              message: "1 counted cash difference(s) have no explanation; explain each in the cash register before closing the business day",
              request_id: "req-7",
              details: { counts: [{ cash_shift_id: shiftId, kind: "CLOSING" }] },
            },
          },
          409,
        ),
      ],
      [explain]: [
        jsonResponse(
          { id: "ex1", cash_shift_id: shiftId, kind: "CLOSING", explanation: "Coins miscounted", user_id: MANAGER.id, created_at: "2026-11-08T23:00:00" },
          201,
        ),
      ],
    });
    fireEvent.click(await screen.findByRole("button", { name: "Close business day" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm close day" }));

    expect(await screen.findByText(/Record an explanation for each one below\./)).toBeTruthy();
    const card = await screen.findByRole("article", { name: /Explain these counted differences first/ });
    // No amount is shown, only what needs explaining.
    expect(card.textContent).not.toMatch(/€/);
    const form = within(card).getByRole("form", { name: "Explain the closing count" });
    fireEvent.change(within(form).getByLabelText("Explanation"), { target: { value: "Coins miscounted" } });
    fireEvent.click(within(form).getByRole("button", { name: "Record explanation" }));

    expect(await screen.findByText("Explanation recorded.")).toBeTruthy();
    const [request] = sent(calls, explain);
    expect(request.body).toEqual({ explanation: "Coins miscounted" });
    expect(request.key).toBeTruthy();
    await vi.waitFor(() => expect(screen.queryByRole("article", { name: /Explain these counted differences first/ })).toBeNull());
  });

  it("says to close the open shift first", async () => {
    open(MANAGER, {
      [CLOSE]: [
        jsonResponse(
          {
            detail: "Close the open cash shift before closing the business day",
            error: { code: "CASH_SHIFT_STILL_OPEN", message: "Close the open cash shift before closing the business day", request_id: "req-8" },
          },
          409,
        ),
      ],
    });
    fireEvent.click(await screen.findByRole("button", { name: "Close business day" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm close day" }));
    expect(await screen.findByText(/Count and close it in Cash Register first\./)).toBeTruthy();
  });
});
