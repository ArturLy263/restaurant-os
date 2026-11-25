# 0002 — Idempotency keys stored in the business transaction

**Status:** accepted (implemented: `api/idempotency.py`, migration `b7c3e9a41d2f`)

## Context

Tablets on restaurant Wi-Fi lose answers. A retried "send round" must not
send twice, and a retried payment or cash movement must never move money
twice. A deduplication cache outside the database can disagree with what
was committed.

## Decision

- Mutations accept an `Idempotency-Key`; money and cash-register
  operations **require** it (428 `IDEMPOTENCY_KEY_REQUIRED` otherwise).
- The key is claimed with `INSERT … ON CONFLICT DO NOTHING` and the
  successful response is stored **in the same transaction** as the change,
  its audit events and its outbox events. Only 2xx results are stored; a
  failure rolls back the change and the key together, so a retry runs
  against the current state.
- Keys are scoped to the user and restaurant. The same key with the same
  request replays the stored response (`Idempotent-Replayed: true`); the
  same key with a different request is 422 `IDEMPOTENCY_KEY_REUSED`
  (owner decision: 409 is reserved for "the current state forbids this").
  Concurrent requests with one key wait for the first. Results live at
  least 24 h.
- The cashier frontend stores every mutation with its key and body
  **before** sending it (`cashier/pendingOperations.ts`). An unknown
  outcome can then only be retried with the same key, never re-entered by
  hand.

## Consequences

- A committed change always has its stored result and vice versa.
- Clients must create one key per user action and reuse it only for
  retries of that action.
