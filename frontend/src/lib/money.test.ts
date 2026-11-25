import { describe, expect, it } from "vitest";

import { centsToDecimal, formatAmount, formatCents, formatMoney, toCents, parseAmountInput } from "./money";

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

describe("centsToDecimal", () => {
  it("writes cents as the API's decimal string", () => {
    expect(centsToDecimal(1250)).toBe("12.50");
    expect(centsToDecimal(5)).toBe("0.05");
    expect(centsToDecimal(0)).toBe("0.00");
    expect(centsToDecimal(-305)).toBe("-3.05");
    expect(centsToDecimal(9999999999)).toBe("99999999.99");
  });

  it("round-trips with toCents", () => {
    for (const cents of [0, 1, 10, 99, 100, 101, 123456, -1, -100]) {
      expect(toCents(centsToDecimal(cents))).toBe(cents);
    }
  });

  it("refuses fractional cents", () => {
    expect(() => centsToDecimal(12.5)).toThrow();
    expect(() => centsToDecimal(Number.NaN)).toThrow();
  });
});

describe("formatAmount", () => {
  it("shows a dash for amounts the server left out", () => {
    expect(formatAmount(null, "EUR")).toBe("—");
    expect(formatAmount(undefined, null)).toBe("—");
  });

  it("formats with the currency when known, else as a plain decimal", () => {
    expect(formatAmount("40.5", "EUR")).toBe(formatCents(4050, "EUR"));
    expect(formatAmount("40.5", null)).toBe("40.50");
  });
});

describe("parseAmountInput", () => {
  it("reads typed amounts as integer cents", () => {
    expect(parseAmountInput("12")).toBe(1200);
    expect(parseAmountInput("12.5")).toBe(1250);
    expect(parseAmountInput("12,05")).toBe(1205);
    expect(parseAmountInput(" 0.10 ")).toBe(10);
    expect(parseAmountInput("0")).toBe(0);
    expect(parseAmountInput("99999999.99")).toBe(9999999999);
  });

  it("refuses anything the backend would refuse", () => {
    for (const text of ["", "-1", "1.234", "1e3", "abc", "1.", ".5", "123456789", "12.50 EUR", "1 000"]) {
      expect(parseAmountInput(text)).toBeNull();
    }
  });

  it("never goes through floating point", () => {
    // 0.1 + 0.2 style inputs stay exact.
    expect(parseAmountInput("0.29")).toBe(29);
    expect(parseAmountInput("1.15")).toBe(115);
  });
});
