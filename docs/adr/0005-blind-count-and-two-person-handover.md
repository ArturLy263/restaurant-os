# 0005 — Blind cash count and two-person shift handover

**Status:** accepted (implemented: `services/cash_service.py`, `services/cash_ledger.py`,
`services/cashier_views.py`; migrations `d9e4b2a7c6f1`, `f2a8c6e4b1d9`)

## Context

A cashier who sees how much cash the drawer "should" hold can make the
count match. A drawer handed from one cashier to the next needs both sides
on record, or a shortfall cannot be attributed.

## Decision

- **Blind count:** closing or handing over a shift sends only the counted
  amount. The server derives the expected cash from the ledger under the
  shift lock and stores expected, counted and difference in an immutable
  closing. Expected cash and differences are returned only to holders of
  `cash_drawer:view_expected` (managers); cashiers get `null`.
- **No oracle (revised, migration `b8d4e6f2a1c3`):** the count is recorded
  as entered, whatever it is, and the shift closes, so it cannot be
  submitted again. (Originally a differing count without an explanation
  was refused with 422 `CLOSING_EXPLANATION_REQUIRED`; retrying with other
  counts then told a cashier whether a guess matched the expected cash.)
  A non-zero difference without an explanation is explained afterwards
  (`POST /cash/shifts/{id}/closing-explanation`, by the person who counted
  or a manager) into the append-only `cash_count_explanations`; the
  business day cannot close while any difference is unexplained (409
  `CASH_DIFFERENCE_UNEXPLAINED` and the database trigger
  `business_days_explained_before_close`). Once a count is final its
  `explanation_required` flag tells the counter one bit — "it differed" —
  which can no longer change the count; no amount is ever shown.
- **Two-person handover:** the outgoing cashier closes their shift with
  their count. The incoming cashier confirms by opening the next shift with
  the cash *they* count, which records a handover between the two shifts
  (handed over, received, difference; a differing count is recorded as
  entered and explained afterwards through
  `POST /cash/shifts/{id}/handover-explanation`). The incoming
  person must differ from the one who closed (409 `HANDOVER_SAME_USER`),
  unless they hold `cash_handovers:self_confirm`; such handovers are marked
  self-confirmed. Between the two steps no money can move (there is no
  open shift).
- **Ledger:** payments, refunds, cash movements, corrections, closings and
  handovers are append-only and can only be written into an open shift of
  an open business day; the database triggers refuse anything else. A day
  closes only without open shifts or active orders, and its report is
  frozen at that moment.

## Consequences

- Every shortfall is attached to a shift, a person and an explanation.
- The first shift of a new day also receives the previous day's last
  closing as a handover, so the person who closed last cannot open first
  the next day without a self-confirm permission.
