import { fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resetServerClock } from "../api/serverClock";
import { formatCents } from "../lib/money";
import {
  BUSINESS_DAY,
  CASHIER,
  CASHIER_REGISTER,
  CASHIER_TWO,
  closedShift,
  DAY_WITHOUT_SHIFT,
  EMPTY_REGISTER,
  HANDOVER_PENDING,
  MANAGER,
  MANAGER_REGISTER,
  shift,
} from "../test/cashierFixtures";
import { deferred, findSection, openCashierSection, sent } from "../test/cashierTesting";
import { errorResponse, jsonResponse } from "../test/fixtures";
import { resetStoreForTests } from "../test/pendingOperationsTesting";
import { pendingOperations } from "./pendingOperations";

const REGISTER = "GET /api/v1/cash/register";
const HANDOVERS = "GET /api/v1/cash/handovers?limit=5";
const SHIFT_ID = CASHIER_REGISTER.shift!.id;
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

function open(user = CASHIER, registers: Response[], routes: Record<string, Response[] | Array<() => Promise<Response>>> = {}) {
  return openCashierSection(user, "register", {
    [REGISTER]: registers,
    [HANDOVERS]: [jsonResponse([])],
    ...routes,
  });
}

/** Operations left in storage that are not confirmed successes. */
const openRecords = () => pendingOperations.getSnapshot().operations.filter((op) => op.status !== "succeeded");

describe("business day", () => {
  it("opens the business day and reloads the register", async () => {
    const { calls } = open(CASHIER, [jsonResponse(EMPTY_REGISTER), jsonResponse(DAY_WITHOUT_SHIFT)], {
      "POST /api/v1/business-days": [jsonResponse(BUSINESS_DAY, 201)],
    });
    await findSection("Cash Register");
    fireEvent.click(await screen.findByRole("button", { name: "Open business day" }));

    expect(await screen.findByText(/Business day .* opened\./)).toBeTruthy();
    const [request] = sent(calls, "POST /api/v1/business-days");
    expect(request.body).toEqual({});
    expect(request.key).toBeTruthy();
    await vi.waitFor(() => expect(calls.filter((call) => call.key === REGISTER).length).toBe(2));
    expect(await screen.findByRole("form", { name: "Open a shift" })).toBeTruthy();
  });

  it("does not offer opening the day without the permission", async () => {
    open({ ...MANAGER, permissions: ["cash_shifts:view"] }, [jsonResponse(EMPTY_REGISTER)]);
    await screen.findByText(/No business day is open/);
    expect(screen.queryByRole("button", { name: "Open business day" })).toBeNull();
  });
});

describe("opening a shift", () => {
  it("opens the first shift with the counted opening cash", async () => {
    const { calls } = open(CASHIER, [jsonResponse(DAY_WITHOUT_SHIFT), jsonResponse(CASHIER_REGISTER)], {
      "POST /api/v1/cash/shifts": [jsonResponse(shift(), 201)],
    });
    const form = await screen.findByRole("form", { name: "Open a shift" });
    fireEvent.change(within(form).getByLabelText("Opening cash"), { target: { value: "150" } });
    fireEvent.click(within(form).getByRole("button", { name: "Open shift" }));

    expect(await screen.findByText("Shift opened.")).toBeTruthy();
    const [request] = sent(calls, "POST /api/v1/cash/shifts");
    expect(request.body).toEqual({ opening_amount: "150.00", handover_reason: null });
    expect(request.key).toBeTruthy();
    expect(await screen.findByRole("form", { name: "Count and close the shift" })).toBeTruthy();
  });

  it("validates the amount before sending anything", async () => {
    const { calls } = open(CASHIER, [jsonResponse(DAY_WITHOUT_SHIFT)]);
    const form = await screen.findByRole("form", { name: "Open a shift" });
    fireEvent.click(within(form).getByRole("button", { name: "Open shift" }));
    expect(within(form).getByText("Enter an amount.")).toBeTruthy();
    fireEvent.change(within(form).getByLabelText("Opening cash"), { target: { value: "12.345" } });
    expect(within(form).getByText(/at most two decimals/)).toBeTruthy();
    expect(sent(calls, "POST /api/v1/cash/shifts")).toHaveLength(0);
  });
});

