import { afterEach, describe, expect, it, vi } from "vitest";

import { jsonResponse, mockApi } from "../test/fixtures";
import { apiRequest } from "./client";
import {
  estimateServerNow,
  monotonicNow,
  observeServerDate,
  parseServerDate,
  resetServerClock,
  serverDateObservations,
} from "./serverClock";

afterEach(() => resetServerClock());

const SERVER = Date.parse("2026-11-08T12:00:00Z");

describe("server clock", () => {
  it("is unknown until a response carried a valid Date header", () => {
    expect(estimateServerNow()).toBeNull();
    observeServerDate(null);
    observeServerDate("not a date");
    expect(estimateServerNow()).toBeNull();
    expect(serverDateObservations()).toBe(0);
  });

  it("parses the HTTP date format", () => {
    observeServerDate("Sun, 08 Nov 2026 12:00:00 GMT", 1_000);
    expect(estimateServerNow(1_000)).toBe(SERVER);
    expect(serverDateObservations()).toBe(1);
  });

  it("advances with the monotonic clock, not the device wall clock", () => {
    observeServerDate(new Date(SERVER).toUTCString(), 1_000);
    expect(estimateServerNow(61_000)).toBe(SERVER + 60_000);
    // A monotonic clock never runs backwards; if it seems to, no time is added.
    expect(estimateServerNow(500)).toBe(SERVER);
  });

  it("is fed by every API response, including errors", async () => {
    mockApi({
      "GET /x": [jsonResponse({}, 200, { Date: new Date(SERVER).toUTCString() })],
      "GET /y": [
        jsonResponse({ error: { code: "E", message: "m", request_id: "r" } }, 500, {
          Date: new Date(SERVER + 3_600_000).toUTCString(),
        }),
      ],
    });
    await apiRequest("/x");
    expect(Math.abs(estimateServerNow()! - SERVER)).toBeLessThan(2_000);
    await apiRequest("/y").catch(() => undefined);
    expect(Math.abs(estimateServerNow()! - (SERVER + 3_600_000))).toBeLessThan(2_000);
    expect(serverDateObservations()).toBe(2);
  });
});

describe("server clock safety", () => {
  it("rejects malformed and missing Date headers", () => {
    expect(parseServerDate(undefined)).toBeNull();
    expect(parseServerDate("")).toBeNull();
    expect(parseServerDate("Someday, 99 Foo 20xx")).toBeNull();
    expect(parseServerDate("Sun, 08 Nov 2026 12:00:00 GMT")).toBe(SERVER);
  });

  it("does not let an out-of-order older observation replace a newer one", () => {
    observeServerDate(new Date(SERVER + 60_000).toUTCString(), 1_000);
    observeServerDate(new Date(SERVER).toUTCString(), 2_000); // arrived late
    expect(estimateServerNow(2_000)).toBe(SERVER + 61_000);
    expect(serverDateObservations()).toBe(2);
    observeServerDate(new Date(SERVER + 120_000).toUTCString(), 3_000);
    expect(estimateServerNow(3_000)).toBe(SERVER + 120_000);
  });

  it("without performance.now never adds elapsed time (no wall-clock substitute)", () => {
    vi.stubGlobal("performance", undefined);
    expect(monotonicNow()).toBeNull();
    const dateNow = vi.spyOn(Date, "now");
    observeServerDate(new Date(SERVER).toUTCString());
    expect(estimateServerNow()).toBe(SERVER);
    // However much the device clock moves, the estimate stays a lower bound.
    dateNow.mockReturnValue(SERVER + 10 * 3_600_000);
    expect(estimateServerNow()).toBe(SERVER);
  });

  it("an estimate from a monotonic observation ignores a missing monotonic clock later", () => {
    observeServerDate(new Date(SERVER).toUTCString(), 1_000);
    expect(estimateServerNow(null)).toBe(SERVER);
  });
});
