import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { cashierMutations } from "../api/cashier";
import { ApiError } from "../api/client";
import { CASHIER, MANAGER } from "../test/cashierFixtures";
import { FakeLockManager } from "../test/fakeLocks";
import { errorResponse, jsonResponse, mockApi } from "../test/fixtures";
import { observeServerDate, resetServerClock } from "../api/serverClock";
import {
  classifyFailure,
  isSensitiveFieldName,
  mergeRecord,
  CLOCK_TOLERANCE_MS,
  SERVER_TIME_MARGIN_MS,
  executeOperation,
  hashBody,
  OperationBlockedError,
  operationsOf,
  PendingOperationStore,
  RETENTION_MS,
  RETRY_WINDOW_MS,
  STALE_SENDING_MS,
  startOperation,
  STORAGE_KEY,
  LEGACY_STORAGE_KEY,
  looksLikeCardNumber,
  cardCandidates,
  tombstoneApplies,
  type PendingOperation,
  type Tombstone,
} from "./pendingOperations";

const TOKEN = "token-1";
const BODY = { amount: "12.50", method: "CASH", tip_amount: "1.00" };
const PAYMENT = cashierMutations.createPayment("b1", { amount: "12.50", method: "CASH", tip_amount: "1.00" });
const PAYMENT_PATH = "POST /api/v1/bills/b1/payments";
const T0 = new Date("2026-11-08T10:00:00Z").getTime();

interface StoredV2 {
  version: number;
  operations: PendingOperation[];
  removed: Record<string, string>;
  invalid: unknown[];
}

function stored(): StoredV2 {
  return JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? '{"version":2,"operations":[],"removed":{},"invalid":[]}');
}

interface TabOptions {
  now?: () => number;
  locks?: FakeLockManager | null;
  tabId?: string;
  // Estimated server time (creation anchor); defaults to the tab's clock.
  serverNow?: () => number | null;
  // Fresh server time before a retry; defaults to the tab's clock.
  fetchServerTime?: () => Promise<number | null>;
  channelName?: string | null;
}

/** A store simulating one browser tab, with its own clock (server time = that clock unless given). */
function tab(options: TabOptions = {}) {
  const clock = () => (options.now ? options.now() : Date.now());
  return new PendingOperationStore({
    now: () => new Date(clock()),
    locks: options.locks ?? null,
    serverNow: options.serverNow ?? clock,
    fetchServerTime: options.fetchServerTime ?? (async () => clock()),
    tabId: options.tabId,
    // Each test tab gets no broadcast unless asked (keeps tests independent).
    channelName: options.channelName ?? null,
  });
}

const NO_SERVER_TIME = { serverNow: () => null, fetchServerTime: async () => null };

function keyOf(init: RequestInit | undefined): string {
  return (init?.headers as Record<string, string>)["Idempotency-Key"];
}

/** A request that hangs until released. */
function hangingRoute() {
  let release: (response: Response) => void = () => {};
  const handler = () => new Promise<Response>((resolve) => (release = resolve));
  return { handler, release: (response: Response) => release(response) };
}

const timeoutResponse = (init: RequestInit | undefined) =>
  new Promise<Response>((_, reject) => {
    init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  });

/** First attempt times out; the operation is then "unknown". */
async function timedOutPayment(store: PendingOperationStore) {
  await startOperation(store, PAYMENT, { user: CASHIER, token: TOKEN, timeoutMs: 5 }).catch(() => undefined);
  const [op] = store.getSnapshot().operations;
  expect(op.status).toBe("unknown");
  return op;
}

beforeEach(() => window.localStorage.clear());
afterEach(() => window.localStorage.clear());

describe("creating an operation", () => {
  it("stores every field before anything is sent", async () => {
    const { fetchMock } = mockApi({});
    const store = tab({ now: () => T0 });

    const op = await store.create(CASHIER, PAYMENT);

    expect(fetchMock).not.toHaveBeenCalled();
    expect(op).toMatchObject({
      userId: CASHIER.id,
      userName: CASHIER.name,
      restaurantId: CASHIER.restaurant_id,
      type: "payment.create",
      method: "POST",
      path: "/api/v1/bills/b1/payments",
      body: BODY,
      bodyHash: hashBody(BODY),
      createdAt: "2026-11-08T10:00:00.000Z",
      status: "pending",
      attempts: 0,
      mayHaveCommitted: false,
    });
    expect(op.idempotencyKey).toBeTruthy();
    expect(op.idempotencyKey).not.toBe(op.id);
    expect(stored().version).toBe(2);
    expect(stored().operations).toEqual([op]);
  });

  it("never stores the token, credential-like fields or card numbers", async () => {
    const store = tab();
    await store.create(CASHIER, PAYMENT);
    expect(window.localStorage.getItem(STORAGE_KEY)).not.toContain(TOKEN);
    expect(window.localStorage.getItem(STORAGE_KEY)).not.toMatch(/Bearer|access_token/);

    const withCard = { ...PAYMENT, body: { amount: "1.00", method: "CARD", card_number: "4111111111111111" } };
    await expect(store.create(CASHIER, withCard)).rejects.toThrow(/sensitive/);
    const nested = { ...PAYMENT, body: { amount: "1.00", meta: [{ cvv: "123" }] } };
    await expect(store.create(CASHIER, nested)).rejects.toThrow(/sensitive/);
    const typedIn = cashierMutations.refundPayment("p1", { reason: "card 4111 1111 1111 1111 refused" });
    await expect(store.create(CASHIER, typedIn)).rejects.toThrow(/card number/);
    // An ordinary reference with digits is fine.
    await store.create(CASHIER, cashierMutations.refundPayment("p1", { reason: "receipt 000123" }));
    expect(stored().operations).toHaveLength(2);
  });

  it("survives a reload (a new store over the same storage)", async () => {
    const op = await tab().create(CASHIER, PAYMENT);
    const reloaded = tab();
    expect(reloaded.getSnapshot().operations).toEqual([op]);
    expect(reloaded.getSnapshot().persistent).toBe(true);
  });
});

describe("sending", () => {
  it("records an authoritative success and drops the body", async () => {
    const { calls } = mockApi({ [PAYMENT_PATH]: [jsonResponse({ id: "p1" }, 201)] });
    const store = tab();

    await expect(startOperation(store, PAYMENT, { user: CASHIER, token: TOKEN })).resolves.toEqual({ id: "p1" });

    const [op] = store.getSnapshot().operations;
    expect(op.status).toBe("succeeded");
    expect(op.resolvedAt).toBeTruthy();
    expect(op.body).toBeUndefined();
    expect(stored().operations[0].body).toBeUndefined();
    expect(store.retryBlock(op, CASHIER)).toBe("resolved");
    expect(keyOf(calls[0].init)).toBe(op.idempotencyKey);
  });

  it("persists the operation as sending, by this tab, while the request is in flight", async () => {
    let seen: PendingOperation[] = [];
    mockApi({
      [PAYMENT_PATH]: [
        () => {
          seen = stored().operations;
          return jsonResponse({ id: "p1" }, 201);
        },
      ],
    });
    const store = tab({ tabId: "tab-a" });
    await startOperation(store, PAYMENT, { user: CASHIER, token: TOKEN });
    expect(seen).toEqual([
      expect.objectContaining({ status: "sending", attempts: 1, sendingBy: expect.objectContaining({ tabId: "tab-a" }) }),
    ]);
  });

  it("keeps a network failure unknown and retries with the same key and body after a reload", async () => {
    const bodies: string[] = [];
    const keys: string[] = [];
    let fail = true;
    mockApi({
      [PAYMENT_PATH]: [
        (init) => {
          bodies.push(String(init?.body));
          keys.push(keyOf(init));
          if (fail) throw new TypeError("Failed to fetch");
          return jsonResponse({ id: "p1" }, 201);
        },
      ],
    });
    const store = tab();

    await expect(startOperation(store, PAYMENT, { user: CASHIER, token: TOKEN })).rejects.toBeInstanceOf(ApiError);
    const [op] = store.getSnapshot().operations;
    expect(op).toMatchObject({ status: "unknown", mayHaveCommitted: true });
    expect(op.lastError?.code).toBe("NETWORK_ERROR");

    fail = false;
    const reloaded = tab();
    await expect(executeOperation(reloaded, op.id, { user: CASHIER, token: "a-new-token" })).resolves.toEqual({
      id: "p1",
    });

    expect(keys).toEqual([op.idempotencyKey, op.idempotencyKey]);
    expect(bodies[1]).toBe(bodies[0]);
    expect(bodies[0]).toBe(JSON.stringify(BODY));
    expect(reloaded.get(op.id)?.status).toBe("succeeded");
  });

  it("times out conservatively", async () => {
    mockApi({ [PAYMENT_PATH]: [timeoutResponse] });
    await timedOutPayment(tab());
  });

  it.each([
    [500, "INTERNAL_ERROR", "unknown"],
    [502, "HTTP_502", "unknown"],
    [504, "HTTP_504", "unknown"],
    [422, "IDEMPOTENCY_KEY_REUSED", "unknown"],
    [401, "UNAUTHORIZED", "pending"],
    [409, "BILL_ALREADY_PAID", "failed"],
    [403, "FORBIDDEN", "failed"],
    [422, "VALIDATION_ERROR", "failed"],
  ] as const)("a first attempt answered %i %s leaves the operation %s", async (status, code, outcome) => {
    mockApi({ [PAYMENT_PATH]: [errorResponse(status, code, "refused")] });
    const store = tab();
    await expect(startOperation(store, PAYMENT, { user: CASHIER, token: TOKEN })).rejects.toBeInstanceOf(ApiError);
    const [op] = store.getSnapshot().operations;
    expect(op.status).toBe(outcome);
    expect(op.mayHaveCommitted).toBe(outcome === "unknown");
    expect(op.lastError).toMatchObject({ status, code });
  });

  it("classifies unreadable success responses and unexpected errors as unknown", () => {
    expect(classifyFailure(new ApiError({ status: 200, code: "INVALID_RESPONSE", message: "x" }))).toBe("unknown");
    expect(classifyFailure(new Error("boom"))).toBe("unknown");
  });
});

describe("P1-1: uncertainty is permanent", () => {
  it.each([
    [401, "UNAUTHORIZED"],
    [403, "PERMISSION_DENIED"],
    [500, "INTERNAL_ERROR"],
    [409, "NO_OPEN_CASH_SHIFT"],
    [422, "VALIDATION_ERROR"],
  ] as const)("a timed-out payment retried with a %i %s stays unknown", async (status, code) => {
    const keys: string[] = [];
    const bodies: string[] = [];
    mockApi({
      [PAYMENT_PATH]: [
        (init) => {
          keys.push(keyOf(init));
          bodies.push(String(init?.body));
          return timeoutResponse(init);
        },
        (init) => {
          keys.push(keyOf(init));
          bodies.push(String(init?.body));
          return errorResponse(status, code, "refused");
        },
      ],
    });
    const store = tab();
    const op = await timedOutPayment(store);

    await expect(executeOperation(store, op.id, { user: CASHIER, token: TOKEN })).rejects.toBeInstanceOf(ApiError);

    const after = store.get(op.id)!;
    expect(after.status).toBe("unknown");
    expect(after.mayHaveCommitted).toBe(true);
    expect(after.lastError).toMatchObject({ status, code });
    expect(after.body).toEqual(BODY);
    expect(store.canDismiss(after, CASHIER)).toBe(false);
    await expect(store.dismiss(op.id, CASHIER)).rejects.toThrow(/may have been completed/);
    expect(keys).toEqual([op.idempotencyKey, op.idempotencyKey]);
    expect(bodies[1]).toBe(bodies[0]);
  });

  it("is resolved by a later successful retry", async () => {
    mockApi({ [PAYMENT_PATH]: [timeoutResponse, errorResponse(401, "UNAUTHORIZED", "expired"), jsonResponse({ id: "p1" }, 201)] });
    const store = tab();
    const op = await timedOutPayment(store);
    await executeOperation(store, op.id, { user: CASHIER, token: TOKEN }).catch(() => undefined);
    expect(store.get(op.id)?.status).toBe("unknown");

    await expect(executeOperation(store, op.id, { user: CASHIER, token: TOKEN })).resolves.toEqual({ id: "p1" });
    expect(store.get(op.id)).toMatchObject({ status: "succeeded", attempts: 3 });
  });

  it("an operation abandoned mid-request becomes unknown and keeps that", async () => {
    let now = T0;
    const store = tab({ now: () => now, tabId: "gone" });
    const op = await store.create(CASHIER, PAYMENT);
    await store.beginSend(op.id, CASHIER, null);

    now += STALE_SENDING_MS + 1;
    const reloaded = tab({ now: () => now, tabId: "new" });
    const recovered = reloaded.get(op.id)!;
    expect(reloaded.effectiveStatus(recovered)).toBe("unknown");
    expect(reloaded.canDismiss(recovered, CASHIER)).toBe(false);

    mockApi({ [PAYMENT_PATH]: [errorResponse(401, "UNAUTHORIZED", "expired")] });
    await executeOperation(reloaded, op.id, { user: CASHIER, token: TOKEN }).catch(() => undefined);
    expect(reloaded.get(op.id)).toMatchObject({ status: "unknown", mayHaveCommitted: true });
  });

  it("a merge between tabs never drops uncertainty", async () => {
    const store = tab();
    const op = await store.create(CASHIER, PAYMENT);
    // Tab B recorded a refusal at a higher revision without knowing that
    // tab A had seen a timeout.
    const refused = { ...op, status: "failed", mayHaveCommitted: false, revision: 9 };
    const doubtful = { ...op, status: "unknown", mayHaveCommitted: true, revision: 3 };
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ version: 2, operations: [refused, doubtful], removed: {}, invalid: [] }),
    );
    store.reconcile();
    expect(store.get(op.id)).toMatchObject({ status: "unknown", mayHaveCommitted: true });
  });
});

