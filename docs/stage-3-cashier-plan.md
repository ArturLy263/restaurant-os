# Stage 3 — Cashier module: architecture audit and plan

Audit of `main` at `e74717b` (2026-11-01), done before any Stage 3 code.
Owner decisions A–D were made on 2026-11-01: all four recommendations were
accepted (see **Decisions**).

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

## Decisions (owner, 2026-11-01: recommended option accepted for A–D)

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

Decision B makes an open shift a precondition of every payment, so the
register and shift tables (and opening a shift) come first in Stage 3.1;
corrections, movements and closing follow in 3.2.

| Stage | Scope | Migrations | Main risks |
| --- | --- | --- | --- |
| 3.1 Bills and payments | `payments` gets restaurant, user, currency, tip, confirmation, external reference, shift; method CHECK; `payment_allocations`; `refunds`; equal-split proposal; item split; provider interface; financial outbox contracts + publisher filter; permission additions; order item `Money` switch (D) | 1–2 additive migrations, legacy rows nullable | concurrency (balance, item double-pay), idempotency replays, refund limits |
| 3.2 Register and shifts | `cash_registers`, `cash_shifts`, `cash_opening_corrections`, `cash_movements`, `cash_shift_closings`; open/close/correct/move endpoints; shift locking | 1 additive migration | close vs concurrent payment/movement; correction semantics |
| 3.3 Cashier frontend | Cashier mode: workspace, bill details, payment (cash/card/mixed/tip), split, refund, shift, correction, closing | none | money display only from API strings and integer cents |
| 3.4 Reconciliation and reports | shift report (totals by method, revenue excl. tips, tips, refunds, movements, expected vs counted); full E2E from the Waiter POS through payment and close | none | report totals must equal the ledger |

## Revision 2 — BusinessDay and cashier handover (approved 2026-11-01)

Owner requirement (2026-11-01): the restaurant's **BusinessDay** is
independent of **CashShift** (and of any future EmployeeShift). A cashier
can close their cash shift at 16:00 and hand the shared register over to
the next cashier, who opens a new cash shift, while the business day stays
open and the restaurant keeps taking orders and payments.

Approved by the owner on 2026-11-01 with decisions E and F below and
the clarifications in "Approved decisions and clarifications". Where it
conflicts with the sections above, it replaces them.

### Three independent lifecycles

| Lifecycle | Owner | Opens / closes | Holds |
| --- | --- | --- | --- |
| BusinessDay | restaurant | opened before service, closed after the last shift | the day's sales reporting period (`business_date`) |
| CashShift | cash register | opened and closed by cashiers, several per day, one after another | one drawer accountability period: opening, cash flows, count |
| EmployeeShift | employee | **not built in Stage 3** | attendance; no table or column refers to it, so it can be added later without touching cash |

A cash shift belongs to exactly one business day and one register; a
business day has any number of cash shifts. Nothing about orders, tables,
bills or kitchen preparations refers to a cash shift.

### Data model changes

- **`business_days`** (new): `id`, `restaurant_id`, `business_date`
  (DATE, the restaurant's local date when opened), `status` (`OPEN` →
  `CLOSED`, a new state machine), `opened_by_user_id`, `opened_at`,
  `closed_by_user_id`, `closed_at`, `closing_summary` (JSONB, the day
  report frozen at close).
  - UNIQUE (`restaurant_id`, `business_date`); a partial unique index
    allows one `OPEN` day per restaurant; a closed day is never reopened.
  - UNIQUE (`id`, `restaurant_id`) for composite foreign keys.
- **`cash_shifts`** gains `business_day_id` (NOT NULL), with a composite
  FK (`business_day_id`, `restaurant_id`) → `business_days`, and UNIQUE
  (`id`, `business_day_id`) so that payments and refunds can reference
  the pair.
  - Unchanged: one `OPEN` shift per register, `opening_amount` immutable,
    opening corrections, movements and closing.
