import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import { cashierMutations } from "../api/cashier";
import { observeServerDate, resetServerClock } from "../api/serverClock";
import { writeToken } from "../auth/tokenStorage";
import { CASHIER, CASHIER_REGISTER, CASHIER_TWO } from "../test/cashierFixtures";
import { errorResponse, fakeToken, jsonResponse, mockApi } from "../test/fixtures";
import { resetStoreForTests } from "../test/pendingOperationsTesting";
import { pendingOperations, RETENTION_MS, STORAGE_KEY } from "./pendingOperations";

// Sign-out on a shared till: the token is revoked on the server, and the
// next person finds none of the previous user's resolved cashier records;
// anything that may still need a retry or a check is kept.

const PAYMENT = cashierMutations.createPayment("b1", { amount: "12.50", method: "CASH" });
const MOVEMENT = cashierMutations.recordCashMovement("s1", { type: "CASH_OUT", amount: "5.00", reason: "Bread" });

beforeEach(() => {
  window.localStorage.clear();
  resetStoreForTests(pendingOperations);
  observeServerDate(new Date().toUTCString());
});

afterEach(() => {
  window.localStorage.clear();
  resetStoreForTests(pendingOperations);
  resetServerClock();
});

async function seedOperations() {
  // CASHIER: one confirmed, one refused before anything ran, one unknown.
  const confirmed = await pendingOperations.create(CASHIER, PAYMENT);
  await pendingOperations.beginSend(confirmed.id, CASHIER, null);
  await pendingOperations.recordSuccess(confirmed.id);

  const refused = await pendingOperations.create(CASHIER, MOVEMENT);
  await pendingOperations.beginSend(refused.id, CASHIER, null);
  await pendingOperations.recordFailure(refused.id, "failed", { status: 409, code: "INSUFFICIENT_CASH", message: "No", requestId: null });

  const unknown = await pendingOperations.create(CASHIER, { ...PAYMENT, path: "/api/v1/bills/b2/payments" });
  await pendingOperations.beginSend(unknown.id, CASHIER, null);
  await pendingOperations.recordFailure(unknown.id, "unknown", { status: 0, code: "NETWORK_ERROR", message: "lost", requestId: null });

  // Another employee's confirmed record.
  const others = await pendingOperations.create(CASHIER_TWO, PAYMENT);
  await pendingOperations.beginSend(others.id, CASHIER_TWO, null);
  await pendingOperations.recordSuccess(others.id);
  return { confirmed, refused, unknown, others };
}

describe("purgeResolvedOf", () => {
  it("removes only the user's resolved records and keeps everything still in doubt", async () => {
    const { confirmed, refused, unknown, others } = await seedOperations();

    await expect(pendingOperations.purgeResolvedOf(CASHIER)).resolves.toBe(2);

    const left = pendingOperations.getSnapshot().operations.map((op) => op.id).sort();
    expect(left).toEqual([others.id, unknown.id].sort());
    const persisted = JSON.parse(window.localStorage.getItem(STORAGE_KEY)!);
    expect(persisted.removed[confirmed.id].kind).toBe("purged");
    expect(persisted.removed[refused.id].kind).toBe("purged");
    // The unresolved payment keeps its body and key for a safe retry.
    const kept = pendingOperations.get(unknown.id)!;
    expect(kept.body).toEqual({ amount: "12.50", method: "CASH" });
    expect(kept.status).toBe("unknown");
  });

  it("keeps resolved history no longer than half a day", () => {
    expect(RETENTION_MS).toBe(12 * 60 * 60 * 1000);
  });
});

function signIn() {
  const token = fakeToken({ sub: CASHIER.id, exp: Math.floor(Date.now() / 1000) + 3600 });
  writeToken(token);
  return token;
}

describe("sign-out", () => {
  it("revokes the token on the server and clears the user's resolved records", async () => {
    const { unknown } = await seedOperations();
    const token = signIn();
    const { calls } = mockApi({
      "GET /api/v1/me": [jsonResponse(CASHIER, 200, { Date: new Date().toUTCString() })],
      "GET /api/v1/cash/register": [jsonResponse(CASHIER_REGISTER)],
      "POST /api/v1/auth/logout": [new Response(null, { status: 204 })],
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeTruthy();

    await vi.waitFor(() => expect(calls.some((call) => call.key === "POST /api/v1/auth/logout")).toBe(true));
    const logout = calls.find((call) => call.key === "POST /api/v1/auth/logout")!;
    expect((logout.init?.headers as Record<string, string>).Authorization).toBe(`Bearer ${token}`);
    await vi.waitFor(() =>
      expect(pendingOperations.getSnapshot().operations.filter((op) => op.userId === CASHIER.id).map((op) => op.id)).toEqual([
        unknown.id,
      ]),
    );
  });

  it("still signs out when the server cannot be reached", async () => {
    signIn();
    mockApi({
      "GET /api/v1/me": [jsonResponse(CASHIER)],
      "GET /api/v1/cash/register": [jsonResponse(CASHIER_REGISTER)],
      "POST /api/v1/auth/logout": [errorResponse(503, "SERVICE_UNAVAILABLE", "down")],
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeTruthy();
  });

  it("does not call the server when the session already ended (401)", async () => {
    signIn();
    const { calls } = mockApi({
      "GET /api/v1/me": [jsonResponse(CASHIER)],
      "GET /api/v1/cash/register": [errorResponse(401, "NOT_AUTHENTICATED", "Could not validate credentials")],
    });
    render(<App />);
    expect(await screen.findByText("Your session has expired. Please sign in again.")).toBeTruthy();
    expect(calls.some((call) => call.key === "POST /api/v1/auth/logout")).toBe(false);
  });
});

describe("sign-in throttling", () => {
  it("explains a 429 without saying whether the account exists", async () => {
    mockApi({
      "POST /api/v1/auth/login": [errorResponse(429, "TOO_MANY_LOGIN_ATTEMPTS", "Too many sign-in attempts. Try again later.")],
    });
    render(<App />);
    fireEvent.change(await screen.findByLabelText("Email"), { target: { value: "someone@example.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "whatever" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Too many sign-in attempts. Wait a few minutes, then try again.")).toBeTruthy();
  });
});
