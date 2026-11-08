import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import { cashierMutations } from "../api/cashier";
import type { CurrentUser } from "../api/types";
import { AuthProvider } from "../auth/AuthContext";
import { writeToken } from "../auth/tokenStorage";
import { formatCents } from "../lib/money";
import { CASHIER, CASHIER_REGISTER, EMPTY_REGISTER, MANAGER, MANAGER_REGISTER } from "../test/cashierFixtures";
import { errorResponse, fakeToken, jsonResponse, mockApi } from "../test/fixtures";
import { WAITER } from "../test/posFixtures";
import { SectionGuard } from "./CashierWorkspace";
import { PendingOperationsBanner } from "./PendingOperationsBanner";
import { PendingOperationStore, pendingOperations } from "./pendingOperations";
import { observeServerDate, resetServerClock } from "../api/serverClock";
import { resetStoreForTests } from "../test/pendingOperationsTesting";
import { REGISTER_POLL_MS } from "./useRegister";

const REGISTER = "GET /api/v1/cash/register";
const root = document.documentElement;

function signIn(user: CurrentUser, register: Response | Response[] = jsonResponse(CASHIER_REGISTER)) {
  writeToken(fakeToken({ sub: user.id, exp: Math.floor(Date.now() / 1000) + 3600 }));
  return mockApi({
    "GET /api/v1/me": [jsonResponse(user)],
    [REGISTER]: Array.isArray(register) ? register : [register],
  });
}

function registerCalls(calls: Array<{ key: string }>) {
  return calls.filter((call) => call.key === REGISTER).length;
}

function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
}

beforeEach(() => {
  window.localStorage.clear();
  resetStoreForTests(pendingOperations);
});

afterEach(() => {
  vi.useRealTimers();
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
  window.localStorage.clear();
  resetStoreForTests(pendingOperations);
  delete root.dataset.theme;
  delete root.dataset.workspace;
  resetServerClock();
});

describe("cashier workspace shell", () => {
  it("opens for a cashier with the employee, role and the sections they may use", async () => {
    signIn(CASHIER);
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Overview" })).toBeTruthy();
    const banner = document.querySelector<HTMLElement>(".cx-topbar")!;
    expect(within(banner).getByText("Carla Cashier")).toBeTruthy();
    expect(within(banner).getByText("CASHIER")).toBeTruthy();

    const nav = screen.getByRole("navigation", { name: "Cashier sections" });
    const labels = within(nav)
      .getAllByRole("button")
      .map((button) => button.textContent);
    expect(labels).toEqual(["Overview", "Orders", "Payments", "Cash Register", "Settings"]);
    expect(within(nav).getByRole("button", { name: "Overview" }).getAttribute("aria-current")).toBe("page");
    expect(root.dataset.workspace).toBe("cashier");
  });

  it("shows Reports to managers", async () => {
    signIn(MANAGER, jsonResponse(MANAGER_REGISTER));
    render(<App />);
    const nav = await screen.findByRole("navigation", { name: "Cashier sections" });
    expect(within(nav).getByRole("button", { name: "Reports" })).toBeTruthy();
  });

  it("navigates between sections and remembers the choice for the tab", async () => {
    signIn(CASHIER);
    render(<App />);
    const nav = await screen.findByRole("navigation", { name: "Cashier sections" });

    for (const [label, heading] of [
      ["Orders", "Orders"],
      ["Payments", "Payments"],
      ["Cash Register", "Cash Register"],
      ["Settings", "Settings"],
    ]) {
      fireEvent.click(within(nav).getByRole("button", { name: label }));
      expect(screen.getByRole("heading", { name: heading })).toBeTruthy();
      expect(within(nav).getByRole("button", { name: label }).getAttribute("aria-current")).toBe("page");
    }
    expect(window.sessionStorage.getItem("restaurantos.cashier.section")).toBe("settings");
  });

  it("ignores a remembered section the user may not open", async () => {
    window.sessionStorage.setItem("restaurantos.cashier.section", "reports");
    signIn(CASHIER);
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Overview" })).toBeTruthy();
  });

  it("guards sections even when asked to render one directly", () => {
    render(
      <SectionGuard user={CASHIER} section="reports">
        <p>secret report</p>
      </SectionGuard>,
    );
    expect(screen.getByRole("heading", { name: "No access" })).toBeTruthy();
    expect(screen.queryByText("secret report")).toBeNull();
  });

  it("signs out through the existing session flow", async () => {
    signIn(CASHIER);
    render(<App />);
    await screen.findByRole("heading", { name: "Overview" });

    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));

    expect(await screen.findByRole("button", { name: /sign in/i })).toBeTruthy();
    expect(window.sessionStorage.getItem("restaurantos.accessToken")).toBeNull();
    expect(root.dataset.workspace).toBeUndefined();
  });

  it("switches between Premium Dark and Light in Settings", async () => {
    root.dataset.theme = "dark";
    signIn(CASHIER);
    render(<App />);
    const nav = await screen.findByRole("navigation", { name: "Cashier sections" });
    fireEvent.click(within(nav).getByRole("button", { name: "Settings" }));

    const dark = screen.getByRole("radio", { name: /Premium Dark/ }) as HTMLInputElement;
    const light = screen.getByRole("radio", { name: /Light/ }) as HTMLInputElement;
    expect(dark.checked).toBe(true);

    fireEvent.click(light);
    expect(root.dataset.theme).toBe("light");
    expect(window.localStorage.getItem("restaurantos.theme")).toBe("light");
    expect((screen.getByRole("radio", { name: /Light/ }) as HTMLInputElement).checked).toBe(true);
  });

  it("runs pending-operation upkeep while signed in on any screen, and stops at sign-out", async () => {
    const stop = vi.fn();
    const start = vi.spyOn(pendingOperations, "startUpkeep").mockReturnValue(stop);
    writeToken(fakeToken({ sub: WAITER.id, exp: Math.floor(Date.now() / 1000) + 3600 }));
    mockApi({ "GET /api/v1/me": [jsonResponse(WAITER)], "GET /api/v1/tables": [jsonResponse([])] });
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Tables" })).toBeTruthy();
    expect(start).toHaveBeenCalled();
    expect(stop).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("button", { name: /sign in/i })).toBeTruthy();
    expect(stop).toHaveBeenCalled();
  });

  it("leaves the waiter POS out of the cashier theme", async () => {
    writeToken(fakeToken({ sub: WAITER.id, exp: Math.floor(Date.now() / 1000) + 3600 }));
    root.dataset.workspace = "cashier";
    mockApi({ "GET /api/v1/me": [jsonResponse(WAITER)], "GET /api/v1/tables": [jsonResponse([])] });
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Tables" })).toBeTruthy();
    expect(root.dataset.workspace).toBeUndefined();
  });
});

