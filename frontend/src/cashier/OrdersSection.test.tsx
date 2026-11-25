import { fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resetServerClock } from "../api/serverClock";
import { formatCents } from "../lib/money";
import { BILL_ID, billDetail, CASHIER, CASHIER_REGISTER, cashierOrder, MANAGER } from "../test/cashierFixtures";
import { deferred, findSection, openCashierSection, sent } from "../test/cashierTesting";
import { errorResponse, jsonResponse } from "../test/fixtures";
import { resetStoreForTests } from "../test/pendingOperationsTesting";
import { pendingOperations } from "./pendingOperations";

const PAYABLE = "GET /api/v1/cashier/orders?view=payable";
const SETTLED = "GET /api/v1/cashier/orders?view=settled";
const ORDER = cashierOrder();
const CREATE = `POST /api/v1/bills/order/${ORDER.order_id}`;
const BILL = `GET /api/v1/bills/${BILL_ID}`;

beforeEach(() => {
  window.localStorage.clear();
  resetStoreForTests(pendingOperations);
});

afterEach(() => {
  window.localStorage.clear();
  resetStoreForTests(pendingOperations);
  resetServerClock();
});

const ORDERS = [
  ORDER,
  cashierOrder({
    order_id: "or000000-0000-4000-8000-000000000005",
    table_number: 5,
    table_name: "Terrace",
    bill_id: BILL_ID,
    bill_status: "OPEN",
    paid_total: "10.00",
    amount_due: "20.00",
  }),
  cashierOrder({ order_id: "or000000-0000-4000-8000-000000000007", table_number: 7, order_status: "OPEN" }),
];

describe("payable orders", () => {
  it("lists every active order with what is due and the next action", async () => {
    openCashierSection(CASHIER, "orders", { [PAYABLE]: [jsonResponse(ORDERS)] });
    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(3);

    expect(within(rows[0]).getByText("Table 3")).toBeTruthy();
    expect(within(rows[0]).getByText("No bill yet")).toBeTruthy();
    expect(within(rows[0]).getByRole("button", { name: "Create bill: Table 3" })).toBeTruthy();

    expect(within(rows[1]).getByText("Table 5 · Terrace")).toBeTruthy();
    expect(within(rows[1]).getByText("Bill open")).toBeTruthy();
    expect(within(rows[1]).getByRole("button", { name: "Take payment: Table 5 · Terrace" })).toBeTruthy();
    expect(rows[1].textContent).toContain("20.00");

    expect(within(rows[2]).getByText("Not sent")).toBeTruthy();
    expect(within(rows[2]).queryByRole("button")).toBeNull();
  });

  it("explains an empty list", async () => {
    openCashierSection(CASHIER, "orders", { [PAYABLE]: [jsonResponse([])] });
    expect(await screen.findByText("No open orders")).toBeTruthy();
  });

  it("shows a load error and retries", async () => {
    openCashierSection(CASHIER, "orders", {
      [PAYABLE]: [errorResponse(500, "INTERNAL_ERROR", "Something went wrong"), jsonResponse(ORDERS)],
    });
    expect(await screen.findByText("Could not load the orders")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("table")).toBeTruthy();
  });

  it("switches to settled bills", async () => {
    const { calls } = openCashierSection(CASHIER, "orders", {
      [PAYABLE]: [jsonResponse(ORDERS)],
      [SETTLED]: [jsonResponse([cashierOrder({ bill_id: BILL_ID, bill_status: "PAID", amount_due: "0.00", paid_total: "40.50", order_status: "CLOSED" })])],
    });
    await screen.findByRole("table");
    fireEvent.click(screen.getByRole("radio", { name: "Settled" }));
    expect(await screen.findByRole("button", { name: "View bill: Table 3" })).toBeTruthy();
    expect(calls.some((call) => call.key === SETTLED)).toBe(true);
  });
});

