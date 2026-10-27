# RestaurantOS kitchen display (frontend)

Vite + React + TypeScript client for station displays (KDS). It talks to
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

## How it works

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
   time is recomputed every 30 s; the queue itself is refetched only on
   *Refresh* and after an action.
4. **Actions** — Start / Ready / Serve call
   `POST /api/v1/preparations/{id}/start|ready|serve`. Buttons are hidden
   when `/me` lacks the permission (`preparations:update` or
   `preparations:serve`); the backend still checks every request. A 409
   (changed or cancelled elsewhere) shows a notice and refetches.

Errors use the backend envelope (`error.code`, `error.message`,
`error.request_id`); see `src/api/client.ts`. Backend timestamps are naive
UTC and are parsed as UTC.

Live updates over the station event stream are not implemented yet.
