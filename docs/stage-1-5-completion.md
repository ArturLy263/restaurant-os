# Stage 1.5 completion report

Contract and reliability hardening, audited against the October 12
roadmap (epics A–D) on branch `public-main`. The roadmap predates most of
the code, so every requirement was checked against the current code,
migrations, tests and CI rather than assumed missing.

Audited base: `349e2b5`. Work commits: `fca32c4`, `c03de04`, `ba166c8`,
`9718495`, `319eafa` (CI job and this report) and the documentation
commit recording the owner decisions of 2026-10-30. Nothing is pushed.

## Completion matrix

| Req | Status | Evidence |
| --- | --- | --- |
| **A1** Pinned dependencies, reproducible setup | PASS | `backend/requirements.txt` (pinned), `backend/requirements-test.txt`, `backend/constraints.txt` (every transitive package; all 41 pins match the installed environment, `pip check` clean); `frontend/package-lock.json` (clean `npm ci` verified) |
| **A2** Mandatory PostgreSQL integration tests in CI | PASS (verified locally) | `.github/workflows/ci.yml`: PostgreSQL 16 service, `REQUIRE_TEST_DATABASE=1`, a step proving an unreachable database fails the run (exit 1), a step proving every test is marked (exit 5). All steps reproduced locally with the same commands and expected exit codes; no test skipped. Last remote run predates the 25 unpushed commits (see "Before sign-off"). |
| **A3** Unit/integration markers, separate commands | PASS | `pytest.ini` strict markers `unit`/`integration`/`migrations`, assigned automatically in `backend/tests/conftest.py` from the fixtures a test uses; `pytest -m unit` runs with no database (259 tests) |
| **A4** Migration upgrade/downgrade on a disposable database | PASS | `backend/tests/migrations/test_migrations.py` on a newly created database whose identity is verified before each command: single head, base→head→base, every migration's downgrade reverses its upgrade (now including CHECK constraints), models match migrations, data-preserving checks for 5 migrations |
| **B1** Centralized, consistent state transitions | PASS — fixed in `fca32c4` | `backend/app/core/states.py` lists every status and allowed change; all 13 service status writes go through it (enforced by `tests/unit/test_states.py`); a guard that lets an illegal change through now fails and rolls back (`tests/api/test_state_rules.py`, mutation-checked); CHECK constraint per status column (migration `f4c2a8d6b1e3`); preparation rules equal the v2 event contract |
| **B2** Product name and price snapshots | PASS — fixed in `c03de04` | price: `order_items.unit_price`/`total_price` (existed); name: `order_items.product_name_snapshot` (migration `a3d9e7c1f5b2`, nullable, no backfill); modifiers already stored name and price; `tests/api/test_product_name_snapshot.py` |
| **B3** Bills before/after partial and full payment | PASS | `tests/api/test_payments.py` (split payments close bill, order and table; under/overpayment), `test_bill_adjustments.py` (27 cases incl. cancellation after partial/full payment), `tests/services/test_bill_service.py` (re-checks under lock), core contract flow |
| **B4** Stable business error codes, request IDs | PASS | `app/core/errors.py` (`ErrorCode`), envelope `{detail, error: {code, message, request_id}}`, `X-Request-ID`; `tests/api/test_error_codes.py`, `tests/unit/test_error_contract.py` (unique codes, hidden internals, request id echo) |
| **B5** Verified core API contracts | PASS — added in `9718495` | `tests/api/test_core_contracts.py`: create order, add item, send, ready, create bill, partial and final payment validated against the published OpenAPI schemas (exact fields, types, UUID/timestamp formats); status codes and models pinned; errors use the envelope |
| **C1** POS permissions on write endpoints | PASS | every write route depends on `require_permission(...)` (checked by route introspection; only login and `/health` are open); `tests/api/test_permissions.py`, `test_authorization_rules.py` |
| **C2** Tenant and station scope | PASS | `OrderService.ensure_station_visible`, station scope in `AuthContext`; triggers `e1b7d3a9c4f2`; `tests/api/test_station_scope.py`, `test_tenant_consistency.py`, cross-tenant 404 tests |
| **C3** `/me` | PASS | `GET /api/v1/me`: id, name, email, restaurant_id, role, permissions, stations |
| **C4** Persistent idempotency with response replay | PASS — completed in `ba166c8` | table `idempotency_keys` (migration `b7c3e9a41d2f`), key + change + response in one transaction; now on all 12 POS/KDS writes (added: add item, create bill, start/ready/serve); `tests/api/test_idempotency.py` (31 tests), `tests/integration/test_mutation_concurrency.py` |
| **C5** Reused key with a different payload is deterministic | PASS — resolved by owner decision 2026-10-30 | **422** `IDEMPOTENCY_KEY_REUSED`, every time, nothing run (`app/api/idempotency.py`; `test_same_key_with_different_payload_is_rejected`). The roadmap's 409 was amended to 422; see "C5 decision". |
| **D1** Transactional outbox | PASS | `outbox_events` (migration `f3a9c1e7b5d2`); written in the business transaction; `tests/integration/test_outbox_atomicity.py` |
| **D2** Delivery worker, retries, dedup, failures | PASS | `app/workers/outbox_worker.py` (`SKIP LOCKED`, backoff, dead letters, publish timeout, per-order ordering, retention); `tests/integration/test_outbox_worker.py`, `test_outbox_delivery_policies.py`; clients deduplicate on event id |
| **D3** Versioned event contracts | PASS | `app/schemas/outbox.py` registry, golden payloads in `tests/unit/test_event_contracts.py` |
| **D4** Authenticated, tenant/station-scoped WebSocket after commit | PASS | `app/realtime/` (first-message auth, re-authorization, close codes); `tests/integration/test_station_event_stream.py`, `test_station_event_fanout.py`; real API → PostgreSQL → outbox → WebSocket → Chrome delivery shown on the isolated `restaurantos_e2e` database |