describe("closing a shift (blind count)", () => {
  it("never shows a cashier the expected cash, and sends only the count", async () => {
    const { calls } = open(CASHIER, [jsonResponse(CASHIER_REGISTER), jsonResponse(HANDOVER_PENDING)], {
      [`POST /api/v1/cash/shifts/${SHIFT_ID}/handover`]: [jsonResponse(closedShift(), 200)],
    });
    const form = await screen.findByRole("form", { name: "Count and close the shift" });
    expect(screen.queryByText("Expected cash")).toBeNull();
    expect(screen.getByText(/Blind count/)).toBeTruthy();

    fireEvent.change(within(form).getByLabelText("Cash counted in the drawer"), { target: { value: "200" } });
    fireEvent.click(within(form).getByRole("button", { name: "Hand over shift" }));
    // An irreversible step is confirmed first; nothing is sent yet.
    expect(sent(calls, `POST /api/v1/cash/shifts/${SHIFT_ID}/handover`)).toHaveLength(0);
    expect(within(form).getByText(/This cannot be undone/)).toBeTruthy();
    fireEvent.click(within(form).getByRole("button", { name: "Confirm handover" }));

    const notice = await screen.findByText(/Shift handed over\./);
    expect(notice.textContent).toContain(`Counted ${eur(20000)}.`);
    expect(notice.textContent).not.toMatch(/Expected|difference/);
    expect(notice.textContent).toContain("The next cashier confirms the cash by opening their shift.");
    expect(sent(calls, `POST /api/v1/cash/shifts/${SHIFT_ID}/handover`)[0].body).toEqual({
      counted_amount: "200.00",
      closing_explanation: null,
    });
  });

  it("hides expected cash and the difference from a cashier even if a response carried them", async () => {
    open(CASHIER, [jsonResponse({ ...CASHIER_REGISTER, shift: shift({ expected_cash: "321.00", expected_visible: true }) })], {
      [`POST /api/v1/cash/shifts/${SHIFT_ID}/handover`]: [
        jsonResponse(
          closedShift({
            closing: {
              expected_amount: "321.00",
              counted_amount: "300.00",
              difference: "-21.00",
              explanation: "x",
              explanation_required: false,
              user: { id: CASHIER.id, name: CASHIER.name },
              created_at: "2026-11-08T15:00:00",
            },
          }),
        ),
      ],
    });
    const form = await screen.findByRole("form", { name: "Count and close the shift" });
    expect(screen.queryByText(eur(32100))).toBeNull();
    fireEvent.change(within(form).getByLabelText("Cash counted in the drawer"), { target: { value: "300" } });
    fireEvent.change(within(form).getByLabelText("Explanation, if you know the count differs (optional)"), { target: { value: "x" } });
    fireEvent.click(within(form).getByRole("button", { name: "Hand over shift" }));
    fireEvent.click(within(form).getByRole("button", { name: "Confirm handover" }));
    const notice = await screen.findByText(/Shift handed over\./);
    expect(notice.textContent).not.toContain(eur(32100));
    expect(notice.textContent).not.toContain(eur(-2100));
  });

  it("records a differing count once, then asks for an explanation without revealing any amount", async () => {
    const route = `POST /api/v1/cash/shifts/${SHIFT_ID}/close`;
    const explain = `POST /api/v1/cash/shifts/${SHIFT_ID}/closing-explanation`;
    const recorded = closedShift({
      closing: {
        expected_amount: null,
        counted_amount: "195.50",
        difference: null,
        explanation: null,
        explanation_required: true,
        user: { id: CASHIER.id, name: CASHIER.name },
        created_at: "2026-11-08T15:00:00",
      },
    });
    const afterClose = {
      ...HANDOVER_PENDING,
      previous_closing: { ...HANDOVER_PENDING.previous_closing!, explanation_required: true },
    };
    const { calls } = open(CASHIER, [jsonResponse(CASHIER_REGISTER), jsonResponse(afterClose), jsonResponse(HANDOVER_PENDING)], {
      [route]: [jsonResponse(recorded, 200)],
      [explain]: [
        jsonResponse({
          id: "ex1", cash_shift_id: SHIFT_ID, kind: "CLOSING", explanation: "Coins miscounted",
          user_id: CASHIER.id, created_at: "2026-11-08T15:01:00",
        }, 201),
      ],
    });
    const form = await screen.findByRole("form", { name: "Count and close the shift" });
    fireEvent.click(within(form).getByRole("radio", { name: /Close the register/ }));
    fireEvent.change(within(form).getByLabelText("Cash counted in the drawer"), { target: { value: "195.50" } });
    fireEvent.click(within(form).getByRole("button", { name: "Close shift" }));
    fireEvent.click(within(form).getByRole("button", { name: "Confirm close" }));

    const notice = await screen.findByText(/Shift closed\./);
    expect(notice.textContent).toContain("The count differs from what the register expected: explain it below.");
    expect(notice.textContent).not.toMatch(/Expected|difference [-−]?€/);

    // The count went out once; it is final and cannot be retried.
    expect(sent(calls, route).map((request) => request.body)).toEqual([{ counted_amount: "195.50", explanation: null }]);

    const explainForm = await screen.findByRole("form", { name: "Explain the closing count" });
    expect(explainForm.textContent).not.toMatch(/€/);
    fireEvent.click(within(explainForm).getByRole("button", { name: "Record explanation" }));
    expect(within(explainForm).getByText("Explain the difference.")).toBeTruthy();
    expect(sent(calls, explain)).toHaveLength(0);
    fireEvent.change(within(explainForm).getByLabelText("Explanation"), { target: { value: "Coins miscounted" } });
    fireEvent.click(within(explainForm).getByRole("button", { name: "Record explanation" }));

    expect(await screen.findByText("Explanation recorded.")).toBeTruthy();
    const [request] = sent(calls, explain);
    expect(request.body).toEqual({ explanation: "Coins miscounted" });
    expect(request.key).toBeTruthy();
    await vi.waitFor(() => expect(screen.queryByRole("form", { name: "Explain the closing count" })).toBeNull());
  });

  it("offers the explanation only to whoever counted it or a manager", async () => {
    const afterClose = {
      ...HANDOVER_PENDING,
      previous_closing: { ...HANDOVER_PENDING.previous_closing!, explanation_required: true },
    };
    open(CASHIER_TWO, [jsonResponse(afterClose)]);
    await screen.findByRole("form", { name: "Open a shift" });
    expect(screen.queryByRole("form", { name: "Explain the closing count" })).toBeNull();
  });

  it("shows a manager the expected cash and the difference the server returned", async () => {
    open(MANAGER, [jsonResponse(MANAGER_REGISTER)], {
      [`POST /api/v1/cash/shifts/${SHIFT_ID}/close`]: [
        jsonResponse(
          closedShift({
            expected_visible: true,
            closing: {
              expected_amount: "987.65",
              counted_amount: "985.00",
              difference: "-2.65",
              explanation: "Short",
              explanation_required: false,
              user: { id: MANAGER.id, name: MANAGER.name },
              created_at: "2026-11-08T15:00:00",
            },
          }),
        ),
      ],
    });
    expect((await screen.findAllByText(eur(98765))).length).toBeGreaterThan(0);
    const form = screen.getByRole("form", { name: "Count and close the shift" });
    fireEvent.click(within(form).getByRole("radio", { name: /Close the register/ }));
    fireEvent.change(within(form).getByLabelText("Cash counted in the drawer"), { target: { value: "985" } });
    fireEvent.change(within(form).getByLabelText("Explanation, if you know the count differs (optional)"), { target: { value: "Short" } });
    fireEvent.click(within(form).getByRole("button", { name: "Close shift" }));
    fireEvent.click(within(form).getByRole("button", { name: "Confirm close" }));

    const notice = await screen.findByText(/Shift closed\./);
    expect(notice.textContent).toContain(`Expected ${eur(98765)}, difference ${eur(-265)}.`);
  });
});

