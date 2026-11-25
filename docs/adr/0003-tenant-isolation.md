# 0003 — Tenant isolation from the signed-in user, backed by the database

**Status:** accepted (implemented: `core/dependencies.py`, services, migrations
`e1b7d3a9c4f2`, `d9e4b2a7c6f1`)

## Context

Every record belongs to one restaurant. A request must not read or change
another restaurant's data, and a bug must not be able to link two
restaurants' rows (an order item pointing at another restaurant's product,
a preparation at another restaurant's station).

## Decision

- The restaurant is **never taken from the request**. `get_current_user`
  reloads the user from the JWT subject (inactive users are refused), and
  every service filters by `current_user.restaurant_id`.
- Another restaurant's record answers exactly like a missing one (404), so
  its existence is not revealed.
- Permissions come from the user's role row. A role of another restaurant,
  or an unknown role name, grants nothing. Kitchen and bar users only work
  their assigned, active stations of their station type.
- The database is the backstop: triggers refuse preparations, order items
  and products that would link two restaurants, and the cashier tables
  reference each other through `(id, restaurant_id)` keys.

## Consequences

- One database, one schema, row-level ownership: simple to run and test,
  enough for a single-group deployment.
- The triggers check inserts and updates of the linking columns; rows
  written before the migration are not re-validated (the upgrade only logs
  them).
