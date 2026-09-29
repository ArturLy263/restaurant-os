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

## Running tests

```bash
pip install -r backend/requirements-test.txt
docker compose up -d postgres
pytest
```

Tests never touch the development database. They use a separate
PostgreSQL database named after `DATABASE_URL` with `_test` appended
(override with `TEST_DATABASE_URL`; the name must end with `_test`).
It is created and migrated automatically, and each test runs in a
transaction that is rolled back. Database-backed tests are skipped
when PostgreSQL is unreachable.