describe("overview", () => {
  it("shows the register, business day, shift and previous closing", async () => {
    signIn(CASHIER);
    render(<App />);
    await screen.findByRole("heading", { name: "Overview" });

    const shift = screen.getByRole("article", { name: "Current shift" });
    expect(within(shift).getByText("You")).toBeTruthy();
    expect(within(shift).getByText(formatCents(15000, "EUR"))).toBeTruthy();
    expect(screen.getByRole("article", { name: "Business day" }).textContent).toContain("Open");
    expect(screen.getByRole("article", { name: "Register" }).textContent).toContain("In use");
    expect(screen.getByRole("article", { name: "Previous closing" }).textContent).toContain("Night Cashier");
    expect(screen.getByRole("article", { name: "Signed in" }).textContent).toContain("Carla Cashier");
  });

  it("explains an empty register", async () => {
    signIn(CASHIER, jsonResponse(EMPTY_REGISTER));
    render(<App />);
    expect(await screen.findByText("No business day is open.")).toBeTruthy();
    expect(screen.getByText("No cash shift is open.")).toBeTruthy();
    expect(screen.queryByRole("article", { name: "Previous closing" })).toBeNull();
  });

  it("never shows expected cash to a cashier, even if a response carried it", async () => {
    // A misbehaving response or cache: the screen checks the permission too.
    signIn(CASHIER, jsonResponse(MANAGER_REGISTER));
    render(<App />);
    await screen.findByRole("heading", { name: "Overview" });

    expect(screen.queryByText("Expected cash")).toBeNull();
    expect(screen.queryByText("Expected")).toBeNull();
    expect(screen.queryByText(formatCents(98765, "EUR"))).toBeNull();
    expect(screen.queryByText(formatCents(43210, "EUR"))).toBeNull();
    expect(screen.getByText(/expected cash is shown to managers only/)).toBeTruthy();
  });

  it("shows expected cash to a manager", async () => {
    signIn(MANAGER, jsonResponse(MANAGER_REGISTER));
    render(<App />);
    await screen.findByRole("heading", { name: "Overview" });

    const shift = screen.getByRole("article", { name: "Current shift" });
    expect(within(shift).getByText("Expected cash")).toBeTruthy();
    expect(within(shift).getByText(formatCents(98765, "EUR"))).toBeTruthy();
    const previous = screen.getByRole("article", { name: "Previous closing" });
    expect(within(previous).getByText(formatCents(43210, "EUR"))).toBeTruthy();
    expect(within(previous).getByText(formatCents(43000, "EUR"))).toBeTruthy();
  });

  it("handles a 403 without crashing", async () => {
    signIn(CASHIER, errorResponse(403, "FORBIDDEN", "Not allowed"));
    render(<App />);
    expect(await screen.findByRole("heading", { name: "No access" })).toBeTruthy();
    // The rest of the workspace still works.
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    expect(screen.getByRole("heading", { name: "Settings" })).toBeTruthy();
  });

  it("signs out on a 401", async () => {
    signIn(CASHIER, errorResponse(401, "UNAUTHORIZED", "Expired"));
    render(<App />);
    expect(await screen.findByText(/session has expired/i)).toBeTruthy();
  });

  it("keeps the last state when a refresh fails", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    signIn(CASHIER, [jsonResponse(CASHIER_REGISTER), errorResponse(503, "HTTP_503", "Unavailable")]);
    render(<App />);
    await screen.findByRole("heading", { name: "Overview" });

    await act(() => vi.advanceTimersByTimeAsync(REGISTER_POLL_MS));

    expect(await screen.findByText(/Could not refresh/)).toBeTruthy();
    expect(screen.getByRole("article", { name: "Current shift" })).toBeTruthy();
  });
});

