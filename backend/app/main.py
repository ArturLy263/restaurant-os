import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.error_handlers import register_error_handlers
from app.api.routes.auth import router as auth_router
from app.api.routes.orders import router as orders_router
from app.api.routes.preparations import router as preparations_router
from app.api.routes.kitchen import router as kitchen_router
from app.api.routes.me import router as me_router
from app.api.routes.bills import router as bills_router
from app.api.routes.events import router as events_router
from app.api.routes.station_events import router as station_events_router
from app.api.routes.tables import router as tables_router
from app.api.routes.menu import router as menu_router
from app.api.routes.cash import business_days_router, router as cash_router
from app.api.routes.cashier import router as cashier_router
from app.api.routes.payments import router as payments_router
from app.core.config import Settings, settings
from app.core.http_security import BodySizeLimitMiddleware, SecurityHeadersMiddleware
from app.core.request_id import RequestIdMiddleware
from app.realtime.delivery import StationDeliveryRunner
from app.realtime.hub import StationEventHub


@asynccontextmanager
async def lifespan(app: FastAPI, config: Settings | None = None):
    # Started by the server, never on import; see app.realtime.delivery.
    config = config if config is not None else settings
    runner = None

    if config.outbox_websocket_delivery:
        runner = StationDeliveryRunner(app.state.station_events, config)
        runner.start()

    try:
        yield
    finally:
        if runner is not None:
            # Waits for the event being delivered; off the event loop, so
            # closing streams are not blocked meanwhile.
            await asyncio.to_thread(runner.stop)


def create_app(config: Settings = settings) -> FastAPI:
    """The API, configured by `config` (tests build variants of it)."""

    # The interactive docs and the schema are public only when enabled
    # (by default everywhere but production).
    docs = config.docs_enabled
    app = FastAPI(
        title="RestaurantOS API",
        version="1.0.0",
        lifespan=lambda app: lifespan(app, config),
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )

    # Open station event streams of this process (app.realtime); in memory,
    # so connections to another API process are not shared.
    app.state.station_events = StationEventHub(
        max_connections=config.websocket_max_connections,
        queue_size=config.websocket_queue_size,
    )

    # Innermost first: the body limit raises inside the error handlers'
    # reach; headers and the request ID wrap every response, errors too.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=config.max_request_body_bytes)
    if config.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.cors_allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
            expose_headers=["X-Request-ID", "Retry-After", "Idempotent-Replayed"],
            max_age=600,
        )
    app.add_middleware(SecurityHeadersMiddleware, hsts_max_age_seconds=config.hsts_max_age_seconds)
    app.add_middleware(RequestIdMiddleware)
    register_error_handlers(app)

    for router in (
        auth_router,
        me_router,
        orders_router,
        preparations_router,
        kitchen_router,
        bills_router,
        events_router,
        station_events_router,
        tables_router,
        menu_router,
        cashier_router,
        payments_router,
        business_days_router,
        cash_router,
    ):
        app.include_router(router)

    @app.get("/health")
    def health_check():
        return {"status": "ok"}

    return app


app = create_app()
