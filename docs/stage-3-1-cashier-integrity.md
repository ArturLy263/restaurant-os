# Stage 3.1 — Cashier financial integrity

Fixes and controls from the Stage 3 post-merge audit, before any cashier
frontend. Migration `f2a8c6e4b1d9`. Tests: `backend/tests/api/test_cashier_integrity.py`,
`backend/tests/integration/test_cashier_concurrency.py`,
`backend/tests/migrations/test_migrations.py`.

## Refunds only on settled bills

`POST /payments/{id}/refunds` refunds a payment of a **PAID** bill only;
otherwise 409 `BILL_NOT_SETTLED`, and nothing changes. While a bill is OPEN
its amount due is total − payments, so a refund there would let the bill
close as paid with less money kept than its total. A mistaken payment on an
open bill is settled first, then refunded. Partial and full refunds, the
split between refunded bill money (`amount`, reduces revenue) and refunded
tip (`tip_amount`, reduces tips only) are unchanged.

## Two-party handover

The drawer passes from the outgoing to the incoming cashier in two steps,
each under the acting employee's own login:

1. **Outgoing cashier:** `POST /cash/shifts/{id}/handover`
   `{"counted_amount": "150.00"}` (or `/close`). The shift closes with their
   count (`cash_shift_closings.user_id`). The former one-step body
   (`received_amount`, `handover_reason`) is refused (422).
2. **Incoming cashier:** `POST /cash/shifts`
   `{"opening_amount": "130.00", "handover_reason": "Only 130 in the drawer"}`.
   The new shift opens with the cash physically received, and a
   `cash_shift_handovers` row records both shifts, both employees, both
   counts, the difference (−20.00), the reason and the time, in the
   incoming shift's business day. A non-zero difference needs a reason
   (422 `HANDOVER_REASON_REQUIRED`). No cash movement, revenue or expense
   is written; the previous shift's closing and a closed day's report stay
   as they were.

The employee who closed the previous shift can't confirm it (409
`HANDOVER_SAME_USER`). **Manager override:** holders of
`cash_handovers:self_confirm` (MANAGER, ADMIN) may, e.g. an owner who
closes at night and opens in the morning; the handover then shows the same
user on both sides (`self_confirmed: true` in the API, the day report and
the `cash_shift.handover_recorded` event).

**Pending period.** Between the two steps the register has no open shift:
payments, refunds and cash movements are refused (409) and nothing is
recorded; `GET /cash/register` shows `previous_closing.handover_pending:
true`. It ends the moment the incoming cashier confirms. This replaces the
former atomic one-step handover (which avoided the gap but could not
identify two people). Two cashiers confirming at once open one shift (the
register row lock and `uq_cash_shifts_one_open`).

## Mandatory idempotency keys

Payments, refunds, cash movements, opening corrections, and opening,
closing and handing over a shift refuse a request without an
`Idempotency-Key` (428 `IDEMPOTENCY_KEY_REQUIRED`) before running. Replays,
key reuse (422 `IDEMPOTENCY_KEY_REUSED`, an earlier owner decision) and
concurrent duplicates behave as documented in the README. Other routes,
business days included, keep the key optional.

## Blind cash count

Expected cash is visible only with `cash_drawer:view_expected` (MANAGER,
ADMIN). For everyone else (cashiers) the API returns null for:

- a shift's `expected_cash` and `drawer` (`expected_visible: false`);
- a closing's `expected_amount` and `difference`;
- a handover's `previous_expected_amount`;
- a closing's `counted_amount`, and the register's
  `previous_closing.counted_amount`, while the handover is pending, except
  for the employee who counted it, so the incoming cashier can't learn what
  to "find".

Refusals (`CLOSING_EXPLANATION_REQUIRED`, `HANDOVER_REASON_REQUIRED`,
`INSUFFICIENT_CASH`) no longer quote amounts. Reports stay management only.

Known limit: a refusal still says *that* a count differs, so repeated
attempts can probe the expected amount (one bit per attempt; refused
attempts are not recorded yet). A later stage can accept a differing count
without an explanation and collect the explanation from a manager, or
record refused counts in the audit log.

## Database hardening (`f2a8c6e4b1d9`)

- `payments` are append-only (update and delete refused), like the rest
  of the ledger.
- Payments, payment allocations, refunds, cash movements, opening
  corrections, closings and handovers can only be inserted into an OPEN
  shift of an OPEN business day, and a shift only into an OPEN day. The
  trigger reads the shift `FOR SHARE`, so an insert racing a close waits
  for it.
- `Bill.payments` no longer cascades deletes.
- Existing rows are not touched. The downgrade drops only these triggers
  and their function.

## Paying adjusted bills by items

Every adjustment today is an item's own price added (`ITEM_ADDITION`) or
cancelled (`ITEM_CANCELLATION`), so the adjusted total is the sum of the
uncancelled items and each item is paid at its own price, to the cent.
If a bill carries any other adjustment (e.g. a future bill-level discount)
or the totals disagree, paying by items is refused (409
`ITEM_SPLIT_UNAVAILABLE`) instead of prorating; pay by amount.

## Permissions and business dates

- `bills:adjust` (cancelling billed items, voiding billed orders) is
  management only; cashiers lost it.
- A business day is named after the restaurant-local date it trades for,
  which may be yesterday (a day opened after midnight). It defaults to
  today, is never in the future (422 `INVALID_BUSINESS_DATE`) and is
  always after the latest existing day (422). Earlier than yesterday needs
  `business_days:open_past` (MANAGER, ADMIN; 403 otherwise) and is recorded
  as `backdated: true` in the `business_day.opened` event.

## Typed reports

Day and shift reports, drawers and handovers have explicit response
schemas (`BusinessDayReport`, `DayShiftReport`, `ShiftReport`,
`DrawerReport`, `FlowsReport`, `MethodTotals`, `HandoverReport`), with the
same field names and two-decimal string amounts as before.
