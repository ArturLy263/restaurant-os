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
  that is rolled back.
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