## C5 decision (2026-10-30)

The owner kept **422 Unprocessable Content** `IDEMPOTENCY_KEY_REUSED` for
an existing `Idempotency-Key` reused with a different request. This is
intentional and is not to be changed to 409: in this API 409 means the
current state forbids the operation, while a reused key is an invalid
request that no retry can fix (this also follows the IETF
Idempotency-Key draft, which keeps 409 for a request still in progress).

Recorded in: the October 12 roadmap (kept outside this repository; its C5
readiness criterion now reads 422, with a dated note), the README "Idempotency keys" section, `app/api/idempotency.py`
(module docstring and the OpenAPI 422 response description, unchanged)
and `tests/api/test_idempotency.py` (asserts 422 and the code). No code
change.

## Monetary values (owner decision 2026-10-30)

Target contract: decimal strings for every monetary value (`"12.50"`).
Audit result (`docs/monetary-contract.md`): storage is `Numeric(10, 2)`
and `Decimal` everywhere; bill, adjustment and payment amounts already
are decimal strings; **three response fields are JSON numbers**:
`OrderItemResponse.unit_price`, `OrderItemResponse.total_price` and
`OrderItemModifierResponse.price` (in add item, cancel item, get/send/void
order); the payment request also accepts JSON numbers. Changing them is
a breaking contract change, so it is a **Stage 2 contract requirement**,
done together with the Waiter POS client; current behaviour is kept and
documented in the README. Not a Stage 1.5 blocker: no existing client
reads these fields.

## Fixes and commits

| Commit | Requirement | Change |
| --- | --- | --- |
| `fca32c4` | B1 | central status rules; status CHECK constraints (migration `f4c2a8d6b1e3`, stops without changes if a table holds an unknown status); migration round trip compares CHECK constraints |
| `c03de04` | B2 | `order_items.product_name_snapshot` (migration `a3d9e7c1f5b2`); order responses gain optional `items[].product_name`; queue shows the stored name |
| `ba166c8` | C4 | optional `Idempotency-Key` on add item, create bill, start/ready/serve |
| `9718495` | B5 | core POS contract tests |
| (this commit) | A2 | CI job for the frontend (`npm ci`, typecheck, tests, build); this report, the deployment proposal and the audit checkpoint |

API changes are additive only: one optional response field and an
optional header on five routes. Frontend behaviour is unchanged.

## Results

| Check | Result |
| --- | --- |
| Backend, CI steps run locally | unit 259 passed (database unreachable); integration 566 passed; migrations 9 passed; missing database fails with exit 1; no unmarked tests; no skips. Total **834** (783 before the audit). |
| Backend warnings | 19,371 (unit 3, integration 19,336, migrations 32) vs 18,887 for 783 tests; the same 6 warning categories as before (`datetime.utcnow` deprecations in services and SQLAlchemy defaults, Starlette/httpx and anyio deprecations, one Pydantic serializer warning); the increase comes from the new tests |
| Frontend | clean `npm ci` of the committed tree: typecheck OK, **80** tests passed, build OK; headless-Chrome browser test (`npm run e2e`) passed |
| Migrations | validated only on disposable databases created by the test suite and on `restaurant_os_test`; `restaurant_os` and `restaurantos_e2e` were not migrated, downgraded, reset or written |

## Before sign-off

1. ~~Decide C5~~ — done 2026-10-30 (422 kept).
2. Push when ready and confirm the first GitHub Actions run (both jobs)
   is green; `gh` is not installed locally, so no remote run has been
   observed for these commits.
3. ~~Upgrade the development database~~ — done: `restaurant_os` is at
   head `a3d9e7c1f5b2` and `alembic check` reports no differences
   (verified 2026-10-30). `restaurantos_e2e` is still at `e1b7d3a9c4f2`;
   upgrade it only if it is reused (it is a disposable test database).

## Verdict

All P0 requirements, including C5 (422 by owner decision), are
implemented and verified locally. Stage 1.5 is **complete pending the
first remote CI run**, which needs the push. No architectural,
destructive or data-rewriting change is outstanding. Monetary
decimal strings for order item prices are a Stage 2 contract
requirement, not a Stage 1.5 gap.

## Stage 2 sequence (proposal)

The API has no read endpoints for the menu (categories, products,
modifiers), tables or a table's orders, and no management endpoints;
those come first. The POS work starts with the monetary contract change
in `docs/monetary-contract.md` (order item and modifier prices become
decimal strings, payment requests strings only).

1. **Waiter POS**: read endpoints for tables (with their active order),
   menu (categories, products, modifier groups) and open orders; a waiter
   web client (open order, add items with modifiers, send rounds, cancel
   before billing), sending `Idempotency-Key` on every write.
2. **Kitchen/Bar refinement**: assigned-station login for KITCHEN/BAR
   users, manager discard in the UI, sound/visual alerts, a real-browser
   test against the API in CI.
3. **Cashier**: bill view and payments (split, partial), bill adjustments,
   receipts; idempotent payments from the client.
4. **Manager setup**: CRUD for menu, modifiers, tables, stations, users
   and station assignments, with audit events and permission checks.

Cross-cutting: replace `datetime.utcnow()` with timezone-aware UTC (the
main warning source), and decide the historical test data cleanup (the
script is dry-run only).

Deployment direction: see `docs/deployment-proposal.md`.