describe("cash in and out", () => {
  const ROUTE = `POST /api/v1/cash/shifts/${SHIFT_ID}/movements`;

  it("records cash in, then reports a refused cash out", async () => {
    const { calls } = open(CASHIER, [jsonResponse(CASHIER_REGISTER)], {
      [ROUTE]: [
        jsonResponse({
          id: "mv1",
          cash_shift_id: SHIFT_ID,
          type: "CASH_IN",
          amount: "50.00",
          reason: "Change float",
          payment_id: null,
          refund_id: null,
          user: { id: CASHIER.id, name: CASHIER.name },
          created_at: "2026-11-08T10:00:00",
        }, 201),
        errorResponse(409, "INSUFFICIENT_CASH", "The drawer does not hold that much cash"),
      ],
    });
    const form = await screen.findByRole("form", { name: "Cash in or out" });
    fireEvent.click(within(form).getByRole("button", { name: "Record cash in" }));
    expect(within(form).getByText("Enter an amount.")).toBeTruthy();
    expect(within(form).getByText("Give a reason.")).toBeTruthy();

    fireEvent.change(within(form).getByLabelText("Amount"), { target: { value: "50" } });
    fireEvent.change(within(form).getByLabelText("Reason"), { target: { value: "Change float" } });
    fireEvent.click(within(form).getByRole("button", { name: "Record cash in" }));
    expect(await screen.findByText(`Cash in of ${eur(5000)} recorded.`)).toBeTruthy();

    fireEvent.click(within(form).getByRole("radio", { name: "Cash out" }));
    fireEvent.change(within(form).getByLabelText("Amount"), { target: { value: "5000" } });
    fireEvent.change(within(form).getByLabelText("Reason"), { target: { value: "Supplier" } });
    fireEvent.click(within(form).getByRole("button", { name: "Record cash out" }));
    expect(await screen.findByText(/Could not record the cash movement: The drawer does not hold that much cash/)).toBeTruthy();

    expect(sent(calls, ROUTE).map((request) => request.body)).toEqual([
      { type: "CASH_IN", amount: "50.00", reason: "Change float" },
      { type: "CASH_OUT", amount: "5000.00", reason: "Supplier" },
    ]);
    // The success is kept as history; the refusal is not left behind.
    expect(openRecords()).toHaveLength(0);
  });

  it("rejects a zero amount", async () => {
    const { calls } = open(CASHIER, [jsonResponse(CASHIER_REGISTER)]);
    const form = await screen.findByRole("form", { name: "Cash in or out" });
    fireEvent.change(within(form).getByLabelText("Amount"), { target: { value: "0" } });
    fireEvent.change(within(form).getByLabelText("Reason"), { target: { value: "x" } });
    fireEvent.click(within(form).getByRole("button", { name: "Record cash in" }));
    expect(within(form).getByText("The amount must be greater than zero.")).toBeTruthy();
    expect(sent(calls, ROUTE)).toHaveLength(0);
  });

  it("lists the shift's movements", async () => {
    open(CASHIER, [
      jsonResponse({
        ...CASHIER_REGISTER,
        shift: shift({
          movements: [
            {
              id: "mv1",
              cash_shift_id: SHIFT_ID,
              type: "CASH_OUT",
              amount: "12.00",
              reason: "Bread delivery",
              payment_id: null,
              refund_id: null,
              user: { id: CASHIER.id, name: CASHIER.name },
              created_at: "2026-11-08T10:00:00",
            },
          ],
        }),
      }),
    ]);
    const table = await screen.findByRole("table");
    expect(within(table).getByText("Bread delivery")).toBeTruthy();
    expect(within(table).getByText(`−${eur(1200)}`)).toBeTruthy();
  });
});

