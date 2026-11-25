# 0001 — Transactional outbox, delivered to station WebSockets

**Status:** accepted (implemented: `services/outbox_service.py`, `workers/outbox_worker.py`,
`realtime/`; migrations `f3a9c1e7b5d2`, `a7d3e5f1c9b4`, `c5e8a2d4f6b1`)

## Context

Kitchen and bar displays must learn about new rounds and status changes
quickly, but an order or preparation change must never be announced if its
transaction rolls back, and must never be lost if it commits. Publishing
straight from the request handler cannot guarantee either: the publish can
succeed while the commit fails, or the other way round.

## Decision

- Services write an `outbox_events` row **in the same transaction** as the
  change (`OutboxService.record` only adds it to the session). Payloads are
  versioned contracts (`schemas/outbox.py`) without actors or free text.
- A worker claims undelivered rows with `FOR UPDATE SKIP LOCKED`, one
  transaction per event, and sets `published_at` only after the publisher
  succeeded. Failures retry with backoff and are dead-lettered after
  `OUTBOX_MAX_ATTEMPTS`. An event is not claimed while an older event of the
  same order is undelivered, so each order's events stay in order.
- For the displays, the API process itself runs the worker
  (`OUTBOX_WEBSOCKET_DELIVERY=true`) and publishes into an in-memory hub of
  authenticated, station-scoped WebSocket streams. Unknown event types
  become `resync_required`.
- Clients treat the stream as a hint: they load the queue over HTTP after
  every `subscribed`, deduplicate on event ids, reload on
  `resync_required`, and refresh every 20 s while no live event has been
  confirmed on the connection.

## Consequences

- Delivery is at-least-once; the KDS is idempotent by event id and by
  reloading.
- The hub is per process: live delivery needs exactly one API process with
  the flag on. A multi-process deployment would need a shared broker; that
  is out of scope (see `docs/deployment-proposal.md`).
- The standalone worker (`python -m app.workers.outbox_worker`) refuses to
  run `run` while the in-process delivery is enabled, so events are not
  claimed by a process that cannot reach the streams.
