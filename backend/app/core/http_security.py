"""HTTP hardening for the API (pure ASGI, so WebSockets pass untouched).

- Security headers on every HTTP response: no MIME sniffing, no referrer,
  no framing, a Content-Security-Policy that lets an API response run or
  load nothing, `Cache-Control: no-store` on /api responses (they carry
  personal and financial data), and Strict-Transport-Security only when
  configured (hsts_max_age_seconds > 0), i.e. only behind HTTPS.
- A request body limit (max_request_body_bytes): larger bodies get 413
  REQUEST_TOO_LARGE, whether announced by Content-Length or streamed.

The frontend's own CSP is set where its files are served (vite preview,
or the reverse proxy in production): see docs/security.md.
"""

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import ApiError, ErrorCode

# What an API response may do in a browser: nothing.
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
# FastAPI's interactive docs load Swagger UI / ReDoc from jsDelivr with an
# inline bootstrap script; only these pages get this policy, and they are
# disabled in production.
DOCS_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.redoc.ly; "
    "worker-src 'self' blob:; "
    "frame-ancestors 'none'; base-uri 'none'"
)
DOCS_PATHS = ("/docs", "/redoc")


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, hsts_max_age_seconds: int = 0):
        self.app = app
        self.hsts = (
            f"max-age={hsts_max_age_seconds}; includeSubDomains" if hsts_max_age_seconds > 0 else None
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        is_docs = path.startswith(DOCS_PATHS)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
                headers.setdefault("Content-Security-Policy", DOCS_CSP if is_docs else API_CSP)
                if path.startswith("/api/"):
                    headers.setdefault("Cache-Control", "no-store")
                if self.hsts:
                    headers.setdefault("Strict-Transport-Security", self.hsts)
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _too_large(limit: int) -> ApiError:
    return ApiError(
        status_code=413,
        code=ErrorCode.REQUEST_TOO_LARGE,
        message=f"The request body is larger than {limit} bytes",
    )


class BodySizeLimitMiddleware:
    """Refuses request bodies over `max_bytes`. Placed inside the
    exception handlers' reach: the 413 is raised from the body read, so it
    becomes the usual error envelope."""

    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        announced = Headers(scope=scope).get("content-length")
        too_large_announced = False
        if announced is not None:
            try:
                too_large_announced = int(announced) > self.max_bytes
            except ValueError:
                too_large_announced = False
        received = 0
        limit = self.max_bytes

        async def limited_receive() -> Message:
            nonlocal received
            if too_large_announced:
                raise _too_large(limit)
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _too_large(limit)
            return message

        await self.app(scope, limited_receive, send)
