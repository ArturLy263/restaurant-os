"""Request IDs for correlating API responses with server logs.

Each request gets an ID: the client's X-Request-ID if it is a safe token,
otherwise a new random one. It is stored in the request state, returned in
the X-Request-ID response header and included in every error body.
"""

import re
import uuid

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send


REQUEST_ID_HEADER = "X-Request-ID"

# Accepted from clients as-is: short, printable and safe to log.
_CLIENT_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def new_request_id() -> str:
    return uuid.uuid4().hex


def get_request_id(request: Request) -> str:
    """The current request's ID (assigned by RequestIdMiddleware)."""

    request_id = getattr(request.state, "request_id", None)

    if request_id is None:
        request_id = new_request_id()
        request.state.request_id = request_id

    return request_id


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Request(scope).headers.get(REQUEST_ID_HEADER)

        if incoming is not None and _CLIENT_REQUEST_ID.fullmatch(incoming):
            request_id = incoming
        else:
            request_id = new_request_id()

        # Request.state reads scope["state"], so handlers see the same ID.
        scope.setdefault("state", {})["request_id"] = request_id
        header = (REQUEST_ID_HEADER.lower().encode(), request_id.encode())

        async def send_with_request_id(message: Message):
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != header[0]
                ]
                headers.append(header)
                message = {**message, "headers": headers}

            await send(message)

        await self.app(scope, receive, send_with_request_id)
