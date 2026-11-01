# Stage 3 — Cashier module: architecture audit and plan

Audit of `main` at `e74717b` (2026-11-01), done before any Stage 3 code.
Owner decisions still needed are listed under **Open decisions**; nothing that
conflicts with current behaviour is implemented until they are made.

## 3.0 Audit — what exists

### Financial model (reuse, extend)

| Piece | Where | State |
| --- | --- | --- |
| `bills` | `app/db/models/bill.py` | One per order (`order_id` UNIQUE, so duplicate bills are already impossible). `subtotal`/`total` are immutable original amounts; `adjustments_total`, `adjusted_total`, `paid_total`, `amount_due` are derived. Status `OPEN → PAID / VOID` (`app/core/states.py`, CHECK constraint). |
| `bill_adjustments` | same | Signed, append-only (`ITEM_CANCELLATION`, `ITEM_ADDITION`), with reason, user and a partial unique index (one applied adjustment per item and type). |
| `payments` | same | `bill_id`, `amount`, `method` (**free string**), `status` (only `COMPLETED`), `created_at`. **No** restaurant, user, currency, tip, external reference, confirmation data or idempotency link. |
| Line snapshots | `order_items`, `order_item_modifiers` | Product name, `unit_price` (base), `total_price` ((base + modifiers) × qty), modifier name/price snapshots. Once a bill exists, items can't be changed (`PATCH` → 409 `ORDER_BILLED`); cancellation only through the bill (an adjustment). They are already immutable financial snapshots of the bill's lines. |
| Services | `BillService.create_bill`, `create_payment`, `get_bill`; `OrderService.cancel_billed_item`, `void_order` | Every write locks the order row (`OrderRepository.get_for_restaurant(for_update=True)`), then re-reads the bill (`BillRepository.get_for_order`). Cross-tenant ids → 404. |
| Idempotency | `app/api/idempotency.py` | Optional `Idempotency-Key` on create bill and payment; same key + same body replays; different body → 422 `IDEMPOTENCY_KEY_REUSED` (owner decision C5). Business change, key and response commit together. |
| Audit | `EventService.record` → `operational_events` | Same transaction; `bill.created`, `payment.completed`, `order.closed`, `bill.adjusted`, `bill.voided`; money as decimal strings. |
| Outbox | `OutboxService.record`, `app/schemas/outbox.py` `CONTRACTS` | Transactional. **Only station events exist**, and the only consumer is the station WebSocket publisher. |
| Permissions | `app/core/permissions.py` | `CASHIER` = orders:view, bills:view/create/adjust, payments:create. `MANAGER`/`ADMIN` = all. `WAITER` has no payment permission. |
| Money in API | `app/schemas/money.py` `Money`, `docs/monetary-contract.md` | Bills/payments return decimal strings; the payment request still **also accepts JSON numbers**; order item prices are still **JSON floats** (the planned Stage 2 contract switch was not done). |
| Frontend | `frontend/src` | Mode switch by permission (`App.tsx`: POS, KDS); shared top bar, banners, dialogs, steppers, `money.ts` (integer cents); **no payment or bill UI at all**. |
| Tests | `backend/tests` | Rolled-back session per test, `committed_order` + `tests/concurrency.py` for real row-lock tests, `tests/disposable_database.py` for destructive ones; real-stack E2E (`npm run e2e:pos`) on a disposable DB. |

Development data (`restaurant_os`, read only): 5 `PAID` bills, 8 `COMPLETED`
payments (3 CASH, 5 CARD), 2 restaurants (EUR), only ADMIN roles. Migrations
must keep these rows valid.

### Findings that shape the design

1. **The final payment closes the order and frees the table** (`create_payment`:
   `ORDER → CLOSED`, `TABLE → AVAILABLE`, `order.closed` event). The spec says
   settling a bill "must not silently modify the original order". → **Decision A**.
2. **Payments are not tied to any cash shift or user**, and nothing requires a
   shift. Making payments require an open shift changes an existing endpoint.
   → **Decision B**.
3. **Tips**: where cash tips go physically decides the expected-cash formula.
   → **Decision C**.
4. **Money contract**: new endpoints will use `Money` strings in and out. The
   pending switch of order item prices (floats → strings) and strict string
   input on the existing payment endpoint are breaking changes. → **Decision D**.
5. **Outbox publisher**: `StationEventPublisher` answers any event type it
   can't route with `resync_required` to **every station stream of the
   restaurant**. Emitting `payment.completed` naively would make every KDS
   reload on each payment. Plan: financial contracts are registered in
   `CONTRACTS` with no station audience; the station publisher marks them
   delivered without notifying station streams (tested). No new messaging
   infrastructure.
6. `payments.method` is unconstrained; dev data holds only `CASH`/`CARD`, so a
   CHECK constraint is safe.
7. Legacy payments have no user, shift, or currency. New columns are nullable
   for rows written before the migration (documented, excluded from shift
   reconciliation); the services always set them, and a CHECK on rows created
   after the migration isn't possible without a marker, so tests enforce it.

### Design choices made in this audit (no owner input needed)

- **No `bill_items` table.** Bill lines are the order's non-cancelled items
  plus their snapshots, which are immutable once billed (`ORDER_BILLED`);
  copying them would create a second source of truth. Items added later stay
  charged through `ITEM_ADDITION` adjustments, as today.
- **Refunds don't reopen bills.** A bill's `amount_due` stays
  `adjusted_total − paid_total` (gross payments). Refunds are separate rows
  linked to a payment; net revenue = payments − refunds.
- **Tips live on the payment** (`payments.tip_amount ≥ 0`), never in
  `amount`, so they never touch `amount_due` or revenue. With several
  payments, each carries its own tip; a tip is never split or summed twice.
  Tip refunds are a separate amount on the refund.
