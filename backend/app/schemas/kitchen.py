import uuid
from datetime import datetime

from pydantic import BaseModel


class StationResponse(BaseModel):
    id: uuid.UUID
    name: str
    type: str
    is_active: bool


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
    # The order's status and its bill's (null before billing). A CLOSED
    # order is fully paid; its open preparations stay in the queue until
    # served or discarded, since payment is not service.
    order_status: str
    bill_status: str | None
