// The server's clock, read from the Date header of API responses.
//
// Pending operations must never retry after the backend may have forgotten
// an idempotency key (24 h after it first stored it, by the SERVER's
// clock). The device's wall clock cannot be trusted for that: it can be
// wrong or changed at any time. So time is taken from the server, and the
// time elapsed since an observation is measured with the page's monotonic
// clock (performance.now), which the wall clock does not affect.
//
// The estimate kept here is a LOWER bound of the server time now:
// - the Date header rounds down to the second;
// - performance.now may stand still while the device sleeps;
// - without performance.now, no elapsed time is added at all (the wall
//   clock is never used as a substitute);
// - an observation older than the current estimate (a response that
//   arrived out of order) does not replace it.
// It is used where an early value is the safe side (an operation's
// creation time; deciding that a window has certainly passed). A retry
// never uses it: it reads the Date header of a fresh GET /api/v1/me.

interface Observation {
  // Server time from the Date header (whole seconds, rounded down).
  serverTime: number;
  // performance.now() when it was observed; null without a monotonic clock.
  monotonicAt: number | null;
}

let latest: Observation | null = null;
let count = 0;

/** performance.now(), or null when the page has no monotonic clock. */
export function monotonicNow(): number | null {
  return typeof performance !== "undefined" && typeof performance.now === "function" ? performance.now() : null;
}

/** A Date header as epoch milliseconds; null when missing or malformed. */
export function parseServerDate(header: string | null | undefined): number | null {
  if (!header) return null;
  const time = Date.parse(header);
  return Number.isFinite(time) ? time : null;
}

/** Records the server time from a response's Date header. */
export function observeServerDate(header: string | null, monotonic: number | null = monotonicNow()): void {
  const serverTime = parseServerDate(header);
  if (serverTime === null) return;
  count += 1;
  const current = estimateServerNow(monotonic);
  // An older time than already known (out of order): keep the newer one.
  if (current !== null && serverTime < current) return;
  latest = { serverTime, monotonicAt: monotonic };
}

/** How many valid Date headers were observed. */
export function serverDateObservations(): number {
  return count;
}

/**
 * The server time now, never later than the true server time; null when
 * no observation was made in this page.
 */
export function estimateServerNow(monotonic: number | null = monotonicNow()): number | null {
  if (!latest) return null;
  if (latest.monotonicAt === null || monotonic === null) return latest.serverTime;
  return latest.serverTime + Math.max(0, monotonic - latest.monotonicAt);
}

/** Tests only. */
export function resetServerClock(): void {
  latest = null;
  count = 0;
}
