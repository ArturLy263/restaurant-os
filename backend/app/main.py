from fastapi import FastAPI

from app.api.error_handlers import register_error_handlers
from app.api.routes.auth import router as auth_router
from app.api.routes.orders import router as orders_router
from app.api.routes.preparations import router as preparations_router
from app.api.routes.kitchen import router as kitchen_router
from app.api.routes.me import router as me_router
from app.api.routes.bills import router as bills_router
from app.api.routes.events import router as events_router
from app.core.request_id import RequestIdMiddleware


app = FastAPI(
    title="RestaurantOS API",
    version="1.0.0",
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

@app.get("/health")
def health_check():
    return {"status": "ok"}