describe("overview polling", () => {
  it("polls every 15 seconds while visible and stops while hidden", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { calls } = signIn(CASHIER);
    render(<App />);
    await screen.findByRole("heading", { name: "Overview" });
    expect(registerCalls(calls)).toBe(1);

    await act(() => vi.advanceTimersByTimeAsync(REGISTER_POLL_MS - 100));
    expect(registerCalls(calls)).toBe(1);
    await act(() => vi.advanceTimersByTimeAsync(200));
    expect(registerCalls(calls)).toBe(2);

    setVisibility("hidden");
    await act(() => vi.advanceTimersByTimeAsync(REGISTER_POLL_MS * 4));
    expect(registerCalls(calls)).toBe(2);

    // Refreshes at once when shown again, then keeps the interval.
    setVisibility("visible");
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(registerCalls(calls)).toBe(3);
    await act(() => vi.advanceTimersByTimeAsync(REGISTER_POLL_MS));
    expect(registerCalls(calls)).toBe(4);
  });

  it("stops polling when another section is shown", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { calls } = signIn(CASHIER);
    render(<App />);
    await screen.findByRole("heading", { name: "Overview" });

    fireEvent.click(screen.getByRole("button", { name: "Orders" }));
    await act(() => vi.advanceTimersByTimeAsync(REGISTER_POLL_MS * 3));
    expect(registerCalls(calls)).toBe(1);
  });

  it("stops polling after a 403", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { calls } = signIn(CASHIER, errorResponse(403, "FORBIDDEN", "Not allowed"));
    render(<App />);
    await screen.findByRole("heading", { name: "No access" });
    await act(() => vi.advanceTimersByTimeAsync(REGISTER_POLL_MS * 3));
    expect(registerCalls(calls)).toBe(1);
  });
});

