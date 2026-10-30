import uuid
from datetime import datetime

from pydantic import BaseModel


class TableActiveOrderResponse(BaseModel):
    id: uuid.UUID
    status: str
    # The waiter who opened the order.
    user_id: uuid.UUID
    guest_count: int
    created_at: datetime
    # Items added but not sent yet, and items sent (not cancelled).
    unsent_items: int
    sent_items: int


class TableResponse(BaseModel):
    id: uuid.UUID
    number: int
    name: str | None
    capacity: int
    status: str
    # The table's OPEN or SENT order; null when it has none.
    active_order: TableActiveOrderResponse | None
