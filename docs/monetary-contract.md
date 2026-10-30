# Monetary values in the API

Owner decision (2026-10-30): the target API contract is **decimal
strings for every monetary value**, e.g. `"12.50"`, never JSON
floating-point numbers. Bill and payment amounts already follow it; order
item and modifier prices do not yet.

This file is the audit of every monetary field (verified at `319eafa`)
and the plan to reach the target. Changing a JSON number into a string is
a breaking change for any client that does arithmetic or comparisons on
it, so the change is **scheduled for Stage 2** as one coordinated contract
update, not made silently.

## Storage

Every money column is `Numeric(10, 2)` and mapped to `Decimal`; the
services compute only with `Decimal`. There is no float in storage or in
business logic.

| Table.column | Model | Meaning |
| --- | --- | --- |
| `products.price` | `Product.price` | current menu price |
| `modifiers.price_adjustment` | `Modifier.price_adjustment` | current modifier surcharge (default `0.00`) |
| `order_items.unit_price` | `OrderItem.unit_price` | product price at add time, without modifiers |
| `order_items.total_price` | `OrderItem.total_price` | `(unit_price + modifier surcharges) × quantity` |
| `order_item_modifiers.price_snapshot` | `OrderItemModifier.price_snapshot` | modifier surcharge at add time |
| `bills.subtotal`, `bills.total` | `Bill` | original bill amount |
| `bill_adjustments.amount` | `BillAdjustment.amount` | signed adjustment |
| `payments.amount` | `Payment.amount` | payment |

Derived (Python properties on `Bill`, `Decimal` sums starting at
`Decimal("0.00")`): `adjustments_total`, `adjusted_total`, `paid_total`,
`amount_due`.

## API fields

"Wire" is the JSON actually returned, checked with a request against the
test database; "OpenAPI" is what `app.openapi()` publishes.

| # | Schema.field | Direction | Routes | Pydantic | OpenAPI | Wire | Target met |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `CreateBillResponse.subtotal`, `.total`, `.adjustments_total`, `.adjusted_total`, `.paid_total`, `.amount_due` | response | `POST /bills/order/{order_id}` | `Decimal` | string (decimal pattern) | `"15.00"` | yes |
| 2 | `BillResponse` same six fields | response | `GET /bills/{bill_id}`, `POST /bills/{bill_id}/items/{item_id}/cancel` | `Decimal` | string | `"9.25"` | yes |
| 3 | `BillAdjustmentResponse.amount` | response | inside 1 and 2 | `Decimal` | string | `"-12.50"` | yes |
| 4 | `PaymentResponse.amount` | response | `POST /bills/{bill_id}/payments` | `Decimal` | string | `"1.00"` for input `"1"` | yes |
| 5 | `PaymentRequest.amount` | request | `POST /bills/{bill_id}/payments` | `Decimal`, `gt=0`, `max_digits=10`, `decimal_places=2` | `anyOf: [number, string]` | accepts `"15.00"`, `"15"`, `15`, `1.25`; rejects `"0.001"` and `0.1 + 0.2` with 422 | **partly**: strings accepted, numbers also accepted |
| 6 | `OrderItemResponse.unit_price` | response | `POST /orders/{id}/items`, `POST /orders/{id}/items/{item_id}/cancel`, inside 8 | `float` | number | `2.5` | **no** |
| 7 | `OrderItemResponse.total_price` | response | same as 6 | `float` | number | `2.5`, `0` | **no** |
| 8 | `OrderDetailResponse.items[]` (fields 6, 7, 9) | response | `GET /orders/{id}`, `POST /orders/{id}/send`, `POST /orders/{id}/void` | — | — | — | **no** (through 6, 7, 9) |
| 9 | `OrderItemModifierResponse.price` (alias of `price_snapshot`) | response | inside 6 and 8 | `float` | number | `1.0` | **no** |

Not in the API: `products.price` and `modifiers.price_adjustment` (there
are no menu endpoints yet; Stage 2 adds them).

Other places that carry money:

