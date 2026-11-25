import { fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resetServerClock } from "../api/serverClock";
import type { CurrentUser } from "../api/types";
import { formatCents } from "../lib/money";
import { BILL_ID, billDetail, CASHIER, cashierOrder, payment } from "../test/cashierFixtures";
import { deferred, findSection, openCashierSection, sent } from "../test/cashierTesting";
import { errorResponse, jsonResponse } from "../test/fixtures";
import { resetStoreForTests } from "../test/pendingOperationsTesting";
import { pendingOperations } from "./pendingOperations";

const PAYABLE = "GET /api/v1/cashier/orders?view=payable";
const BILL = `GET /api/v1/bills/${BILL_ID}`;
const PAY = `POST /api/v1/bills/${BILL_ID}/payments`;
const eur = (cents: number) => formatCents(cents, "EUR");

beforeEach(() => {
  window.localStorage.clear();
  resetStoreForTests(pendingOperations);
});

afterEach(() => {
  window.localStorage.clear();
  resetStoreForTests(pendingOperations);
  resetServerClock();
});

type Route = Response | (() => Promise<Response>);

/** Opens the bill of table 3 from Orders, as a cashier does. */
async function openBill(bills: Response[], routes: Record<string, Route[]> = {}, user: CurrentUser = CASHIER) {
  const api = openCashierSection(user, "orders", {
    [PAYABLE]: [jsonResponse([cashierOrder({ bill_id: BILL_ID, bill_status: "OPEN" })])],
    [BILL]: bills,
    ...routes,
  });
  fireEvent.click(await screen.findByRole("button", { name: "Take payment: Table 3" }));
  await findSection("Payments");
  const form = await screen.findByRole("form", { name: "Take a payment" });
  return { ...api, form };
}

const due = () => screen.getByRole("status", { name: "Amount due" });

