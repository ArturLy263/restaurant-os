import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class PreparationResponse(BaseModel):
    id: uuid.UUID
    order_item_id: uuid.UUID
    station_id: uuid.UUID
    status: str
    created_at: datetime
    started_at: datetime | None
    ready_at: datetime | None
    served_at: datetime | None
    cancelled_at: datetime | None


class DiscardPreparationRequest(BaseModel):
    # Recorded in the audit trail (operational_events), never in outbox
    # events.
    reason: str = Field(..., min_length=1, max_length=500)
