import uuid
from datetime import datetime

from pydantic import BaseModel


class PreparationQueueItem(BaseModel):
    id: uuid.UUID
    order_item_id: uuid.UUID
    station_id: uuid.UUID
    status: str
    created_at: datetime
    started_at: datetime | None
    ready_at: datetime | None