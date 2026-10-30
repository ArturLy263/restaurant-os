# RestaurantOS waiter POS and kitchen display (frontend)

Vite + React + TypeScript client for waiters (POS) and station displays
(KDS). After sign-in the screen follows the user's permissions: waiters get
the POS, kitchen and bar staff the KDS, and managers can switch between the
two in the top bar. It talks to
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
   buttons are disabled while a request runs. If the connection drops
   while sending, the outcome is unknown: the next tap on Send reuses the
   same key, so the server replays the first result instead of sending the
   round twice.
6. **Money** — amounts are converted to integer cents before any
   arithmetic (`src/pos/money.ts`); menu prices arrive as decimal strings,
   order item prices still as JSON numbers (see
   `docs/monetary-contract.md`).

Orders opened by another waiter are read-only unless the user may manage
all orders (managers).

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
