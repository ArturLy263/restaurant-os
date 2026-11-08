import type { CashierOperationType, MutationRequest } from "../api/cashier";
import { apiRequest, isApiError, newIdempotencyKey } from "../api/client";
import { estimateServerNow, parseServerDate } from "../api/serverClock";
import { Permission, type CurrentUser } from "../api/types";
import { sha256Hex } from "../lib/sha256";

// Pending operations: every cashier mutation is written to storage BEFORE
// it is sent, with the exact body and Idempotency-Key, so that a request
// whose outcome is unknown (connection lost, timeout, server error, the
// tab closed) can be repeated safely: the backend replays the stored
// result for the same user, key and body instead of moving money twice.
//
// Invariants:
// 1. Uncertainty is permanent. Once an attempt may have been committed
//    (`mayHaveCommitted`), only an authoritative success from the server
//    resolves the operation. A later 401/403/4xx comes from checks that
//    run BEFORE the backend looks the key up, so it says nothing about the
//    earlier attempt: the operation stays "unknown".
// 2. Nothing is lost across tabs. Every write re-reads storage and merges
//    by operation id (higher revision wins, uncertainty is OR-ed), under a
//    Web Lock where available. Removals leave tombstones so another tab's
//    stale copy cannot bring a record back.
// 3. A live request is never treated as abandoned. The sending tab holds a
//    per-operation Web Lock and refreshes a heartbeat; other tabs consider
//    a "sending" record abandoned only once the heartbeat is stale.
// 4. Retries stop well before the backend forgets the key. For anything
//    that may have been committed, the window is measured in SERVER time
//    only: a lower bound of the server time at creation (stored with the
//    record) against the Date header of a fresh GET /api/v1/me fetched
//    right before the retry. No server time → no retry. The device clock
//    never decides expiry for such an operation. Expiry established from
//    server time is permanent and deletes the body at once.
//    ASSUMPTION (operational, not verified by any frontend test): the Date
//    header comes from the API host's clock, while the backend expires
//    idempotency keys with PostgreSQL now() (the database host's clock).
//    The 20 h window leaves a 4 h margin before the backend's 24 h only if
//    those clocks differ by less than that margin. A server clock that
//    steps backwards stretches the effective window by the size of the
//    step; this relies on such steps staying well inside the margin.
// 5. An operation belongs to the employee who started it: keys are scoped
//    to the user on the backend, so the same request under another account
//    would be a new payment. It is never retried for anyone else.
// 6. A person's check is recorded, never a deletion, and never presented
//    as the server's answer. Nobody checks their own operation; checking
//    another employee's needs cash_reports:view in the same restaurant,
//    verified here and not only in the screen.
// 8. Tombstones (removal records) never delete a copy newer than the
//    removal. A dismissal tombstone never deletes an operation that may
//    have been committed and is unresolved; a retention tombstone (a
//    resolved record purged after 7 days) supersedes every older copy.
// 7. No tokens, card data or other credentials are ever stored; request
//    bodies are dropped once an operation can no longer be retried.

export type PendingOperationStatus =
  // Stored; not sent, or refused by sign-in before it ran (401): safe to send.
  | "pending"
  // A request is in flight (see effectiveStatus for abandoned ones).
  | "sending"
  // The server may or may not have committed it.
  | "unknown"
  // The server refused it before anything could have been committed.
  | "failed"
  // A person recorded that they checked an unknown operation. Not an
  // answer from the server: the outcome is still unconfirmed.
  | "checked"
  // The server confirmed it (fresh or replayed result).
  | "succeeded";

export interface PendingOperationError {
  status: number;
  code: string;
  message: string;
  requestId: string | null;
}

export interface OperationCheck {
  checkedByUserId: string;
  checkedByName: string;
  checkedAt: string;
  note: string | null;
  previousStatus: PendingOperationStatus;
  previousError: PendingOperationError | null;
}

export interface PendingOperation {
  id: string;
  userId: string;
  // For display only ("started by …").
  userName: string;
  restaurantId: string;
  type: CashierOperationType;
  method: "POST";
  path: string;
  // undefined for a request without a body, and once it can no longer be
  // retried (the hash stays).
  body?: unknown;
  bodyHash: string;
  idempotencyKey: string;
  createdAt: string;
  updatedAt: string;
  status: PendingOperationStatus;
  attempts: number;
  // Set once an attempt may have reached the server with an unknown
  // result; never cleared (invariant 1).
  mayHaveCommitted: boolean;
  lastError: PendingOperationError | null;
  // The tab sending it right now, and its last heartbeat.
  sendingBy: { tabId: string; heartbeatAt: string } | null;
  // A lower bound of the server time when it was created (ms), or null
  // when no server time was known: then, once in doubt, it cannot be
  // retried (the window cannot be proven).
  serverCreatedAt: number | null;
  // The retry window has passed (by any clock). Sticky: a clock that moves
  // back later does not reopen it.
  retryExpired: boolean;
  check: OperationCheck | null;
  resolvedAt: string | null;
  // Incremented on every change; decides merges between tabs.
  revision: number;
}

export interface PendingOperationsSnapshot {
  operations: readonly PendingOperation[];
  // Everything known is saved in storage. false while storage is missing,
  // blocked or full, until a write actually succeeds again.
  persistent: boolean;
  // Some records exist only in this tab's memory.
  unsaved: boolean;
  storageAvailable: boolean;
  // Stored data that could not be read was kept aside (or left untouched).
  recoveredCorruptData: boolean;
}

export const STORAGE_KEY = "restaurantos.cashier.pendingOperations.v2";
// Where earlier builds kept the records (bare list, or the v2 object):
// merged into STORAGE_KEY and removed once that write succeeded.
export const LEGACY_STORAGE_KEY = "restaurantos.cashier.pendingOperations.v1";
const CORRUPT_KEY_PREFIX = "restaurantos.cashier.pendingOperations.corrupt.";
const STORAGE_LOCK = "restaurantos.cashier.pendingOperations";
const OPERATION_LOCK_PREFIX = "restaurantos.cashier.operation.";

// The backend keeps idempotency keys for 24 h after it first stores them.
// Retries stop at 20 h (4 h margin for clock error).
export const BACKEND_KEY_LIFETIME_MS = 24 * 60 * 60 * 1000;
export const RETRY_WINDOW_MS = 20 * 60 * 60 * 1000;
// A device clock that went back further than this since the operation was
// created cannot be trusted to measure the window.
export const CLOCK_TOLERANCE_MS = 5 * 60 * 1000;
// Added to a fetched server time (Date rounds down to the second, plus the
// time until the request leaves) and subtracted from the creation anchor.
export const SERVER_TIME_MARGIN_MS = 5_000;
export const REQUEST_TIMEOUT_MS = 20_000;
export const HEARTBEAT_MS = 10_000;
// Background tabs may run timers only once a minute: a heartbeat older
// than this means the sending tab is gone.
export const STALE_SENDING_MS = 90_000;
// Confirmed, checked and refused records are kept this long, then purged.
export const RETENTION_MS = 7 * 24 * 60 * 60 * 1000;
const TOMBSTONE_RETENTION_MS = 30 * 24 * 60 * 60 * 1000;
const REFRESH_MS = 15_000;
const UPKEEP_MS = 60_000;

