# RestaurantOS waiter POS and kitchen display (frontend)

Vite + React + TypeScript client for waiters (POS) and station displays
(KDS). After sign-in the screen follows the user's permissions: waiters get
the POS, kitchen and bar staff the KDS, cashiers the cashier workspace, and
managers can switch between them in the top bar. It talks to
the FastAPI backend only through `/api`; in development the Vite server
proxies `/api` (HTTP and WebSocket) to `http://127.0.0.1:8000`, so no CORS
setup is needed.

## Run

Requires Node 20.19+ (or 22.12+) and the API running on port 8000 (see the
root README).

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173
```

Point the proxy elsewhere with `API_PROXY_TARGET=http://host:port npm run dev`.

| Script              | What it does                                  |
| ------------------- | --------------------------------------------- |
| `npm run dev`       | Dev server with the `/api` proxy              |
| `npm test`          | Unit and component tests (Vitest, jsdom)      |
| `npm run typecheck` | TypeScript check only                         |
| `npm run build`     | Type check, then production build in `dist/`  |
| `npm run preview`   | Serve the production build (also proxies `/api`) |
| `npm run e2e`       | Browser test in headless Chrome (see below)   |
| `npm run e2e:pos`   | Real-stack POS + KDS acceptance (see below)   |

## Waiter POS

1. **Tables** — `GET /api/v1/tables`, refreshed every 20 s: number, name,
   seats, status and the open order (not sent / sent counts, another
   waiter's). A free table asks for the number of guests and opens an
   order (`POST /api/v1/orders`); a table with an order opens it.
2. **Order** — `GET /api/v1/menu` and `GET /api/v1/orders/{id}` (the order
   is refreshed every 15 s, so kitchen progress shows up). Menu on the
   left (categories, products with price, station and availability),
   order on the right (stacked on narrow screens).
3. **Adding** — a product without options is added with one tap; one with
   modifier groups opens a dialog that enforces the group rules
   (required, at most N) and takes a quantity and a note. Unsent items are
   stored on the server right away (status `ADDED`), so the order survives
   a reload or another device.
4. **Next round** — unsent items show their destination station; quantity
   (`PATCH`), note (`PATCH`) and remove (`POST …/cancel`) apply to them
   only. **Send round** sends them all in one request and reports where
   they went; items already sent show their station and preparation status.
5. **No duplicates** — every change carries a new `Idempotency-Key`;
   buttons are disabled while a request runs, and taps on a product, an
   item or Send that arrive before the screen re-renders are ignored. If
   the connection drops while sending, the order is reloaded: if it shows
   the round as sent, the round is reported as sent; otherwise the outcome
   is unknown and the next tap on Send reuses the same key, so the server
   replays the first result instead of sending the round twice.
   When the tables or the order cannot be reloaded, the screen keeps what
   it shows and says that it may be out of date until a reload succeeds.
6. **Money** — amounts are converted to integer cents before any
   arithmetic (`src/pos/money.ts`); menu prices arrive as decimal strings,
   order item prices still as JSON numbers (see
   `docs/monetary-contract.md`).

Orders opened by another waiter are read-only unless the user may manage
all orders (managers).

## Cashier workspace (Stage 3.2.1 foundation)

Offered to users with a cashier section's permission (`cash_shifts:view`,
`bills:view` or `cash_reports:view`); managers switch to it in the top bar.

- **Sections** — Overview, Orders, Payments, Cash Register, Reports and
  Settings. Each is shown only when `/me` lists the permissions its
  endpoints need (`cashier/sections.ts`); the backend still checks every
  request, and a 403 shows a "No access" state. Only Overview and Settings
  are functional so far; the others are placeholders.
- **Overview** — `GET /api/v1/cash/register` every 15 s while the section
  is shown and the tab is visible: register, business day, shift, the
  signed-in employee and the previous closing. Expected cash appears only
  with `cash_drawer:view_expected` (cashiers count blind); the screen checks
  the permission even though the backend already leaves the amount out.