describe("unresolved operations warning", () => {
  const PAYMENT = cashierMutations.createPayment("b1", { amount: "5.00", method: "CASH" });
  const PAYMENT_ROUTE = "POST /api/v1/bills/b1/payments";

  /** A payment whose first attempt timed out, stored as another page load would have left it. */
  async function storeUnknown(user: CurrentUser) {
    // The page has seen the server's time (as sign-in always provides).
    observeServerDate(new Date().toUTCString());
    const op = await pendingOperations.create(user, PAYMENT);
    await pendingOperations.beginSend(op.id, user, null);
    await pendingOperations.recordFailure(op.id, "unknown", {
      status: 0,
      code: "NETWORK_ERROR",
      message: "Cannot reach the server for 5.00.",
      requestId: null,
    });
    return op;
  }

  function signInWith(routes: Record<string, Response[]>) {
    writeToken(fakeToken({ sub: CASHIER.id, exp: Math.floor(Date.now() / 1000) + 3600 }));
    return mockApi({
      "GET /api/v1/me": [jsonResponse(CASHIER, 200, { Date: new Date().toUTCString() })],
      [REGISTER]: [jsonResponse(CASHIER_REGISTER)],
      ...routes,
    });
  }

  const region = () => screen.findByRole("region", { name: "Unresolved operations" });

  it("is hidden when nothing is pending", async () => {
    signIn(CASHIER);
    render(<App />);
    await screen.findByRole("heading", { name: "Overview" });
    expect(screen.queryByRole("region", { name: "Unresolved operations" })).toBeNull();
  });

  it("warns about an unknown outcome without offering to dismiss it as not sent", async () => {
    await storeUnknown(CASHIER);
    signIn(CASHIER);
    render(<App />);

    const banner = await region();
    expect(banner.textContent).toContain("1 of your operations has an unknown outcome");
    expect(within(banner).getByText(/Outcome unknown — not checked/)).toBeTruthy();
    expect(banner.textContent).not.toMatch(/Not sent/);
    expect(within(banner).queryByRole("button", { name: "Dismiss" })).toBeNull();
    expect(within(banner).getByRole("button", { name: "Retry" })).toBeTruthy();
  });

  it("retries with the stored key and body and shows the server's confirmation", async () => {
    const op = await storeUnknown(CASHIER);
    const { calls } = signInWith({ [PAYMENT_ROUTE]: [jsonResponse({ id: "p1" }, 201)] });
    render(<App />);

    fireEvent.click(within(await region()).getByRole("button", { name: "Retry" }));

    await vi.waitFor(() => expect(screen.getByText(/Recent on this device/)).toBeTruthy());
    expect(screen.getByText(/Confirmed by the server/)).toBeTruthy();
    expect(screen.queryByText(/unknown outcome/)).toBeNull();
    const payment = calls.find((call) => call.key === PAYMENT_ROUTE)!;
    expect((payment.init?.headers as Record<string, string>)["Idempotency-Key"]).toBe(op.idempotencyKey);
    expect(payment.init?.body).toBe(JSON.stringify({ amount: "5.00", method: "CASH" }));
  });

  it("keeps the outcome unknown when a retry is refused by a permission check", async () => {
    await storeUnknown(CASHIER);
    signInWith({ [PAYMENT_ROUTE]: [errorResponse(403, "PERMISSION_DENIED", "Missing permission")] });
    render(<App />);

    fireEvent.click(within(await region()).getByRole("button", { name: "Retry" }));

    await vi.waitFor(() => expect(screen.getByText(/Last answer: Missing permission/)).toBeTruthy());
    const banner = await region();
    expect(within(banner).getByText(/Outcome unknown — not checked/)).toBeTruthy();
    expect(within(banner).queryByRole("button", { name: "Dismiss" })).toBeNull();
  });

  it("explains when a retry is blocked because the server's time cannot be obtained", async () => {
    await storeUnknown(CASHIER);
    // /me answers without a Date header: no trustworthy server time.
    writeToken(fakeToken({ sub: CASHIER.id, exp: Math.floor(Date.now() / 1000) + 3600 }));
    const { calls } = mockApi({
      "GET /api/v1/me": [new Response(JSON.stringify(CASHIER), { status: 200, headers: { "Content-Type": "application/json" } })],
      [REGISTER]: [jsonResponse(CASHIER_REGISTER)],
    });
    render(<App />);

    fireEvent.click(within(await region()).getByRole("button", { name: "Retry" }));

    await vi.waitFor(() => expect(screen.getByText(/current time could not be confirmed/)).toBeTruthy());
    expect(within(await region()).getByText(/Outcome unknown — not checked/)).toBeTruthy();
    expect(calls.some((call) => call.key.startsWith("POST "))).toBe(false);
  });

  it("never offers the owner a check of their own operation", async () => {
    await storeUnknown(CASHIER);
    signIn(CASHIER);
    render(<App />);
    const banner = await region();
    expect(within(banner).queryByRole("button", { name: /Record a check/ })).toBeNull();
    expect(banner.textContent).toContain("Only a manager can record a check of this operation.");
  });

  it("shows Retry with an explanation when the page has no server time yet", async () => {
    await storeUnknown(CASHIER);
    resetServerClock(); // as after a reload, before any response
    writeToken(fakeToken({ sub: CASHIER.id, exp: Math.floor(Date.now() / 1000) + 3600 }));
    // /me (sign-in) answers without a Date header: still no server time.
    mockApi({
      "GET /api/v1/me": [new Response(JSON.stringify(CASHIER), { status: 200, headers: { "Content-Type": "application/json" } })],
      [REGISTER]: [new Response(JSON.stringify(CASHIER_REGISTER), { status: 200, headers: { "Content-Type": "application/json" } })],
    });
    render(<App />);
    const banner = await region();
    expect(banner.textContent).toMatch(/must not be sent again until it can be/);
    expect(within(banner).getByRole("button", { name: "Retry" })).toBeTruthy();
  });

  it("shows another employee's operation without amounts or server messages, and no retry", async () => {
    await storeUnknown(MANAGER);
    signIn(CASHIER);
    render(<App />);

    const banner = await region();
    expect(banner.textContent).toContain("1 started by other employees on this device");
    expect(banner.textContent).toContain("started by Max Manager");
    expect(banner.textContent).not.toContain("5.00");
    expect(banner.textContent).not.toContain("Cannot reach the server");
    expect(within(banner).queryByRole("button", { name: "Retry" })).toBeNull();
    expect(within(banner).queryByRole("button", { name: /Record a check/ })).toBeNull();
  });

  it("lets a manager record a check of another employee's operation, kept as a record", async () => {
    const op = await storeUnknown(CASHIER);
    writeToken(fakeToken({ sub: MANAGER.id, exp: Math.floor(Date.now() / 1000) + 3600 }));
    mockApi({ "GET /api/v1/me": [jsonResponse(MANAGER)], [REGISTER]: [jsonResponse(MANAGER_REGISTER)] });
    render(<App />);

    const banner = await region();
    fireEvent.click(within(banner).getByRole("button", { name: /Record a check/ }));
    expect(within(banner).getByText(/does not confirm, reverse or repeat/)).toBeTruthy();
    fireEvent.change(within(banner).getByLabelText(/What did you check/), { target: { value: "On the bill" } });
    fireEvent.click(within(banner).getByRole("button", { name: "Record check" }));

    await vi.waitFor(() => expect(pendingOperations.get(op.id)?.status).toBe("checked"));
    expect(pendingOperations.get(op.id)?.check).toMatchObject({ checkedByUserId: MANAGER.id, note: "On the bill" });
  });

  it("tells the owner a check was recorded, not that the server confirmed it", async () => {
    const op = await storeUnknown(CASHIER);
    await pendingOperations.markChecked(op.id, MANAGER, { note: "Seen on the bill" });
    signIn(CASHIER);
    render(<App />);

    const banner = await region();
    fireEvent.click(within(banner).getByText(/Recent on this device/));
    expect(banner.textContent).toContain("Checked by Max Manager");
    expect(banner.textContent).toContain("outcome not confirmed by the server");
    expect(banner.textContent).not.toContain("Confirmed by the server");
    expect(within(banner).queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("keeps the operation through sign-out and back", async () => {
    await storeUnknown(CASHIER);
    signIn(CASHIER);
    const { unmount } = render(<App />);
    await region();
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    unmount();

    signIn(CASHIER);
    render(<App />);
    expect(await region()).toBeTruthy();
  });

  it("warns when operations cannot be stored on this device", () => {
    const original = Object.getOwnPropertyDescriptor(window, "localStorage")!;
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      get() {
        throw new DOMException("blocked", "SecurityError");
      },
    });
    try {
      resetStoreForTests(pendingOperations);
      render(
        <AuthProvider>
          <PendingOperationsBanner store={pendingOperations} user={CASHIER} token="t" />
        </AuthProvider>,
      );
      expect(screen.getByRole("alert").textContent).toContain("cannot store pending operations");
    } finally {
      Object.defineProperty(window, "localStorage", original);
    }
  });

  it("keeps warning while an operation exists only in memory", async () => {
    const full = {
      getItem: () => null,
      setItem: () => {
        throw new DOMException("full", "QuotaExceededError");
      },
    } as unknown as Storage;
    const store = new PendingOperationStore({ storage: () => full, locks: null, serverNow: () => null });
    await store.create(CASHIER, PAYMENT);
    render(
      <AuthProvider>
        <PendingOperationsBanner store={store} user={CASHIER} token="t" />
      </AuthProvider>,
    );
    expect(screen.getByRole("alert").textContent).toContain("exist only in this tab");
  });
});
