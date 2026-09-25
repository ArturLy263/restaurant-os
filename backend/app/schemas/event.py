import uuid
from datetime import datetime

from pydantic import BaseModel


class OperationalEventResponse(BaseModel):
    id: uuid.UUID
    restaurant_id: uuid.UUID
    user_id: uuid.UUID | None
    event_type: str
    entity_type: str
    entity_id: uuid.UUID
    event_metadata: dict | None
    created_at: datetime