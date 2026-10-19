# RestaurantOS

Restaurant management platform designed from
real restaurant operational experience.

## Goal

Build a system that connects:

- Tables
- Orders
- Menu
- Kitchen
- Printers
- Payments
- Inventory
- Analytics

## Status

🟡 Project discovery / planning

## Project layout

```text
alembic.ini                 Alembic configuration (run alembic from the repo root)
docker-compose.yml          PostgreSQL 16 for local development (port 5433)
pytest.ini                  pytest configuration and test markers
backend/app/                FastAPI application (entry point: app.main:app)
backend/alembic/            database migrations
backend/requirements.txt    pinned runtime dependencies
backend/requirements-test.txt  pinned test dependencies (includes runtime)
backend/constraints.txt     exact versions of all transitive dependencies
backend/seed.py             demo data for an empty database (run once)
backend/tests/              test suite
.github/workflows/ci.yml    CI: unit, integration and migration tests
```

## Development setup

Requirements: Python 3.14 and Docker. All commands run from the
repository root.

1. Create a virtual environment and install the dependencies:

   ```bash
   python3.14 -m venv .venv
   source .venv/bin/activate
   pip install -r backend/requirements-test.txt -c backend/constraints.txt
   pip check
   ```

   To run the API only, `backend/requirements.txt` is enough. The
   constraints file pins every transitive dependency to the versions the
   project is tested with.