| Place | Format | Target met |
| --- | --- | --- |
| Audit events (`GET /events`, `event_metadata`): `unit_price`, `total_price`, modifier `price`, `subtotal`, `total`, `amount`, `adjusted_total`, `amount_due`, `total_paid` | `str(Decimal)`, e.g. `"12.50"` | yes |
| Outbox / WebSocket event contracts (`app/schemas/outbox.py`) | no money fields by design | n/a |
| Stored idempotent responses (`idempotency_keys.response_body`) | copy of the response as it was serialized, replayed unchanged for 24 h | follows 1–9 |
| Station queue (`GET /stations/{id}/preparations`) | no money fields | n/a |
| Frontend (`frontend/src/api/types.ts`) | no money fields (the KDS shows no prices) | n/a |

## Inconsistencies

1. **Order item prices are JSON numbers** (fields 6, 7, 9) while bill
   amounts are decimal strings. They lose the scale (`2.5` instead of
   `"2.50"`, `0` instead of `"0.00"`), and a client that sums them in
   JavaScript floats can drift from the bill. The values themselves are
   exact: they come from `Numeric(10, 2)` and every possible value
   round-trips through a float.
2. **The payment request accepts JSON numbers** (field 5). Safe in
   practice (more than two decimals is rejected, so `0.1 + 0.2` cannot
   slip through), but the published schema advertises `number` as an
   equal option.
3. **The decimal-string pattern is loose.** Pydantic's generic
   pattern also matches `"1"`, `"+1.5"` or `"007"`; responses are
   always two decimals in practice because every value comes from a
   `Numeric(10, 2)` column or a `Decimal` sum of them, but the schema does
   not promise it.
4. **Tests pin the current number format**:
   `tests/api/test_order_item_modifiers.py` (`== 12.5`, `== 15.5`,
   modifier prices `1.0`/`2.0`),
   `tests/api/test_bill_adjustments.py` (`total_price == 0`),
   `tests/api/test_core_contracts.py` (comment "Item prices are JSON
   numbers").

No other monetary field returns a JSON number.

## Transition plan (Stage 2)

Do it in one change before the first client that shows prices (the
Waiter POS). Today no client reads fields 5–9: the only frontend is the
kitchen display, which shows no money, so the change breaks nothing that
exists; it is deferred only so that it lands together with the client
that consumes it, its types and its tests.

| Step | Field(s) | Change | Tests and docs |
| --- | --- | --- | --- |
| 1 | — | add a shared `Money` type in `app/schemas/` (`Decimal` with `max_digits=10, decimal_places=2`, serialized as a fixed two-decimal string, OpenAPI `type: string` with a strict `^-?\d{1,8}\.\d{2}$` pattern and an example) | unit test: `Decimal("2.5")` → `"2.50"`, `Decimal("0")` → `"0.00"`, negatives |
| 2 | 6, 7, 9 | `float` → `Money` | update the three tests above to expect strings; `test_core_contracts.py` asserts `"12.50"`/`"25.00"` and type `string` |
| 3 | 1–4 | `Decimal` → `Money` (format already right; makes the two-decimal promise explicit in OpenAPI) | contract test asserts every monetary field in every response schema is a `Money` string (one introspection test over `app.openapi()`) |
| 4 | 5 | accept strings only (`Money` with `strict` input); a JSON number gets 422 | test number → 422 `VALIDATION_ERROR`, string → 201; README curl already sends a string |
| 5 | new menu endpoints | `products.price`, `modifiers.price_adjustment` exposed as `Money` from the start | covered by step 3's introspection test |
| 6 | frontend | `type Money = string` in `frontend/src/api/types.ts`; display without float arithmetic (format the string; sums come from the API) | Vitest for formatting |
| 7 | docs | README "Money values" section → describe the final contract; this file marked done | — |

Notes for the change:

- Idempotent replays: `idempotency_keys.response_body` keeps the old
  format for up to 24 hours after deploy. Acceptable before any client
  exists; if clients exist by then, re-serialize stored bodies through the
  response model on replay (the model is already known per route) or wait
  out the 24 hours.
- No database migration is needed: storage is already `Numeric(10, 2)`.
- Step 4 is the only change that rejects previously accepted input; if a
  client already sends numbers by then, accept both for one release and
  log number inputs first.

Until then the current behaviour stays as it is and is documented in the
README.