describe("bill", () => {
  it("shows the lines, totals and the server's amount due", async () => {
    await openBill([jsonResponse(billDetail())]);
    expect(screen.getByText("Classic burger")).toBeTruthy();
    expect(screen.getByText("Cheese")).toBeTruthy();
    expect(screen.getByText("Pizza Margherita")).toBeTruthy();
    expect(due().textContent).toContain(eur(4050));
  });

  it("asks to choose an order when no bill is selected", async () => {
    openCashierSection(CASHIER, "payments", { [PAYABLE]: [jsonResponse([])] });
    expect(await screen.findByText("No bill selected")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Go to Orders" }));
    expect(await findSection("Orders")).toBeTruthy();
  });
});

describe("cash payment", () => {
  it("records the amount due, shows change worked out locally, and the bill turns paid", async () => {
    const paid = billDetail({ status: "PAID", paid_total: "40.50", amount_due: "0.00", paid_at: "2026-11-08T13:05:00", payments: [] });
    const { calls, form } = await openBill([jsonResponse(billDetail()), jsonResponse(paid)], {
      [PAY]: [jsonResponse(payment(), 201)],
    });
    expect((within(form).getByLabelText("Amount") as HTMLInputElement).value).toBe("40.50");
    fireEvent.change(within(form).getByLabelText("Cash received (optional, for change)"), { target: { value: "50" } });
    expect(within(form).getByText(eur(950))).toBeTruthy();

    fireEvent.click(within(form).getByRole("button", { name: `Record cash payment · ${eur(4050)}` }));
    const notice = await screen.findByText(/Cash payment of .* recorded/);
    expect(notice.textContent).toContain(`Give ${eur(950)} change.`);
    expect(notice.textContent).toContain("The bill is paid in full.");

    const [request] = sent(calls, PAY);
    // Only what the backend takes: the cash received and the change stay here.
    expect(request.body).toEqual({ amount: "40.50", method: "CASH" });
    expect(request.key).toBeTruthy();
    await vi.waitFor(() => expect(due().textContent).toContain("Paid in full"));
    expect(screen.queryByRole("form", { name: "Take a payment" })).toBeNull();
  });

  it("refuses too little cash received, and more than is due, before sending", async () => {
    const { calls, form } = await openBill([jsonResponse(billDetail())]);
    fireEvent.change(within(form).getByLabelText("Cash received (optional, for change)"), { target: { value: "20" } });
    expect(within(form).getByText(`Not enough: ${eur(2050)} short.`)).toBeTruthy();
    fireEvent.click(within(form).getByRole("button", { name: /Record cash payment/ }));

    fireEvent.change(within(form).getByLabelText("Cash received (optional, for change)"), { target: { value: "" } });
    fireEvent.change(within(form).getByLabelText("Amount"), { target: { value: "41" } });
    fireEvent.click(within(form).getByRole("button", { name: /Record cash payment/ }));
    expect(within(form).getByText(`At most the amount due (${eur(4050)}).`)).toBeTruthy();
    expect(sent(calls, PAY)).toHaveLength(0);
  });

  it("takes a partial payment with a tip and shows what remains", async () => {
    const after = billDetail({ paid_total: "20.00", amount_due: "20.50", tips_total: "2.00" });
    const { calls, form } = await openBill([jsonResponse(billDetail()), jsonResponse(after)], {
      [PAY]: [jsonResponse(payment({ amount: "20.00", tip_amount: "2.00" }), 201)],
    });
    fireEvent.change(within(form).getByLabelText("Amount"), { target: { value: "20" } });
    fireEvent.change(within(form).getByLabelText("Tip (optional)"), { target: { value: "2" } });
    expect(within(form).getByText(/A partial payment: .* stays due\./)).toBeTruthy();
    fireEvent.click(within(form).getByRole("button", { name: `Record cash payment · ${eur(2200)}` }));

    const notice = await screen.findByText(/Cash payment of .* recorded/);
    expect(notice.textContent).toContain(`(tip ${eur(200)})`);
    expect(notice.textContent).toContain(`Still due: ${eur(2050)}.`);
    expect(sent(calls, PAY)[0].body).toEqual({ amount: "20.00", method: "CASH", tip_amount: "2.00" });
    await vi.waitFor(() => expect(due().textContent).toContain(eur(2050)));
    expect((within(form).getByLabelText("Amount") as HTMLInputElement).value).toBe("20.50");
  });

  it("shows a refusal from the server and leaves nothing unresolved", async () => {
    const { form } = await openBill([jsonResponse(billDetail())], {
      [PAY]: [errorResponse(409, "ORDER_HAS_UNSENT_ITEMS", "Order has unsent items; send or cancel them before the final payment")],
    });
    fireEvent.click(within(form).getByRole("button", { name: /Record cash payment/ }));
    expect(await screen.findByText(/The payment was not recorded: Order has unsent items/)).toBeTruthy();
    expect(pendingOperations.getSnapshot().operations).toHaveLength(0);
  });
});

describe("card payment", () => {
  it("requires the cashier to confirm the terminal approved it, and sends no card data", async () => {
    const { calls, form } = await openBill([jsonResponse(billDetail())], {
      [PAY]: [jsonResponse(payment({ method: "CARD", confirmation_method: "MANUAL", external_reference: "A-1029" }), 201)],
    });
    fireEvent.click(within(form).getByRole("radio", { name: "Card" }));
    expect(within(form).queryByLabelText("Cash received (optional, for change)")).toBeNull();
    fireEvent.click(within(form).getByRole("button", { name: `Record card payment · ${eur(4050)}` }));
    expect(within(form).getByText("Confirm that the card terminal approved the payment.")).toBeTruthy();
    expect(sent(calls, PAY)).toHaveLength(0);

    fireEvent.click(within(form).getByRole("checkbox", { name: `The card terminal approved ${eur(4050)}` }));
    fireEvent.change(within(form).getByLabelText("Terminal receipt number (optional)"), { target: { value: "A-1029" } });
    fireEvent.click(within(form).getByRole("button", { name: `Record card payment · ${eur(4050)}` }));
    expect(await screen.findByText(/Card payment of .* recorded/)).toBeTruthy();
    expect(sent(calls, PAY)[0].body).toEqual({
      amount: "40.50",
      method: "CARD",
      card_confirmed: true,
      external_reference: "A-1029",
    });
  });

  it("never stores or sends what looks like a card number", async () => {
    const { calls, form } = await openBill([jsonResponse(billDetail())]);
    fireEvent.click(within(form).getByRole("radio", { name: "Card" }));
    fireEvent.click(within(form).getByRole("checkbox", { name: /The card terminal approved/ }));
    fireEvent.change(within(form).getByLabelText("Terminal receipt number (optional)"), {
      target: { value: "4111 1111 1111 1111" },
    });
    fireEvent.click(within(form).getByRole("button", { name: /Record card payment/ }));
    expect(await screen.findByText(/The payment was not recorded: Refusing to store what looks like a card number/)).toBeTruthy();
    expect(sent(calls, PAY)).toHaveLength(0);
    expect(pendingOperations.getSnapshot().operations).toHaveLength(0);
  });
});

describe("equal split", () => {
  const SPLIT = `GET /api/v1/bills/${BILL_ID}/split?parts=3`;

  it("splits by the server's shares and pays them one after another", async () => {
    const afterFirst = billDetail({ paid_total: "13.50", amount_due: "27.00" });
    const { calls, form } = await openBill([jsonResponse(billDetail()), jsonResponse(afterFirst)], {
      [SPLIT]: [jsonResponse({ bill_id: BILL_ID, amount_due: "40.50", shares: ["13.50", "13.50", "13.50"] })],
      [PAY]: [jsonResponse(payment({ amount: "13.50" }), 201), jsonResponse(payment({ id: "p2", amount: "13.50", method: "CARD" }), 201)],
    });
    fireEvent.click(within(form).getByRole("button", { name: "More guests" }));
    fireEvent.click(within(form).getByRole("button", { name: "Split in 3" }));

    const shares = await within(form).findByRole("list", { name: "Shares" });
    expect(within(shares).getAllByRole("button")).toHaveLength(3);
    expect((within(form).getByLabelText("Amount") as HTMLInputElement).value).toBe("13.50");
    expect(within(form).getByText("Share 1 of 3")).toBeTruthy();

    fireEvent.click(within(form).getByRole("button", { name: `Record cash payment · ${eur(1350)}` }));
    expect(await screen.findByText(new RegExp(`Still due: ${eur(2700).replace(/[.$]/g, "\\$&")}`))).toBeTruthy();
    await vi.waitFor(() => expect(due().textContent).toContain(eur(2700)));

    // The paid share is marked and the next one is chosen.
    expect(within(shares).getByRole("button", { name: `Share 1 of 3: ${eur(1350)}, paid` })).toHaveProperty("disabled", true);
    expect(within(form).getByText("Share 2 of 3")).toBeTruthy();

    fireEvent.click(within(form).getByRole("radio", { name: "Card" }));
    fireEvent.click(within(form).getByRole("checkbox", { name: /The card terminal approved/ }));
    fireEvent.click(within(form).getByRole("button", { name: `Record card payment · ${eur(1350)}` }));
    await screen.findByText(/Card payment of .* recorded/);

    expect(sent(calls, PAY).map((request) => request.body)).toEqual([
      { amount: "13.50", method: "CASH" },
      { amount: "13.50", method: "CARD", card_confirmed: true },
    ]);
  });

  it("shows when the server refuses a split", async () => {
    const { form } = await openBill([jsonResponse(billDetail())], {
      [`GET /api/v1/bills/${BILL_ID}/split?parts=2`]: [errorResponse(409, "NOTHING_DUE", "Nothing is due on this bill")],
    });
    fireEvent.click(within(form).getByRole("button", { name: "Split in 2" }));
    expect(await within(form).findByText("Nothing is due on this bill")).toBeTruthy();
  });
});

describe("submitting once", () => {
  it("records one payment for a double click", async () => {
    const response = deferred(jsonResponse(payment(), 201));
    const { calls, form } = await openBill([jsonResponse(billDetail())], { [PAY]: [response.handler] });
    const button = within(form).getByRole("button", { name: /Record cash payment/ });
    fireEvent.click(button);
    fireEvent.click(button);
    await vi.waitFor(() => expect(within(form).getByRole("button", { name: "Recording…" })).toHaveProperty("disabled", true));
    response.release();
    await screen.findByText(/Cash payment of .* recorded/);
    expect(sent(calls, PAY)).toHaveLength(1);
  });

  it("keeps a payment with an unknown outcome for a retry with the same key", async () => {
    const { calls, form } = await openBill([jsonResponse(billDetail())], {
      [PAY]: [errorResponse(504, "HTTP_504", "The RestaurantOS server did not respond in time.")],
    });
    fireEvent.click(within(form).getByRole("button", { name: /Record cash payment/ }));
    expect(await screen.findByText(/did not confirm this/)).toBeTruthy();
    const region = await screen.findByRole("region", { name: "Unresolved operations" });
    expect(region.textContent).toContain("unknown outcome");

    const [op] = pendingOperations.getSnapshot().operations;
    expect(op.status).toBe("unknown");
    expect(op.idempotencyKey).toBe(sent(calls, PAY)[0].key);
  });
});
