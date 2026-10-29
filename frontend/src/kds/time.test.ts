import { describe, expect, it } from "vitest";

import { formatClock, formatElapsed, minutesSince, parseApiTimestamp } from "./time";

describe("time helpers", () => {
  it("treats timestamps without an offset as UTC", () => {
    expect(parseApiTimestamp("2026-10-22T18:00:00.123456").toISOString()).toBe("2026-10-22T18:00:00.123Z");
    expect(parseApiTimestamp("2026-10-22T18:00:00+02:00").toISOString()).toBe("2026-10-22T16:00:00.000Z");
    expect(parseApiTimestamp("2026-10-22T18:00:00Z").toISOString()).toBe("2026-10-22T18:00:00.000Z");
  });

  it("counts whole minutes and never goes negative", () => {
    const now = new Date("2026-10-22T18:12:59Z");
    expect(minutesSince("2026-10-22T18:00:00", now)).toBe(12);
    expect(minutesSince("2026-10-22T18:20:00", now)).toBe(0);
  });

  it("yields NaN for an unparseable timestamp", () => {
    expect(minutesSince("not a time", new Date("2026-10-22T18:00:00Z"))).toBeNaN();
  });

  it("formats clock times and shows a dash for an unparseable timestamp", () => {
    expect(formatClock("2026-10-22T18:04:05")).toMatch(/\d/);
    expect(formatClock("not a time")).toBe("—");
  });

  it("formats elapsed time", () => {
    expect(formatElapsed(0)).toBe("now");
    expect(formatElapsed(7)).toBe("7 min");
    expect(formatElapsed(60)).toBe("1 h");
    expect(formatElapsed(95)).toBe("1 h 35 min");
  });

  it("formats elapsed time at the unit boundaries", () => {
    expect(formatElapsed(0.9)).toBe("now");
    expect(formatElapsed(1)).toBe("1 min");
    expect(formatElapsed(59)).toBe("59 min");
    expect(formatElapsed(59.9)).toBe("59 min");
    expect(formatElapsed(61)).toBe("1 h 1 min");
    expect(formatElapsed(1439)).toBe("23 h 59 min");
    expect(formatElapsed(1440)).toBe("1 d");
    expect(formatElapsed(1499)).toBe("1 d");
    expect(formatElapsed(1500)).toBe("1 d 1 h");
    expect(formatElapsed(2 * 1440)).toBe("2 d");
    // 848 h 34 min
    expect(formatElapsed(848 * 60 + 34)).toBe("35 d 8 h");
  });

  it("formats negative and invalid durations safely", () => {
    expect(formatElapsed(-5)).toBe("now");
    expect(formatElapsed(Number.NaN)).toBe("—");
    expect(formatElapsed(Number.POSITIVE_INFINITY)).toBe("—");
  });
});