describe("two-person handover", () => {
  it("does not let the outgoing cashier receive their own handover", async () => {
    open(CASHIER, [jsonResponse(HANDOVER_PENDING)]);
    expect(await screen.findByText(/You closed the previous shift\. Another employee must count the cash/)).toBeTruthy();
    expect(screen.queryByRole("form", { name: "Open a shift" })).toBeNull();
  });

  it("lets the incoming cashier count blind, records the count once, then takes the explanation", async () => {
    const handover = {
      id: "ho1",
      cash_register_id: "cr000000-0000-4000-8000-000000000001",
      business_day_id: BUSINESS_DAY.id,
      from_shift_id: "sh000000-0000-4000-8000-000000000001",
      to_shift_id: "sh000000-0000-4000-8000-000000000002",
      previous_expected_amount: null,
      handed_over_amount: "200.00",
      received_amount: "199.00",
      difference: "-1.00",
      reason: null,
      explanation_required: true,
      outgoing_user: { id: CASHIER.id, name: CASHIER.name },
      incoming_user: { id: CASHIER_TWO.id, name: CASHIER_TWO.name },
      self_confirmed: false,
      created_at: "2026-11-08T15:05:00",
    };
    const received = shift({
      id: "sh000000-0000-4000-8000-000000000002",
      opened_by: { id: CASHIER_TWO.id, name: CASHIER_TWO.name },
      handover,
    });
    const explained = shift({
      ...received,
      handover: { ...handover, reason: "One euro missing", explanation_required: false },
    });
    const registerWith = (s: ReturnType<typeof shift>) => ({
      ...HANDOVER_PENDING,
      shift: s,
      previous_closing: { ...HANDOVER_PENDING.previous_closing!, handover_pending: false },
    });
    const explain = `POST /api/v1/cash/shifts/${received.id}/handover-explanation`;
    const { calls } = open(CASHIER_TWO, [jsonResponse(HANDOVER_PENDING), jsonResponse(registerWith(received)), jsonResponse(registerWith(explained))], {
      "POST /api/v1/cash/shifts": [jsonResponse(received, 201)],
      [explain]: [
        jsonResponse({
          id: "ex2", cash_shift_id: received.id, kind: "HANDOVER", explanation: "One euro missing",
          user_id: CASHIER_TWO.id, created_at: "2026-11-08T15:06:00",
        }, 201),
      ],
    });
    const form = await screen.findByRole("form", { name: "Open a shift" });
    expect(within(form).getByText(/closed by Carla Cashier/)).toBeTruthy();
    // Blind: nothing tells the incoming cashier what was handed over.
    expect(form.textContent).not.toContain(eur(20000));

    fireEvent.change(within(form).getByLabelText("Cash counted in the drawer"), { target: { value: "199" } });
    fireEvent.click(within(form).getByRole("button", { name: "Open shift" }));
    expect(
      await screen.findByText("Shift opened. Handover from Carla Cashier recorded. Your count differs from what was handed over: explain it below."),
    ).toBeTruthy();
    // One count, recorded as entered.
    expect(sent(calls, "POST /api/v1/cash/shifts").map((request) => request.body)).toEqual([
      { opening_amount: "199.00", handover_reason: null },
    ]);

    const explainForm = await screen.findByRole("form", { name: "Explain the handover count" });
    fireEvent.change(within(explainForm).getByLabelText("Explanation"), { target: { value: "One euro missing" } });
    fireEvent.click(within(explainForm).getByRole("button", { name: "Record explanation" }));
    expect(await screen.findByText("Explanation recorded.")).toBeTruthy();
    expect(sent(calls, explain)[0].body).toEqual({ explanation: "One euro missing" });

    const card = await screen.findByRole("article", { name: "Handover into this shift" });
    await vi.waitFor(() => expect(within(card).getByText("One euro missing")).toBeTruthy());
    expect(within(card).getByText("Two people")).toBeTruthy();
  });

  it("lets a manager confirm their own handover, saying so", async () => {
    open(MANAGER, [jsonResponse({ ...HANDOVER_PENDING, previous_closing: { ...HANDOVER_PENDING.previous_closing!, closed_by: { id: MANAGER.id, name: MANAGER.name } } })]);
    const form = await screen.findByRole("form", { name: "Open a shift" });
    expect(within(form).getByText(/the handover will show you on both sides/)).toBeTruthy();
  });

  it("shows a server refusal of the same person on both sides", async () => {
    open(CASHIER_TWO, [jsonResponse(HANDOVER_PENDING)], {
      "POST /api/v1/cash/shifts": [errorResponse(409, "HANDOVER_SAME_USER", "You closed the previous shift; the cash must be received and counted by another employee")],
    });
    const form = await screen.findByRole("form", { name: "Open a shift" });
    fireEvent.change(within(form).getByLabelText("Cash counted in the drawer"), { target: { value: "200" } });
    fireEvent.click(within(form).getByRole("button", { name: "Open shift" }));
    expect(await screen.findByText(/Could not open the shift: You closed the previous shift/)).toBeTruthy();
  });
});