- **`cash_shift_handovers`** (new): links a register's consecutive
  shifts. Columns: `id`, `restaurant_id`, `cash_register_id`,
  `business_day_id` (the incoming shift's day), `from_shift_id`
  (UNIQUE), `to_shift_id` (UNIQUE), `previous_expected_amount`,
  `handed_over_amount` (= the outgoing shift's counted amount),
  `received_amount` (= the incoming shift's opening amount),
  `difference` (= received − handed over, CHECK), `reason` (required when
  the difference ≠ 0, CHECK), `outgoing_user_id` (who closed),
  `incoming_user_id` (who opened), `created_at`. Composite keys keep both
  shifts on the same register and the incoming shift in the record's
  day.
- **`payments`** and **`refunds`** gain `business_day_id`, with a
  composite FK (`cash_shift_id`, `business_day_id`) → `cash_shifts` (`id`,
  `business_day_id`), so a payment's day is always its shift's day.
  - For payments the column is nullable: pre-module rows have neither.
    `ck_payments_recorded_in_shift` (NOT VALID) also requires it on new
    rows.
  - For refunds it is NOT NULL.
- **States**: `BUSINESS_DAY` (`OPEN` → `CLOSED`) is added to
  `app/core/states.py` and `BY_TABLE`; its CHECK goes into the cashier
  migration's `STATUSES`.

### Rules

1. **Opening a business day** (`business_days:open`: CASHIER, MANAGER):
   refused while another day of the restaurant is `OPEN`
   (409 `BUSINESS_DAY_ALREADY_OPEN`) or if that `business_date` already
   exists (409 `BUSINESS_DAY_EXISTS`). Audit and outbox
   `business_day.opened`.
2. **Opening a cash shift** requires an `OPEN` business day
   (409 `NO_OPEN_BUSINESS_DAY`) and no `OPEN` shift on the register. The
   shift is attached to that day. The opening amount is what the incoming
   cashier counted (**received amount**).
3. **Handover reconciliation.** When a shift opens and its register has
   an earlier closed shift, the latest one is its predecessor, **whatever
   its business day**: the drawer's cash is one continuous chain, also
   overnight. A `cash_shift_handovers` row is written with
   `handed_over_amount` = the predecessor's counted amount and
   `received_amount` = the new opening amount.
   - If they differ, the opening request must carry a reason
     (422 `HANDOVER_REASON_REQUIRED`); see "Handover discrepancy".
   - The difference belongs to the handover, not to either shift: it
     changes neither shift's expected cash or closing difference, and it
     is listed in the day report for the manager.
   - The very first shift of a register has no predecessor and no
     handover row.
4. **Atomic handover** — *superseded in Stage 3.1 by a two-party handover
   (docs/stage-3-1-cashier-integrity.md): the one-step call recorded the
   caller as both cashiers.* (`POST /cash/shifts/{id}/handover`; needs both
   `cash_shifts:close` and `cash_shifts:open`). In one transaction it
   closes the outgoing shift (counted amount, closing explanation) and
   opens the incoming one (received amount, incoming cashier, handover
   reason), so there is no moment without an open shift and payments
   never stop.
   - The incoming cashier is the authenticated user; the outgoing
     cashier's identity comes from their own close fields.
   - If the outgoing cashier must also confirm in RestOS, this becomes a
     two-step "close" then "open" (rule 5), which leaves a short gap.
   - A safe drop at handover is a `CASH_OUT` with a reason recorded
     **before** counting, so the handed-over amount is what stays in the
     drawer.
5. **Separate close and open** stay possible (e.g. the last shift of the
   day, or a break with nobody at the register). Between them no payment
   can be taken (409 `NO_OPEN_CASH_SHIFT`, decision B). Orders, sending,
   bill creation and item changes continue.
6. **Closing a cash shift** locks only the shift row; it never touches
   orders, tables, bills, preparations or the business day.
   - Unpaid and partly paid bills stay `OPEN` and are paid in the next
     shift; one bill's payments may come from several shifts.
   - Payments and refunds each belong to the shift that was open when
     they were made.
7. **Closing a business day** (`business_days:close`: MANAGER) is
   refused while a cash shift of the day is `OPEN`
   (409 `CASH_SHIFT_STILL_OPEN`), and, per Decision E, while orders are
   still active. It writes the frozen day report and never changes
   orders, tables or bills. Audit and outbox `business_day.closed`.
8. **Payments and refunds** require an `OPEN` business day and an `OPEN`
   cash shift on the register. They record both (the shift's day). Both
   are locked in this order — order row, then cash shift row (`FOR
   UPDATE`) — and the shift row lock also guards the day:
   - the day can only close when no shift is open;
   - closing a day locks the day row and checks the shifts under that
     lock;
   - opening a shift locks the day row.

   So no payment can land in a closed day, and a day can't close while a
   payment is committing.
9. **Refund attribution**: a refund belongs to the shift and day in which
   it is made, not to the payment's. Example: a cash refund on day 2 for
   a day-1 payment reduces day-2 cash and day-2 net revenue, while the
   day-1 report stays as it was frozen. This is standard practice:
   closed periods are never rewritten.

### Money flows by period

| Flow | CashShift (drawer) | BusinessDay (report) |
| --- | --- | --- |
| Cash payment `amount` | + expected cash | revenue, cash |
| Card payment `amount` | — (not in the drawer) | revenue, card |
| Cash tip | + expected cash (`CASH_TIP` movement) | tips, cash; never revenue |
| Card tip | — | tips, card; never revenue |
| Cash refund (amount + tip) | − expected cash (`CASH_REFUND`) | − revenue / − tips, cash |
| Card refund | — | − revenue / − tips, card |
| `CASH_IN` / `CASH_OUT` (incl. safe drop, tip payout) | ± expected cash | listed, not revenue |
| Opening correction | changes the effective opening only | listed |
| Shift difference (counted − expected) | on its closing | sum of all shifts' differences |
| Handover difference | none | listed per handover |

The **day report** combines:
- revenue excluding tips, by method;
- tips, by method;
- refunds;
- per-shift summaries (opening, corrections, movements, expected,
  counted, difference, cashiers);
- handovers with their differences;
- the number and amount of bills still open at close (zero if Decision E
  blocks).

### Approved decisions and clarifications (2026-11-01)

**E. Closing a business day** is refused while the day has open orders
(OPEN or SENT), unpaid bills or partly paid bills with an outstanding
balance: 409 `BUSINESS_DAY_HAS_OPEN_ORDERS` with every blocking order
listed (order id, table number and name, order status, bill id and
status, outstanding amount). Nothing is closed, cancelled, deleted or
carried over automatically; a manager resolves each item first. Because
orders don't belong to a day (F), "the day's open orders" means every
active order of the restaurant at closing time.

**F. Orders don't need a business day** in Stage 3. Waiter POS order
creation and the kitchen workflow are unchanged. Payments and refunds
require an OPEN business day and an OPEN cash shift.

**Cross-day handovers.** A new shift is linked to the latest closed shift
on the same register even when that shift belongs to an earlier
business day. The handover record belongs to the **incoming** shift's
day, references both shifts, and never changes the earlier day or its
report.

**Concurrency.** Payments, refunds, shift opening, closing and atomic
handovers run in one transaction each, with row locks taken in a fixed
order: order row → business day row → cash shift row. A payment racing
a shift close either commits fully to the old shift before the close
reads the ledger (the close waits for its lock), or waits and then
finds the shift closed and is rejected (409 `NO_OPEN_CASH_SHIFT`). It is
never lost, duplicated or assigned to an ambiguous shift. An atomic
handover closes the outgoing and opens the incoming shift in one
transaction.

**Refunds and tips.** A refund records `amount` (refunded bill money,
reduces sales revenue) and `tip_amount` (refunded tip, reduces tips only)
separately; a tip refund never reduces revenue.

**Closed periods are immutable.** A closed business day (including its
`closing_summary`), a closed cash shift, its closing and every handover
can't be updated or deleted: database triggers refuse it, in addition to
the services never doing it. The ledgers (movements, corrections,
refunds, payment allocations) are append-only the same way. A later
correction is a new, audited operation in the currently open period.

### Handover discrepancy (owner requirement, 2026-11-01)

The incoming cashier enters the cash **physically received and
counted**. RestOS compares it with the outgoing shift's **counted
closing amount**:

| Value | Where it is kept |
| --- | --- |
| Previous expected cash | `cash_shift_handovers.previous_expected_amount` (copied from the outgoing closing) |
| Previous counted closing cash | `cash_shift_handovers.handed_over_amount` |
| Amount received by the incoming cashier | `cash_shift_handovers.received_amount` |
| Handover discrepancy | `cash_shift_handovers.difference` = received − handed over (CHECK) |
| New shift opening balance | `cash_shifts.opening_amount` = received amount |

- A non-zero difference requires a reason (422
  `HANDOVER_REASON_REQUIRED`); the row stores the outgoing shift and
  cashier, the incoming shift and cashier, both amounts, the
  difference, the reason, the time and the incoming business day.
- It is its own audit event (`cash_shift.handover_recorded`) and outbox
  event. It never creates a CASH_IN, CASH_OUT, expense or revenue entry:
  it changes neither shift's expected cash or closing difference.
- It never blocks: the shift opens with the received amount and payments
  continue. A manager's review may be recorded later as a separate,
  append-only acknowledgement (not part of Stage 3).
- It appears in the register's handover history and in the incoming
  business day's report (with the day's sum of handover differences),
  never in the outgoing day's frozen report.
- Example: yesterday's shift closed with €150 counted; today's cashier
  counts €130 → handover −€20 with a reason; today's shift opens at €130;
  today's expected cash starts at €130; yesterday's report still shows
  €150.

### Migration `d9e4b2a7c6f1`

It has been applied only to disposable databases, which were dropped
afterwards. The development database `restaurant_os` and the test
database `restaurant_os_test` are both still at `c7e1a5b9d3f8` (checked
read-only). Because no persistent database has run it, it is **edited in
place**:
- add `business_days` and `cash_shift_handovers`;
- add `business_day_id` to `cash_shifts`, `payments` and `refunds`, with
  the composite keys above;
- add `business_days` to `STATUSES`;
- extend `ck_payments_recorded_in_shift`.

No second migration is needed. Had any persistent database been at
`d9e4b2a7c6f1`, a new revision on top of it would be required instead,
since an applied migration is never edited. Nothing is applied to the
development or test databases until the owner says so.
