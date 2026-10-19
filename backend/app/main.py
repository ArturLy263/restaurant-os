from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.error_handlers import register_error_handlers
from app.api.routes.auth import router as auth_router
from app.api.routes.orders import router as orders_router
from app.api.routes.preparations import router as preparations_router
from app.api.routes.kitchen import router as kitchen_router
from app.api.routes.me import router as me_router
from app.api.routes.bills import router as bills_router
from app.api.routes.events import router as events_router
from app.api.routes.station_events import router as station_events_router
from app.core.config import settings
from app.core.request_id import RequestIdMiddleware
from app.realtime.delivery import StationDeliveryRunner
from app.realtime.hub import StationEventHub


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Started by the server, never on import; see app.realtime.delivery.
    runner = None

    if settings.outbox_websocket_delivery:
        runner = StationDeliveryRunner(app.state.station_events, settings)
        runner.start()

    try:
        yield
    finally:
        if runner is not None:
            runner.stop()


app = FastAPI(
    title="RestaurantOS API",
    version="1.0.0",
    lifespan=lifespan,
)

# Open station event streams of this process (app.realtime); in memory,
# so connections to another API process are not shared.
app.state.station_events = StationEventHub(
    max_connections=settings.websocket_max_connections,
    queue_size=settings.websocket_queue_size,
)

app.add_middleware(RequestIdMiddleware)
register_error_handlers(app)

app.include_router(auth_router)
app.include_router(me_router)
app.include_router(orders_router)
app.include_router(preparations_router)
app.include_router(kitchen_router)
app.include_router(bills_router)
app.include_router(events_router)
app.include_router(station_events_router)

@app.get("/health")
def health_check():
    return {"status": "ok"}
