import { describe, expect, it } from "vitest";

import { errorResponse, jsonResponse, mockApi } from "../test/fixtures";
import {
  cashierMutations,
  getBill,
  getBusinessDayReport,
  getRegister,
  getShift,
  listBusinessDays,
  listCashierOrders,
  listHandovers,
  MISSING_IDEMPOTENCY_KEY,
  type MutationRequest,
  sendMutation,
  splitBillEqually,
} from "./cashier";
import { ApiError } from "./client";

const TOKEN = "token-1";

function headersOf(init: RequestInit | undefined): Record<string, string> {
  return (init?.headers ?? {}) as Record<string, string>;
}

describe("cashier reads", () => {
  it("call the backend paths with the token", async () => {
    const { calls } = mockApi({
      "GET /api/v1/cash/register": [jsonResponse({})],
      "GET /api/v1/cash/shifts/s%2F1": [jsonResponse({})],
      "GET /api/v1/cash/handovers?limit=10": [jsonResponse([])],
      "GET /api/v1/cash/handovers": [jsonResponse([])],
      "GET /api/v1/business-days?limit=7": [jsonResponse([])],
      "GET /api/v1/business-days/d1/report": [jsonResponse({})],
      "GET /api/v1/cashier/orders?view=payable": [jsonResponse([])],
      "GET /api/v1/cashier/orders?view=settled": [jsonResponse([])],
      "GET /api/v1/bills/b1": [jsonResponse({})],
      "GET /api/v1/bills/b1/split?parts=3": [jsonResponse({})],
    });

    await getRegister(TOKEN);
    await getShift(TOKEN, "s/1");
    await listHandovers(TOKEN, 10);
    await listHandovers(TOKEN);
    await listBusinessDays(TOKEN, 7);
    await getBusinessDayReport(TOKEN, "d1");
    await listCashierOrders(TOKEN);
    await listCashierOrders(TOKEN, "settled");
    await getBill(TOKEN, "b1");
    await splitBillEqually(TOKEN, "b1", 3);

    expect(calls).toHaveLength(10);
    for (const call of calls) {
      expect(headersOf(call.init).Authorization).toBe(`Bearer ${TOKEN}`);
      expect(headersOf(call.init)["Idempotency-Key"]).toBeUndefined();
    }
  });

  it("raise ApiError for a 403 like every other request", async () => {
    mockApi({ "GET /api/v1/cash/register": [errorResponse(403, "FORBIDDEN", "Not allowed")] });
    const error = await getRegister(TOKEN).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).isForbidden).toBe(true);
  });
});

describe("cashier mutations", () => {
  const cases = [
    [cashierMutations.openBusinessDay({ business_date: "2026-11-08" }), "/api/v1/business-days", { business_date: "2026-11-08" }],
    [cashierMutations.closeBusinessDay("d1"), "/api/v1/business-days/d1/close", undefined],
    [cashierMutations.openShift({ opening_amount: "150.00" }), "/api/v1/cash/shifts", { opening_amount: "150.00" }],
    [cashierMutations.closeShift("s1", { counted_amount: "10.00" }), "/api/v1/cash/shifts/s1/close", { counted_amount: "10.00" }],
    [cashierMutations.handOverShift("s1", { counted_amount: "10.00" }), "/api/v1/cash/shifts/s1/handover", { counted_amount: "10.00" }],
    [
      cashierMutations.correctOpeningAmount("s1", { opening_amount: "20.00", reason: "typo" }),
      "/api/v1/cash/shifts/s1/opening-corrections",
      { opening_amount: "20.00", reason: "typo" },
    ],
    [
      cashierMutations.recordCashMovement("s1", { type: "CASH_OUT", amount: "5.00", reason: "change" }),
      "/api/v1/cash/shifts/s1/movements",
      { type: "CASH_OUT", amount: "5.00", reason: "change" },
    ],
    [cashierMutations.createBill("o1"), "/api/v1/bills/order/o1", undefined],
    [cashierMutations.cancelBilledItem("b1", "i1", "wrong"), "/api/v1/bills/b1/items/i1/cancel", { reason: "wrong" }],
    [cashierMutations.voidOrder("o1", "left"), "/api/v1/orders/o1/void", { reason: "left" }],
    [
      cashierMutations.createPayment("b1", { amount: "12.50", method: "CASH" }),
      "/api/v1/bills/b1/payments",
      { amount: "12.50", method: "CASH" },
    ],
    [
      cashierMutations.refundPayment("p1", { amount: "2.00", reason: "cold" }),
      "/api/v1/payments/p1/refunds",
      { amount: "2.00", reason: "cold" },
    ],
  ] as const;

  it.each(cases)("%# sends POST %s with the Idempotency-Key", async (request, path, body) => {
    const { calls } = mockApi({ [`POST ${path}`]: [jsonResponse({ ok: true })] });

    await expect(sendMutation<unknown>(TOKEN, request as MutationRequest, "key-123")).resolves.toEqual({ ok: true });

    expect(calls).toHaveLength(1);
    const headers = headersOf(calls[0].init);
    expect(headers["Idempotency-Key"]).toBe("key-123");
    expect(headers.Authorization).toBe(`Bearer ${TOKEN}`);
    expect(calls[0].init?.body).toBe(body === undefined ? undefined : JSON.stringify(body));
  });

  it("marks the routes the backend refuses without a key", () => {
    const required = Object.values({
      openShift: cashierMutations.openShift({ opening_amount: "1" }),
      closeShift: cashierMutations.closeShift("s", { counted_amount: "1" }),
      handOverShift: cashierMutations.handOverShift("s", { counted_amount: "1" }),
      correct: cashierMutations.correctOpeningAmount("s", { opening_amount: "1", reason: "r" }),
      movement: cashierMutations.recordCashMovement("s", { type: "CASH_IN", amount: "1", reason: "r" }),
      payment: cashierMutations.createPayment("b", { amount: "1", method: "CARD" }),
      refund: cashierMutations.refundPayment("p", { reason: "r" }),
    });
    expect(required.every((request) => request.requiresIdempotencyKey)).toBe(true);
    expect(cashierMutations.createBill("o").requiresIdempotencyKey).toBe(false);
  });

  it("never sends a mutation without a key", async () => {
    const { fetchMock } = mockApi({});
    await expect(sendMutation(TOKEN, cashierMutations.createBill("o1"), "")).rejects.toThrow(MISSING_IDEMPOTENCY_KEY);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("passes server errors through as ApiError", async () => {
    mockApi({ "POST /api/v1/cash/shifts": [errorResponse(409, "SHIFT_ALREADY_OPEN", "A shift is open.")] });
    const error = await sendMutation(TOKEN, cashierMutations.openShift({ opening_amount: "1" }), "k").catch(
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe("SHIFT_ALREADY_OPEN");
    expect((error as ApiError).requestId).toBe("req-123");
  });
});