describe("creating a bill", () => {
  it("creates the bill with the existing mutation and opens it in Payments", async () => {
    const { calls } = openCashierSection(CASHIER, "orders", {
      [PAYABLE]: [jsonResponse(ORDERS)],
      [CREATE]: [jsonResponse({ ...billDetail(), currency: undefined }, 201)],
      [BILL]: [jsonResponse(billDetail())],
    });
    fireEvent.click(await screen.findByRole("button", { name: "Create bill: Table 3" }));

    expect(await findSection("Payments")).toBeTruthy();
    expect(await screen.findByRole("status", { name: "Amount due" })).toBeTruthy();
    const [request] = sent(calls, CREATE);
    expect(request.body).toBeUndefined();
    expect(request.key).toBeTruthy();
    const nav = screen.getByRole("navigation", { name: "Cashier sections" });
    expect(within(nav).getByRole("button", { name: "Payments" }).getAttribute("aria-current")).toBe("page");
  });

  it("creates one bill for a double click", async () => {
    const response = deferred(jsonResponse(billDetail(), 201));
    const { calls } = openCashierSection(CASHIER, "orders", {
      [PAYABLE]: [jsonResponse(ORDERS)],
      [CREATE]: [response.handler],
      [BILL]: [jsonResponse(billDetail())],
    });
    const button = await screen.findByRole("button", { name: "Create bill: Table 3" });
    fireEvent.click(button);
    fireEvent.click(button);
    await vi.waitFor(() => expect(screen.getByRole("button", { name: "Create bill: Table 3" }).textContent).toBe("Creating…"));
    response.release();
    await findSection("Payments");
    expect(sent(calls, CREATE)).toHaveLength(1);
  });

  it("shows the server's refusal and stays on the list", async () => {
    openCashierSection(CASHIER, "orders", {
      [PAYABLE]: [jsonResponse(ORDERS)],
      [CREATE]: [errorResponse(409, "ORDER_NOT_READY_FOR_BILLING", "Order is not ready for billing")],
    });
    fireEvent.click(await screen.findByRole("button", { name: "Create bill: Table 3" }));
    expect(await screen.findByText("Could not create the bill for Table 3: Order is not ready for billing (request req-123)")).toBeTruthy();
    expect(screen.getByRole("heading", { level: 2, name: "Orders" })).toBeTruthy();
  });

  it("offers no bill creation without bills:create", async () => {
    openCashierSection({ ...MANAGER, permissions: ["bills:view", "payments:create"] }, "orders", { [PAYABLE]: [jsonResponse(ORDERS)] });
    await screen.findByRole("table");
    expect(screen.queryByRole("button", { name: /Create bill/ })).toBeNull();
    expect(screen.getByRole("button", { name: "Take payment: Table 5 · Terrace" })).toBeTruthy();
  });

  it("opens an existing bill from the list", async () => {
    openCashierSection(CASHIER, "orders", {
      [PAYABLE]: [jsonResponse(ORDERS)],
      [BILL]: [jsonResponse(billDetail({ paid_total: "10.00", amount_due: "20.00" }))],
    });
    fireEvent.click(await screen.findByRole("button", { name: "Take payment: Table 5 · Terrace" }));
    const due = await screen.findByRole("status", { name: "Amount due" });
    expect(due.textContent).toContain(formatCents(2000, "EUR"));
  });
});

describe("currency", () => {
  it("formats amounts with the restaurant's currency once the register told it", async () => {
    openCashierSection(CASHIER, "overview", {
      "GET /api/v1/cash/register": [jsonResponse(CASHIER_REGISTER)],
      [PAYABLE]: [jsonResponse([ORDER])],
    });
    await findSection("Overview");
    await screen.findByText("Current shift");
    const nav = screen.getByRole("navigation", { name: "Cashier sections" });
    fireEvent.click(within(nav).getByRole("button", { name: "Orders" }));
    const table = await screen.findByRole("table");
    expect(within(table).getAllByText(formatCents(4050, "EUR")).length).toBeGreaterThan(0);
    expect(window.sessionStorage.getItem("restaurantos.cashier.currency")).toBe("EUR");
  });
});