// Field names that must never reach storage, compared without case and
// separators ("Card-Number", "card_number" and "cardNumber" are one).
const SENSITIVE_KEYS = new Set([
  "token",
  "accesstoken",
  "refreshtoken",
  "idtoken",
  "authorization",
  "password",
  "passcode",
  "secret",
  "pin",
  "pinblock",
  "pan",
  "cardnumber",
  "cardno",
  "cardnum",
  "primaryaccountnumber",
  "accountnumber",
  "cvv",
  "cvv2",
  "cvc",
  "cvc2",
  "cvn",
  "csc",
  "cid",
  "securitycode",
  "cardsecuritycode",
  "verificationcode",
  "expiry",
  "expirydate",
  "expiration",
  "expirationdate",
  "expmonth",
  "expyear",
  "trackdata",
  "track1",
  "track2",
  "magstripe",
]);
// Parts that mark a field as sensitive wherever they appear in its name.
const SENSITIVE_PARTS = ["password", "token", "secret", "cardnumber", "cvv", "cvc", "securitycode", "trackdata"];

export function isSensitiveFieldName(key: string): boolean {
  const name = key.toLowerCase().replace(/[^a-z0-9]/g, "");
  return SENSITIVE_KEYS.has(name) || SENSITIVE_PARTS.some((part) => name.includes(part));
}

/** Why an operation may not be sent now; null when it may. */
export type RetryBlock =
  | "other_user"
  | "in_flight"
  | "failed"
  | "checked"
  | "resolved"
  | "expired"
  // In doubt, but no server time was known at creation: unprovable.
  | "unverifiable"
  // In doubt and no fresh server time could be obtained now.
  | "no_server_time"
  | null;

type OperationOwner = Pick<CurrentUser, "id" | "name" | "restaurant_id">;
type UserRef = Pick<CurrentUser, "id" | "restaurant_id">;

/** The part of the Web Locks API used here (navigator.locks). */
export interface LockManagerLike {
  request<T>(
    name: string,
    options: { mode?: "exclusive"; ifAvailable?: boolean },
    callback: (lock: unknown) => Promise<T> | T,
  ): Promise<T>;
  // Which locks are held, by any tab of this origin.
  query?(): Promise<{ held?: Array<{ name?: string }> }>;
}

interface StoreOptions {
  storage?: () => Storage | null;
  now?: () => Date;
  // null: no Web Locks (in-tab queue only).
  locks?: LockManagerLike | null;
  tabId?: string;
  // Estimated server time now (only used as a lower bound), or null.
  serverNow?: () => number | null;
  // A fresh server time from the server, or null if none could be obtained.
  fetchServerTime?: (token: string) => Promise<number | null>;
  // BroadcastChannel for confirmations between tabs; null disables it.
  channelName?: string | null;
}

function defaultStorage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

const CHANNEL_NAME = "restaurantos.cashier.pendingOperations";

function openChannel(name: string | null): BroadcastChannel | null {
  if (name === null || typeof BroadcastChannel === "undefined") return null;
  try {
    const channel = new BroadcastChannel(name);
    // Node (tests) would otherwise keep the process alive for it.
    (channel as unknown as { unref?: () => void }).unref?.();
    return channel;
  } catch {
    return null;
  }
}

function defaultLocks(): LockManagerLike | null {
  const locks = typeof navigator !== "undefined" ? (navigator as { locks?: unknown }).locks : undefined;
  return locks && typeof (locks as LockManagerLike).request === "function" ? (locks as LockManagerLike) : null;
}

/**
 * A fresh server time: the Date header of THIS GET /api/v1/me response (an
 * existing, read-only, authenticated endpoint), requested with
 * cache: "no-store". null when the request fails, the answer is not a
 * success, or its Date is missing or malformed — another response arriving
 * meanwhile is never used instead. A 401 is rethrown (the session ended).
 */
export async function fetchServerTimeFromMe(token: string): Promise<number | null> {
  let date: number | null = null;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    await apiRequest<unknown>("/api/v1/me", {
      token,
      signal: controller.signal,
      cache: "no-store",
      onResponse: (response) => {
        if (response.ok) date = parseServerDate(response.headers.get("Date"));
      },
    });
  } catch (error) {
    if (isApiError(error) && error.isUnauthorized) throw error;
    return null;
  } finally {
    clearTimeout(timer);
  }
  return date;
}

/** The exact JSON text the request sends (empty without a body). */
export function serializeBody(body: unknown): string {
  return body === undefined ? "" : JSON.stringify(body);
}

export function hashBody(body: unknown): string {
  return sha256Hex(serializeBody(body));
}

function luhnValid(digits: string): boolean {
  let sum = 0;
  for (let i = 0; i < digits.length; i++) {
    let digit = Number(digits[digits.length - 1 - i]);
    if (i % 2 === 1) {
      digit *= 2;
      if (digit > 9) digit -= 9;
    }
    sum += digit;
  }
  return sum % 10 === 0;
}

const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi;
// 13–19 digits, optionally grouped by single spaces, dots or dashes, not
// followed by a letter or digit. "Not preceded by a letter or digit" is
// checked by hand in cardCandidates: regex lookbehind is not supported by
// Safari before 16.4 and would stop the whole bundle from loading there.
const CARD_CANDIDATE_SOURCE = "\\d(?:[ .-]?\\d){12,18}(?![0-9A-Za-z])";
const ALPHANUMERIC = /[0-9A-Za-z]/;

/**
 * Every candidate the pattern `(?<![0-9A-Za-z])\d(?:[ .-]?\d){12,18}(?![0-9A-Za-z])`
 * would match, found without lookbehind: a match glued to a preceding
 * letter or digit is skipped and the search resumes one character later,
 * exactly as the regex engine would retry there.
 */
export function cardCandidates(text: string): string[] {
  const pattern = new RegExp(CARD_CANDIDATE_SOURCE, "g");
  const found: string[] = [];
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text)) !== null) {
    const before = match.index > 0 ? text[match.index - 1] : "";
    if (before !== "" && ALPHANUMERIC.test(before)) {
      pattern.lastIndex = match.index + 1;
      continue;
    }
    found.push(match[0]);
  }
  return found;
}

/** A card number in free text or as a number (13–19 digits, Luhn-valid). */
export function looksLikeCardNumber(value: string | number): boolean {
  if (typeof value === "number") {
    if (!Number.isInteger(value) || value < 0) return false;
    const digits = BigInt(value).toString();
    return digits.length >= 13 && digits.length <= 19 && luhnValid(digits);
  }
  // Identifiers (UUIDs) contain digit runs that are not card numbers.
  const text = value.replace(UUID, " ");
  for (const candidate of cardCandidates(text)) {
    const digits = candidate.replace(/[ .-]/g, "");
    if (digits.length >= 13 && digits.length <= 19 && luhnValid(digits)) return true;
  }
  return false;
}

/** Throws when the body carries a credential-like field or a card number. */
export function assertNoSensitiveData(value: unknown, path = "body"): void {
  if (typeof value === "string" || typeof value === "number") {
    if (looksLikeCardNumber(value)) throw new Error(`Refusing to store what looks like a card number in ${path}.`);
    return;
  }
  if (Array.isArray(value)) {
    value.forEach((entry, index) => assertNoSensitiveData(entry, `${path}[${index}]`));
    return;
  }
  if (typeof value !== "object" || value === null) return;
  for (const [key, entry] of Object.entries(value)) {
    if (isSensitiveFieldName(key)) {
      throw new Error(`Refusing to store sensitive field ${path}.${key}.`);
    }
    assertNoSensitiveData(entry, `${path}.${key}`);
  }
}

const STATUSES: readonly PendingOperationStatus[] = ["pending", "sending", "unknown", "failed", "checked", "succeeded"];
const TERMINAL: ReadonlySet<PendingOperationStatus> = new Set(["failed", "checked", "succeeded"]);
const UNRESOLVED: ReadonlySet<PendingOperationStatus> = new Set(["pending", "sending", "unknown", "failed"]);
// On a tie between two tabs' copies, the safer reading wins.
const PRECEDENCE: Record<PendingOperationStatus, number> = {
  pending: 0,
  failed: 1,
  sending: 2,
  unknown: 3,
  checked: 4,
  succeeded: 5,
};

