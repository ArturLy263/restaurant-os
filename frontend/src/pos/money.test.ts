import { describe, expect, it } from "vitest";

import { formatCents, formatMoney, toCents } from "./money";

describe("toCents", () => {
  it("reads decimal strings exactly", () => {
    expect(toCents("12.50")).toBe(1250);
    expect(toCents("12.5")).toBe(1250);
    expect(toCents("12")).toBe(1200);
    expect(toCents("0.00")).toBe(0);
    expect(toCents("-3.05")).toBe(-305);
    expect(toCents("99999999.99")).toBe(9999999999);
  });

  it("reads the JSON numbers still used for item prices", () => {
    expect(toCents(12.5)).toBe(1250);
    expect(toCents(0.3)).toBe(30);
    expect(toCents(40.5)).toBe(4050);
  });

  it("sums without float drift", () => {
    expect(toCents("0.10") + toCents("0.20")).toBe(30);
  });

  it("rejects what is not an amount", () => {
    expect(() => toCents("1.234")).toThrow();
    expect(() => toCents("abc")).toThrow();
  });
});

describe("formatting", () => {
  it("formats in the restaurant's currency", () => {
    const formatted = formatCents(1250, "EUR");
    expect(formatted).toContain("12");
    expect(formatted).toContain("50");
    expect(formatted).toMatch(/€|EUR/);
    expect(formatMoney("4.00", "EUR")).toBe(formatCents(400, "EUR"));
  });

  it("falls back for unknown currency codes", () => {
    expect(formatCents(1250, "??")).toBe("12.50 ??");
  });
});
