# 0004 — Money as Decimal, decimal strings on the wire

**Status:** accepted (implemented: models, `schemas/money.py`, `frontend/src/lib/money.ts`;
audit and migration plan in [`docs/monetary-contract.md`](../monetary-contract.md))

## Context

Floating point cannot represent most cent amounts exactly; an equal split
or a change calculation done in floats drifts by a cent. JSON numbers are
parsed as floats by JavaScript.

## Decision

- Every money column is `Numeric(10, 2)` mapped to `Decimal`; services
  compute only with `Decimal`.
- The target API contract (owner decision 2026-10-30) is a **decimal string
  with two decimals** (`"12.50"`). Bill, payment, cash and report fields
  use it; new cashier request fields accept only decimal strings
  (`MoneyInput`), while a payment's `amount` still accepts a JSON number
  for one release (deprecated).
  Order item and modifier prices are still JSON numbers on some legacy
  responses; `monetary-contract.md` lists each field and the plan.
- The frontend converts every amount to **integer cents** before any
  arithmetic (`toCents`, `centsToDecimal`, `parseAmountInput`) and never
  does money math in floats. Authoritative amounts (amount due, expected
  cash, report totals) always come from the server; the client only adds
  up for display (cash change, a split's remaining shares).

## Consequences

- Equal splits are exact to the cent (the server gives leftover cents to
  the first shares).
- The remaining float fields are a known, documented inconsistency.