- **Pending operations** — `cashier/pendingOperations.ts` stores every
  cashier mutation in `localStorage` (`restaurantos.cashier.pendingOperations.v2`;
  records under the earlier `.v1` key are merged in and that key is removed
  only after the new one was written) before sending it: user, restaurant,
  path, body and its SHA-256, Idempotency-Key, status, revision. Rules:
  - *Uncertainty is permanent*: once an attempt may have been committed
    (network error, timeout, 5xx, a reload mid-request), the operation
    stays *unknown* until the server answers a retry with success. A later
    401/403/4xx comes from checks that run before the backend looks the key
    up, so it never downgrades the operation, and it cannot be dismissed.
  - *No cross-tab loss*: every write re-reads storage and merges by id
    (deterministically: revision, safer status, then a total order), under
    a Web Lock where the browser has them. Uncertainty, checks, server
    confirmations and expiry survive every merge.
  - *Tombstones*: a dismissal (of something that certainly changed nothing)
    never deletes a copy in doubt or newer than it; a retention removal (a
    resolved record purged after 7 days) supersedes every older copy, so a
    stale tab cannot bring it — or its body — back.
  - *Live vs abandoned*: the sending tab holds a per-operation Web Lock and
    refreshes a heartbeat every 10 s. With Web Locks a request whose lock
    is free may be retried at once (e.g. after a reload); for up to 90 s
    after its last heartbeat it is shown as *outcome being recovered* (its
    tab may hold a server answer it could not save) and cannot be checked
    or dismissed. Tabs also tell each other about server confirmations
    over `BroadcastChannel` where available. Without Web Locks a request
    counts as abandoned after 90 s without a heartbeat.
  - *Without Web Locks* (e.g. plain HTTP, where `navigator.locks` does not
    exist) nothing on the frontend makes two tabs mutually exclusive, and
    two tabs of the same employee may send the same stored request at the
    same moment. Safety then rests on the backend's idempotency contract
    (`backend/app/api/idempotency.py`): keys are scoped to the user, a
    request whose key is in use by a request still running waits for it
    and then replays its stored result, and the same key with a different
    body is refused (422). Both tabs send the identical key and body, so
    the operation runs at most once. This has not been exercised with real
    browsers yet.
  - *Retry window*: 20 h (the backend keeps keys 24 h). For anything that
    may have been committed only server time counts: the record stores a
    lower bound of the server time at creation, and a retry first reads
    the `Date` header of a fresh `GET /api/v1/me` (`cache: "no-store"`; no
    other response is used). No anchor or no fresh time means no retry; a
    401 ends the session and sends nothing. The device clock never expires
    such an operation, so clock corrections cannot shorten or extend it.
    Expiry proven by server time is saved at once, deletes the body and is
    permanent. Upkeep (saving unsaved records, expiry proven by server
    time, purging finished records) runs every minute while someone is
    signed in, on any screen, and stops at sign-out.
  - *Clock assumption (operational, not verified by these tests)*: retry
    authorization uses the API host's clock (`Date` header), while the
    backend expires idempotency keys with PostgreSQL `now()` (the database
    host's clock). The 20 h window leaves a 4 h margin before the backend's
    24 h only if the API and database clocks differ by less than that
    margin — keep both hosts time-synchronized. A server clock that steps
    backwards stretches the effective window by the size of the step; the
    design relies on such steps staying well within the margin.
  - *Checks are records*: a manager (`cash_reports:view`, same restaurant)
    can record a check of another employee's unknown operation — never of
    their own; the store enforces it. It keeps who checked, when, a note
    and the previous outcome, labelled as not confirmed by the server.
  - *Confirmations*: a server success is never turned back into unknown;
    if storage cannot take it, it is kept in the tab with a warning and
    saved by the next upkeep.
  - *Shared devices*: other employees' operations show only type, time and
    who started them. Bodies are dropped once a retry is no longer
    possible; confirmed, checked and refused records are purged after 7
    days; unresolved ones are never purged or cleared by sign-out. No
    tokens or card data are stored: credential and card field names are
    refused in any spelling, and card numbers in text (grouped by spaces,
    dots or dashes) or as numbers are refused.
  - *No storage*: operations are kept in memory with a warning that stays
    until a write to storage actually succeeds.
  - *Browsers*: no regex lookbehind is used (Safari before 16.4 cannot parse
    it, and one unparsable expression stops the whole bundle). The build
    keeps Vite's default target.
- **Theme** — Premium Dark by default, Light in Settings; remembered per
  device and applied by an inline script in `index.html` before React
  renders. It only restyles the cashier workspace (`<html
  data-workspace="cashier">`), not the POS or the kitchen display.
- **Money** — `src/lib/money.ts` (shared with the POS) converts API amounts
  to integer cents; no float arithmetic.

## How the kitchen display works

1. **Sign in** — `POST /api/v1/auth/login`, then `GET /api/v1/me`. The
   access token is kept in `sessionStorage` (survives a reload, not closing
   the tab) and is only trusted once `/me` accepts it. The session ends when
   the token's `exp` passes or any request returns 401.
2. **Station** — `GET /api/v1/stations` lists the stations the user may
   view. A single station is opened directly; otherwise the user picks one.
3. **Queue** — `GET /api/v1/stations/{id}/preparations`, shown as three
   columns (New / Preparing / Ready) with one ticket per order: table,
   product, quantity, modifiers and received/started/ready times. Tickets
   of fully paid (`order_status` `CLOSED`) orders show a **Paid** badge and
   stay in the queue until served; payment is not service. Elapsed
   time is recomputed every 30 s.
4. **Live updates** — the queue opens the station's event stream,
   `/api/v1/stations/{id}/events` (same origin; proxied in development),
   and follows the backend's "Client synchronization" rules
   (`src/kds/stationLive.ts`):
   - The first message is `{"type": "authenticate", "token": ...}`; the
     token is never put in the URL. Nothing is relied on before the
     server answers `subscribed`.
   - The queue is loaded at once for display and again after every
     `subscribed`. Events that arrive during a load are buffered and
     reconciled with its result in arrival order.
   - Events are deduplicated on their `id` (the last 500, kept across
     reconnects) and keyed by preparation: `preparation.status_changed`
     (v1, v2) moves or removes a preparation when its `previous_status`
     matches what is shown and is otherwise ignored; `order.sent` with an
     unknown preparation reloads the queue (events carry no product or
     table). `resync_required`, unknown event types or versions and
     unreadable messages reload the queue too.
   - A dropped connection (or 4429, 1013, 1012, no `subscribed` within
     15 s) reconnects after 0.5–1 s, doubling up to 15–30 s, and reloads.
     When the browser comes back online or the display becomes visible
     again, a pending reconnect is brought forward (never sooner than 1 s
     after the previous attempt), and an open stream reloads the queue in
     case a sleeping device missed events. 4401 ends the session; 4403 and
     4404 stop live updates for the station without retrying.
   - *Refresh* always works and the queue is still refetched after an
     action. The stream never triggers actions.

   The toolbar status says only what the client can verify:

   | Status | Meaning |
   | --- | --- |
   | *Connecting…* | opening the stream, waiting for `subscribed` |
   | *Connected* | subscribed, but nothing delivered on this connection yet |
   | *Live* | the server delivered an event or `resync_required` on this connection |
   | *Reconnecting…* | the stream dropped; retrying with backoff |
   | *Live updates off* | refused (4401/4403/4404); the tooltip says why |

   Events only flow while the API runs with `OUTBOX_WEBSOCKET_DELIVERY=true`
   (one API process; see the root README). With it off the stream still
   subscribes, and the protocol has no way to say whether delivery is on,
   so the display stays *Connected*: an idle station and an API that
   delivers nothing look the same until the first event arrives. Each new
   connection starts at *Connected* again.
5. **Actions** — Start / Ready / Serve call
   `POST /api/v1/preparations/{id}/start|ready|serve`. Buttons are hidden
   when `/me` lacks the permission (`preparations:update` or
   `preparations:serve`); the backend still checks every request. A 409
   (changed or cancelled elsewhere) shows a notice and refetches.

## Real-stack acceptance test

`npm run e2e:pos` (`e2e/pos.e2e.mjs`) runs the Waiter POS and the Kitchen
and Bar displays together, in four headless Chrome instances (waiter,
cook, barman, a second waiter), against the Vite dev server, a FastAPI
process it starts with `OUTBOX_WEBSOCKET_DELIVERY=true`, and PostgreSQL.
It never uses the development or test database:
`scripts/pos_e2e/database.py` creates a disposable database (random name
and comment token, as in the migration tests) on the `.env` server,
migrates and seeds it, and drops it when the run ends.

It covers a first order (modifiers, a note, quantity changes, removal,
routing to Kitchen and Bar, prices against PostgreSQL), a second round,
preparation progress shown to the waiter, rapid taps, a lost response to
a send (with and without a failed reload), a round refused as a whole,
the API stopping and restarting (displays reconnect), redelivered outbox
events, tenant isolation and access rules, and the layout at 375, 768,
1024, 1440 and 1920 px. Needs Docker's PostgreSQL, the backend virtualenv
(`../.venv`, or `PYTHON`) and Chrome; screenshots and `layout.json` go to
a temporary directory (or `ARTIFACTS`). Takes about two minutes.

## Browser test

`npm run e2e` (`e2e/kds.e2e.mjs`) runs the display in headless Chrome
against the Vite dev server, with `e2e/mockApi.mjs` standing in for the API
(HTTP and a minimal WebSocket server), so it needs no backend, database or
extra packages: Chrome is driven over the DevTools protocol with Node's own
`fetch` and `WebSocket` (Node 22+). It signs in, opens a station and walks
the stream through `subscribed`, events (with a duplicate), a server restart
(1012), going offline and back online, a station change, 4403 and 4401; it
also checks the layout at 375, 768, 1024, 1280 and 1920 px and fails on
page errors, console errors or unexpected WebSocket frame errors.
Screenshots go to `e2e/artifacts/` (or `E2E_ARTIFACTS`). It finds Chrome at
the usual install paths or `CHROME_PATH`, and exits with status 2 when there
is none. It is not part of `npm test`, which stays browser-free.

Errors use the backend envelope (`error.code`, `error.message`,
`error.request_id`); see `src/api/client.ts`. Backend timestamps are naive
UTC and are parsed as UTC.
