import uuid
from datetime import datetime

from pydantic import BaseModel


class PreparationQueueModifier(BaseModel):
    modifier_id: uuid.UUID
    # The name stored when the item was added (order_item_modifiers).
    name: str


class PreparationQueueItem(BaseModel):
    id: uuid.UUID
    order_item_id: uuid.UUID
    station_id: uuid.UUID
    status: str
    created_at: datetime
    started_at: datetime | None
    ready_at: datetime | None

    # What to prepare, for displays.
    order_id: uuid.UUID
    table_number: int
    table_name: str | None
    # The product's current name; order items store no name snapshot.
    product_name: str
    quantity: int
    modifiers: list[PreparationQueueModifier]
