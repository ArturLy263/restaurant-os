import type { Money } from "../api/types";

/**
 * Integer cents from an API amount ("12.50", or 12.5 for the fields that
 * are still JSON numbers). Amounts have at most two decimals, so cents are
 * exact; all arithmetic is done on them, never on floats.
 */
export function toCents(value: Money): number {
  if (typeof value === "number") return Math.round(value * 100);
  const match = /^(-?)(\d+)(?:\.(\d{1,2}))?$/.exec(value.trim());
  if (!match) throw new Error(`Not a money amount: ${value}`);
  const [, sign, units, fraction = ""] = match;
  const cents = Number(units) * 100 + Number(fraction.padEnd(2, "0"));
  return sign ? -cents : cents;
}

const formatters = new Map<string, Intl.NumberFormat>();

/** "€12.50" for 1250 cents in EUR (the browser's locale decides the layout). */
export function formatCents(cents: number, currency: string): string {
  let formatter = formatters.get(currency);
  if (!formatter) {
    try {
      formatter = new Intl.NumberFormat(undefined, { style: "currency", currency });
    } catch {
      // An unknown currency code: plain number with the code.
      return `${(cents / 100).toFixed(2)} ${currency}`;
    }
    formatters.set(currency, formatter);
  }
  return formatter.format(cents / 100);
}

export function formatMoney(value: Money, currency: string): string {
  return formatCents(toCents(value), currency);
}

/**
 * "12.50" for 1250 cents: the decimal string the API takes for amounts,
 * built from the integer digits (no float division).
 */
export function centsToDecimal(cents: number): string {
  if (!Number.isSafeInteger(cents)) throw new Error(`Not a whole number of cents: ${cents}`);
  const sign = cents < 0 ? "-" : "";
  const digits = String(Math.abs(cents)).padStart(3, "0");
  return `${sign}${digits.slice(0, -2)}.${digits.slice(-2)}`;
}

/**
 * An optional API amount for display: "—" when the server left it out
 * (e.g. expected cash the viewer may not see), and the plain decimal when
 * the currency is not known.
 */
export function formatAmount(value: Money | null | undefined, currency: string | null): string {
  if (value === null || value === undefined) return "—";
  const cents = toCents(value);
  return currency ? formatCents(cents, currency) : centsToDecimal(cents);
}