- **Split payments**:
  - *Equal split* is a server-computed proposal over the current `amount_due`:
    `N` shares, each `floor(due / N)` to the cent, the first `due mod N` cents
    added one cent each to the first shares. Each share is then paid as an
    ordinary partial payment; nothing is reserved, so concurrent payments
    can't be overcharged (the balance check runs under the order lock).
  - *Split by items*: a payment may name order items. Each named item gets a
    `payment_allocations` row (item, payment, amount = the item's
    `total_price`); a partial unique index on active allocations per item makes
    paying an item twice impossible even under concurrency. Amount-based and
    item-based payments can be mixed; the sum can never exceed `amount_due`.
- **Card payments** are recorded `COMPLETED` with
  `confirmation = MANUAL` and the confirming user and time, plus an optional
  terminal/transaction reference (free text, validated so that it can't hold
  a card number: digit runs of 12+ are rejected). No card data fields exist.
  A `PaymentProvider` interface (`authorize/capture/refund` →
  `PENDING/COMPLETED/FAILED`) with one `ManualConfirmationProvider` is the
  extension point; status `PENDING` exists so a pending provider payment
  blocks shift closing.
- **Cash register**: `cash_registers` (restaurant, name); the first one is
  created on demand, and a partial unique index allows one active register
  per restaurant in this version (to be dropped when multiple registers
  arrive).
- **Shifts**: `cash_shifts` (register, restaurant, `opening_amount`
  [immutable, as entered], opened by/at, status `OPEN → CLOSED`); a partial
  unique index enforces one `OPEN` shift per register.
- **Opening balance corrections**: append-only `cash_opening_corrections`
  (shift, previous amount, new amount, reason, manager, time). The effective
  opening balance is the latest correction's `new_amount`, or
  `opening_amount`. No cash movement is created.
- **Cash movements**: append-only `cash_movements` with types `CASH_IN`,
  `CASH_OUT`, `CASH_REFUND` and, depending on Decision C, `CASH_TIP`; each
  with shift, user, reason and an optional payment/refund link. Cash
  payments are not copied into movements; they are read from `payments`.
- **Closing**: `cash_shift_closings` (one per shift, UNIQUE) stores expected,
  counted, difference, explanation (required when the difference ≠ 0) and a
  JSON summary of every component. Every shift write and every payment or
  refund locks the shift row (`FOR UPDATE`), so a close can't race a
  movement or payment; closing is refused while any payment is `PENDING`.
- **Permissions** (new, existing naming): `bills:split` is not needed
  (covered by `payments:create`); new: `payments:refund`, `cash_shifts:view`,
  `cash_shifts:open`, `cash_shifts:close`, `cash_movements:create`,
  `cash_opening_balance:correct`, `cash_reports:view`. CASHIER gets view,
  open, close and movements; MANAGER/ADMIN everything; WAITER nothing new.
- **Events**: audit events for every mutation; outbox contracts
  `bill.created`, `payment.completed`, `payment.refunded`,
  `cash_shift.opened`, `cash_shift.closed`, `cash_movement.created`,
  `cash_opening_balance.corrected` (ids and amounts only, no free text).

## Open decisions (blocking Stage 3.1)

**A. What happens to the order when the bill is fully paid?**
Today: order `CLOSED`, table `AVAILABLE`, with an `order.closed` audit event.
Recommended: keep it (it is explicit and audited, not silent; the floor
needs the table back), and document it as the one defined effect of
settling a bill. Alternative: settle only the bill; a separate "close
table" action closes the order.

**B. Must every payment and refund belong to an open cash shift?**
Recommended: yes, for CASH and CARD alike, so every payment appears in a
shift's report and reconciliation. Today's endpoint accepts payments
without a shift; after the change it answers 409 `NO_OPEN_CASH_SHIFT`.
Alternative: only CASH requires a shift.

**C. Where do cash tips go?**
Recommended: cash tips enter the drawer and are recorded as an explicit
`CASH_TIP` line in expected cash (never in revenue); paying tips out to
staff is a `CASH_OUT` with a reason. Alternative: cash tips go to a
separate tip jar and are recorded on the payment only, outside the drawer.

**D. Money contract.**
Recommended: all new cashier endpoints take and return `Money` strings
only (JSON numbers rejected with 422); the existing
`POST /bills/{id}/payments` keeps accepting numbers for one release (a
documented deprecation). The order item price switch (floats → strings)
is done as its own commit in Stage 3.1, since the cashier UI shows those
prices; the Waiter POS already handles both.

## Implementation sequence

| Stage | Scope | Migrations | Main risks |
| --- | --- | --- | --- |
| 3.1 Bills and payments | `payments` gets restaurant, user, currency, tip, confirmation, external reference, shift; method CHECK; `payment_allocations`; `refunds`; equal-split proposal; item split; provider interface; financial outbox contracts + publisher filter; permission additions; order item `Money` switch (D) | 1–2 additive migrations, legacy rows nullable | concurrency (balance, item double-pay), idempotency replays, refund limits |
| 3.2 Register and shifts | `cash_registers`, `cash_shifts`, `cash_opening_corrections`, `cash_movements`, `cash_shift_closings`; open/close/correct/move endpoints; shift locking | 1 additive migration | close vs concurrent payment/movement; correction semantics |
| 3.3 Cashier frontend | Cashier mode: workspace, bill details, payment (cash/card/mixed/tip), split, refund, shift, correction, closing | none | money display only from API strings and integer cents |
| 3.4 Reconciliation and reports | shift report (totals by method, revenue excl. tips, tips, refunds, movements, expected vs counted); full E2E from the Waiter POS through payment and close | none | report totals must equal the ledger |
