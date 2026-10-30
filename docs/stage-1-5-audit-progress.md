# Stage 1.5 audit: progress checkpoint

Working notes for resuming the Stage 1.5 (contract and reliability
hardening) audit. Base: `349e2b5`, branch `public-main`, not pushed.
Databases `restaurant_os` and `restaurantos_e2e` are never migrated,
reset or written by this work; migrations are validated only by the test
suite (disposable databases and `restaurant_os_test`).

Last updated: 2026-10-30. **State: audit and implementation complete;
C5 resolved by the owner (422 kept); waiting only on the first remote CI
run.** The full report is `docs/stage-1-5-completion.md`.

## Requirement status

| Req | Status |
| --- | --- |
| A1–A4 | PASS (A2: frontend CI job added; CI steps verified locally) |
| B1 | DONE `fca32c4` (central status rules, CHECK constraints `f4c2a8d6b1e3`) |
| B2 | DONE `c03de04` (`order_items.product_name_snapshot`, `a3d9e7c1f5b2`, no backfill) |
| B3, B4 | PASS |
| B5 | DONE `9718495` (core contract tests) |
| C1–C3 | PASS |
| C4 | DONE `ba166c8` (keys on add item, create bill, start/ready/serve) |
| C5 | PASS: 422 `IDEMPOTENCY_KEY_REUSED` kept by owner decision 2026-10-30; roadmap amended |
| D1–D4 | PASS |

## Checks run (final)

- Backend CI steps reproduced locally: `pip check` clean; 41 constraint
  pins match; unit 259 passed (database unreachable); missing database
  exits 1; integration 566 passed; migrations 9 passed; no unmarked
  tests (exit 5); no skips. Total 834.
- Warnings: same 6 categories as the 783-test baseline; 19,371 in total
  (more tests).
- Frontend: clean `npm ci` of the committed tree, typecheck OK, 80 tests,
  build OK; `npm run e2e` (headless Chrome) passed.
- Per batch: 802 (B1), 806 (B2), 819 (C4) full-suite passes; mutation
  check of the central status rule.
- `restaurant_os` fingerprint unchanged after the session interruption.

## Commits

- `fca32c4` feat: centralize status rules and restrict status values
- `c03de04` feat: keep the product name on order items
- `ba166c8` feat: accept idempotency keys when adding items, billing and preparing
- `9718495` test: pin the contracts of the core POS operations
- `319eafa` ci: test the frontend; docs: Stage 1.5 completion report
- next commit: docs only — C5 decision, monetary contract audit
  (`docs/monetary-contract.md`), README money section

## Next actions

1. Owner: push and confirm both GitHub Actions jobs are green.
2. Then Stage 2, starting with the monetary contract change
   (`docs/monetary-contract.md`) and the Waiter POS read endpoints.

Done: C5 decided (422); `restaurant_os` upgraded to head `a3d9e7c1f5b2`
by the owner (`alembic check` clean, verified 2026-10-30).

No tests are running and nothing is blocked on the agent side.
