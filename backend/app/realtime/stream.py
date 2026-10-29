"""Station event stream: one WebSocket connection to one station.

Protocol (`/api/v1/stations/{station_id}/events`):

1. The server accepts the connection; the client's first message must be
   `{"type": "authenticate", "token": "<access token>"}` (the same token
   as `Authorization: Bearer` on HTTP), within
   WEBSOCKET_AUTH_TIMEOUT_SECONDS. The token is never part of the URL, so
   it does not reach access logs. Other fields are ignored: tenant, role
   and stations are always read from the database, never from the client.
2. The connection is authorized exactly like `GET /api/v1/stations/
   {station_id}/preparations`: permission `preparations:view`, then the
   user's station scope (before the station is looked up), then the
   station must belong to the user's restaurant.
3. On success the server sends `{"type": "subscribed", "station_id": ...}`
   and from then on only server messages (see app.realtime.publisher).
   Client messages after authentication are read and ignored.

Authorization is checked again every WEBSOCKET_REAUTH_INTERVAL_SECONDS
(an inactive user, a changed role or a removed station assignment closes
the connection) and the connection is closed when the token expires.

Close codes: 4000 + the HTTP status of the equivalent HTTP error (4401
not authenticated, 4403 forbidden, 4404 station not found), with the
error code (e.g. STATION_ACCESS_DENIED) as the reason; 4429
SLOW_CONSUMER when the client fell more than WEBSOCKET_QUEUE_SIZE
messages behind; 1013 TOO_MANY_CONNECTIONS when the process is full.
After any close the client reconnects and reloads the station queue over
HTTP before applying new events.

Logs name the user, station and close code, never the token.
"""

import asyncio
import json
import logging
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.dependencies import authenticate_token, build_auth_context
from app.core.errors import ApiError, ErrorCode
from app.core.permissions import Permission
from app.db.dependencies import get_db
from app.realtime.hub import StationEventHub, StationSubscription
from app.services.order_service import OrderService


logger = logging.getLogger("app.realtime")

CLOSE_SLOW_CONSUMER = 4429
CLOSE_TRY_AGAIN_LATER = 1013


@dataclass(frozen=True)
class StreamGrant:
    """What an authorized connection may receive, read from the database."""

    user_id: uuid.UUID
    restaurant_id: uuid.UUID
    expires_at: datetime


class StreamClosed(Exception):
    def __init__(self, code: int, reason: str):
        self.code = code
        self.reason = reason
        super().__init__(f"{code} {reason}")


def _not_authenticated() -> StreamClosed:
    return StreamClosed(4401, ErrorCode.NOT_AUTHENTICATED)


@contextmanager
def _database(app):
    # The application's get_db, honouring dependency overrides, for one
    # short unit of work: a connection is never held while streaming.
    provider = app.dependency_overrides.get(get_db, get_db)
    yield from provider()


def authorize_station_stream(app, token: str, station_id: uuid.UUID) -> StreamGrant:
    """The grant for `token` on `station_id`; raises StreamClosed."""

    try:
        with _database(app) as db:
            authenticated = authenticate_token(db, token)

            # A long-lived connection needs an end: no token without one.
            if authenticated.expires_at is None:
                raise _not_authenticated()

            user = authenticated.user
            context = build_auth_context(db, user)

            if Permission.PREPARATIONS_VIEW not in context.permissions:
                raise StreamClosed(4403, ErrorCode.PERMISSION_DENIED)

            OrderService.ensure_station_visible(
                db,
                user.restaurant_id,
                station_id,
                station_scope=context.station_scope,
            )

            return StreamGrant(
                user_id=user.id,
                restaurant_id=user.restaurant_id,
                expires_at=authenticated.expires_at,
            )
    except ApiError as exc:
        raise StreamClosed(4000 + exc.status_code, str(exc.code)) from None


