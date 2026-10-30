# Deployment proposal (not deployed)

A proposal for the first hosted environments. Nothing here has been set
up, bought or registered.

## Target

Cloud-hosted, multi-tenant SaaS: every restaurant is a tenant in one
application and one database (tenant isolation is enforced by the API and
by PostgreSQL triggers). No offline POS mode is assumed.

## Components

| Part | Choice |
| --- | --- |
| Frontend | React/Vite static build (`frontend/dist`), served by Caddy |
| API | FastAPI modular monolith (uvicorn), **one process** |
| Database | PostgreSQL 16; separate staging and production databases |
| Events | transactional outbox, delivered by the API process to station WebSockets (`OUTBOX_WEBSOCKET_DELIVERY=true`) |
| Edge | Caddy: automatic HTTPS, serves the frontend, proxies `/api` (HTTP and WebSocket) to the API |
| Host | Docker Compose on a European VPS (staging first) |
| CI/CD | GitHub Actions: the existing test jobs gate a build and deploy job |

Same-origin `/api` keeps the browser free of CORS and lets the station
WebSocket use the same host and certificate.

## One API process

The station event hub is in memory, so exactly one API process may run
with delivery enabled; each process only knows its own WebSocket
connections. Scale vertically first. Running several processes later
needs a shared broker (e.g. PostgreSQL `LISTEN/NOTIFY` or a message
broker); that is a deliberate later decision, not part of this setup.

## Environments and data

- Staging and production: separate VPS or at least separate Compose
  projects, databases, credentials and domains.
- Migrations: `alembic upgrade head` as an explicit deploy step before the
  new API starts; never automatic downgrades in production.
- Backups: nightly `pg_dump` (plus WAL archiving once production data
  matters) stored off the host, encrypted; a restore into a scratch
  database tested regularly.
- Outbox retention: the existing `outbox_worker cleanup` command from cron.

## Operations

- Secrets (`DATABASE_URL`, `JWT_SECRET_KEY`): GitHub Actions secrets and an
  `.env` file on the host readable only by the deploy user; never in the
  repository or images.
- Monitoring: container health checks (`/health`), uptime checks on the
  public URL, log collection, and alerts on outbox dead letters and
  backlog (queries in the README).
- Access: SSH keys only, firewall open for 80/443 only, PostgreSQL not
  exposed publicly.

## Not decided yet

Provider and region, domain names, log/metrics tooling, and when to
introduce a second API process.
