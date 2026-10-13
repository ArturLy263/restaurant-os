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

Status codes are unchanged: 400/422 for invalid input, 401 for missing or
invalid credentials, 403 for other users' or other restaurants'
resources, 404 for missing ones, 409 when the current state forbids the
operation.

### Idempotency keys

These mutations accept an `Idempotency-Key` header:

| Operation | Endpoint |
| --- | --- |
| Open an order for a table | `POST /api/v1/orders` |
| Send an order's new items | `POST /api/v1/orders/{order_id}/send` |
| Cancel an order item | `POST /api/v1/orders/{order_id}/items/{item_id}/cancel` |
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