const isString = (value: unknown): value is string => typeof value === "string";

function normalizeError(value: unknown): PendingOperationError | null {
  if (typeof value !== "object" || value === null) return null;
  const error = value as Record<string, unknown>;
  return {
    status: typeof error.status === "number" ? error.status : 0,
    code: isString(error.code) ? error.code : "UNKNOWN",
    message: isString(error.message) ? error.message : "",
    requestId: isString(error.requestId) ? error.requestId : null,
  };
}

function normalizeCheck(value: unknown): OperationCheck | null {
  if (typeof value !== "object" || value === null) return null;
  const check = value as Record<string, unknown>;
  if (!isString(check.checkedByUserId) || !isString(check.checkedAt)) return null;
  return {
    checkedByUserId: check.checkedByUserId,
    checkedByName: isString(check.checkedByName) ? check.checkedByName : "",
    checkedAt: check.checkedAt,
    note: isString(check.note) ? check.note : null,
    previousStatus: STATUSES.includes(check.previousStatus as PendingOperationStatus)
      ? (check.previousStatus as PendingOperationStatus)
      : "unknown",
    previousError: normalizeError(check.previousError),
  };
}

/** A stored record, with defaults for fields older versions lacked; null if unusable. */
function normalize(value: unknown): PendingOperation | null {
  if (typeof value !== "object" || value === null) return null;
  const op = value as Record<string, unknown>;
  if (
    !isString(op.id) ||
    !isString(op.userId) ||
    !isString(op.restaurantId) ||
    !isString(op.type) ||
    op.method !== "POST" ||
    !isString(op.path) ||
    !isString(op.bodyHash) ||
    !isString(op.idempotencyKey) ||
    !isString(op.createdAt) ||
    !STATUSES.includes(op.status as PendingOperationStatus)
  ) {
    return null;
  }
  const status = op.status as PendingOperationStatus;
  const sendingBy =
    typeof op.sendingBy === "object" &&
    op.sendingBy !== null &&
    isString((op.sendingBy as Record<string, unknown>).tabId) &&
    isString((op.sendingBy as Record<string, unknown>).heartbeatAt)
      ? (op.sendingBy as PendingOperation["sendingBy"])
      : null;
  return {
    id: op.id,
    userId: op.userId,
    userName: isString(op.userName) ? op.userName : "",
    restaurantId: op.restaurantId,
    type: op.type as CashierOperationType,
    method: "POST",
    path: op.path,
    // An expired record never keeps a body, whatever wrote it.
    ...("body" in op && op.body !== undefined && op.retryExpired !== true ? { body: op.body } : {}),
    bodyHash: op.bodyHash,
    idempotencyKey: op.idempotencyKey,
    createdAt: op.createdAt,
    updatedAt: isString(op.updatedAt) ? op.updatedAt : op.createdAt,
    status,
    attempts: typeof op.attempts === "number" && op.attempts >= 0 ? op.attempts : 0,
    // Records without the flag (version 1): anything that was ever in doubt is.
    mayHaveCommitted:
      typeof op.mayHaveCommitted === "boolean"
        ? op.mayHaveCommitted || status === "unknown" || status === "checked"
        : status === "unknown" || status === "sending" || status === "checked",
    lastError: normalizeError(op.lastError),
    sendingBy,
    serverCreatedAt:
      typeof op.serverCreatedAt === "number" && Number.isFinite(op.serverCreatedAt) ? op.serverCreatedAt : null,
    retryExpired: op.retryExpired === true,
    check: normalizeCheck(op.check),
    resolvedAt: isString(op.resolvedAt) ? op.resolvedAt : null,
    revision: typeof op.revision === "number" ? op.revision : 0,
  };
}

function normalizeTombstone(value: unknown): Tombstone | null {
  // Older builds: only the time (removes any revision), or no kind. Both are
  // read as dismissals, which never delete an operation in doubt.
  if (isString(value)) return { at: value, revision: Number.MAX_SAFE_INTEGER, kind: "dismissed" };
  if (typeof value !== "object" || value === null) return null;
  const entry = value as Record<string, unknown>;
  if (!isString(entry.at) || typeof entry.revision !== "number") return null;
  return { at: entry.at, revision: entry.revision, kind: entry.kind === "purged" ? "purged" : "dismissed" };
}

/** The stronger of two tombstones for one id: higher revision, then retention. */
function strongerTombstone(a: Tombstone, b: Tombstone): Tombstone {
  if (a.revision !== b.revision) return a.revision > b.revision ? a : b;
  if (a.kind !== b.kind) return a.kind === "purged" ? a : b;
  return a.at >= b.at ? a : b;
}

/** A creation anchor that is never later than the true server time. */
function anchorFrom(serverNow: number | null): number | null {
  return serverNow === null ? null : serverNow - SERVER_TIME_MARGIN_MS;
}

/**
 * Whether `user` may record checks of other employees' operations
 * (management: the backend gives cash_reports:view to managers only).
 */
export function mayCheckOthersOperations(user: Pick<CurrentUser, "permissions">): boolean {
  return user.permissions.includes(Permission.CashReportsView);
}