2. Configure the environment and adjust the values:

   ```bash
   cp .env.example .env
   ```

   | Variable | Used for |
   | --- | --- |
   | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | the Docker PostgreSQL container |
   | `DATABASE_URL` | the API, Alembic and seed script (development database) |
   | `JWT_SECRET_KEY` | signing access tokens; use a long random value |
   | `ADMIN_PASSWORD` | password of the demo admin created by `seed.py` |
   | `TEST_DATABASE_URL` | optional, see [Tests](#tests) |

   Environment variables take precedence over `.env`.
   `REQUIRE_TEST_DATABASE` (see [Tests](#tests)) is read from the
   environment only.

3. Start PostgreSQL:

   ```bash
   docker compose up -d postgres
   ```

4. Apply the migrations to the development database:

   ```bash
   alembic upgrade head
   alembic current
   ```

5. Load the demo data (restaurant, admin user, menu, tables, kitchen
   station, modifiers):

   ```bash
   python backend/seed.py
   ```

   > **Warning:** `seed.py` is not safe to run repeatedly. Run it
   > **once**, on a freshly migrated, empty database. It always inserts a
   > new demo restaurant and never checks for or updates existing data:
   > on an already seeded database it aborts with a duplicate-key error
   > for `admin@restaurantos.com` (and rolls back), and on a database
   > with other data but no demo admin it adds another demo restaurant
   > next to it. To start over, use a fresh database.
   > (`backend/seed_modifiers.py` only adds the burger modifiers to
   > databases seeded before `seed.py` included them.)

6. Start the API:

   ```bash
   uvicorn app.main:app --app-dir backend --reload
   ```

   Check it with `curl http://127.0.0.1:8000/health`; the interactive API
   docs are at <http://127.0.0.1:8000/docs>. Log in as
   `admin@restaurantos.com` with `ADMIN_PASSWORD`.

## API conventions

### Errors

Every error response, whatever its status, has the same body:

```json
{
  "detail": "Payment exceeds remaining bill amount",
  "error": {
    "code": "PAYMENT_EXCEEDS_AMOUNT_DUE",
    "message": "Payment exceeds remaining bill amount",
    "request_id": "3f2b8c1e9d4a4f0b8e6c2a7d5b1e0f93"
  }
}
```

- `error.code` is stable; program against it. The full list is
  `ErrorCode` in `backend/app/core/errors.py` (e.g. `ORDER_NOT_FOUND`,
  `TABLE_NOT_AVAILABLE`, `BILL_NOT_OPEN`, `ORDER_HAS_UNSENT_ITEMS`).
  Errors raised by the framework get a code from their status
  (`NOT_FOUND`, `NOT_AUTHENTICATED`, ...).
- `error.message` is for people and may change.
- `detail` is kept for compatibility with existing clients. It equals
  `message`, except for request validation errors (422,
  `VALIDATION_ERROR`), where it is still FastAPI's list of field errors.
- Unexpected failures return 500 `INTERNAL_ERROR` with the message
  `Internal server error`; the exception is logged on the server, never
  returned.
- Every response, successful or not, has an `X-Request-ID` header, equal
  to `error.request_id` on errors and included in the server log line of
  unexpected errors. A client may send its own `X-Request-ID` (1-128
  characters from `A-Z a-z 0-9 . _ : -`); anything else is replaced by a
  generated ID.

Status codes: 400/422 for invalid input, 401 for missing or invalid
credentials, 403 when the user's role lacks the permission
(`PERMISSION_DENIED`) or the order belongs to another user, 404 for
missing resources and for other restaurants' resources, 409 when the
current state forbids the operation.

### Roles and permissions

Every endpoint except `POST /api/v1/auth/login`, `GET /health` and
`GET /api/v1/me` requires a permission, checked on the backend for each
request. A user's permissions come from their role: the `roles` row of
their restaurant whose `name` is one of the codes below (exact, upper
case). The role is read from the database on every request, so changing a
user's role takes effect without a new token.

| Permission | Endpoints | WAITER | KITCHEN | BAR | CASHIER | MANAGER, ADMIN |
| --- | --- | :-: | :-: | :-: | :-: | :-: |
| `orders:create` | `POST /orders` | ✓ | | | | ✓ |
| `orders:add_items` | `POST /orders/{id}/items` | ✓ | | | | ✓ |
| `orders:send` | `POST /orders/{id}/send` | ✓ | | | | ✓ |
| `orders:view` | `GET /orders/{id}` | ✓ | | | ✓ | ✓ |
| `orders:cancel_items` | `POST /orders/{id}/items/{item_id}/cancel` (unbilled items) | ✓ | | | | ✓ |
| `orders:void` | `POST /orders/{id}/void` | ✓ | | | | ✓ |
| `orders:manage_all` | modify orders created by others (see below) | | | | | ✓ |
| `preparations:view` | `GET /stations/{id}/preparations` | | ✓ | ✓ | | ✓ |
| `preparations:update` | `POST /preparations/{id}/start`, `/ready` | | ✓ | ✓ | | ✓ |
| `preparations:serve` | `POST /preparations/{id}/serve` | ✓ | ✓ | ✓ | | ✓ |
| `preparations:all_stations` | every station, not only assigned ones (see below) | ✓ | | | | ✓ |
| `bills:create` | `POST /bills/order/{order_id}` | | | | ✓ | ✓ |
| `bills:adjust` | `POST /bills/{id}/items/{item_id}/cancel`; voiding a billed order | | | | ✓ | ✓ |
| `bills:view` | `GET /bills/{id}` | | | | ✓ | ✓ |
| `payments:create` | `POST /bills/{id}/payments` | | | | ✓ | ✓ |
| `events:view` | `GET /events` | | | | | ✓ |
| `menu:manage`, `tables:manage` | none yet | | | | | ✓ |

(Paths are under `/api/v1`.) `ADMIN` is the administrator role created by
`seed.py`; it has every permission, like `MANAGER`. The policy lives in
`backend/app/core/permissions.py`.

- A missing permission is 403 `PERMISSION_DENIED`
  (`"Missing permission: payments:create"`). It is checked before the
  endpoint looks anything up, so it reveals nothing about the resource.
- A role name that is not listed (e.g. `SOMMELIER` or `waiter`), or a role
  of another restaurant, grants no permissions.
- Adding items to, sending, cancelling items of and voiding an order is
  limited to the user who created it (403 `ORDER_ACCESS_DENIED`), unless
  the user has `orders:manage_all` (managers). Viewing is not: any user
  with `orders:view` sees every order of the restaurant. The override only
  lifts the creator rule; every state and financial rule still applies.
- Once an order has a bill, its items are cancelled only through the
  bill: `POST /bills/{bill_id}/items/{item_id}/cancel` (`bills:adjust`)
  records an `ITEM_CANCELLATION` adjustment; the bill's original total
  never changes, and the bill must be open and unpaid, as before. The
  order endpoint answers 409 `ORDER_BILLED` for billed orders, for every
  role, so it can never change an issued bill. Voiding an order that has
  a bill (which voids the bill) also requires `bills:adjust`, so in
  practice a manager.
- Preparations move `PENDING → IN_PROGRESS → READY → SERVED`; anything
  else is 409 `INVALID_PREPARATION_TRANSITION`. Waiters may only serve
  (`READY → SERVED`). Transitions lock the order, like cancellations, so
  a cancelled preparation cannot be revived.
- Audit events record the user who acted (`user_id`); events about an
  order also record its creator as `order_created_by` in their metadata.
- Another restaurant's orders, bills, stations and preparations are
  reported exactly like missing ones (404), never as 403.
- Kitchen and bar users work only the stations assigned to them (see
  [Station scope](#station-scope)).

There is no API to manage roles or users yet; `seed.py` creates only the
`ADMIN` role. Other roles are `roles` rows with the codes above.

#### Station scope

A user without `preparations:all_stations` (in practice `KITCHEN` and
`BAR`) may view a station's queue and move its preparations only for
stations assigned to them in the `station_assignments` table. An
assignment counts only if the station

- belongs to the user's restaurant,
- is active (`stations.is_active`), and
- has the type of the user's role: `KITCHEN` users need `KITCHEN`
  stations, `BAR` users `BAR` stations (`stations.type`).

Anything else grants nothing: a Kitchen or Bar user without assignments
has no stations, and assignment rows of other roles are ignored.
Managers, admins and waiters (who serve dishes from every station) have
`preparations:all_stations`. Assignments are read on every request, so
adding or removing one applies immediately.

Outside their scope, the queue and every transition answer 403
`STATION_ACCESS_DENIED`. The queue checks the scope before looking the
station up, so unassigned, missing and other restaurants' stations get
the same answer; a preparation of another restaurant stays 404.

There is no API for assignments yet. Until there is, add them with SQL on
the database in question:

```sql
INSERT INTO station_assignments (user_id, station_id)
SELECT u.id, s.id
FROM users u
JOIN stations s ON s.restaurant_id = u.restaurant_id
WHERE u.email = 'cook@example.com' AND s.name = 'Kitchen';

-- and to remove one:
DELETE FROM station_assignments
WHERE user_id = (SELECT id FROM users WHERE email = 'cook@example.com');
```

`GET /api/v1/me` returns the authenticated user, for clients deciding what
to show:

```json
{
  "id": "6c1e…",
  "name": "Ana",
  "email": "ana@example.com",
  "restaurant_id": "0f3a…",
  "role": {"id": "9d2b…", "name": "CASHIER"},
  "permissions": ["bills:create", "bills:view", "orders:view", "payments:create"],
  "stations": []
}
```

`role` is `null` if the user's role belongs to another restaurant;
`permissions` is empty for an unrecognized role. `stations` lists the
stations whose preparations the user may work (`{id, name, type}`, only
valid assignments): `null` means every station, `[]` none.

### Idempotency keys

These mutations accept an `Idempotency-Key` header:

| Operation | Endpoint |
| --- | --- |
| Open an order for a table | `POST /api/v1/orders` |
| Send an order's new items | `POST /api/v1/orders/{order_id}/send` |
| Cancel an order item (no bill yet) | `POST /api/v1/orders/{order_id}/items/{item_id}/cancel` |
| Cancel a billed item (bill adjustment) | `POST /api/v1/bills/{bill_id}/items/{item_id}/cancel` |
| Void (cancel) an order | `POST /api/v1/orders/{order_id}/void` |
| Take a payment (closes the order when fully paid) | `POST /api/v1/bills/{bill_id}/payments` |

Clients should generate a new key (e.g. a UUID) for each operation and
reuse it only when retrying that same operation, for example after a
timeout or a lost connection.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/bills/$BILL_ID/payments \
  -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: 0b6f2a52-6d3e-4f61-a3c5-2b8f1f4c9e10" \
  -H "Content-Type: application/json" \
  -d '{"amount": "15.00", "method": "CARD"}'
```

- **Without a key** the request behaves exactly as before: no
  deduplication. A retried payment is a second payment (if the bill
  allows it); a retried send or void gets a 409. The key is optional so
  existing clients keep working; new clients should always send it.
- **Format:** 1-255 printable ASCII characters without spaces; otherwise
  400 `IDEMPOTENCY_KEY_INVALID`.
- **Scope:** keys belong to the authenticated user within their
  restaurant. The same key from another user or restaurant is a different
  key and never returns someone else's result.
- **Same key, same request:** the operation is not run again. The
  response is the stored status code and body of the first request,
  with an extra `Idempotent-Replayed: true` header. "Same request" means
  the same method, endpoint, path parameters and body; JSON key order and
  amount formatting (`15`, `"15.0"`, `"15.00"`) do not matter.
- **Same key, different request:** 422 `IDEMPOTENCY_KEY_REUSED`, every
  time, and nothing is run.
- **Concurrent requests with the same key:** the second waits until the
  first finishes, then gets its stored result. The operation runs once.
- **When the operation fails:** only successful (2xx) results are stored.
  An error response (e.g. 409) or a crash rolls back the business change
  and the key together, so nothing is stored and a retry with the same key
  runs the operation again against the current state. A request waiting on
  a failed one runs the operation itself.
- **Atomicity:** the business change, its audit events, the key and the
  stored response are committed in one database transaction. A committed
  change always has its stored result, and a stored result always belongs
  to a committed change.
- **Lifetime:** stored results are kept for at least 24 hours. After that
  the key may be used again as a new key (expired records are replaced
  when their key is reused; there is no background purge yet).
- A replay returns the result as it was, not the current state: replaying
  a send does not send items added since. Use a new key for the next send.

### Concurrency guarantees

- Writes to an order, its items, its bill and its payments lock the order
  row first (`SELECT ... FOR UPDATE`), so they apply one at a time.
- Opening an order locks the table row. A table has at most one active
  (`OPEN` or `SENT`) order: a concurrent second request gets 409
  `TABLE_NOT_AVAILABLE` (or `TABLE_HAS_ACTIVE_ORDER` if the table is
  marked available while an order is still active). Once the order is
  `CLOSED` (fully paid) or `CANCELLED` (voided) the table is free again.
- This is enforced by the lock, not by a unique index: existing
  development databases can contain several active orders for one table
  from before this rule, and a migration must not fail on them or rewrite
  them. Such tables stay `OCCUPIED` and simply reject new orders until
  their orders are closed or voided.

### Transactional outbox

Selected mutations also write an integration event to the
`outbox_events` table, for later delivery to kitchen/bar displays and
other consumers. `published_at` stays `NULL` until the delivery worker
below has delivered the event.

The outbox is separate from the audit history. `operational_events`
records who did what, for people; `outbox_events` is delivery data for
systems, carries no actor and no free text, and is not exposed by the API.

| Event type | Version | Written when | Payload |
| --- | --- | --- | --- |
| `order.sent` | 1 | an order's new items are sent (`POST /orders/{id}/send`) | `order_id`, `table_id`, `initial`, `items[]`: `order_item_id`, `preparation_id`, `station_id`, `product_id`, `quantity` |
| `preparation.status_changed` | 1 | a preparation is started, ready or served, or cancelled with its item | `preparation_id`, `order_id`, `order_item_id`, `station_id`, `previous_status`, `status` |

Every event has a stable `id` (consumers deduplicate on it), its
`event_type` and `schema_version`, the `restaurant_id` it belongs to, its
aggregate (`aggregate_type` `order`, `aggregate_id` the order) and
`created_at`.

#### Event contracts

Each (`event_type`, `schema_version`) pair has one payload model in
`backend/app/schemas/outbox.py`, listed in `CONTRACTS`. `event_type` says
what happened and never changes meaning; `schema_version` numbers the
shape and rules of that type's payload. The envelope (the columns above)
comes from the contract, so `aggregate_id` is always the payload's
`order_id`.

A payload is valid only if it is exactly the canonical JSON of its model:
strict JSON types (`2`, not `"2"` or `2.0`; `true`, not `1`), lower-case
hyphenated UUIDs, no missing or unknown fields, and the version's rules.
v1 requires `quantity` >= 1 and at least one sent item, each item and
preparation once, and only the five transitions it describes
(`PENDING`->`IN_PROGRESS`->`READY`->`SERVED`, `PENDING`/`IN_PROGRESS`->
`CANCELLED`). Every event is checked to parse back before it is written;
one that does not fails the change (500) like any outbox failure.

Consumers call `parse_payload(event_type, schema_version, payload)`:

- `UnsupportedEventError`: the pair is not known to this consumer (e.g. a
  newer version during a rolling deploy). It is not malformed; skip or
  park it, never guess its fields.
- `InvalidEventPayloadError`: a known pair whose payload breaks its
  contract. Its message names the failing fields, never their values.

Both derive from `EventContractError`. The delivery worker does not
parse payloads: it delivers any committed event, so a consumer's version
support never dead-letters events or blocks an order.

Compatibility rules for changing a contract:

- **Same version:** documentation, or a new optional field consumers of
  that version may ignore. Payloads forbid unknown fields, so every
  consumer of the version must be updated first; when in doubt, add a
  version.
- **New version** (a new model next to the old one): renaming, removing,
  retyping or making a field required, changing a field's meaning, and
  widening or narrowing allowed values (a new status or transition).
  Producers switch to it only once consumers support it.
- **Never:** editing or removing a version while its events may still be
  stored or undelivered, or rewriting stored payloads. A new meaning is a
  new `event_type`, not a new version.
- Event types are added only together with a consumer.

`backend/tests/unit/test_event_contracts.py` holds a golden v1 payload
per contract; they must keep parsing and dumping unchanged.

- **Atomic:** the event is written in the same transaction as the change
  it describes (and its audit events and Idempotency-Key result). A
  rollback leaves neither; if the event cannot be written, the change
  fails (500) and nothing is committed.
- **Once per change:** a rejected request (404, 403, 409, ...) writes no
  event, and an Idempotency-Key replay does not run the operation again,
  so it writes no second event.
- **Order:** an order's events are written under its row lock, and
  `created_at` is taken at insert time (`clock_timestamp()`), so ordering
  one order's events by `created_at` follows the order the changes were
  committed in. There is no global order across orders.
- Events are not backfilled for changes made before the table existed.

#### Delivery worker

`app.workers.outbox_worker` hands committed events to a publisher. Nothing
starts it automatically: importing the application runs no background
work. Run it as its own process, from `backend/`:

```bash
cd backend
python -m app.workers.outbox_worker            # deliver until SIGINT/SIGTERM
python -m app.workers.outbox_worker cleanup    # delete old delivered events, then exit
```

On SIGINT or SIGTERM the worker finishes the event it is delivering (at
most the publish timeout), stops, closes its database connections and
exits with status 0. Its connections show up in `pg_stat_activity` as
`application_name = 'restaurantos-outbox-worker'`. An invalid setting
stops it before it connects. In code, `OutboxWorker(engine, publisher,
...)` offers `process_batch()` (one bounded pass) and `run(stop_event)`.

A publisher is any object with `publish(message)`: returning means
delivered, raising means not. The only transport so far,
`LoggingPublisher`, logs event metadata (never payloads).

Settings (environment or `.env`; out-of-range values are rejected):

| Variable | Default | Allowed | Meaning |
| --- | --- | --- | --- |
| `OUTBOX_WORKER_BATCH_SIZE` | 100 | 1-1000 | events per batch |
| `OUTBOX_WORKER_POLL_INTERVAL_SECONDS` | 1.0 | >0-300 | pause when there is nothing to do |
| `OUTBOX_WORKER_RESTAURANT_ID` | unset (all) | a UUID | deliver/clean up only this restaurant's events |
| `OUTBOX_MAX_ATTEMPTS` | 10 | 1-100 | failed attempts before an event is dead-lettered |
| `OUTBOX_PUBLISH_TIMEOUT_SECONDS` | 30 | >0-300 | how long to wait for one publisher call |
| `OUTBOX_RETENTION_DAYS` | 7 | 1-3650 | `cleanup` deletes events delivered longer ago |
| `OUTBOX_CLEANUP_BATCH_SIZE` | 1000 | 1-10000 | rows deleted per cleanup transaction |

- **One event per transaction:** the worker claims one due, undelivered,
  not dead-lettered event with `SELECT ... FOR UPDATE SKIP LOCKED`, calls
  the publisher while holding the row lock, and sets `published_at` only
  after the publisher returned. Concurrent workers skip claimed rows, so
  an event is never handed to two publishers at the same time.
- **At least once, not exactly once:** if the worker dies between a
  successful publish and its commit, the event stays undelivered and is
  published again. Consumers must deduplicate on the event `id`.
- **Failures:** a failed attempt increments `attempts`, sets
  `last_attempt_at`, stores the exception type and message (truncated to
  1000 characters) in `last_error`, and delays the next attempt
  (`next_attempt_at`: 5 s, doubling, at most 5 minutes). A batch tries
  each event at most once. Logs name the event and the exception type,
  never the payload or the exception message.
- **Dead letters:** the `OUTBOX_MAX_ATTEMPTS`-th failed attempt sets
  `dead_lettered_at` instead of a retry time. A dead-lettered event is
  never retried automatically, keeps its id and payload, and is logged at
  ERROR level. It stays undelivered, so **it keeps holding back every
  later event of its order** (other orders are not affected). There is no
  recovery tooling yet; do not edit these rows by hand without a decided
  recovery policy.
- **Publish timeout:** the publisher runs in a separate thread, and the
  worker stops waiting for it after `OUTBOX_PUBLISH_TIMEOUT_SECONDS`.
  Python cannot interrupt a blocking call, so **a timed-out call is not
  cancelled**: it is abandoned and may still complete, and deliver, later.
  The attempt is recorded as failed (`PublishTimeout` in `last_error`),
  which releases the row lock; the event is retried, so a late-completing
  call can produce a duplicate (at least once). While an abandoned call is
  still running the worker claims no events, so it never has more than one
  publisher call in flight. The timeout bounds how long the worker waits
  and holds a row lock, not how long the publisher runs.
- **Order:** an event is not claimed while an older event of the same
  order is undelivered (claimed elsewhere, waiting for a retry, failing or
  dead-lettered), so each order's events are delivered in order.
- **Tenants:** each message carries its event's `restaurant_id`;
  `OUTBOX_WORKER_RESTAURANT_ID` limits a worker to one restaurant. The
  worker only updates the delivery columns, never an event's type,
  version, scope, aggregate or payload.

Inspecting dead letters and stuck events (read-only):

```sql
-- Dead-lettered events, newest first
SELECT id, restaurant_id, event_type, aggregate_id, attempts,
       last_attempt_at, dead_lettered_at, last_error
FROM outbox_events
WHERE dead_lettered_at IS NOT NULL
ORDER BY dead_lettered_at DESC;

-- Events held back behind a dead letter of the same order
SELECT later.id, later.event_type, later.aggregate_id, later.created_at
FROM outbox_events dead
JOIN outbox_events later
  ON later.aggregate_type = dead.aggregate_type
 AND later.aggregate_id = dead.aggregate_id
 AND (later.created_at, later.id) > (dead.created_at, dead.id)
 AND later.published_at IS NULL
WHERE dead.dead_lettered_at IS NOT NULL;
```

`last_error` holds the exception message of the publisher, so read it
with the same care as the payload.

**Retention:** `cleanup` deletes events delivered more than
`OUTBOX_RETENTION_DAYS` ago (never less than one day), oldest delivery
first, in transactions of at most `OUTBOX_CLEANUP_BATCH_SIZE` rows
(`DELETE ... WHERE id IN (SELECT ... LIMIT n FOR UPDATE SKIP LOCKED)`),
at most `--max-batches` (default 100) per run. Undelivered and
dead-lettered events are never deleted. It runs only when invoked, e.g.
from cron: `cd backend && python -m app.workers.outbox_worker cleanup`.

## Tests

```bash
pytest                    # everything
pytest -m unit            # no database needed
pytest -m integration     # uses the PostgreSQL test database
pytest -m migrations      # migration round-trips on a disposable database
```

Every test is marked automatically from the fixtures it uses: tests that
use the test database are `integration`, tests that use a disposable
migration database are `migrations`, all others are `unit`. Markers are
strict, so a misspelled marker fails collection.

### Test databases

Tests never touch the development database.

- **Integration tests** use a separate database named after
  `DATABASE_URL` with `_test` appended (e.g. `restaurant_os_test`), or
  `TEST_DATABASE_URL` if set; the name must end with `_test`. It is
  created and migrated automatically, and each test runs in a transaction
  that is rolled back. Concurrency tests
  (`backend/tests/integration/test_mutation_concurrency.py`) need real
  commits on separate connections instead: they commit their own
  restaurant to the test database and delete everything it owns
  afterwards. They order concurrent requests by holding row locks and
  waiting on PostgreSQL's lock graph (`pg_blocking_pids`), not on timers
  (see `backend/tests/concurrency.py`).
- **Migration tests** create a new, empty database on the same server
  under a random name (`restaurantos_disposable_<random>_test`), tagged
  with a random token. Before migrating or dropping it they verify that
  the connection really reaches that database and carries that token, and
  they drop it afterwards, also when a test fails. The user in
  `TEST_DATABASE_URL` therefore needs the `CREATEDB` privilege (the Docker
  default user has it). If a run is killed, a leftover
  `restaurantos_disposable_*_test` database can be dropped by hand.

### When PostgreSQL is unavailable

By default, database tests are **skipped** when PostgreSQL is unreachable,
so `pytest -m unit` and quick local runs work without Docker. Read the
skip summary: a green run with skipped database tests did not test the
database.

Set `REQUIRE_TEST_DATABASE=1` to make database tests mandatory: an
unreachable server, or any skipped `integration`/`migrations` test, then
fails the run. CI always sets it.

```bash
REQUIRE_TEST_DATABASE=1 pytest
```

### Migration checks

`pytest -m migrations` verifies on a disposable database that

- the migration history has a single head,
- upgrading from an empty database to head and downgrading back to base
  works, leaving the schema empty,
- every migration's downgrade exactly reverses its upgrade,
- the SQLAlchemy models match the migrated schema (the equivalent of
  `alembic check`).

The model comparison uses Alembic autogenerate and has its limits: it
compares tables, columns, types, nullability, indexes, unique constraints
and foreign keys, but not server defaults, CHECK constraints, triggers,
sequences or anything created with raw SQL, and it cannot verify data
migrations. A renamed column appears as a drop plus an add. Review
generated migrations by hand.

To create a migration after changing a model:

```bash
alembic revision --autogenerate -m "describe the change"
```

## Continuous integration

`.github/workflows/ci.yml` runs on pushes to `main` and `public-main` and
on pull requests, with Python 3.14 and a PostgreSQL 16 service container.
It installs the pinned dependencies, runs `pip check`, the unit tests
without a database, a negative check that a missing test database fails
the run, the integration tests and the migration tests, all with
`REQUIRE_TEST_DATABASE=1`. It uses only throwaway credentials for the
job-local database and needs no secrets.

## Updating dependencies

Edit the pins in `backend/requirements*.txt`, then regenerate the
constraints from a fresh environment:

```bash
python3.14 -m venv /tmp/restaurantos-deps
/tmp/restaurantos-deps/bin/pip install -r backend/requirements-test.txt
/tmp/restaurantos-deps/bin/pip check
/tmp/restaurantos-deps/bin/pip freeze
```

Replace the package lines of `backend/constraints.txt` with the
`pip freeze` output and run the full test suite.