class StationEventStream:

    def __init__(self, websocket: WebSocket, station_id: uuid.UUID):
        self.websocket = websocket
        self.station_id = station_id
        self.hub: StationEventHub = websocket.app.state.station_events

    async def run(self) -> None:
        await self.websocket.accept()
        subscription: StationSubscription | None = None
        user_id = None

        try:
            token = await self._read_token()
            grant = await self._authorize(token)
            user_id = grant.user_id

            subscription = self.hub.subscribe(
                restaurant_id=grant.restaurant_id,
                station_id=self.station_id,
                user_id=grant.user_id,
            )

            if subscription is None:
                raise StreamClosed(CLOSE_TRY_AGAIN_LATER, "TOO_MANY_CONNECTIONS")

            logger.info(
                "Station stream opened: user %s, station %s",
                user_id,
                self.station_id,
            )
            await self.websocket.send_json(
                {"type": "subscribed", "station_id": str(self.station_id)}
            )
            await self._stream(subscription, token, grant)

        except StreamClosed as closed:
            logger.info(
                "Station stream closed: user %s, station %s, code %d %s",
                user_id or "unauthenticated",
                self.station_id,
                closed.code,
                closed.reason,
            )
            await self._close(closed.code, closed.reason)

        except (WebSocketDisconnect, OSError):
            # The client went away (OSError: the server's send failed).
            pass

        finally:
            if subscription is not None:
                self.hub.unsubscribe(subscription)
                logger.info(
                    "Station stream ended: user %s, station %s",
                    user_id,
                    self.station_id,
                )

    async def _read_token(self) -> str:
        try:
            raw = await asyncio.wait_for(
                self.websocket.receive_text(),
                timeout=settings.websocket_auth_timeout_seconds,
            )
        except TimeoutError:
            raise _not_authenticated() from None
        except KeyError:
            # A binary first message (receive_text found no "text").
            raise _not_authenticated() from None

        try:
            message = json.loads(raw)
        except ValueError:
            raise _not_authenticated() from None

        if not isinstance(message, dict) or message.get("type") != "authenticate":
            raise _not_authenticated()

        token = message.get("token")

        if not isinstance(token, str) or not token:
            raise _not_authenticated()

        return token

    async def _authorize(self, token: str) -> StreamGrant:
        return await run_in_threadpool(
            authorize_station_stream,
            self.websocket.app,
            token,
            self.station_id,
        )

    async def _stream(
        self,
        subscription: StationSubscription,
        token: str,
        grant: StreamGrant,
    ) -> None:
        loop = asyncio.get_running_loop()
        disconnected = asyncio.create_task(self._until_disconnect())
        next_check = loop.time() + settings.websocket_reauth_interval_seconds

        try:
            while True:
                expires_in = (
                    grant.expires_at - datetime.now(timezone.utc)
                ).total_seconds()

                if expires_in <= 0:
                    raise _not_authenticated()

                timeout = min(next_check - loop.time(), expires_in)
                woken = asyncio.create_task(subscription.wait())

                try:
                    await asyncio.wait(
                        {disconnected, woken},
                        timeout=max(timeout, 0),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    woken.cancel()

                if disconnected.done():
                    return

                if subscription.overflowed:
                    raise StreamClosed(CLOSE_SLOW_CONSUMER, "SLOW_CONSUMER")

                for message in subscription.drain():
                    await self.websocket.send_json(message)

                if loop.time() >= next_check:
                    grant = await self._authorize(token)
                    next_check = (
                        loop.time() + settings.websocket_reauth_interval_seconds
                    )
        finally:
            disconnected.cancel()

    async def _until_disconnect(self) -> None:
        while True:
            message = await self.websocket.receive()

            if message["type"] == "websocket.disconnect":
                return

    async def _close(self, code: int, reason: str) -> None:
        try:
            await self.websocket.close(code=code, reason=reason)
        except (RuntimeError, WebSocketDisconnect, OSError):
            # Already closed by the client.
            pass
