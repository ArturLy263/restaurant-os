# Architecture decision records

Short records of decisions that shape RestaurantOS. Each documents what the
code already does and why; none describes planned work.

| # | Decision |
| --- | --- |
| [0001](0001-transactional-outbox-and-websocket-delivery.md) | Transactional outbox, delivered to station WebSockets from the API process |
| [0002](0002-idempotency-keys.md) | Idempotency keys stored in the business transaction |
| [0003](0003-tenant-isolation.md) | Tenant isolation from the signed-in user, backed by the database |
| [0004](0004-monetary-representation.md) | Money as `Decimal` / `Numeric(10,2)` and decimal strings on the wire |
| [0005](0005-blind-count-and-two-person-handover.md) | Blind cash count and two-person shift handover |

Security hardening built on these (sign-in throttling, token revocation,
configuration checks, HTTP headers and the blind count without an oracle)
is described in [`docs/security.md`](../security.md).
