# Security

What RestaurantOS protects, how, and what is still left to a deployment.
This is a portfolio project: the protections below are implemented and
tested, but **RestaurantOS is not production-ready** until the deployment
checklist at the end is done and reviewed.

## Implemented protections

### Authentication

- **Passwords:** bcrypt (cost 12). Passwords over 72 bytes are refused.
- **Sign-in answers never reveal an account.** An unknown email, a wrong
  password and an inactive account all get the same `401
  INVALID_CREDENTIALS`. They also cost the same work: an unknown email is
  checked against a dummy bcrypt hash computed once per process
  (`verify_password_for_unknown_user` in `app/core/security.py`).
- **Emails** are compared trimmed and in lower case, so changing their
  capitalization neither finds another account nor escapes throttling.
- **Throttling** (`app/services/login_throttle.py`):
  - **What is counted:** failed sign-ins, per submitted email and per
    client address, within a 15-minute window.
  - **Blocking:** past 5 failures for an email, or 50 for an address, that
    key is blocked for 30 s, doubling with each further failure, capped at
    15 minutes.
  - **Answers:** while a key is blocked, `429 TOO_MANY_LOGIN_ATTEMPTS`
    with `Retry-After`. It is keyed on what was submitted, so it says
    nothing about whether the account exists.
  - **No permanent lockout:** blocks lapse on their own, and a successful
    sign-in clears the email's counter.
  - **Storage:** counters live in PostgreSQL (`login_throttles`, keys
    stored as sha256 hashes, never raw emails or addresses), so every API
    process shares them.
  - **Settings:** all limits are `LOGIN_*` settings, validated at startup.
- **Tokens:** HS256 JWTs, valid for 60 minutes, carrying the user id, a
  token version (`ver`) and a unique id (`jti`). Every request:
  - reloads the user;
  - refuses inactive users at once;
  - refuses a `ver` that differs from `users.token_version`;
  - refuses a revoked `jti`;
  - accepts only the configured algorithm.

  The role and restaurant are read from the database, never from the
  token.
- **Sign-out** (`POST /api/v1/auth/logout`) revokes that token: only its
  `jti` is stored, in `revoked_tokens`, until it would have expired.
  Station WebSocket streams re-authorize every 30 s, so they close too.
- **Password changes end every earlier session.** A database trigger
  (`users_bump_token_version`) increments `token_version` whenever
  `password_hash` changes, however the password was changed.

### Configuration (`app/core/config.py`)

- **The API refuses to start** when:
  - `JWT_SECRET_KEY` is empty, contains a placeholder (`change_me`,
    `example`, …), is repetitive (fewer than 10 distinct characters) or is
    shorter than 24 characters (32 in production);
  - in production (`ENVIRONMENT=production`): the database password is a
    placeholder, a common default or shorter than 12 characters, or
    `CORS_ALLOWED_ORIGINS` contains `*`.
- **Configuration errors never print configuration values**
  (`hide_input_in_errors`), and the JWT secret is excluded from the
  settings' `repr`.
- Generate a secret with
  `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

### Authorization and tenant isolation

Unchanged by this hardening; see
[ADR 0003](adr/0003-tenant-isolation.md):
- every route requires a permission, except `/auth/login` and `/health`;
- `/me` and `/auth/logout` require only a valid token;
- the restaurant comes from the signed-in user, and other restaurants'
  records answer like missing ones;
- database triggers refuse cross-restaurant links.

### Cash: blind count without an oracle

See [ADR 0005](adr/0005-blind-count-and-two-person-handover.md).

- **The problem:** a closing count that differed from the expected cash
  used to be refused (422) until an explanation was added. Retrying with
  different counts therefore told a cashier whether a guess matched, so
  they could find the expected amount and hide a shortage. The same
  applied to the handover count.
- **Counts are now recorded as entered,** with the same response whether
  or not they match. The shift then closes (or the next shift opens), so a
  count can never be submitted twice.
- **A non-zero difference is explained afterwards,** through
  `POST /cash/shifts/{id}/closing-explanation` or `…/handover-explanation`.
  Only the person who counted, or a manager, may do this; the table
  `cash_count_explanations` is append-only.
- **The business day cannot close** while any difference is unexplained.
  The service refuses with `409 CASH_DIFFERENCE_UNEXPLAINED`, listing only
  shift ids. The database trigger `business_days_explained_before_close`
  refuses it too. Frozen reports therefore explain every difference.
- **Accepted trade-off:** once a count is recorded and final, its
  `explanation_required` flag tells the cashier one bit: "my count
  differed". It cannot be used to change the count, and no amount is ever
  shown.

### HTTP (`app/core/http_security.py`)

- **Headers on every API response:**
  - `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'; …`
    (an API response may run or load nothing);
  - `X-Content-Type-Options: nosniff`;
  - `Referrer-Policy: no-referrer`;
  - `X-Frame-Options: DENY`;
  - `Cross-Origin-Opener-Policy: same-origin`;
  - `Cache-Control: no-store` on `/api/`.

  The `/docs` and `/redoc` pages get a policy that lets Swagger UI and
  ReDoc load, and they are off in production.