/** A key order-independent JSON text, for a total order of two copies. */
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (typeof value === "object" && value !== null) {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, entry]) => entry !== undefined)
      .sort(([x], [y]) => (x < y ? -1 : x > y ? 1 : 0));
    return `{${entries.map(([key, entry]) => `${JSON.stringify(key)}:${canonical(entry)}`).join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

/**
 * The copy whose fields win: higher revision, then the safer status, then
 * the later update, more attempts, and finally the larger canonical text —
 * a total order, so the result never depends on argument order.
 */
function newerCopy(a: PendingOperation, b: PendingOperation): PendingOperation {
  if (a.revision !== b.revision) return a.revision > b.revision ? a : b;
  if (PRECEDENCE[a.status] !== PRECEDENCE[b.status]) return PRECEDENCE[a.status] > PRECEDENCE[b.status] ? a : b;
  if (a.updatedAt !== b.updatedAt) return a.updatedAt > b.updatedAt ? a : b;
  if (a.attempts !== b.attempts) return a.attempts > b.attempts ? a : b;
  return canonical(a) >= canonical(b) ? a : b;
}

/**
 * Two copies of one record (two tabs). Monotonic: never loses uncertainty,
 * a recorded check, an authoritative success or an expiry.
 */
export function mergeRecord(a: PendingOperation, b: PendingOperation): PendingOperation {
  const winner = newerCopy(a, b);
  const other = winner === a ? b : a;

  const mayHaveCommitted = a.mayHaveCommitted || b.mayHaveCommitted;
  const retryExpired = a.retryExpired || b.retryExpired;
  const check = winner.check ?? other.check;
  let status = winner.status;
  let resolvedAt = winner.resolvedAt;
  if (a.status === "succeeded" || b.status === "succeeded") {
    // The server's confirmation is a fact, whichever copy is newer.
    status = "succeeded";
    resolvedAt = winner.resolvedAt ?? other.resolvedAt;
  } else if (a.status === "checked" || b.status === "checked") {
    // After a person's check nothing is retried any more.
    status = "checked";
  } else if (mayHaveCommitted && (status === "pending" || status === "failed")) {
    // A refusal recorded without knowing that an attempt may have committed.
    status = "unknown";
  }
  const anchors = [a.serverCreatedAt, b.serverCreatedAt].filter((value): value is number => value !== null);
  const merged: PendingOperation = {
    ...winner,
    status,
    mayHaveCommitted,
    retryExpired,
    check,
    resolvedAt,
    sendingBy: status === "sending" ? winner.sendingBy : null,
    // The earlier anchor is the safer one.
    serverCreatedAt: anchors.length > 0 ? Math.min(...anchors) : null,
    revision: Math.max(a.revision, b.revision),
  };
  if (retryExpired || status === "checked" || status === "succeeded" || status === "failed") delete merged.body;
  return merged;
}

/** A removal record: when, the revision that was removed, and why. */
export interface Tombstone {
  at: string;
  revision: number;
  // dismissed: an operation that certainly changed nothing was removed by
  // its owner. purged: a resolved (confirmed, checked or refused) record
  // was removed by retention.
  kind: "dismissed" | "purged";
}

/** Whether a tombstone may remove this copy (invariant 8). */
export function tombstoneApplies(op: PendingOperation, tombstone: Tombstone): boolean {
  // Never a copy changed after the removal.
  if (op.revision > tombstone.revision) return false;
  // The record had been resolved at that revision: every older copy is
  // history from before the resolution.
  if (tombstone.kind === "purged") return true;
  return !(op.mayHaveCommitted && UNRESOLVED.has(op.status));
}

interface StoredState {
  operations: Map<string, PendingOperation>;
  // id → removal; keeps stale copies in other tabs from coming back.
  removed: Record<string, Tombstone>;
  // Entries that could not be read; written back unchanged, never dropped.
  invalid: unknown[];
}

type ReadResult =
  // legacy: whether the earlier key was read and may be removed after a
  // successful write ("migrate"), must stay ("keep"), or was absent.
  // writable: whether the current key may be overwritten (false when it
  // held unreadable data that could not be copied aside).
  | { kind: "ok"; state: StoredState; hadInvalid: boolean; legacy: "none" | "migrate" | "keep"; writable: boolean }
  | { kind: "unavailable" }
  // Unreadable as a whole: writing would destroy it unless it was copied.
  | { kind: "corrupt"; preserved: boolean };

export class PendingOperationStore {
  private readonly getStorage: () => Storage | null;
  private readonly now: () => Date;
  private readonly locks: LockManagerLike | null;
  private readonly serverNow: () => number | null;
  private readonly fetchServerTimeFn: (token: string) => Promise<number | null>;
  readonly tabId: string;

  private memory = new Map<string, PendingOperation>();
  private removed: Record<string, Tombstone> = {};
  // Requests this tab is sending right now.
  private readonly inFlight = new Set<string>();
  // Operation id → device time when Web Locks showed its lock free.
  private readonly lockFreeSince = new Map<string, number>();
  private unsaved = false;
  private storageAvailable = true;
  private recoveredCorruptData = false;
  private queue: Promise<unknown> = Promise.resolve();
  private snapshot: PendingOperationsSnapshot = {
    operations: [],
    persistent: true,
    unsaved: false,
    storageAvailable: true,
    recoveredCorruptData: false,
  };
  private readonly listeners = new Set<() => void>();
  private refreshTimer: number | undefined;
  private readonly channel: BroadcastChannel | null;

  constructor(options: StoreOptions = {}) {
    this.getStorage = options.storage ?? defaultStorage;
    this.now = options.now ?? (() => new Date());
    this.locks = options.locks === undefined ? defaultLocks() : options.locks;
    this.tabId = options.tabId ?? newIdempotencyKey();
    this.serverNow = options.serverNow ?? (() => estimateServerNow());
    this.fetchServerTimeFn = options.fetchServerTime ?? fetchServerTimeFromMe;
    this.channel = openChannel(options.channelName === undefined ? CHANNEL_NAME : options.channelName);
    if (this.channel) this.channel.onmessage = (event: MessageEvent) => this.onConfirmation(event.data);
    this.reconcile();
  }

  /**
   * Another tab of this origin got a server confirmation (it may not have
   * been able to save it): applied to this tab's memory, saved with the
   * next write. Only ever moves a record to "succeeded".
   */
  private onConfirmation(message: unknown): void {
    if (typeof message !== "object" || message === null) return;
    const { type, id, resolvedAt } = message as Record<string, unknown>;
    if (type !== "confirmed" || !isString(id) || !isString(resolvedAt)) return;
    const op = this.memory.get(id);
    if (!op || op.status === "succeeded") return;
    const operations = new Map(this.memory);
    const next = this.revise(op, { status: "succeeded", sendingBy: null, resolvedAt });
    delete next.body;
    operations.set(id, next);
    this.publish({ operations, removed: this.removed, invalid: [] });
  }

  getSnapshot = (): PendingOperationsSnapshot => this.snapshot;

  subscribe = (listener: () => void): (() => void) => {
    if (this.listeners.size === 0 && typeof window !== "undefined") {
      window.addEventListener("storage", this.onStorage);
      // Re-evaluates abandoned requests and the retry window as time passes.
      this.refreshTimer = window.setInterval(() => {
        this.reconcile();
        void this.refreshLiveness();
        void this.maintain().catch(() => undefined);
      }, REFRESH_MS);
    }
    this.listeners.add(listener);
    // Another tab may have written while nobody here was listening.
    this.reconcile();
    void this.refreshLiveness();
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size === 0 && typeof window !== "undefined") {
        window.removeEventListener("storage", this.onStorage);
        window.clearInterval(this.refreshTimer);
      }
    };
  };

  private onStorage = (event: StorageEvent) => {
    if (event.key === STORAGE_KEY || event.key === null) this.reconcile();
  };

  // --- Reading and merging ---

  private readStorage(): ReadResult {
    const storage = this.getStorage();
    if (!storage) return { kind: "unavailable" };
    let raw: string | null;
    let legacyRaw: string | null;
    try {
      raw = storage.getItem(STORAGE_KEY);
      legacyRaw = storage.getItem(LEGACY_STORAGE_KEY);
    } catch {
      return { kind: "unavailable" };
    }
    const parsed = this.parseStored(storage, raw);
    // An unreadable current key was copied aside (diagnostics) when
    // possible; the earlier key's records are still read and shown.
    const current: Extract<ReadResult, { kind: "ok" }> =
      parsed.kind === "ok"
        ? parsed
        : {
            kind: "ok",
            state: { operations: new Map(), removed: {}, invalid: [] },
            hadInvalid: true,
            legacy: "none",
            writable: parsed.kind === "corrupt" && parsed.preserved,
          };
    if (legacyRaw === null) return current;

    // Migration: records of the earlier key are merged in, never dropped.
    const legacy = this.parseStored(storage, legacyRaw);
    if (legacy.kind !== "ok") {
      // Unreadable legacy data stays where it is (and was copied aside).
      return { ...current, hadInvalid: true, legacy: "keep" };
    }
    const state = current.state;
    for (const [id, op] of legacy.state.operations) {
      const known = state.operations.get(id);
      state.operations.set(id, known ? mergeRecord(known, op) : op);
    }
    for (const [id, tombstone] of Object.entries(legacy.state.removed)) {
      const known = state.removed[id];
      state.removed[id] = known ? strongerTombstone(known, tombstone) : tombstone;
    }
    state.invalid.push(...legacy.state.invalid);
    return {
      kind: "ok",
      state,
      hadInvalid: current.hadInvalid || legacy.hadInvalid,
      legacy: "migrate",
      writable: current.writable,
    };
  }

  private parseStored(storage: Storage, raw: string | null): ReadResult {
    const empty: StoredState = { operations: new Map(), removed: {}, invalid: [] };
    if (!raw) return { kind: "ok", state: empty, hadInvalid: false, legacy: "none", writable: true };

    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      return { kind: "corrupt", preserved: this.setAside(storage, raw) };
    }
    // The first format stored a bare list; version 2 an object.
    let entries: unknown;
    const removed: Record<string, Tombstone> = {};
    let invalid: unknown[] = [];
    if (Array.isArray(parsed)) {
      entries = parsed;
    } else if (typeof parsed === "object" && parsed !== null && (parsed as { version?: unknown }).version === 2) {
      const value = parsed as { operations?: unknown; removed?: unknown; invalid?: unknown };
      entries = value.operations;
      if (typeof value.removed === "object" && value.removed !== null) {
        for (const [id, entry] of Object.entries(value.removed as Record<string, unknown>)) {
          const tombstone = normalizeTombstone(entry);
          if (tombstone) removed[id] = tombstone;
        }
      }
      if (Array.isArray(value.invalid)) invalid = [...value.invalid];
    } else {
      return { kind: "corrupt", preserved: this.setAside(storage, raw) };
    }
    if (!Array.isArray(entries)) return { kind: "corrupt", preserved: this.setAside(storage, raw) };

    const operations = new Map<string, PendingOperation>();
    let hadInvalid = invalid.length > 0;
    for (const entry of entries) {
      const op = normalize(entry);
      if (op) {
        const existing = operations.get(op.id);
        operations.set(op.id, existing ? mergeRecord(existing, op) : op);
      } else {
        invalid.push(entry);
        hadInvalid = true;
      }
    }
    return { kind: "ok", state: { operations, removed, invalid }, hadInvalid, legacy: "none", writable: true };
  }

  /** Copies unreadable data to its own key (once per content); whether it is safe now. */
  private setAside(storage: Storage, raw: string): boolean {
    const key = `${CORRUPT_KEY_PREFIX}${sha256Hex(raw).slice(0, 16)}`;
    try {
      if (storage.getItem(key) === raw) return true;
      storage.setItem(key, raw);
      return true;
    } catch {
      return false;
    }
  }

  /** Storage merged with this tab's memory; nothing is dropped. */
  private merged(read: ReadResult): StoredState {
    const operations = new Map<string, PendingOperation>();
    const removed: Record<string, Tombstone> = { ...this.removed };
    let invalid: unknown[] = [];
    if (read.kind === "ok") {
      for (const [id, tombstone] of Object.entries(read.state.removed)) {
        const known = removed[id];
        removed[id] = known ? strongerTombstone(known, tombstone) : tombstone;
      }
      invalid = read.state.invalid;
      for (const [id, op] of read.state.operations) operations.set(id, op);
    }
    for (const [id, op] of this.memory) {
      const stored = operations.get(id);
      operations.set(id, stored ? mergeRecord(stored, op) : op);
    }
    for (const [id, tombstone] of Object.entries(removed)) {
      const op = operations.get(id);
      if (!op) continue;
      if (tombstoneApplies(op, tombstone)) operations.delete(id);
      // The operation is in doubt or newer than the removal: it stays, and
      // the conflicting tombstone goes.
      else delete removed[id];
    }
    return { operations, removed, invalid };
  }

  private publish(state: StoredState): void {
    this.memory = state.operations;
    this.removed = state.removed;
    this.snapshot = {
      operations: [...state.operations.values()].sort((a, b) => a.createdAt.localeCompare(b.createdAt)),
      persistent: this.storageAvailable && !this.unsaved,
      unsaved: this.unsaved,
      storageAvailable: this.storageAvailable,
      recoveredCorruptData: this.recoveredCorruptData,
    };
    for (const listener of this.listeners) listener();
  }

  /** Re-reads storage and merges it into memory (no write). */
  reconcile(): void {
    const read = this.readStorage();
    this.noteRead(read);
    this.publish(this.merged(read));
  }

  private noteRead(read: ReadResult): void {
    this.storageAvailable = read.kind !== "unavailable";
    if (read.kind === "corrupt" || (read.kind === "ok" && read.hadInvalid)) this.recoveredCorruptData = true;
  }

  // --- Writing ---

  private withStorageLock<T>(fn: () => T): Promise<T> {
    if (this.locks) return this.locks.request(STORAGE_LOCK, { mode: "exclusive" }, () => fn());
    // No Web Locks: serialize within this tab; the read-merge-write below
    // runs synchronously, which keeps the cross-tab window small.
    const run = this.queue.then(fn, fn);
    this.queue = run.catch(() => undefined);
    return run;
  }

  /**
   * Re-reads storage, merges, applies `change` and writes everything back,
   * as one step under the storage lock. `change` may throw: nothing is
   * written then.
   */
  private mutate<T>(change: (operations: Map<string, PendingOperation>, removed: Record<string, Tombstone>) => T): Promise<T> {
    return this.withStorageLock(() => {
      const read = this.readStorage();
      this.noteRead(read);
      const state = this.merged(read);
      this.recoverAbandoned(state.operations);
      const result = change(state.operations, state.removed);
      this.purge(state);
      this.persist(read, state);
      this.publish(state);
      return result;
    });
  }

  private persist(read: ReadResult, state: StoredState): void {
    const storage = this.getStorage();
    // Never overwrite data that could not be read and could not be copied.
    if (
      !storage ||
      read.kind === "unavailable" ||
      (read.kind === "corrupt" && !read.preserved) ||
      (read.kind === "ok" && !read.writable)
    ) {
      this.unsaved = true;
      return;
    }
    try {
      storage.setItem(
        STORAGE_KEY,
        JSON.stringify({
          version: 2,
          operations: [...state.operations.values()],
          removed: state.removed,
          invalid: state.invalid,
        }),
      );
      this.unsaved = false;
    } catch {
      this.unsaved = true;
      return;
    }
    if (read.kind === "ok" && read.legacy === "migrate") {
      try {
        storage.removeItem(LEGACY_STORAGE_KEY);
      } catch {
        // Read and merged again next time; nothing is lost.
      }
    }
  }

  private recoverAbandoned(operations: Map<string, PendingOperation>): void {
    for (const [id, op] of operations) {
      // Only once the heartbeat is stale: a request whose lock is free but
      // which ended moments ago may have a confirmation its tab could not
      // save yet ("being recovered"); its record is left as it is.
      if (op.status === "sending" && !this.isLive(op) && !this.isRecovering(op)) {
        operations.set(id, this.revise(op, { status: "unknown", mayHaveCommitted: true, sendingBy: null }));
      }
    }
  }

  private purge(state: StoredState): void {
    const now = this.now().getTime();
    for (const [id, op] of state.operations) {
      const updated = Date.parse(op.updatedAt);
      if (TERMINAL.has(op.status) && Number.isFinite(updated) && now - updated > RETENTION_MS) {
        state.operations.delete(id);
        state.removed[id] = { at: new Date(now).toISOString(), revision: op.revision, kind: "purged" };
        continue;
      }
      // Certainly past the retry window by server time: it can never be
      // retried, so the body goes and the expiry sticks.
      if (op.status !== "sending" && !op.retryExpired && this.expiredByServerTime(op, this.serverNow())) {
        const next = this.revise(op, { retryExpired: true });
        delete next.body;
        state.operations.set(id, next);
      }
    }
    for (const [id, tombstone] of Object.entries(state.removed)) {
      const time = Date.parse(tombstone.at);
      if (Number.isFinite(time) && now - time > TOMBSTONE_RETENTION_MS) delete state.removed[id];
    }
  }

  private revise(op: PendingOperation, changes: Partial<PendingOperation>): PendingOperation {
    return { ...op, ...changes, revision: op.revision + 1, updatedAt: this.now().toISOString() };
  }

  // --- Queries ---

  get(id: string): PendingOperation | undefined {
    return this.memory.get(id);
  }

  /** Whether a "sending" record's request is still running somewhere. */
  isLive(op: PendingOperation): boolean {
    if (op.status !== "sending" || !op.sendingBy) return false;
    if (op.sendingBy.tabId === this.tabId) return this.inFlight.has(op.id);
    const heartbeat = Date.parse(op.sendingBy.heartbeatAt);
    if (!Number.isFinite(heartbeat)) return false;
    // Web Locks said nobody held its lock after this claim was made: every
    // sender holds the operation's lock for the whole request.
    const lockFreeAt = this.lockFreeSince.get(op.id);
    if (lockFreeAt !== undefined && heartbeat < lockFreeAt) return false;
    const age = this.now().getTime() - heartbeat;
    // A heartbeat from the future (clock moved back) is not trusted for long.
    return age <= STALE_SENDING_MS && age >= -STALE_SENDING_MS;
  }

  /**
   * A "sending" record whose request is no longer running (its lock is
   * free) but whose heartbeat is recent: its tab may hold a server answer it
   * could not save yet. Shown as "being recovered"; retry (same key) is
   * allowed, a manual check is not until the heartbeat is stale.
   */
  isRecovering(op: PendingOperation): boolean {
    if (op.status !== "sending" || !op.sendingBy || this.isLive(op)) return false;
    if (op.sendingBy.tabId === this.tabId) return false;
    const heartbeat = Date.parse(op.sendingBy.heartbeatAt);
    if (!Number.isFinite(heartbeat)) return false;
    const age = this.now().getTime() - heartbeat;
    return age <= STALE_SENDING_MS && age >= -STALE_SENDING_MS;
  }

  /** The status to act on: an abandoned "sending" is "unknown". */
  effectiveStatus(op: PendingOperation): PendingOperationStatus {
    return op.status === "sending" && !this.isLive(op) ? "unknown" : op.status;
  }

  /**
   * Whether server time proves the window has passed: `serverNow` must be
   * a lower bound of the server time (a Date header or the estimate), and
   * the anchor is a lower bound of the creation time. Never the device clock.
   */
  expiredByServerTime(op: PendingOperation, serverNow: number | null): boolean {
    if (op.status === "succeeded" || op.status === "checked") return false;
    return op.serverCreatedAt !== null && serverNow !== null && serverNow - op.serverCreatedAt > RETRY_WINDOW_MS;
  }

  /** Whether the operation can only be retried with a fresh server time. */
  needsServerTime(op: PendingOperation): boolean {
    return op.mayHaveCommitted || this.effectiveStatus(op) === "unknown";
  }

  /**
   * Whether a retry is still safely inside the backend's key lifetime.
   * For anything that may have been committed only server time counts:
   * `serverNow` must be a FRESH server time and the record must have a
   * server anchor. Operations never in doubt cannot run twice; for them
   * server time is used when known and the device clock otherwise.
   */
  retryWindow(op: PendingOperation, serverNow: number | null): "open" | "expired" | "unverifiable" | "no_server_time" {
    if (op.retryExpired) return "expired";
    if (!Number.isFinite(Date.parse(op.createdAt))) return "unverifiable";
    if (this.needsServerTime(op)) {
      if (op.serverCreatedAt === null) return "unverifiable";
      if (serverNow === null) return "no_server_time";
      // serverNow is rounded down: the margin makes it an upper bound.
      const serverElapsed = serverNow + SERVER_TIME_MARGIN_MS - op.serverCreatedAt;
      if (serverElapsed > RETRY_WINDOW_MS) return "expired";
      // Server time before the creation anchor: the anchor cannot be right.
      if (serverElapsed < -CLOCK_TOLERANCE_MS) return "unverifiable";
      return "open";
    }
    const server = serverNow ?? this.serverNow();
    if (op.serverCreatedAt !== null && server !== null) {
      return server + SERVER_TIME_MARGIN_MS - op.serverCreatedAt > RETRY_WINDOW_MS ? "expired" : "open";
    }
    const deviceElapsed = this.now().getTime() - Date.parse(op.createdAt);
    return deviceElapsed > RETRY_WINDOW_MS || deviceElapsed < -CLOCK_TOLERANCE_MS ? "expired" : "open";
  }

  /**
   * Whether `user` may send the operation now, and if not, why. Without
   * `serverNow` the estimate is used: good enough for showing a button,
   * never for sending (beginSend gets a fresh time).
   */
  retryBlock(op: PendingOperation, user: UserRef, serverNow: number | null = this.serverNow()): RetryBlock {
    // (Display only when serverNow is the estimate; sending passes a fresh time.)
    if (op.userId !== user.id || op.restaurantId !== user.restaurant_id) return "other_user";
    const status = this.effectiveStatus(op);
    if (status === "succeeded") return "resolved";
    if (status === "checked") return "checked";
    if (status === "failed") return "failed";
    if (status === "sending") return "in_flight";
    const window = this.retryWindow(op, serverNow);
    return window === "open" ? null : window;
  }

  /** A fresh server time, or null; a 401 is rethrown. */
  fetchServerTime(token: string): Promise<number | null> {
    return this.fetchServerTimeFn(token);
  }

  /** Can be removed as never having changed anything (never once in doubt). */
  canDismiss(op: PendingOperation, user: Pick<CurrentUser, "id">): boolean {
    const status = this.effectiveStatus(op);
    return op.userId === user.id && !op.mayHaveCommitted && (status === "pending" || status === "failed");
  }

  // --- Changes ---

  /** Stores a new operation for `owner`; nothing is sent yet. */
  async create(owner: OperationOwner, request: MutationRequest): Promise<PendingOperation> {
    assertNoSensitiveData(request.body);
    // The stored body is exactly what JSON sends (undefined fields dropped).
    const body: unknown = request.body === undefined ? undefined : JSON.parse(JSON.stringify(request.body));
    const timestamp = this.now().toISOString();
    const operation: PendingOperation = {
      id: newIdempotencyKey(),
      userId: owner.id,
      userName: owner.name,
      restaurantId: owner.restaurant_id,
      type: request.type,
      method: request.method,
      path: request.path,
      ...(body === undefined ? {} : { body }),
      bodyHash: hashBody(body),
      idempotencyKey: newIdempotencyKey(),
      createdAt: timestamp,
      updatedAt: timestamp,
      status: "pending",
      attempts: 0,
      mayHaveCommitted: false,
      lastError: null,
      sendingBy: null,
      serverCreatedAt: anchorFrom(this.serverNow()),
      retryExpired: false,
      check: null,
      resolvedAt: null,
      revision: 1,
    };
    await this.mutate((operations) => {
      operations.set(operation.id, operation);
    });
    return operation;
  }

  /**
   * Claims the operation for sending from this tab; returns what to send.
   * `freshServerTime` is the Date of a fresh /me (null for a first send);
   * `holdsOperationLock` says this tab holds the operation's Web Lock, so
   * nobody else is sending it. A refusal that establishes expiry is saved.
   */
  async beginSend(
    id: string,
    user: UserRef,
    freshServerTime: number | null,
    holdsOperationLock = false,
  ): Promise<PendingOperation> {
    const outcome = await this.mutate((operations): PendingOperation | OperationBlockedError => {
      let op = operations.get(id);
      if (!op) return new OperationBlockedError("missing", `No pending operation ${id}.`);
      if (op.status === "sending" && op.sendingBy?.tabId !== this.tabId && holdsOperationLock) {
        // Its sender is gone (the lock was free): an attempt in doubt.
        op = this.revise(op, { status: "unknown", mayHaveCommitted: true, sendingBy: null });
        operations.set(id, op);
      }
      const block = this.retryBlock(op, user, freshServerTime);
      if (block) {
        if (!op.retryExpired && this.needsServerTime(op) && this.expiredByServerTime(op, freshServerTime)) {
          const expired = this.revise(op, { retryExpired: true });
          delete expired.body;
          operations.set(id, expired);
        }
        return new OperationBlockedError(block, BLOCK_MESSAGES[block]);
      }
      if (hashBody(op.body) !== op.bodyHash) {
        return new OperationBlockedError("integrity", "The stored request does not match its hash; it will not be sent.");
      }
      const next = this.revise(op, {
        status: "sending",
        attempts: op.attempts + 1,
        // Retrying something already in doubt keeps it in doubt.
        mayHaveCommitted: op.mayHaveCommitted || op.status === "unknown",
        sendingBy: { tabId: this.tabId, heartbeatAt: this.now().toISOString() },
      });
      operations.set(id, next);
      this.inFlight.add(id);
      return next;
    });
    if (outcome instanceof OperationBlockedError) throw outcome;
    return outcome;
  }

  heartbeat(id: string): Promise<void> {
    return this.mutate((operations) => {
      const op = operations.get(id);
      if (op?.status === "sending" && op.sendingBy?.tabId === this.tabId) {
        operations.set(id, this.revise(op, { sendingBy: { tabId: this.tabId, heartbeatAt: this.now().toISOString() } }));
      }
    });
  }

  /**
   * The server confirmed the operation: authoritative, whatever came before.
   * If storage cannot take it, the confirmation is still kept in this tab's
   * memory (with the "not saved" warning) and saved by the next write; it
   * is never turned back into "unknown" or "not sent".
   */
  async recordSuccess(id: string): Promise<void> {
    const confirm = (operations: Map<string, PendingOperation>) => {
      // Released inside the locked step: until then this tab's own copy
      // must not look abandoned to itself.
      this.inFlight.delete(id);
      const op = operations.get(id);
      if (!op || op.status === "succeeded") return;
      const next = this.revise(op, { status: "succeeded", sendingBy: null, resolvedAt: this.now().toISOString() });
      delete next.body;
      operations.set(id, next);
    };
    try {
      await this.mutate(confirm);
    } catch {
      // The locked write itself failed: record it in memory only.
      const operations = new Map(this.memory);
      confirm(operations);
      this.unsaved = true;
      this.publish({ operations, removed: this.removed, invalid: [] });
    }
    // Tell other tabs at once, whether or not storage took it.
    const resolvedAt = this.memory.get(id)?.resolvedAt;
    if (resolvedAt) {
      try {
        this.channel?.postMessage({ type: "confirmed", id, resolvedAt });
      } catch {
        // Other tabs learn it from storage (or recover it by retrying).
      }
    }
  }

  /** Records a failed attempt; never weakens what is already known. */
  recordFailure(
    id: string,
    outcome: Exclude<PendingOperationStatus, "sending" | "checked" | "succeeded">,
    error: PendingOperationError,
  ): Promise<void> {
    return this.mutate((operations) => {
      this.inFlight.delete(id);
      const op = operations.get(id);
      if (!op || op.status === "succeeded" || op.status === "checked") return;
      const inDoubt = outcome === "unknown" || op.mayHaveCommitted;
      if (op.status === "sending" && op.sendingBy && op.sendingBy.tabId !== this.tabId) {
        // Another tab took the operation over: leave its attempt alone.
        operations.set(id, this.revise(op, { lastError: error, mayHaveCommitted: inDoubt }));
        return;
      }
      const status = inDoubt ? "unknown" : outcome;
      const next = this.revise(op, { status, mayHaveCommitted: inDoubt, sendingBy: null, lastError: error });
      if (status === "failed") delete next.body;
      operations.set(id, next);
    });
  }

  /**
   * Removes an operation that certainly changed nothing: refused by the
   * server or by sign-in on its only attempts, never once in doubt.
   */
  dismiss(id: string, user: Pick<CurrentUser, "id">): Promise<void> {
    return this.mutate((operations, removed) => {
      const op = operations.get(id);
      if (!op) return;
      if (op.userId !== user.id) throw new Error("Only the employee who started it can dismiss this operation.");
      if (!this.canDismiss(op, user)) {
        throw new Error("An operation that may have been completed cannot be dismissed; retry it or record a check.");
      }
      operations.delete(id);
      removed[id] = { at: this.now().toISOString(), revision: op.revision, kind: "dismissed" };
    });
  }

  /**
   * Records that a person checked an unknown operation (e.g. in the
   * register). The record is kept; it does not say what the server did.
   * Never by the operation's own employee; another employee needs
   * cash_reports:view in the same restaurant. Not while it is being sent.
   */
  markChecked(
    id: string,
    verifier: Pick<CurrentUser, "id" | "name" | "restaurant_id" | "permissions">,
    options: { note?: string | null } = {},
  ): Promise<void> {
    const run = (holdsOperationLock: boolean) =>
      this.mutate((operations) => {
        let op = operations.get(id);
        if (!op) throw new Error(`No pending operation ${id}.`);
        if (op.restaurantId !== verifier.restaurant_id) throw new Error("This operation belongs to another restaurant.");
        if (op.userId === verifier.id) {
          throw new Error("You cannot check your own operation; a manager must check it.");
        }
        if (!mayCheckOthersOperations(verifier)) {
          throw new Error("Only a manager can check another employee's operation.");
        }
        if (this.isRecovering(op)) {
          throw new Error("The outcome of this operation is still being recovered; try again in a few minutes.");
        }
        if (op.status === "sending" && op.sendingBy?.tabId !== this.tabId && holdsOperationLock) {
          op = this.revise(op, { status: "unknown", mayHaveCommitted: true, sendingBy: null });
        }
        const status = this.effectiveStatus(op);
        if (status === "sending") throw new Error("The operation is still being sent.");
        if (status !== "unknown") throw new Error("Only an operation with an unknown outcome can be checked.");
        const note = options.note?.trim() ? options.note.trim().slice(0, 500) : null;
        const next = this.revise(op, {
          status: "checked",
          sendingBy: null,
          check: {
            checkedByUserId: verifier.id,
            checkedByName: verifier.name,
            checkedAt: this.now().toISOString(),
            note,
            previousStatus: status,
            previousError: op.lastError,
          },
        });
        // No retry after a check: the body is no longer needed.
        delete next.body;
        operations.set(id, next);
      });
    // With Web Locks, a request in flight anywhere holds the operation's lock.
    return this.locks ? this.withOperationLock(id, () => run(true)) : run(false);
  }

  /** Whether this store uses Web Locks (then every sender holds an operation lock). */
  get usesWebLocks(): boolean {
    return this.locks !== null;
  }

  /**
   * Asks Web Locks which operation locks are held: a "sending" record whose
   * lock is free was abandoned (its tab closed or reloaded), so it need not
   * wait for its heartbeat to go stale. No-op without Web Locks.
   */
  async refreshLiveness(): Promise<void> {
    if (!this.locks?.query) return;
    // Taken BEFORE asking: a claim made while the query is pending has a
    // later heartbeat than this, so it is never treated as abandoned.
    const checkedAt = this.now().getTime();
    let held: Set<string>;
    try {
      const snapshot = await this.locks.query();
      held = new Set((snapshot.held ?? []).map((lock) => lock.name ?? ""));
    } catch {
      return;
    }
    let changed = false;
    for (const op of this.memory.values()) {
      if (op.status !== "sending" || op.sendingBy?.tabId === this.tabId) continue;
      if (held.has(`${OPERATION_LOCK_PREFIX}${op.id}`)) {
        changed = this.lockFreeSince.delete(op.id) || changed;
      } else if (!this.lockFreeSince.has(op.id)) {
        this.lockFreeSince.set(op.id, checkedAt);
        changed = true;
      }
    }
    if (changed) this.reconcile();
  }

  /**
   * Starts the periodic upkeep for a signed-in session (App calls it while
   * someone is signed in, whatever screen is shown); returns the stop
   * function. Upkeep only saves unsaved records and expiry proven by server
   * time and purges finished records: it never retries, checks or shows
   * anything, so it needs no particular user. Every minute; cheap when
   * there is nothing to do.
   */
  startUpkeep(intervalMs = UPKEEP_MS): () => void {
    if (typeof window === "undefined") return () => undefined;
    const timer = window.setInterval(() => void this.maintain().catch(() => undefined), intervalMs);
    return () => window.clearInterval(timer);
  }

  /**
   * Periodic upkeep: saves what exists only in memory and persists expiry
   * that server time has established — without waiting for another write.
   */
  async maintain(): Promise<void> {
    const serverNow = this.serverNow();
    const due = [...this.memory.values()].some(
      (op) => op.status !== "sending" && !op.retryExpired && this.expiredByServerTime(op, serverNow),
    );
    if (this.unsaved || due) await this.mutate(() => undefined);
  }

  /** Forgets this tab's claim if recording the outcome itself failed. */
  release(id: string): void {
    this.inFlight.delete(id);
  }

  // --- Locks for one operation ---

  /**
   * Runs `fn` holding this operation's Web Lock, so that two tabs never
   * send it at once; without Web Locks the heartbeat check alone applies.
   */
  withOperationLock<T>(id: string, fn: () => Promise<T>): Promise<T> {
    if (!this.locks) return fn();
    return this.locks.request(`${OPERATION_LOCK_PREFIX}${id}`, { mode: "exclusive", ifAvailable: true }, (lock) => {
      if (!lock) throw new OperationBlockedError("in_flight", BLOCK_MESSAGES.in_flight);
      return fn();
    });
  }
}

/** How a failed attempt reads on its own; recordFailure combines it with history. */
export function classifyFailure(error: unknown): "unknown" | "pending" | "failed" {
  if (error instanceof DOMException && (error.name === "AbortError" || error.name === "TimeoutError")) {
    return "unknown";
  }
  if (!isApiError(error)) return "unknown";
  if (error.isNetworkError) return "unknown";
  // A success status with an unreadable body: it most likely ran.
  if (error.status >= 200 && error.status < 300) return "unknown";
  // Sign-in is checked before the operation: this attempt did nothing.
  if (error.status === 401) return "pending";
  // The key was already used for another body: whatever ran under it, we
  // cannot tell from here.
  if (error.code === "IDEMPOTENCY_KEY_REUSED") return "unknown";
  if (error.status === 408 || error.status === 429 || error.status >= 500) return "unknown";
  // Other refusals roll the transaction back (see the backend contract).
  if (error.status >= 400) return "failed";
  return "unknown";
}

function toStoredError(error: unknown): PendingOperationError {
  if (isApiError(error)) {
    return { status: error.status, code: error.code, message: error.message, requestId: error.requestId };
  }
  if (error instanceof DOMException) {
    return { status: 0, code: "TIMEOUT", message: "The server did not answer in time.", requestId: null };
  }
  return { status: 0, code: "UNKNOWN", message: error instanceof Error ? error.message : String(error), requestId: null };
}

export class OperationBlockedError extends Error {
  readonly reason: Exclude<RetryBlock, null> | "integrity" | "missing";
  constructor(reason: Exclude<RetryBlock, null> | "integrity" | "missing", message: string) {
    super(message);
    this.name = "OperationBlockedError";
    this.reason = reason;
  }
}

const BLOCK_MESSAGES: Record<Exclude<RetryBlock, null>, string> = {
  other_user: "This operation was started by another employee; only they can retry it.",
  in_flight: "This operation is already being sent.",
  failed: "The server refused this operation; it cannot be retried as it is.",
  checked: "A check was recorded for this operation; it is not retried any more.",
  resolved: "The server already confirmed this operation.",
  expired: "This operation is too old to retry safely. A manager must check the register and record the check.",
  unverifiable:
    "The server's time was not known when this operation was created, so a retry cannot be proven safe. A manager must check the register and record the check.",
  no_server_time:
    "The server's current time could not be confirmed, so this operation must not be sent again yet. Check the connection and try again; do not repeat it by hand.",
};

interface ExecuteOptions {
  user: UserRef;
  token: string;
  timeoutMs?: number;
}

/**
 * Sends a stored operation with its stored body and key. Resolves with the
 * server's answer; rejects with the error after recording the outcome.
 */
export function executeOperation<Result>(
  store: PendingOperationStore,
  operationId: string,
  { user, token, timeoutMs = REQUEST_TIMEOUT_MS }: ExecuteOptions,
): Promise<Result> {
  return store.withOperationLock(operationId, async () => {
    // Every retry (anything but a first send) is checked against a fresh
    // server time; for anything in doubt it is required.
    store.reconcile();
    const current = store.get(operationId);
    const firstSend = current !== undefined && current.status === "pending" && current.attempts === 0;
    const freshServerTime = current && !firstSend ? await store.fetchServerTime(token) : null;
    const op = await store.beginSend(operationId, user, freshServerTime, store.usesWebLocks);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const heartbeat = setInterval(() => void store.heartbeat(op.id).catch(() => undefined), HEARTBEAT_MS);
    let result: Result;
    try {
      result = await apiRequest<Result>(op.path, {
        method: op.method,
        token,
        idempotencyKey: op.idempotencyKey,
        body: op.body,
        signal: controller.signal,
      });
    } catch (error) {
      clearTimeout(timer);
      clearInterval(heartbeat);
      // If recording fails, the record stays "sending" without a heartbeat
      // and turns "unknown": still conservative. The request error wins.
      await store.recordFailure(op.id, classifyFailure(error), toStoredError(error)).catch(() => store.release(op.id));
      throw error;
    }
    clearTimeout(timer);
    clearInterval(heartbeat);
    // A failure to record the success leaves the record in doubt, never resolved.
    await store.recordSuccess(op.id).catch(() => store.release(op.id));
    return result;
  });
}

/** Stores a new mutation for `user` (persisted first), then sends it. */
export async function startOperation<Result>(
  store: PendingOperationStore,
  request: MutationRequest<Result>,
  options: ExecuteOptions & { user: OperationOwner },
): Promise<Result> {
  const op = await store.create(options.user, request);
  return executeOperation<Result>(store, op.id, options);
}

/** Unresolved and history records of this restaurant, split by owner. */
export function operationsOf(snapshot: PendingOperationsSnapshot, user: UserRef) {
  const ofRestaurant = snapshot.operations.filter((op) => op.restaurantId === user.restaurant_id);
  const unresolved = ofRestaurant.filter((op) => !TERMINAL.has(op.status) || op.status === "failed");
  const history = ofRestaurant.filter((op) => op.status === "checked" || op.status === "succeeded");
  return {
    own: unresolved.filter((op) => op.userId === user.id),
    others: unresolved.filter((op) => op.userId !== user.id),
    history,
  };
}

/** The store shared by the cashier workspace. */
export const pendingOperations = new PendingOperationStore();