describe("submitting once", () => {
  it("sends a double-clicked confirmation once and disables the form while it runs", async () => {
    const response = deferred(jsonResponse(closedShift()));
    const route = `POST /api/v1/cash/shifts/${SHIFT_ID}/handover`;
    const { calls } = open(CASHIER, [jsonResponse(CASHIER_REGISTER)], { [route]: [response.handler] });
    const form = await screen.findByRole("form", { name: "Count and close the shift" });
    fireEvent.change(within(form).getByLabelText("Cash counted in the drawer"), { target: { value: "200" } });
    fireEvent.click(within(form).getByRole("button", { name: "Hand over shift" }));
    const confirm = within(form).getByRole("button", { name: "Confirm handover" });
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    await vi.waitFor(() => expect(within(form).getByRole("button", { name: "Closing…" })).toBeTruthy());
    expect((within(form).getByRole("button", { name: "Closing…" }) as HTMLButtonElement).disabled).toBe(true);
    response.release();
    expect(await screen.findByText(/Shift handed over\./)).toBeTruthy();
    expect(sent(calls, route)).toHaveLength(1);
  });

  it("keeps an unconfirmed outcome under unresolved operations instead of offering a second attempt", async () => {
    const route = `POST /api/v1/cash/shifts/${SHIFT_ID}/movements`;
    open(CASHIER, [jsonResponse(CASHIER_REGISTER)], {
      [route]: [errorResponse(503, "SERVICE_UNAVAILABLE", "Try again")],
    });
    const form = await screen.findByRole("form", { name: "Cash in or out" });
    fireEvent.change(within(form).getByLabelText("Amount"), { target: { value: "10" } });
    fireEvent.change(within(form).getByLabelText("Reason"), { target: { value: "Float" } });
    fireEvent.click(within(form).getByRole("button", { name: "Record cash in" }));
    expect(await screen.findByText(/did not confirm this.*retry it there, do not enter it again/)).toBeTruthy();
    expect(await screen.findByRole("region", { name: "Unresolved operations" })).toBeTruthy();
  });
});