- **HSTS** only when `HSTS_MAX_AGE_SECONDS > 0`. Set it only behind HTTPS.
- **Request bodies** over `MAX_REQUEST_BODY_BYTES` (1 MiB by default) get
  `413 REQUEST_TOO_LARGE`, whether the size is announced or the body is
  streamed. The first WebSocket message (the token) is capped at 16 KiB.
- **CORS** is off by default: the frontend and the API share one origin.
  `CORS_ALLOWED_ORIGINS` lists exact origins, without credentials.
- **API docs** (`/docs`, `/redoc`, `/openapi.json`) are on outside
  production and off in production, unless `API_DOCS_ENABLED` says
  otherwise. Disabling them does not change the API.
- **Validation errors** list only the field, the message and the error
  type, never the submitted value (no passwords, terminal references or
  notes). **Database errors** are logged without their bound parameters
  (`hide_parameters=True` on every engine).

### Frontend

- **Content-Security-Policy:** derived from `index.html` in
  `frontend/csp.ts`. Own scripts only, plus the inline theme script by its
  sha256 hash; own styles, own connections (`/api` and the station
  WebSockets); no plugins, no `<base>`, no framing.
  - `vite preview` serves the build with these headers.
  - `npm run e2e:csp` checks the real build in headless Chrome: sign-in,
    the API and the WebSocket work with no violation, and an injected
    inline script is refused.
  - `npm run csp:print` prints the headers for a reverse proxy. Changing
    the inline script changes the hash, and a test keeps them in step.
- **No unsafe HTML** is rendered (React escapes all text). No
  `VITE_`-prefixed environment variables reach the bundle, and there are
  no source maps in the build.
- **Sign-out** revokes the token on the server (best effort; signing out
  never waits). It also removes the user's resolved cashier records from
  this device.

## Remaining limitations

- **The access token lives in `sessionStorage`.** Any script running on
  the page could read it. The CSP and the absence of unsafe HTML are the
  defence; an httpOnly-cookie session would remove the exposure.
- **The cashier's pending-operation store keeps unresolved operations in
  `localStorage`,** including their request bodies (amounts, reasons), so
  a retry can safely reuse the same Idempotency-Key. Resolved records are
  removed at sign-out and kept at most 12 hours otherwise. Bodies are
  dropped as soon as an operation can no longer be retried. On a shared
  till, the next employee's browser keeps unresolved records until they
  are resolved; the banner shows them without amounts. Clearing them
  would risk a duplicate payment.
- **No refresh tokens.** Sessions end after 60 minutes, and revocation
  needs one database lookup per request.
- **Throttling relies on the client address the ASGI server reports.**
  Behind a proxy that address must be configured correctly (see below),
  or every client shares the proxy's address.
- **Not covered by the application:** TLS, HSTS on the frontend host,
  WebSocket frame-size limits beyond uvicorn's `--ws-max-size`, network
  isolation and backups.
- **Dependency CVE scan not run** (see below).

## Deployment checklist (not automated here)

1. Set `ENVIRONMENT=production`. Use a random `JWT_SECRET_KEY` of 32 or
   more characters, a strong database password and an explicit
   `CORS_ALLOWED_ORIGINS` (or none, with one origin for the UI and the API).
2. Terminate TLS at a reverse proxy. Redirect HTTP to HTTPS, and set
   `HSTS_MAX_AGE_SECONDS` (e.g. `31536000`) only once every client uses
   HTTPS.
3. Serve `frontend/dist` from that proxy with the headers from
   `npm run csp:print`, and proxy `/api` (including WebSocket upgrades) to
   the API.
4. Run uvicorn with `--proxy-headers --forwarded-allow-ips=<the proxy's
   address>`. Never use `*`: clients could then forge `X-Forwarded-For`
   and escape the per-address throttle. Bind uvicorn to a private
   interface, and set `--ws-max-size` (e.g. `65536`).
5. Run exactly one API process with `OUTBOX_WEBSOCKET_DELIVERY=true` (see
   [ADR 0001](adr/0001-transactional-outbox-and-websocket-delivery.md)).
6. Keep PostgreSQL on a private network with no published port. The
   Compose file publishes it on `127.0.0.1:5433` for local development
   only. Use a dedicated database role, and back up with encryption.
7. Keep `/docs` off (the default in production).
8. Run the dependency scan below and fix what it reports.

## Dependency scan

Not run: no scanner is installed in this environment, and both scanners
send the dependency list to an external service. With approval:

```bash
pip install pip-audit && pip-audit -r backend/constraints.txt
cd frontend && npm audit --omit=dev
```

Dependencies are pinned (`backend/constraints.txt`, `package-lock.json`),
and CI's GitHub Actions are pinned to commit SHAs.

## Git history

The published history contains no secrets. Two **local, never-pushed**
branches, `terminal` and `backup-terminal`, contain old commits with a
hardcoded weak development password (`backend/seed.py` and three
`scripts/*_e2e.py`). **Never push these branches.** Delete them, or rewrite
their history, before any `git push --all`. The current code contains no
hardcoded credential.