describe("P1-2: two tabs never lose each other's operations", () => {
  it("keeps operations created by two tabs with stale copies", async () => {
    const tabA = tab({ tabId: "a" });
    const tabB = tab({ tabId: "b" }); // loaded before A wrote; no storage event arrives
    const a = await tabA.create(CASHIER, PAYMENT);
    const b = await tabB.create(CASHIER, PAYMENT);

    expect(stored().operations.map((op) => op.id).sort()).toEqual([a.id, b.id].sort());
    tabA.reconcile();
    expect(tabA.getSnapshot().operations).toHaveLength(2);
  });

  it("does not erase another tab's update", async () => {
    mockApi({ [PAYMENT_PATH]: [errorResponse(503, "HTTP_503", "down")] });
    const tabA = tab({ tabId: "a" });
    const tabB = tab({ tabId: "b" });
    const a = await tabA.create(CASHIER, PAYMENT);
    tabB.reconcile();
    const staleCopyInB = tabB.get(a.id)!;
    expect(staleCopyInB.status).toBe("pending");

    await executeOperation(tabA, a.id, { user: CASHIER, token: TOKEN }).catch(() => undefined);
    // B writes something else from its stale memory.
    await tabB.create(CASHIER, PAYMENT);

    const inStorage = stored().operations.find((op) => op.id === a.id)!;
    expect(inStorage).toMatchObject({ status: "unknown", mayHaveCommitted: true });
    expect(tabB.get(a.id)?.status).toBe("unknown");
  });

  it("does not bring back an operation another tab dismissed", async () => {
    mockApi({ [PAYMENT_PATH]: [errorResponse(409, "BILL_ALREADY_PAID", "paid")] });
    const tabA = tab({ tabId: "a" });
    const op = await tabA.create(CASHIER, PAYMENT);
    const tabB = tab({ tabId: "b" });
    await executeOperation(tabA, op.id, { user: CASHIER, token: TOKEN }).catch(() => undefined);
    await tabA.dismiss(op.id, CASHIER);

    await tabB.create(CASHIER, PAYMENT);
    expect(stored().operations.find((entry) => entry.id === op.id)).toBeUndefined();
    expect(tabB.get(op.id)).toBeUndefined();
  });

  it("merges on storage events and on subscribe instead of replacing memory", async () => {
    const failing = { ok: false };
    const storage = {
      getItem: (key: string) => window.localStorage.getItem(key),
      setItem: (key: string, value: string) => {
        if (!failing.ok) throw new DOMException("full", "QuotaExceededError");
        window.localStorage.setItem(key, value);
      },
    } as unknown as Storage;
    const tabA = new PendingOperationStore({ storage: () => storage, locks: null, ...NO_SERVER_TIME });
    const memoryOnly = await tabA.create(CASHIER, PAYMENT);
    // Another tab writes meanwhile.
    const tabB = tab({ tabId: "b" });
    const fromB = await tabB.create(MANAGER, PAYMENT);

    window.dispatchEvent(new StorageEvent("storage", { key: STORAGE_KEY }));
    const unsubscribe = tabA.subscribe(() => undefined);
    window.dispatchEvent(new StorageEvent("storage", { key: STORAGE_KEY }));

    const ids = tabA.getSnapshot().operations.map((op) => op.id);
    expect(ids).toContain(memoryOnly.id);
    expect(ids).toContain(fromB.id);
    unsubscribe();
  });

  it("serializes writes through the Web Locks API when available", async () => {
    const locks = new FakeLockManager();
    const store = tab({ locks });
    await store.create(CASHIER, PAYMENT);
    expect(locks.requested).toContain("restaurantos.cashier.pendingOperations");
  });

  it("works without Web Locks (in-tab queue)", async () => {
    const store = tab({ locks: null });
    await Promise.all([store.create(CASHIER, PAYMENT), store.create(CASHIER, PAYMENT), store.create(CASHIER, PAYMENT)]);
    expect(stored().operations).toHaveLength(3);
  });
});

describe("P2-1: live requests versus abandoned ones", () => {
  it("does not treat another tab's live request as abandoned", async () => {
    const route = hangingRoute();
    mockApi({ [PAYMENT_PATH]: [route.handler] });
    let nowB = Date.now();
    const tabA = tab({ tabId: "a" });
    const tabB = tab({ tabId: "b", now: () => nowB });
    const op = await tabA.create(CASHIER, PAYMENT);
    const sending = executeOperation(tabA, op.id, { user: CASHIER, token: TOKEN });
    await new Promise((resolve) => setTimeout(resolve, 0));

    tabB.reconcile();
    const seen = tabB.get(op.id)!;
    expect(tabB.effectiveStatus(seen)).toBe("sending");
    expect(tabB.retryBlock(seen, CASHIER)).toBe("in_flight");
    await expect(tabB.markChecked(op.id, MANAGER, {})).rejects.toThrow(/still being sent/);
    // A write from tab B leaves the live record alone.
    nowB += STALE_SENDING_MS - 1_000;
    await tabB.create(CASHIER, PAYMENT);
    expect(stored().operations.find((entry) => entry.id === op.id)?.status).toBe("sending");

    route.release(jsonResponse({ id: "p1" }, 201));
    await sending;
    expect(stored().operations.find((entry) => entry.id === op.id)?.status).toBe("succeeded");
  });

  it("keeps a request live while its heartbeat is fresh", async () => {
    let now = T0;
    const tabA = tab({ tabId: "a", now: () => now });
    const tabB = tab({ tabId: "b", now: () => now });
    const op = await tabA.create(CASHIER, PAYMENT);
    await tabA.beginSend(op.id, CASHIER, null);

    now += STALE_SENDING_MS - 1_000;
    await tabA.heartbeat(op.id);
    now += STALE_SENDING_MS - 1_000;
    tabB.reconcile();
    expect(tabB.effectiveStatus(tabB.get(op.id)!)).toBe("sending");

    now += 2_000;
    tabB.reconcile();
    expect(tabB.effectiveStatus(tabB.get(op.id)!)).toBe("unknown");
  });

  it("lets another tab recover an abandoned request with the same key", async () => {
    let now = T0;
    const tabA = tab({ tabId: "a", now: () => now });
    const op = await tabA.create(CASHIER, PAYMENT);
    await tabA.beginSend(op.id, CASHIER, null); // then the tab is closed

    const { calls } = mockApi({ [PAYMENT_PATH]: [jsonResponse({ id: "p1" }, 201)] });
    now += STALE_SENDING_MS + 1;
    const tabB = tab({ tabId: "b", now: () => now });
    await expect(executeOperation(tabB, op.id, { user: CASHIER, token: TOKEN })).resolves.toEqual({ id: "p1" });
    expect(keyOf(calls[0].init)).toBe(op.idempotencyKey);
    expect(tabB.get(op.id)?.status).toBe("succeeded");
  });

  it("never sends one operation from two tabs at once when Web Locks exist", async () => {
    const locks = new FakeLockManager();
    const route = hangingRoute();
    const { fetchMock } = mockApi({ [PAYMENT_PATH]: [route.handler] });
    let nowB = Date.now();
    const tabA = tab({ tabId: "a", locks });
    // Tab B's clock claims the heartbeat is stale; the lock still says no.
    const tabB = tab({ tabId: "b", locks, now: () => nowB });
    const op = await tabA.create(CASHIER, PAYMENT);
    const sending = executeOperation(tabA, op.id, { user: CASHIER, token: TOKEN });
    await new Promise((resolve) => setTimeout(resolve, 0));
    nowB += STALE_SENDING_MS * 2;

    const error = await executeOperation(tabB, op.id, { user: CASHIER, token: TOKEN }).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(OperationBlockedError);
    expect((error as OperationBlockedError).reason).toBe("in_flight");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    route.release(jsonResponse({ id: "p1" }, 201));
    await sending;
  });

  it("does not let a late answer from the first tab disturb a takeover", async () => {
    let now = T0;
    const tabA = tab({ tabId: "a", now: () => now });
    const op = await tabA.create(CASHIER, PAYMENT);
    await tabA.beginSend(op.id, CASHIER, null);
    now += STALE_SENDING_MS + 1;
    const tabB = tab({ tabId: "b", now: () => now });
    // Taking over a request in doubt needs a fresh server time.
    await expect(tabB.beginSend(op.id, CASHIER, null)).rejects.toMatchObject({ reason: "no_server_time" });
    await tabB.beginSend(op.id, CASHIER, now);

    await tabA.recordFailure(op.id, "failed", { status: 409, code: "X", message: "late", requestId: null });
    expect(stored().operations[0]).toMatchObject({
      status: "sending",
      sendingBy: expect.objectContaining({ tabId: "b" }),
      mayHaveCommitted: true,
    });
  });
});

