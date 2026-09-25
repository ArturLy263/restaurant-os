from fastapi import FastAPI

from app.api.routes.auth import router as auth_router
from app.api.routes.orders import router as orders_router
from app.api.routes.preparations import router as preparations_router
from app.api.routes.kitchen import router as kitchen_router
from app.api.routes.bills import router as bills_router
from app.api.routes.events import router as events_router


app = FastAPI(
    title="RestaurantOS API",
    version="1.0.0",
)

app.include_router(auth_router)
app.include_router(orders_router)
app.include_router(preparations_router)
app.include_router(kitchen_router)
app.include_router(bills_router)
app.include_router(events_router)

@app.get("/health")
def health_check():
    return {"status": "ok"}