describe("P2-2: retry deadline", () => {
  async function withCreatedAt(createdAt: string) {
    const store = tab({ now: () => T0 });
    const op = await store.create(CASHIER, PAYMENT);
    const data = stored();
    data.operations[0].createdAt = createdAt;
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
    const reloaded = tab({ now: () => T0 });
    return { reloaded, op: reloaded.get(op.id)! };
  }

  it("never retries a record with an unreadable timestamp, but keeps it", async () => {
    const { fetchMock } = mockApi({});
    const { reloaded, op } = await withCreatedAt("garbage");
    expect(op).toBeDefined();
    expect(reloaded.retryBlock(op, CASHIER)).toBe("unverifiable");
    await expect(executeOperation(reloaded, op.id, { user: CASHIER, token: TOKEN })).rejects.toMatchObject({
      reason: "unverifiable",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("allows a retry up to the safe window and not one millisecond later (server time, margins included)", async () => {
    mockApi({ [PAYMENT_PATH]: [timeoutResponse] });
    const store = tab({ now: () => T0, serverNow: () => T0 });
    const op = await timedOutPayment(store);
    // Anchor = T0 − 5 s; a fresh time counts as up to 5 s later.
    expect(store.get(op.id)?.serverCreatedAt).toBe(T0 - SERVER_TIME_MARGIN_MS);
    const lastAllowed = T0 + RETRY_WINDOW_MS - 2 * SERVER_TIME_MARGIN_MS;
    expect(store.retryBlock(store.get(op.id)!, CASHIER, lastAllowed)).toBeNull();
    expect(store.retryBlock(store.get(op.id)!, CASHIER, lastAllowed + 1)).toBe("expired");
  });

  it("uses the device clock only for an operation never in doubt without server time, and only to block", async () => {
    let now = T0;
    const store = tab({ now: () => now, ...NO_SERVER_TIME });
    const op = await store.create(CASHIER, PAYMENT);
    now = T0 - CLOCK_TOLERANCE_MS + 1;
    expect(store.retryBlock(op, CASHIER)).toBeNull();
    now = T0 - CLOCK_TOLERANCE_MS - 1;
    expect(store.retryBlock(op, CASHIER)).toBe("expired");
    now = T0 + RETRY_WINDOW_MS + 1;
    expect(store.retryBlock(op, CASHIER)).toBe("expired");
    // Not permanent: the device clock never establishes expiry.
    await store.create(CASHIER, PAYMENT);
    expect(store.get(op.id)?.retryExpired).toBe(false);
  });

});

describe("P2-3: recorded checks", () => {
  it("keeps the record with who checked, when, a note and the previous outcome", async () => {
    mockApi({ [PAYMENT_PATH]: [errorResponse(504, "HTTP_504", "Gateway timeout")] });
    const store = tab({ now: () => T0 });
    await startOperation(store, PAYMENT, { user: CASHIER, token: TOKEN }).catch(() => undefined);
    const [op] = store.getSnapshot().operations;

    await store.markChecked(op.id, MANAGER, { note: "  Payment is on the bill  " });

    const checked = store.get(op.id)!;
    expect(checked).toMatchObject({
      id: op.id,
      userId: CASHIER.id,
      status: "checked",
      mayHaveCommitted: true,
      check: {
        checkedByUserId: MANAGER.id,
        checkedByName: MANAGER.name,
        checkedAt: "2026-11-08T10:00:00.000Z",
        note: "Payment is on the bill",
        previousStatus: "unknown",
        previousError: { status: 504, code: "HTTP_504", message: "Gateway timeout", requestId: "req-123" },
      },
    });
    expect(checked.body).toBeUndefined();
    expect(store.retryBlock(checked, CASHIER)).toBe("checked");
    expect(operationsOf(store.getSnapshot(), CASHIER).history.map((entry) => entry.id)).toEqual([op.id]);
  });

  it("allows a check only of an unknown outcome, by a manager of the same restaurant", async () => {
    mockApi({ [PAYMENT_PATH]: [errorResponse(504, "HTTP_504", "down")] });
    const store = tab();
    await startOperation(store, PAYMENT, { user: CASHIER, token: TOKEN }).catch(() => undefined);
    const [op] = store.getSnapshot().operations;
    // Another cashier (no manager permission) may not; the store checks it.
    const colleague = { ...CASHIER, id: "c0000000-0000-4000-8000-0000000000c2", name: "Cody Cashier" };
    await expect(store.markChecked(op.id, colleague)).rejects.toThrow(/Only a manager/);
    await expect(store.markChecked(op.id, { ...MANAGER, restaurant_id: "another" })).rejects.toThrow(/another restaurant/);

    const notSent = await store.create(CASHIER, PAYMENT);
    await expect(store.markChecked(notSent.id, MANAGER)).rejects.toThrow(/unknown outcome/);
    expect(store.get(op.id)?.status).toBe("unknown");
  });
});

describe("P2-4: shared-device exposure and retention", () => {
  it("keeps the body only while a retry is possible", async () => {
    mockApi({ [PAYMENT_PATH]: [errorResponse(409, "BILL_ALREADY_PAID", "paid"), errorResponse(503, "HTTP_503", "down")] });
    const store = tab();
    await startOperation(store, PAYMENT, { user: CASHIER, token: TOKEN }).catch(() => undefined);
    await startOperation(store, PAYMENT, { user: CASHIER, token: TOKEN }).catch(() => undefined);
    const [failed, unknown] = stored().operations;
    expect(failed.status).toBe("failed");
    expect(failed.body).toBeUndefined();
    expect(unknown.status).toBe("unknown");
    expect(unknown.body).toEqual(BODY);
  });

  it("purges finished records after the retention period, never unresolved ones", async () => {
    mockApi({ [PAYMENT_PATH]: [jsonResponse({ id: "p1" }, 201), errorResponse(503, "HTTP_503", "down")] });
    let now = T0;
    const store = tab({ now: () => now });
    await startOperation(store, PAYMENT, { user: CASHIER, token: TOKEN });
    await startOperation(store, PAYMENT, { user: CASHIER, token: TOKEN }).catch(() => undefined);

    now += RETENTION_MS + 1;
    await store.create(CASHIER, PAYMENT); // any write purges
    const statuses = stored().operations.map((op) => op.status).sort();
    expect(statuses).toEqual(["pending", "unknown"]);
  });

  it("splits by owner and restaurant, and sign-out keeps unresolved operations", async () => {
    const store = tab();
    const mine = await store.create(CASHIER, PAYMENT);
    const theirs = await store.create(MANAGER, PAYMENT);
    const elsewhere = await store.create({ ...MANAGER, restaurant_id: "another" }, PAYMENT);
    window.sessionStorage.clear(); // sign-out clears only the session token

    const { own, others } = operationsOf(tab().getSnapshot(), CASHIER);
    expect(own.map((op) => op.id)).toEqual([mine.id]);
    expect(others.map((op) => op.id)).toEqual([theirs.id]);
    expect([...own, ...others].map((op) => op.id)).not.toContain(elsewhere.id);
  });

  it("never sends another employee's or another restaurant's operation", async () => {
    const { fetchMock } = mockApi({});
    const store = tab();
    const op = await store.create(CASHIER, PAYMENT);
    const error = await executeOperation(store, op.id, { user: MANAGER, token: TOKEN }).catch((caught: unknown) => caught);
    expect((error as OperationBlockedError).reason).toBe("other_user");
    expect(store.retryBlock(op, { id: CASHIER.id, restaurant_id: "another" })).toBe("other_user");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("P2-5: memory fallback", () => {
  function flakyStorage() {
    const state = { writable: false };
    const storage = {
      getItem: (key: string) => window.localStorage.getItem(key),
      setItem: (key: string, value: string) => {
        if (!state.writable) throw new DOMException("full", "QuotaExceededError");
        window.localStorage.setItem(key, value);
      },
    } as unknown as Storage;
    return { storage, state };
  }

  it("keeps memory-only operations and claims persistence only after a real write", async () => {
    const { storage, state } = flakyStorage();
    const store = new PendingOperationStore({ storage: () => storage, locks: null, ...NO_SERVER_TIME });
    const op = await store.create(CASHIER, PAYMENT);
    expect(store.getSnapshot()).toMatchObject({ persistent: false, unsaved: true, storageAvailable: true });

    // Storage becomes writable, but nothing was written yet.
    state.writable = true;
    store.reconcile();
    expect(store.get(op.id)).toBeDefined();
    expect(store.getSnapshot().persistent).toBe(false);

    await store.create(CASHIER, PAYMENT);
    expect(store.getSnapshot().persistent).toBe(true);
    expect(stored().operations.map((entry) => entry.id)).toContain(op.id);
  });

  it("works without storage at all and says so", async () => {
    const store = new PendingOperationStore({ storage: () => null, locks: null, ...NO_SERVER_TIME });
    expect(store.getSnapshot()).toMatchObject({ persistent: false, storageAvailable: false });
    const op = await store.create(CASHIER, PAYMENT);
    store.reconcile();
    expect(store.get(op.id)).toBeDefined();
  });
});

describe("unreadable storage", () => {
  it("sets unreadable data aside once and keeps working", async () => {
    window.localStorage.setItem(STORAGE_KEY, "{not json");
    tab();
    tab();
    const copies = Object.keys(window.localStorage).filter((key) => key.includes(".corrupt."));
    expect(copies).toHaveLength(1);
    expect(window.localStorage.getItem(copies[0])).toBe("{not json");

    const store = tab();
    expect(store.getSnapshot().recoveredCorruptData).toBe(true);
    await store.create(CASHIER, PAYMENT);
    expect(stored().operations).toHaveLength(1);
  });

  it("never overwrites unreadable data it could not copy", async () => {
    window.localStorage.setItem(STORAGE_KEY, "{not json");
    const storage = {
      getItem: (key: string) => window.localStorage.getItem(key),
      setItem: () => {
        throw new DOMException("full", "QuotaExceededError");
      },
    } as unknown as Storage;
    const store = new PendingOperationStore({ storage: () => storage, locks: null, ...NO_SERVER_TIME });
    const op = await store.create(CASHIER, PAYMENT);
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe("{not json");
    expect(store.get(op.id)).toBeDefined();
    expect(store.getSnapshot().persistent).toBe(false);
  });

  it("keeps entries it cannot read through later writes", async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([{ id: "half a record" }]));
    const store = tab();
    await store.create(CASHIER, PAYMENT);
    expect(stored().invalid).toEqual([{ id: "half a record" }]);
    expect(store.getSnapshot().recoveredCorruptData).toBe(true);
  });

  it("reads version 1 records conservatively", () => {
    const v1 = {
      id: "op-1",
      userId: CASHIER.id,
      userName: CASHIER.name,
      restaurantId: CASHIER.restaurant_id,
      type: "payment.create",
      method: "POST",
      path: "/api/v1/bills/b1/payments",
      body: BODY,
      bodyHash: hashBody(BODY),
      idempotencyKey: "k",
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
      status: "unknown",
      attempts: 1,
      lastError: null,
    };
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([v1]));
    expect(tab().get("op-1")).toMatchObject({ status: "unknown", mayHaveCommitted: true, revision: 0 });
  });
});

describe("integrity", () => {
  it("refuses to send a stored body that does not match its hash", async () => {
    const { fetchMock } = mockApi({});
    const store = tab();
    const op = await store.create(CASHIER, PAYMENT);
    const data = stored();
    data.operations[0].body = { amount: "999.00", method: "CASH" };
    data.operations[0].revision = 99;
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
    store.reconcile();

    await expect(executeOperation(store, op.id, { user: CASHIER, token: TOKEN })).rejects.toMatchObject({
      reason: "integrity",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not send an operation twice at once in one tab", async () => {
    const route = hangingRoute();
    mockApi({ [PAYMENT_PATH]: [route.handler] });
    const store = tab();
    const op = await store.create(CASHIER, PAYMENT);
    const first = executeOperation(store, op.id, { user: CASHIER, token: TOKEN });
    await new Promise((resolve) => setTimeout(resolve, 0));
    await expect(executeOperation(store, op.id, { user: CASHIER, token: TOKEN })).rejects.toMatchObject({
      reason: "in_flight",
    });
    route.release(jsonResponse({ id: "p1" }, 201));
    await first;
  });

  it("dismisses only what was never in doubt, and only for its owner", async () => {
    mockApi({ [PAYMENT_PATH]: [errorResponse(409, "BILL_ALREADY_PAID", "paid")] });
    const store = tab();
    await startOperation(store, PAYMENT, { user: CASHIER, token: TOKEN }).catch(() => undefined);
    const [failed] = store.getSnapshot().operations;
    await expect(store.dismiss(failed.id, MANAGER)).rejects.toThrow(/Only the employee/);
    await store.dismiss(failed.id, CASHIER);
    expect(store.get(failed.id)).toBeUndefined();
    expect(stored().removed[failed.id]).toBeTruthy();
  });
});

function writeStored(operations: PendingOperation[], removed: Record<string, Tombstone | Omit<Tombstone, "kind"> | string> = {}) {
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ version: 2, operations, removed, invalid: [] }));
}

describe("P2-A: tombstones never delete operations in doubt", () => {
  it("keeps an unknown operation merged with a tombstone, and drops the tombstone", async () => {
    mockApi({ [PAYMENT_PATH]: [timeoutResponse] });
    const store = tab();
    const op = await timedOutPayment(store);
    const current = store.get(op.id)!;
    // A racing tab removed it at the same revision.
    writeStored([], { [op.id]: { at: new Date().toISOString(), revision: current.revision } });

    store.reconcile();
    expect(store.get(op.id)).toMatchObject({ status: "unknown", mayHaveCommitted: true });
    await store.create(CASHIER, PAYMENT); // the next write settles storage
    expect(stored().operations.find((entry) => entry.id === op.id)?.status).toBe("unknown");
    expect(stored().removed[op.id]).toBeUndefined();
  });

  it("keeps a newer copy than the tombstone, even one never in doubt", async () => {
    const store = tab();
    const op = await store.create(CASHIER, PAYMENT);
    const newer = { ...store.get(op.id)!, revision: 5 };
    writeStored([newer], { [op.id]: { at: new Date().toISOString(), revision: 4 } });
    expect(tab().get(op.id)).toBeDefined();
  });

  it("keeps a newer unknown copy merged with an older tombstone", async () => {
    const store = tab();
    const op = await store.create(CASHIER, PAYMENT);
    const unknown = { ...store.get(op.id)!, status: "unknown" as const, mayHaveCommitted: true, revision: 7 };
    writeStored([unknown], { [op.id]: { at: new Date().toISOString(), revision: 2 } });
    expect(tab().get(op.id)).toMatchObject({ status: "unknown", revision: 7 });
  });

  it("does not let a stale tab bring back a dismissed never-sent operation", async () => {
    const tabA = tab({ tabId: "a" });
    const op = await tabA.create(CASHIER, PAYMENT);
    const tabB = tab({ tabId: "b" }); // holds the same revision in memory
    expect(tabB.get(op.id)?.revision).toBe(op.revision);
    await tabA.dismiss(op.id, CASHIER);
    expect(stored().removed[op.id]).toEqual({ at: expect.any(String), revision: op.revision, kind: "dismissed" });

    await tabB.create(CASHIER, PAYMENT);
    tabB.reconcile();
    expect(tabB.get(op.id)).toBeUndefined();
    expect(stored().operations.find((entry) => entry.id === op.id)).toBeUndefined();
  });

  it("keeps a memory-only unresolved operation against a conflicting tombstone", async () => {
    const state = { writable: false };
    const storage = {
      getItem: (key: string) => window.localStorage.getItem(key),
      setItem: (key: string, value: string) => {
        if (!state.writable) throw new DOMException("full", "QuotaExceededError");
        window.localStorage.setItem(key, value);
      },
    } as unknown as Storage;
    mockApi({ [PAYMENT_PATH]: [timeoutResponse] });
    const store = new PendingOperationStore({
      storage: () => storage,
      locks: null,
      serverNow: () => Date.now(),
      fetchServerTime: async () => Date.now(),
    });
    const op = await timedOutPayment(store);
    expect(store.getSnapshot().unsaved).toBe(true);
    // Another tab wrote a tombstone for it, even with a higher revision.
    writeStored([], { [op.id]: { at: new Date().toISOString(), revision: 999 } });

    store.reconcile();
    expect(store.get(op.id)?.status).toBe("unknown");
    state.writable = true;
    await store.create(CASHIER, PAYMENT);
    expect(stored().operations.find((entry) => entry.id === op.id)?.status).toBe("unknown");
  });

  it("never lets an old-format tombstone delete an operation in doubt", async () => {
    mockApi({ [PAYMENT_PATH]: [timeoutResponse] });
    const store = tab();
    const op = await timedOutPayment(store);
    writeStored([store.get(op.id)!], { [op.id]: new Date().toISOString() });
    expect(tab().get(op.id)?.status).toBe("unknown");
  });

  it("keeps uncertainty across tabs: a stale tab cannot dismiss what another tab put in doubt", async () => {
    mockApi({ [PAYMENT_PATH]: [errorResponse(503, "HTTP_503", "down")] });
    const tabA = tab({ tabId: "a" });
    const op = await tabA.create(CASHIER, PAYMENT);
    const tabB = tab({ tabId: "b" });
    expect(tabB.canDismiss(tabB.get(op.id)!, CASHIER)).toBe(true); // B's stale view

    await executeOperation(tabA, op.id, { user: CASHIER, token: TOKEN }).catch(() => undefined);
    await expect(tabB.dismiss(op.id, CASHIER)).rejects.toThrow(/may have been completed/);
    expect(stored().operations.find((entry) => entry.id === op.id)).toMatchObject({ status: "unknown", mayHaveCommitted: true });
    expect(stored().removed[op.id]).toBeUndefined();
  });

  it("still purges checked records after the retention period", async () => {
    mockApi({ [PAYMENT_PATH]: [timeoutResponse] });
    let now = Date.now();
    const store = tab({ now: () => now });
    const op = await timedOutPayment(store);
    await store.markChecked(op.id, MANAGER);
    now += RETENTION_MS + 1;
    await store.create(CASHIER, PAYMENT);
    expect(store.get(op.id)).toBeUndefined();
    expect(stored().removed[op.id]).toBeDefined();
  });
});

describe("P2-B: retries need a trustworthy server time", () => {
  const HOUR = 3600_000;

  async function unknownPaymentAt(options: TabOptions) {
    const route = { calls: 0 };
    mockApi({
      [PAYMENT_PATH]: [
        (init) => {
          route.calls += 1;
          return timeoutResponse(init);
        },
        () => {
          route.calls += 1;
          return jsonResponse({ id: "p1" }, 201);
        },
      ],
    });
    const store = tab(options);
    await startOperation(store, PAYMENT, { user: CASHIER, token: TOKEN, timeoutMs: 5 }).catch(() => undefined);
    const [op] = store.getSnapshot().operations;
    expect(op.status).toBe("unknown");
    return { store, op, route };
  }

  it("cannot retry an operation in doubt that was created without any server time", async () => {
    const fetchServerTime = vi.fn(async () => Date.now());
    const { store, op, route } = await unknownPaymentAt({ serverNow: () => null, fetchServerTime });
    expect(op.serverCreatedAt).toBeNull();

    await expect(executeOperation(store, op.id, { user: CASHIER, token: TOKEN })).rejects.toMatchObject({
      reason: "unverifiable",
    });
    expect(route.calls).toBe(1);
    expect(store.get(op.id)?.status).toBe("unknown");
  });

  it("blocks a retry when the device clock was moved back 6 h and the server says the window passed", async () => {
    let device = T0;
    let server = T0;
    const { store, op, route } = await unknownPaymentAt({
      now: () => device,
      serverNow: () => server,
      fetchServerTime: async () => server,
    });
    // 25 h later in reality; the device clock was set back by 6 h.
    server = T0 + 25 * HOUR;
    device = T0 + 19 * HOUR;
    expect(store.retryBlock(store.get(op.id)!, CASHIER, null)).toBe("no_server_time");

    await expect(executeOperation(store, op.id, { user: CASHIER, token: TOKEN })).rejects.toMatchObject({
      reason: "expired",
    });
    expect(route.calls).toBe(1);
  });

  it("blocks a retry once real elapsed time exceeds the safe window, and the block sticks", async () => {
    let device = T0;
    let server = T0;
    const { store, op } = await unknownPaymentAt({ now: () => device, serverNow: () => server, fetchServerTime: async () => server });
    server = T0 + RETRY_WINDOW_MS + 1;
    await expect(executeOperation(store, op.id, { user: CASHIER, token: TOKEN })).rejects.toMatchObject({ reason: "expired" });

    // The next write notices the passed window by the estimate: the body goes for good.
    await store.create(CASHIER, PAYMENT);
    expect(store.get(op.id)).toMatchObject({ retryExpired: true });
    expect(store.get(op.id)?.body).toBeUndefined();
    // Even if every clock now claims less time has passed, it stays expired.
    server = T0 + HOUR;
    device = T0 + HOUR;
    expect(store.retryBlock(store.get(op.id)!, CASHIER, server)).toBe("expired");
  });

  it("allows a retry inside the window when a fresh server time is obtained first", async () => {
    let server = T0;
    const order: string[] = [];
    const fetchServerTime = vi.fn(async () => {
      order.push("server-time");
      return server;
    });
    const { store, op } = await unknownPaymentAt({ now: () => T0, serverNow: () => T0, fetchServerTime });
    server = T0 + RETRY_WINDOW_MS - 10_000; // within, margin included
    const fetchCalls = (globalThis.fetch as unknown as { mock: { calls: unknown[] } }).mock.calls.length;

    await expect(executeOperation(store, op.id, { user: CASHIER, token: TOKEN })).resolves.toEqual({ id: "p1" });
    expect(fetchServerTime).toHaveBeenCalledTimes(1);
    expect((globalThis.fetch as unknown as { mock: { calls: unknown[] } }).mock.calls.length).toBe(fetchCalls + 1);
    expect(order).toEqual(["server-time"]);
  });

  it("blocks the retry when no server time can be obtained now", async () => {
    const { store, op, route } = await unknownPaymentAt({ serverNow: () => Date.now(), fetchServerTime: async () => null });
    await expect(executeOperation(store, op.id, { user: CASHIER, token: TOKEN })).rejects.toMatchObject({
      reason: "no_server_time",
    });
    expect(route.calls).toBe(1);
    expect(store.get(op.id)?.status).toBe("unknown");
    expect(store.get(op.id)?.body).toEqual(BODY);
  });

  it("never uses a cached (possibly stale) server time estimate for a retry", async () => {
    // The estimate says it is still early; the server cannot be reached now.
    const { store, op } = await unknownPaymentAt({ serverNow: () => T0, fetchServerTime: async () => null, now: () => T0 });
    expect(store.retryBlock(store.get(op.id)!, CASHIER)).toBeNull(); // display only
    await expect(executeOperation(store, op.id, { user: CASHIER, token: TOKEN })).rejects.toMatchObject({
      reason: "no_server_time",
    });
  });

  it("does not ask for server time for a first send", async () => {
    const fetchServerTime = vi.fn(async () => Date.now());
    mockApi({ [PAYMENT_PATH]: [jsonResponse({ id: "p1" }, 201)] });
    await startOperation(tab({ fetchServerTime }), PAYMENT, { user: CASHIER, token: TOKEN });
    expect(fetchServerTime).not.toHaveBeenCalled();
  });

  describe("default server time source (GET /api/v1/me)", () => {
    afterEach(() => resetServerClock());

    function defaultStore(now: () => number) {
      return new PendingOperationStore({ now: () => new Date(now()), locks: null });
    }

    it("reads the Date header of a fresh /me before retrying, then sends the stored request", async () => {
      observeServerDate(new Date(T0).toUTCString());
      const { calls } = mockApi({
        [PAYMENT_PATH]: [timeoutResponse, jsonResponse({ id: "p1" }, 201)],
        "GET /api/v1/me": [jsonResponse(CASHIER, 200, { Date: new Date(T0 + 2 * HOUR).toUTCString() })],
      });
      const store = defaultStore(() => T0);
      await startOperation(store, PAYMENT, { user: CASHIER, token: TOKEN, timeoutMs: 5 }).catch(() => undefined);
      const [op] = store.getSnapshot().operations;
      expect(op.serverCreatedAt).not.toBeNull();

      await expect(executeOperation(store, op.id, { user: CASHIER, token: TOKEN })).resolves.toEqual({ id: "p1" });
      expect(calls.map((call) => call.key)).toEqual([PAYMENT_PATH, "GET /api/v1/me", PAYMENT_PATH]);
    });

    it("blocks the retry when /me brings no Date header, or the server is unreachable", async () => {
      observeServerDate(new Date(T0).toUTCString());
      const { calls } = mockApi({
        [PAYMENT_PATH]: [timeoutResponse],
        "GET /api/v1/me": [
          new Response(JSON.stringify(CASHIER), { status: 200 }),
          () => {
            throw new TypeError("Failed to fetch");
          },
        ],
      });
      const store = defaultStore(() => T0);
      await startOperation(store, PAYMENT, { user: CASHIER, token: TOKEN, timeoutMs: 5 }).catch(() => undefined);
      const [op] = store.getSnapshot().operations;

      for (let i = 0; i < 2; i++) {
        await expect(executeOperation(store, op.id, { user: CASHIER, token: TOKEN })).rejects.toMatchObject({
          reason: "no_server_time",
        });
      }
      expect(calls.filter((call) => call.key === PAYMENT_PATH)).toHaveLength(1);
    });

    it("ends the attempt on a 401 from /me without sending the payment", async () => {
      observeServerDate(new Date(T0).toUTCString());
      const { calls } = mockApi({
        [PAYMENT_PATH]: [timeoutResponse],
        "GET /api/v1/me": [errorResponse(401, "UNAUTHORIZED", "expired")],
      });
      const store = defaultStore(() => T0);
      await startOperation(store, PAYMENT, { user: CASHIER, token: TOKEN, timeoutMs: 5 }).catch(() => undefined);
      const [op] = store.getSnapshot().operations;
      const error = await executeOperation(store, op.id, { user: CASHIER, token: TOKEN }).catch((caught: unknown) => caught);
      expect((error as ApiError).isUnauthorized).toBe(true);
      expect(calls.filter((call) => call.key === PAYMENT_PATH)).toHaveLength(1);
      expect(store.get(op.id)?.status).toBe("unknown");
    });
  });
});

describe("P3: smaller hardening", () => {
  it("keeps a recorded check when merging with a newer copy from another tab", async () => {
    mockApi({ [PAYMENT_PATH]: [timeoutResponse] });
    const store = tab();
    const op = await timedOutPayment(store);
    const base = store.get(op.id)!;
    const checked: PendingOperation = {
      ...base,
      status: "checked",
      revision: base.revision + 1,
      check: {
        checkedByUserId: MANAGER.id,
        checkedByName: MANAGER.name,
        checkedAt: new Date().toISOString(),
        note: null,
        previousStatus: "unknown",
        previousError: null,
      },
    };
    const laterAttempt: PendingOperation = { ...base, status: "unknown", revision: base.revision + 2 };
    const merged = mergeRecord(checked, laterAttempt);
    expect(merged.status).toBe("checked");
    expect(merged.check?.checkedByUserId).toBe(MANAGER.id);
    expect(merged.body).toBeUndefined();
    expect(mergeRecord(laterAttempt, checked)).toEqual(merged);
  });

  it("keeps the server's confirmation over a newer copy in doubt", async () => {
    mockApi({ [PAYMENT_PATH]: [jsonResponse({ id: "p1" }, 201)] });
    const store = tab();
    await startOperation(store, PAYMENT, { user: CASHIER, token: TOKEN });
    const [done] = store.getSnapshot().operations;
    const doubt: PendingOperation = { ...done, status: "unknown", revision: done.revision + 3, resolvedAt: null };
    expect(mergeRecord(done, doubt)).toMatchObject({ status: "succeeded", resolvedAt: done.resolvedAt });
  });

  it("drops the body of a never-sent operation once its window passes, and still allows dismissing it", async () => {
    let now = T0;
    const store = tab({ now: () => now });
    const op = await store.create(CASHIER, PAYMENT);
    now = T0 + RETRY_WINDOW_MS + 1;
    await store.create(CASHIER, PAYMENT);
    expect(store.get(op.id)).toMatchObject({ retryExpired: true });
    expect(stored().operations.find((entry) => entry.id === op.id)?.body).toBeUndefined();
    await store.dismiss(op.id, CASHIER);
    expect(store.get(op.id)).toBeUndefined();
  });

  it.each(["card-number", "Card_Number", "cardNumber", "card_no", "security_code", "cvc2", "CVV", "cardNumberMasked", "pin_block", "exp_month", "Access-Token"])(
    "refuses the sensitive field name %s",
    async (name) => {
      expect(isSensitiveFieldName(name)).toBe(true);
      await expect(tab().create(CASHIER, { ...PAYMENT, body: { amount: "1.00", nested: { [name]: "x" } } })).rejects.toThrow(
        /sensitive/,
      );
    },
  );

  it.each(["amount", "tip_amount", "method", "card_confirmed", "external_reference", "reason", "order_item_ids", "opening_amount"])(
    "allows the API field %s",
    (name) => {
      expect(isSensitiveFieldName(name)).toBe(false);
    },
  );

  it("has no method on the store that wipes its memory", () => {
    const store = tab();
    expect("resetForTests" in store).toBe(false);
    expect(Object.getOwnPropertyNames(PendingOperationStore.prototype)).not.toContain("resetForTests");
  });
});

// ---------------------------------------------------------------------------
// Stage 3.2.1.2 final hardening
// ---------------------------------------------------------------------------

const HOUR = 3600_000;

/** An unknown payment in tab `store`, created with server time T0. */
async function unknownAt(store: PendingOperationStore) {
  mockApi({ [PAYMENT_PATH]: [timeoutResponse] });
  return timedOutPayment(store);
}

describe("retention tombstones vs stale tabs (new P2)", () => {
  it("does not let a stale tab resurrect a purged checked record (memory copy, storage tombstone)", async () => {
    let now = T0;
    const tabA = tab({ tabId: "a", now: () => now });
    const op = await unknownAt(tabA);
    const stale = tab({ tabId: "stale", now: () => now }); // holds the unknown copy, never reconciles
    expect(stale.get(op.id)?.status).toBe("unknown");

    await tabA.markChecked(op.id, MANAGER);
    now += RETENTION_MS + 1;
    await tabA.create(MANAGER, PAYMENT); // retention purge
    expect(stored().removed[op.id]).toMatchObject({ kind: "purged" });

    await stale.create(MANAGER, PAYMENT); // the stale snapshot writes
    expect(stored().operations.find((entry) => entry.id === op.id)).toBeUndefined();
    // Nothing of the cashier's payment (record or body) is back.
    expect(stored().operations.some((entry) => entry.userId === CASHIER.id)).toBe(false);
    expect(stale.get(op.id)).toBeUndefined();
    stale.reconcile();
    expect(stale.get(op.id)).toBeUndefined();
  });

  it("also wins when the stale copy is in storage and the tombstone in memory", async () => {
    let now = T0;
    const tabA = tab({ tabId: "a", now: () => now });
    const op = await unknownAt(tabA);
    const staleCopy = { ...tabA.get(op.id)! };
    await tabA.markChecked(op.id, MANAGER);
    now += RETENTION_MS + 1;
    await tabA.create(MANAGER, PAYMENT);
    const tombstones = stored().removed;
    // Someone writes the old copy back into storage (no Web Locks race).
    writeStored([staleCopy], tombstones);
    tabA.reconcile();
    expect(tabA.get(op.id)).toBeUndefined();
    // And with the tombstone only in A's memory:
    writeStored([staleCopy], {});
    tabA.reconcile();
    expect(tabA.get(op.id)).toBeUndefined();
  });

  it("does not resurrect a purged confirmed record or its body", async () => {
    mockApi({ [PAYMENT_PATH]: [jsonResponse({ id: "p1" }, 201)] });
    let now = T0;
    const tabA = tab({ tabId: "a", now: () => now });
    const created = await tabA.create(CASHIER, PAYMENT);
    const stale = tab({ tabId: "stale", now: () => now }); // pending copy with body
    await executeOperation(tabA, created.id, { user: CASHIER, token: TOKEN });
    now += RETENTION_MS + 1;
    await tabA.create(MANAGER, PAYMENT);
    await stale.create(MANAGER, PAYMENT);
    expect(stored().operations.find((entry) => entry.id === created.id)).toBeUndefined();
  });

  it("never lets a retention tombstone delete a newer unresolved copy", () => {
    const base = { revision: 5, mayHaveCommitted: true, status: "unknown" } as PendingOperation;
    expect(tombstoneApplies({ ...base, revision: 6 }, { at: "x", revision: 5, kind: "purged" })).toBe(false);
    expect(tombstoneApplies({ ...base, revision: 5 }, { at: "x", revision: 5, kind: "purged" })).toBe(true);
    expect(tombstoneApplies({ ...base, revision: 4 }, { at: "x", revision: 5, kind: "purged" })).toBe(true);
  });

  it("keeps the dismissal protection for operations in doubt", () => {
    const doubtful = { revision: 3, mayHaveCommitted: true, status: "unknown" } as PendingOperation;
    const neverSent = { revision: 3, mayHaveCommitted: false, status: "pending" } as PendingOperation;
    const dismissal = { at: "x", revision: 3, kind: "dismissed" } as const;
    expect(tombstoneApplies(doubtful, dismissal)).toBe(false);
    expect(tombstoneApplies(neverSent, dismissal)).toBe(true);
    expect(tombstoneApplies({ ...neverSent, revision: 4 }, dismissal)).toBe(false);
  });

  it("reads kindless and timestamp-only tombstones as dismissals", async () => {
    const store = tab();
    const op = await unknownAt(store);
    const neverSent = await store.create(CASHIER, PAYMENT);
    writeStored([store.get(op.id)!, store.get(neverSent.id)!], {
      [op.id]: { at: new Date().toISOString(), revision: 99 },
      [neverSent.id]: new Date().toISOString(),
    });
    const fresh = tab();
    expect(fresh.get(op.id)?.status).toBe("unknown");
    expect(fresh.get(neverSent.id)).toBeUndefined();
  });

  it("prefers the retention tombstone when two tombstones have the same revision", async () => {
    let now = T0;
    const tabA = tab({ tabId: "a", now: () => now });
    const op = await unknownAt(tabA);
    const staleCopy = { ...tabA.get(op.id)! };
    await tabA.markChecked(op.id, MANAGER);
    const checkedRevision = tabA.get(op.id)!.revision;
    now += RETENTION_MS + 1;
    await tabA.create(MANAGER, PAYMENT); // A now holds a retention tombstone
    // Storage gets a dismissal tombstone of the same revision and the old copy.
    writeStored([staleCopy], { [op.id]: { at: new Date(now).toISOString(), revision: checkedRevision, kind: "dismissed" } });
    await tabA.create(MANAGER, PAYMENT);
    expect(stored().removed[op.id]).toMatchObject({ kind: "purged", revision: checkedRevision });
    expect(tabA.get(op.id)).toBeUndefined();
  });
});

describe("device clock corrections (P3-1)", () => {
  async function inDoubt(now: () => number, server: () => number | null, fetchServerTime?: () => Promise<number | null>) {
    mockApi({ [PAYMENT_PATH]: [timeoutResponse, jsonResponse({ id: "p1" }, 201)] });
    const store = tab({ now, serverNow: server, fetchServerTime: fetchServerTime ?? (async () => server()) });
    const op = await timedOutPayment(store);
    return { store, op };
  }

  it("R5: a 10-minute backward correction keeps a fresh payment retryable with its body", async () => {
    let device = T0;
    const server = T0;
    const { store, op } = await inDoubt(() => device, () => server + 60_000);
    device = T0 - 10 * 60_000;
    await store.create(MANAGER, PAYMENT); // any write runs the upkeep
    expect(store.get(op.id)).toMatchObject({ retryExpired: false, body: BODY });
    await expect(executeOperation(store, op.id, { user: CASHIER, token: TOKEN })).resolves.toEqual({ id: "p1" });
  });

  it("a forward jump of the device clock does not expire a fresh payment either", async () => {
    let device = T0;
    const { store, op } = await inDoubt(() => device, () => T0 + 60_000);
    device = T0 + 30 * HOUR;
    await store.create(MANAGER, PAYMENT);
    expect(store.get(op.id)).toMatchObject({ retryExpired: false, body: BODY });
    await expect(executeOperation(store, op.id, { user: CASHIER, token: TOKEN })).resolves.toEqual({ id: "p1" });
  });

  it("a backward correction never extends the window: server time decides", async () => {
    let device = T0;
    let server = T0;
    const { store, op } = await inDoubt(() => device, () => server);
    device = T0 + 2 * HOUR; // the device says 2 h
    server = T0 + 21 * HOUR; // the server says 21 h
    await expect(executeOperation(store, op.id, { user: CASHIER, token: TOKEN })).rejects.toMatchObject({
      reason: "expired",
    });
  });

  it("without trustworthy time it fails safely and keeps the body", async () => {
    let device = T0;
    const { store, op } = await inDoubt(() => device, () => T0, async () => null);
    device = T0 - 10 * 60_000;
    await expect(executeOperation(store, op.id, { user: CASHIER, token: TOKEN })).rejects.toMatchObject({
      reason: "no_server_time",
    });
    expect(store.get(op.id)).toMatchObject({ retryExpired: false, body: BODY, status: "unknown" });
  });

  it("first sends and operations never in doubt still need no server time", async () => {
    const fetchServerTime = vi.fn(async () => null);
    mockApi({ [PAYMENT_PATH]: [jsonResponse({ id: "p1" }, 201)] });
    await startOperation(tab({ fetchServerTime, ...{ serverNow: () => null } }), PAYMENT, { user: CASHIER, token: TOKEN });
    expect(fetchServerTime).not.toHaveBeenCalled();

    // A sign-in refusal (never in doubt): a retry asks, and may go on without it.
    mockApi({ [PAYMENT_PATH]: [errorResponse(401, "UNAUTHORIZED", "expired"), jsonResponse({ id: "p2" }, 201)] });
    const store = tab({ fetchServerTime, serverNow: () => null });
    await startOperation(store, PAYMENT, { user: CASHIER, token: TOKEN }).catch(() => undefined);
    const [op] = store.getSnapshot().operations.filter((entry) => entry.status === "pending");
    await expect(executeOperation(store, op.id, { user: CASHIER, token: TOKEN })).resolves.toEqual({ id: "p2" });
    expect(fetchServerTime).toHaveBeenCalledTimes(1);
  });
});

describe("expiry is established, persisted and permanent (P3-2)", () => {
  it("a refused retry that discovers expiry saves it and deletes the body at once", async () => {
    let server = T0;
    mockApi({ [PAYMENT_PATH]: [timeoutResponse] });
    const store = tab({ now: () => T0, serverNow: () => T0, fetchServerTime: async () => server });
    const op = await timedOutPayment(store);
    server = T0 + RETRY_WINDOW_MS + HOUR;

    await expect(executeOperation(store, op.id, { user: CASHIER, token: TOKEN })).rejects.toMatchObject({ reason: "expired" });

    // No other write happened: the refusal itself persisted it.
    const record = stored().operations.find((entry) => entry.id === op.id)!;
    expect(record.retryExpired).toBe(true);
    expect(record.body).toBeUndefined();
    server = T0 + HOUR; // a later, earlier-looking time does not reopen it
    expect(store.retryBlock(store.get(op.id)!, CASHIER, server)).toBe("expired");
  });

  it("periodic upkeep persists expiry known from the server estimate without another write", async () => {
    let estimate = T0;
    mockApi({ [PAYMENT_PATH]: [timeoutResponse] });
    const store = tab({ now: () => T0, serverNow: () => estimate });
    const op = await timedOutPayment(store);
    estimate = T0 + RETRY_WINDOW_MS + HOUR;
    await store.maintain();
    expect(stored().operations.find((entry) => entry.id === op.id)).toMatchObject({ retryExpired: true });
    expect(stored().operations.find((entry) => entry.id === op.id)?.body).toBeUndefined();
  });

  it("a stale tab cannot bring an expired body back", async () => {
    let server = T0;
    mockApi({ [PAYMENT_PATH]: [timeoutResponse] });
    const tabA = tab({ tabId: "a", now: () => T0, serverNow: () => T0, fetchServerTime: async () => server });
    const op = await timedOutPayment(tabA);
    const stale = tab({ tabId: "stale", now: () => T0, serverNow: () => T0 }); // copy with body
    expect(stale.get(op.id)?.body).toEqual(BODY);
    server = T0 + RETRY_WINDOW_MS + HOUR;
    await executeOperation(tabA, op.id, { user: CASHIER, token: TOKEN }).catch(() => undefined);

    await stale.create(MANAGER, PAYMENT);
    expect(stored().operations.find((entry) => entry.id === op.id)).toMatchObject({ retryExpired: true });
    expect(stored().operations.find((entry) => entry.id === op.id)?.body).toBeUndefined();
    expect(stale.get(op.id)?.body).toBeUndefined();
  });

  it("never keeps a body on a record read as expired", () => {
    const store = tab();
    const record = {
      ...BASE_RECORD(),
      retryExpired: true,
      body: BODY,
    };
    writeStored([record]);
    expect(tab().get(record.id)?.body).toBeUndefined();
    void store;
  });
});

function BASE_RECORD(): PendingOperation {
  return {
    id: "op-x",
    userId: CASHIER.id,
    userName: CASHIER.name,
    restaurantId: CASHIER.restaurant_id,
    type: "payment.create",
    method: "POST",
    path: "/api/v1/bills/b1/payments",
    body: BODY,
    bodyHash: hashBody(BODY),
    idempotencyKey: "key-x",
    createdAt: new Date(T0).toISOString(),
    updatedAt: new Date(T0).toISOString(),
    status: "unknown",
    attempts: 1,
    mayHaveCommitted: true,
    lastError: null,
    sendingBy: null,
    serverCreatedAt: T0,
    retryExpired: false,
    check: null,
    resolvedAt: null,
    revision: 3,
  };
}

describe("server time comes only from the /me response (P3-3)", () => {
  afterEach(() => resetServerClock());

  function defaultStore() {
    return new PendingOperationStore({ now: () => new Date(T0), locks: null });
  }

  async function unknownWithDefaults(meRoute: Array<Response | ((init: RequestInit | undefined) => Response | Promise<Response>)>) {
    observeServerDate(new Date(T0).toUTCString());
    const api = mockApi({ [PAYMENT_PATH]: [timeoutResponse, jsonResponse({ id: "p1" }, 201)], "GET /api/v1/me": meRoute });
    const store = defaultStore();
    await startOperation(store, PAYMENT, { user: CASHIER, token: TOKEN, timeoutMs: 5 }).catch(() => undefined);
    const [op] = store.getSnapshot().operations;
    return { store, op, api };
  }

  const payments = (calls: Array<{ key: string }>) => calls.filter((call) => call.key === PAYMENT_PATH).length;

  it("requests /me with cache: no-store and uses its own Date header", async () => {
    const { store, op, api } = await unknownWithDefaults([
      jsonResponse(CASHIER, 200, { Date: new Date(T0 + HOUR).toUTCString() }),
    ]);
    await expect(executeOperation(store, op.id, { user: CASHIER, token: TOKEN })).resolves.toEqual({ id: "p1" });
    const me = api.calls.find((call) => call.key === "GET /api/v1/me")!;
    expect(me.init?.cache).toBe("no-store");
  });

  it("does not accept another response's Date when /me fails (R7)", async () => {
    const { store, op, api } = await unknownWithDefaults([
      () => {
        // A concurrent response arrives while /me fails.
        observeServerDate(new Date(T0 + HOUR).toUTCString());
        throw new TypeError("Failed to fetch");
      },
    ]);
    await expect(executeOperation(store, op.id, { user: CASHIER, token: TOKEN })).rejects.toMatchObject({
      reason: "no_server_time",
    });
    expect(payments(api.calls)).toBe(1);
  });

  it.each([
    ["missing", new Response(JSON.stringify(CASHIER), { status: 200 })],
    ["malformed", jsonResponse(CASHIER, 200, { Date: "yesterday-ish" })],
    ["on an error answer", jsonResponse({ error: { code: "E", message: "m", request_id: "r" } }, 500, { Date: new Date(T0).toUTCString() })],
  ])("blocks the retry when the /me Date is %s", async (_label, response) => {
    const { store, op, api } = await unknownWithDefaults([response]);
    await expect(executeOperation(store, op.id, { user: CASHIER, token: TOKEN })).rejects.toMatchObject({
      reason: "no_server_time",
    });
    expect(payments(api.calls)).toBe(1);
  });

  it("ends on a 401 from /me without sending the payment", async () => {
    const { store, op, api } = await unknownWithDefaults([errorResponse(401, "UNAUTHORIZED", "expired")]);
    const error = await executeOperation(store, op.id, { user: CASHIER, token: TOKEN }).catch((caught: unknown) => caught);
    expect((error as ApiError).isUnauthorized).toBe(true);
    expect(payments(api.calls)).toBe(1);
  });
});

describe("retries racing other tabs", () => {
  async function racing(change: (other: PendingOperationStore, id: string) => Promise<unknown>, locks: FakeLockManager | null = null) {
    let id = "";
    let other: PendingOperationStore | null = null;
    const store = tab({
      tabId: "me",
      locks,
      fetchServerTime: async () => {
        await change(other!, id);
        return Date.now();
      },
    });
    const { calls } = mockApi({ [PAYMENT_PATH]: [timeoutResponse, jsonResponse({ id: "p1" }, 201)] });
    const op = await timedOutPayment(store);
    id = op.id;
    other = tab({ tabId: "other", locks });
    const error = await executeOperation(store, op.id, { user: CASHIER, token: TOKEN }).catch((caught: unknown) => caught);
    return { error: error as OperationBlockedError, payments: calls.filter((call) => call.key === PAYMENT_PATH).length, store, id };
  }

  it("does not send when another tab records a check during the time check", async () => {
    const { error, payments } = await racing((other, id) => other.markChecked(id, MANAGER));
    expect(error.reason).toBe("checked");
    expect(payments).toBe(1);
  });

  it("does not send when another tab confirms it during the time check", async () => {
    mockApi({});
    const { error, payments } = await racing(async (other, id) => {
      await other.beginSend(id, CASHIER, Date.now());
      await other.recordSuccess(id);
    });
    expect(error.reason).toBe("resolved");
    expect(payments).toBe(1);
  });

  it("does not send when another tab starts sending it during the time check", async () => {
    const { error, payments } = await racing((other, id) => other.beginSend(id, CASHIER, Date.now()));
    expect(error.reason).toBe("in_flight");
    expect(payments).toBe(1);
  });
});

describe("merge determinism (P3-4)", () => {
  const variants: Array<[string, Partial<PendingOperation>, Partial<PendingOperation>]> = [
    ["lastError", { lastError: { status: 504, code: "A", message: "a", requestId: null } }, { lastError: { status: 0, code: "B", message: "b", requestId: null } }],
    ["attempts", { attempts: 2 }, { attempts: 3 }],
    ["updatedAt", { updatedAt: "2026-11-08T10:00:00.000Z" }, { updatedAt: "2026-11-08T10:00:01.000Z" }],
    [
      "sendingBy",
      { status: "sending", sendingBy: { tabId: "a", heartbeatAt: "2026-11-08T10:00:00.000Z" } },
      { status: "sending", sendingBy: { tabId: "b", heartbeatAt: "2026-11-08T10:00:00.000Z" } },
    ],
    ["status and check", { status: "checked", check: { checkedByUserId: "m", checkedByName: "M", checkedAt: "t", note: null, previousStatus: "unknown", previousError: null } }, { status: "unknown" }],
    ["success", { status: "succeeded", resolvedAt: "t" }, { status: "unknown" }],
    ["expiry", { retryExpired: true }, { retryExpired: false }],
  ];

  it.each(variants)("is the same in both orders for equal revisions differing in %s", (_name, x, y) => {
    const a = { ...BASE_RECORD(), ...x };
    const b = { ...BASE_RECORD(), ...y };
    expect(mergeRecord(a, b)).toEqual(mergeRecord(b, a));
  });

  it("keeps doubt, checks, confirmations and expiry from either side", () => {
    const a = { ...BASE_RECORD(), status: "failed" as const, mayHaveCommitted: false, revision: 9 };
    const b = { ...BASE_RECORD(), retryExpired: true, revision: 2 };
    for (const merged of [mergeRecord(a, b), mergeRecord(b, a)]) {
      expect(merged).toMatchObject({ status: "unknown", mayHaveCommitted: true, retryExpired: true, revision: 9 });
      expect(merged.body).toBeUndefined();
    }
  });
});

describe("card numbers in payloads (P3-8)", () => {
  it.each([
    "4111111111111111",
    "4111 1111 1111 1111",
    "4111-1111-1111-1111",
    "4111.1111.1111.1111",
    "card 5500 0000 0000 0004 declined",
    "378282246310005",
  ])("refuses the text %s", (value) => {
    expect(looksLikeCardNumber(value)).toBe(true);
  });

  it("refuses a card number stored as a number, anywhere in the body", async () => {
    expect(looksLikeCardNumber(4111111111111111)).toBe(true);
    await expect(
      tab().create(CASHIER, { ...PAYMENT, body: { amount: "1.00", method: "CARD", external_reference: 4111111111111111 } }),
    ).rejects.toThrow(/card number/);
  });

  it.each([
    "12.50",
    "99999999.99",
    "2026-11-08",
    "receipt 000123",
    "+34 612 345 678",
    "4111111111111112",
    "123456789012",
    "c0000000-0000-4000-8000-000000000001",
    "11111111-2222-3333-4716-446655440000",
  ])("allows the ordinary value %s", (value) => {
    expect(looksLikeCardNumber(value)).toBe(false);
  });

  it("allows real payloads: amounts, ids and references", async () => {
    const body = {
      amount: "123.45",
      method: "CARD",
      tip_amount: "2.00",
      order_item_ids: ["11111111-2222-3333-4716-446655440000", "c0000000-0000-4000-8000-000000000001"],
      external_reference: "TERM-000123",
      card_confirmed: true,
    };
    await expect(tab().create(CASHIER, { ...PAYMENT, body })).resolves.toBeDefined();
    expect(looksLikeCardNumber(12345)).toBe(false);
    expect(looksLikeCardNumber(12.5)).toBe(false);
  });
});

describe("self-check policy", () => {
  it("rejects an employee checking their own operation, even when called directly", async () => {
    const store = tab();
    const op = await unknownAt(store);
    await expect(store.markChecked(op.id, CASHIER)).rejects.toThrow(/cannot check your own/);
    // Not even a manager on their own operation.
    const managerOp = await (async () => {
      mockApi({ [PAYMENT_PATH]: [timeoutResponse] });
      await startOperation(store, PAYMENT, { user: MANAGER, token: TOKEN, timeoutMs: 5 }).catch(() => undefined);
      return store.getSnapshot().operations.find((entry) => entry.userId === MANAGER.id)!;
    })();
    await expect(store.markChecked(managerOp.id, MANAGER)).rejects.toThrow(/cannot check your own/);
    expect(store.get(op.id)?.status).toBe("unknown");
  });

  it("lets a manager of the same restaurant check another employee's operation", async () => {
    const store = tab();
    const op = await unknownAt(store);
    await store.markChecked(op.id, MANAGER);
    expect(store.get(op.id)?.status).toBe("checked");
  });

  it("refuses employees without cash_reports:view and managers of another restaurant", async () => {
    const store = tab();
    const op = await unknownAt(store);
    const colleague = { ...CASHIER, id: "c0000000-0000-4000-8000-0000000000c2" };
    await expect(store.markChecked(op.id, colleague)).rejects.toThrow(/Only a manager/);
    await expect(store.markChecked(op.id, { ...MANAGER, restaurant_id: "another" })).rejects.toThrow(/another restaurant/);
    // A caller cannot pass an option to bypass it.
    await expect(
      store.markChecked(op.id, colleague, { mayCheckOthers: true } as unknown as { note?: string }),
    ).rejects.toThrow(/Only a manager/);
    expect(store.get(op.id)?.status).toBe("unknown");
  });
});

describe("recording a confirmed success when storage fails (P3-10)", () => {
  it("keeps the confirmation in memory, warns, and saves it later", async () => {
    const state = { writable: true };
    const storage = {
      getItem: (key: string) => window.localStorage.getItem(key),
      setItem: (key: string, value: string) => {
        if (!state.writable) throw new DOMException("full", "QuotaExceededError");
        window.localStorage.setItem(key, value);
      },
      removeItem: (key: string) => window.localStorage.removeItem(key),
    } as unknown as Storage;
    let resolve: (response: Response) => void = () => {};
    mockApi({ [PAYMENT_PATH]: [() => new Promise<Response>((done) => (resolve = done))] });
    const store = new PendingOperationStore({ storage: () => storage, locks: null, serverNow: () => Date.now(), fetchServerTime: async () => Date.now() });
    const sending = startOperation(store, PAYMENT, { user: CASHIER, token: TOKEN });
    await new Promise((done) => setTimeout(done, 0));
    state.writable = false; // storage fills up while the request runs
    resolve(jsonResponse({ id: "p1" }, 201));
    await expect(sending).resolves.toEqual({ id: "p1" });

    const [op] = store.getSnapshot().operations;
    expect(op.status).toBe("succeeded");
    expect(store.getSnapshot()).toMatchObject({ unsaved: true, persistent: false });
    expect(stored().operations[0].status).toBe("sending"); // storage is behind
    store.reconcile();
    expect(store.get(op.id)?.status).toBe("succeeded"); // never turned back

    state.writable = true;
    await store.maintain();
    expect(stored().operations[0].status).toBe("succeeded");
    expect(store.getSnapshot().persistent).toBe(true);
  });

  it("keeps the confirmation in memory even when the storage lock fails", async () => {
    class FailingLocks extends FakeLockManager {
      fail = false;
      override async request<T>(name: string, options: { mode?: "exclusive"; ifAvailable?: boolean }, callback: (lock: unknown) => Promise<T> | T): Promise<T> {
        if (this.fail && name === "restaurantos.cashier.pendingOperations") throw new Error("lock manager gone");
        return super.request(name, options, callback);
      }
    }
    const locks = new FailingLocks();
    let resolve: (response: Response) => void = () => {};
    mockApi({ [PAYMENT_PATH]: [() => new Promise<Response>((done) => (resolve = done))] });
    const store = tab({ locks });
    const sending = startOperation(store, PAYMENT, { user: CASHIER, token: TOKEN });
    await new Promise((done) => setTimeout(done, 0));
    locks.fail = true;
    resolve(jsonResponse({ id: "p1" }, 201));
    await sending;
    expect(store.getSnapshot().operations[0].status).toBe("succeeded");
    expect(store.getSnapshot().unsaved).toBe(true);
  });
});

describe("storage key migration (P3-11)", () => {
  it("moves records from the v1 key into the v2 key, keeping operations in doubt", async () => {
    const legacyRecord = { ...BASE_RECORD(), mayHaveCommitted: undefined, serverCreatedAt: undefined, retryExpired: undefined };
    window.localStorage.setItem(LEGACY_STORAGE_KEY, JSON.stringify([legacyRecord]));
    const store = tab();
    expect(store.get("op-x")).toMatchObject({ status: "unknown", mayHaveCommitted: true });

    await store.create(MANAGER, PAYMENT);
    expect(window.localStorage.getItem(LEGACY_STORAGE_KEY)).toBeNull();
    expect(stored().operations.find((entry) => entry.id === "op-x")).toMatchObject({ status: "unknown", mayHaveCommitted: true });
  });

  it("merges a v1 object written by earlier builds with existing v2 data", async () => {
    const store = tab();
    const current = await store.create(CASHIER, PAYMENT);
    window.localStorage.setItem(
      LEGACY_STORAGE_KEY,
      JSON.stringify({ version: 2, operations: [BASE_RECORD()], removed: {}, invalid: [] }),
    );
    await store.create(MANAGER, PAYMENT);
    const ids = stored().operations.map((entry) => entry.id);
    expect(ids).toContain(current.id);
    expect(ids).toContain("op-x");
    expect(window.localStorage.getItem(LEGACY_STORAGE_KEY)).toBeNull();
  });

  it("keeps the v1 key when the v2 write fails", async () => {
    window.localStorage.setItem(LEGACY_STORAGE_KEY, JSON.stringify([BASE_RECORD()]));
    const storage = {
      getItem: (key: string) => window.localStorage.getItem(key),
      setItem: () => {
        throw new DOMException("full", "QuotaExceededError");
      },
      removeItem: (key: string) => window.localStorage.removeItem(key),
    } as unknown as Storage;
    const store = new PendingOperationStore({ storage: () => storage, locks: null, serverNow: () => null });
    await store.create(MANAGER, PAYMENT);
    expect(window.localStorage.getItem(LEGACY_STORAGE_KEY)).not.toBeNull();
    expect(store.get("op-x")?.status).toBe("unknown");
  });

  it("leaves an unreadable v1 key untouched and warns", async () => {
    window.localStorage.setItem(LEGACY_STORAGE_KEY, "{broken");
    const store = tab();
    await store.create(MANAGER, PAYMENT);
    expect(window.localStorage.getItem(LEGACY_STORAGE_KEY)).toBe("{broken");
    expect(store.getSnapshot().recoveredCorruptData).toBe(true);
  });
});

describe("live checks with Web Locks (P3-9)", () => {
  it("recovers a request abandoned by a reload at once, without waiting for the heartbeat", async () => {
    const locks = new FakeLockManager();
    const before = tab({ tabId: "before-reload", locks });
    const op = await before.create(CASHIER, PAYMENT);
    await before.beginSend(op.id, CASHIER, null); // then the page reloads (its lock is gone)
    await new Promise((done) => setTimeout(done, 5));

    const { calls } = mockApi({ [PAYMENT_PATH]: [jsonResponse({ id: "p1" }, 201)] });
    const after = tab({ tabId: "after-reload", locks });
    expect(after.effectiveStatus(after.get(op.id)!)).toBe("sending"); // heartbeat is fresh
    await after.refreshLiveness();
    expect(after.effectiveStatus(after.get(op.id)!)).toBe("unknown");
    await expect(executeOperation(after, op.id, { user: CASHIER, token: TOKEN })).resolves.toEqual({ id: "p1" });
    expect(keyOf(calls[0].init)).toBe(op.idempotencyKey);
  });

  it("keeps a request live while another tab holds its lock, and blocks a check", async () => {
    const locks = new FakeLockManager();
    const route = hangingRoute();
    mockApi({ [PAYMENT_PATH]: [route.handler] });
    let nowB = Date.now();
    const tabA = tab({ tabId: "a", locks });
    const tabB = tab({ tabId: "b", locks, now: () => nowB });
    const op = await tabA.create(CASHIER, PAYMENT);
    const sending = executeOperation(tabA, op.id, { user: CASHIER, token: TOKEN });
    await new Promise((done) => setTimeout(done, 0));

    tabB.reconcile();
    await tabB.refreshLiveness();
    expect(tabB.effectiveStatus(tabB.get(op.id)!)).toBe("sending");
    nowB += STALE_SENDING_MS * 2; // even a stale heartbeat does not allow a check while the lock is held
    await expect(tabB.markChecked(op.id, MANAGER)).rejects.toMatchObject({ reason: "in_flight" });

    route.release(jsonResponse({ id: "p1" }, 201));
    await sending;
  });

  it("does not treat a claim made after the lock query as abandoned", async () => {
    const locks = new FakeLockManager();
    const tabB = tab({ tabId: "b", locks });
    const tabA = tab({ tabId: "a", locks });
    const op = await tabA.create(CASHIER, PAYMENT);
    // An old abandoned claim is seen free…
    await new PendingOperationStore({ locks, tabId: "gone", serverNow: () => Date.now() }).beginSend(op.id, CASHIER, null);
    tabB.reconcile();
    await tabB.refreshLiveness();
    // …then a live tab claims it anew (later heartbeat).
    await new Promise((done) => setTimeout(done, 5));
    const route = hangingRoute();
    mockApi({ [PAYMENT_PATH]: [route.handler] });
    const live = tab({ tabId: "live", locks, fetchServerTime: async () => Date.now() });
    const sending = executeOperation(live, op.id, { user: CASHIER, token: TOKEN });
    await new Promise((done) => setTimeout(done, 5));
    tabB.reconcile();
    expect(tabB.effectiveStatus(tabB.get(op.id)!)).toBe("sending");
    route.release(jsonResponse({ id: "p1" }, 201));
    await sending;
  });
});

// ---------------------------------------------------------------------------
// Final compatibility and safety patch
// ---------------------------------------------------------------------------

describe("card-number candidates without regex lookbehind (P2 Safari)", () => {
  // The previous implementation, as the reference (Node supports lookbehind).
  const REFERENCE = /(?<![0-9A-Za-z])\d(?:[ .-]?\d){12,18}(?![0-9A-Za-z])/g;
  const reference = (text: string) => [...text.matchAll(REFERENCE)].map((match) => match[0]);

  const cases = [
    "4111111111111111",
    "4111 1111 1111 1111",
    "4111-1111-1111-1111",
    "4111.1111.1111.1111",
    "card 5500 0000 0000 0004 declined",
    "378282246310005",
    "a4111111111111111",
    "4111111111111111x",
    "#4111111111111111#",
    "x 4111111111111111 y",
    "a4111 1111 1111 1111 1111",
    "ab12 4111111111111111",
    "41111111111111111111111",
    "4111  1111 1111 1111",
    "4111..1111.1111.1111",
    "12.50",
    "99999999.99",
    "2026-11-08",
    "receipt 000123",
    "+34 612 345 678",
    "4111111111111112",
    "123456789012",
    "c0000000-0000-4000-8000-000000000001",
    "TERM-000123 / 4111111111111111",
    "",
  ];

  it.each(cases)("finds exactly the reference candidates in %j", (text) => {
    expect(cardCandidates(text)).toEqual(reference(text));
  });

  it("matches the reference on thousands of generated strings", () => {
    const alphabet = "0123456789 .-aZ_";
    let seed = 42;
    const random = () => {
      seed = (seed * 1103515245 + 12345) % 2 ** 31;
      return seed / 2 ** 31;
    };
    for (let i = 0; i < 5000; i++) {
      const length = 10 + Math.floor(random() * 40);
      let text = "";
      for (let j = 0; j < length; j++) {
        // Mostly digits, so that long candidates are common.
        text += random() < 0.7 ? String(Math.floor(random() * 10)) : alphabet[Math.floor(random() * alphabet.length)];
      }
      expect(cardCandidates(text)).toEqual(reference(text));
    }
  });

  it("keeps detection results for strings and numbers", () => {
    for (const value of ["4111111111111111", "4111 1111 1111 1111", "4111.1111.1111.1111", "x 4111-1111-1111-1111 y"]) {
      expect(looksLikeCardNumber(value)).toBe(true);
    }
    for (const value of ["a4111111111111111", "4111111111111111x", "11111111-2222-3333-4716-446655440000", "12.50"]) {
      expect(looksLikeCardNumber(value)).toBe(false);
    }
    expect(looksLikeCardNumber(4111111111111111)).toBe(true);
    expect(looksLikeCardNumber(4111111111111112)).toBe(false);
  });

  it("no application source uses regex lookbehind", async () => {
    const { readdirSync, readFileSync, statSync } = await import("node:fs");
    const { join } = await import("node:path");
    const offending: string[] = [];
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const path = join(dir, name);
        if (statSync(path).isDirectory()) walk(path);
        else if (/\.(ts|tsx)$/.test(name) && !/\.test\.(ts|tsx)$/.test(name) && !path.includes(`${join("src", "test")}`)) {
          readFileSync(path, "utf8")
            .split("\n")
            .forEach((line, index) => {
              const code = line.trim();
              if (code.startsWith("//") || code.startsWith("*")) return;
              if (code.includes("(?<!") || code.includes("(?<=")) offending.push(`${path}:${index + 1}`);
            });
        }
      }
    };
    walk(join(process.cwd(), "src"));
    expect(offending).toEqual([]);
  });
});

describe("Web Locks liveness query race (Q1)", () => {
  class SlowQueryLocks extends FakeLockManager {
    duringQuery: (() => Promise<void>) | null = null;
    override async query() {
      const snapshot = await super.query(); // the snapshot is taken now…
      if (this.duringQuery) await this.duringQuery(); // …and delivered later
      return snapshot;
    }
  }

  it("never treats a send started while the query is pending as abandoned", async () => {
    const locks = new SlowQueryLocks();
    const tabA = tab({ tabId: "a", locks });
    const op = await tabA.create(CASHIER, PAYMENT);
    const tabB = tab({ tabId: "b", locks });
    const route = hangingRoute();
    mockApi({ [PAYMENT_PATH]: [route.handler] });
    let sending: Promise<unknown> = Promise.resolve();
    locks.duringQuery = async () => {
      sending = executeOperation(tabA, op.id, { user: CASHIER, token: TOKEN });
      await new Promise((done) => setTimeout(done, 5));
      tabB.reconcile();
    };

    await tabB.refreshLiveness();

    expect(locks.isHeld(`restaurantos.cashier.operation.${op.id}`)).toBe(true);
    expect(tabB.effectiveStatus(tabB.get(op.id)!)).toBe("sending");
    await tabB.create(MANAGER, PAYMENT); // B writes: A's live record is left alone
    expect(stored().operations.find((entry) => entry.id === op.id)).toMatchObject({
      status: "sending",
      sendingBy: expect.objectContaining({ tabId: "a" }),
    });
    await expect(executeOperation(tabB, op.id, { user: CASHIER, token: TOKEN })).rejects.toMatchObject({
      reason: "in_flight",
    });
    route.release(jsonResponse({ id: "p1" }, 201));
    await sending;
    expect(stored().operations.find((entry) => entry.id === op.id)?.status).toBe("succeeded");
  });

  it("still recovers a genuinely abandoned send at once", async () => {
    const locks = new SlowQueryLocks();
    const gone = tab({ tabId: "gone", locks });
    const op = await gone.create(CASHIER, PAYMENT);
    await gone.beginSend(op.id, CASHIER, null); // its tab then closed
    await new Promise((done) => setTimeout(done, 5));
    const tabB = tab({ tabId: "b", locks });
    await tabB.refreshLiveness();
    expect(tabB.effectiveStatus(tabB.get(op.id)!)).toBe("unknown");
  });

  it("keeps the heartbeat rule without Web Locks", async () => {
    let now = T0;
    const gone = tab({ tabId: "gone", now: () => now });
    const op = await gone.create(CASHIER, PAYMENT);
    await gone.beginSend(op.id, CASHIER, null);
    const tabB = tab({ tabId: "b", now: () => now });
    await tabB.refreshLiveness(); // no-op
    expect(tabB.effectiveStatus(tabB.get(op.id)!)).toBe("sending");
    now += STALE_SENDING_MS + 1;
    expect(tabB.effectiveStatus(tabB.get(op.id)!)).toBe("unknown");
  });
});

describe("corrupt v2 with valid v1 (P3)", () => {
  async function v1Record() {
    mockApi({ [PAYMENT_PATH]: [timeoutResponse] });
    const source = tab();
    const op = await timedOutPayment(source);
    const record = stored().operations.find((entry) => entry.id === op.id)!;
    window.localStorage.clear();
    return record;
  }

  it("shows the v1 operations at once and migrates them with the first write", async () => {
    const record = await v1Record();
    window.localStorage.setItem(STORAGE_KEY, "{corrupt v2");
    window.localStorage.setItem(LEGACY_STORAGE_KEY, JSON.stringify([record]));

    const store = tab();
    expect(store.get(record.id)).toMatchObject({ status: "unknown", mayHaveCommitted: true });
    expect(store.getSnapshot().recoveredCorruptData).toBe(true);

    await store.create(MANAGER, PAYMENT);
    expect(stored().operations.find((entry) => entry.id === record.id)).toMatchObject({ status: "unknown" });
    expect(window.localStorage.getItem(LEGACY_STORAGE_KEY)).toBeNull();
    const asides = Object.keys(window.localStorage).filter((key) => key.includes(".corrupt."));
    expect(asides.map((key) => window.localStorage.getItem(key))).toContain("{corrupt v2");
  });

  it("keeps both keys untouched when the corrupt v2 data cannot be copied aside", async () => {
    const record = await v1Record();
    window.localStorage.setItem(STORAGE_KEY, "{corrupt v2");
    window.localStorage.setItem(LEGACY_STORAGE_KEY, JSON.stringify([record]));
    const storage = {
      getItem: (key: string) => window.localStorage.getItem(key),
      setItem: () => {
        throw new DOMException("full", "QuotaExceededError");
      },
      removeItem: (key: string) => window.localStorage.removeItem(key),
    } as unknown as Storage;
    const store = new PendingOperationStore({ storage: () => storage, locks: null, serverNow: () => null });
    expect(store.get(record.id)?.status).toBe("unknown"); // visible anyway
    await store.create(MANAGER, PAYMENT);
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe("{corrupt v2");
    expect(window.localStorage.getItem(LEGACY_STORAGE_KEY)).not.toBeNull();
    expect(store.getSnapshot()).toMatchObject({ persistent: false, unsaved: true });
  });

  it("keeps v1 when the v2 write fails, and migrates idempotently later", async () => {
    const record = await v1Record();
    window.localStorage.setItem(STORAGE_KEY, "{corrupt v2");
    window.localStorage.setItem(LEGACY_STORAGE_KEY, JSON.stringify([record]));
    const state = { failMain: true };
    const storage = {
      getItem: (key: string) => window.localStorage.getItem(key),
      setItem: (key: string, value: string) => {
        if (key === STORAGE_KEY && state.failMain) throw new DOMException("full", "QuotaExceededError");
        window.localStorage.setItem(key, value);
      },
      removeItem: (key: string) => window.localStorage.removeItem(key),
    } as unknown as Storage;
    const store = new PendingOperationStore({ storage: () => storage, locks: null, serverNow: () => Date.now() });
    await store.create(MANAGER, PAYMENT);
    expect(window.localStorage.getItem(LEGACY_STORAGE_KEY)).not.toBeNull();

    state.failMain = false;
    await store.create(MANAGER, PAYMENT);
    expect(window.localStorage.getItem(LEGACY_STORAGE_KEY)).toBeNull();
    const firstRevision = stored().operations.find((entry) => entry.id === record.id)!.revision;

    // The same v1 data appears again (e.g. an old tab): merged without change.
    window.localStorage.setItem(LEGACY_STORAGE_KEY, JSON.stringify([record]));
    await store.create(MANAGER, PAYMENT);
    const again = stored().operations.filter((entry) => entry.id === record.id);
    expect(again).toHaveLength(1);
    expect(again[0].revision).toBe(firstRevision);
    expect(window.localStorage.getItem(LEGACY_STORAGE_KEY)).toBeNull();
  });
});

describe("confirmed but unsaved, seen from another tab (P3)", () => {
  it("shows a recent lock-free send as being recovered: retry allowed, no check, no dismissal", async () => {
    const locks = new FakeLockManager();
    let now = T0;
    const tabA = tab({ tabId: "a", locks, now: () => now, channelName: null } as TabOptions);
    const op = await tabA.create(CASHIER, PAYMENT);
    await tabA.beginSend(op.id, CASHIER, null); // its answer could not be saved
    const tabB = tab({ tabId: "b", locks, now: () => now });
    now += 1_000;
    await tabB.refreshLiveness();
    const seen = tabB.get(op.id)!;
    expect(tabB.isRecovering(seen)).toBe(true);
    expect(tabB.canDismiss(seen, CASHIER)).toBe(false);
    await expect(tabB.markChecked(op.id, MANAGER)).rejects.toThrow(/still being recovered/);
    await tabB.create(MANAGER, PAYMENT); // B's writes leave the record as it is
    expect(stored().operations.find((entry) => entry.id === op.id)?.status).toBe("sending");

    // Once the heartbeat is stale it is an ordinary unknown outcome.
    now += STALE_SENDING_MS + 1;
    expect(tabB.isRecovering(tabB.get(op.id)!)).toBe(false);
    await tabB.markChecked(op.id, MANAGER);
    expect(tabB.get(op.id)?.status).toBe("checked");
  });

  it("learns another tab's confirmation through BroadcastChannel even when storage failed", async () => {
    const state = { writable: true };
    const storage = {
      getItem: (key: string) => window.localStorage.getItem(key),
      setItem: (key: string, value: string) => {
        if (!state.writable) throw new DOMException("full", "QuotaExceededError");
        window.localStorage.setItem(key, value);
      },
      removeItem: (key: string) => window.localStorage.removeItem(key),
    } as unknown as Storage;
    const channelName = `test-${Math.random()}`;
    const route = hangingRoute();
    mockApi({ [PAYMENT_PATH]: [route.handler] });
    const tabA = new PendingOperationStore({ storage: () => storage, locks: null, serverNow: () => Date.now(), channelName });
    const tabB = new PendingOperationStore({ locks: null, serverNow: () => Date.now(), channelName });
    const sending = startOperation(tabA, PAYMENT, { user: CASHIER, token: TOKEN });
    await new Promise((done) => setTimeout(done, 0));
    tabB.reconcile();
    const [op] = tabB.getSnapshot().operations;
    state.writable = false;
    route.release(jsonResponse({ id: "p1" }, 201));
    await sending;
    expect(stored().operations[0].status).toBe("sending"); // storage is behind

    await vi.waitFor(() => expect(tabB.get(op.id)?.status).toBe("succeeded"));
    expect(tabB.get(op.id)?.body).toBeUndefined();
  });

  it("ignores malformed broadcast messages", async () => {
    const channelName = `test-${Math.random()}`;
    const store = tab({ channelName } as TabOptions);
    const op = await unknownAt(store);
    const sender = new BroadcastChannel(channelName);
    sender.postMessage({ type: "confirmed", id: op.id }); // no resolvedAt
    sender.postMessage("confirmed");
    await new Promise((done) => setTimeout(done, 20));
    expect(store.get(op.id)?.status).toBe("unknown");
    sender.close();
  });
});

describe("application-level upkeep (P3)", () => {
  it("saves unsaved records on its own schedule and stops when told", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const state = { writable: false };
      const storage = {
        getItem: (key: string) => window.localStorage.getItem(key),
        setItem: (key: string, value: string) => {
          if (!state.writable) throw new DOMException("full", "QuotaExceededError");
          window.localStorage.setItem(key, value);
        },
        removeItem: (key: string) => window.localStorage.removeItem(key),
      } as unknown as Storage;
      const store = new PendingOperationStore({ storage: () => storage, locks: null, serverNow: () => null, channelName: null });
      const op = await store.create(CASHIER, PAYMENT);
      state.writable = true;
      const stop = store.startUpkeep(1_000);
      await vi.advanceTimersByTimeAsync(1_100);
      expect(stored().operations.map((entry) => entry.id)).toContain(op.id);
      stop();
      // After stopping, nothing saves an unsaved record on a schedule.
      state.writable = false;
      const later = await store.create(CASHIER, PAYMENT);
      state.writable = true;
      await vi.advanceTimersByTimeAsync(5_000);
      expect(stored().operations.map((entry) => entry.id)).not.toContain(later.id);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("being-recovered wording", () => {
  it("labels a recent lock-free send neutrally for its owner", async () => {
    const { ownStatusLabel } = await import("./PendingOperationsBanner");
    const locks = new FakeLockManager();
    const gone = tab({ tabId: "gone", locks });
    const op = await gone.create(CASHIER, PAYMENT);
    await gone.beginSend(op.id, CASHIER, null);
    await new Promise((done) => setTimeout(done, 5));
    const tabB = tab({ tabId: "b", locks });
    await tabB.refreshLiveness();
    const label = ownStatusLabel(tabB.get(op.id)!, tabB);
    expect(label).toBe("Outcome being recovered — the request may have completed");
    expect(label).not.toMatch(/not sent/i);
  });
});
