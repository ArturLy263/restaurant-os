import uuid

from sqlalchemy.orm import Session

from app.db.models.operational_event import OperationalEvent


class EventService:

    @staticmethod
    def record(
        db: Session,
        restaurant_id: uuid.UUID,
        event_type: str,
        entity_type: str,
        entity_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
        metadata: dict | None = None,
    ) -> OperationalEvent:

        event = OperationalEvent(
            restaurant_id=restaurant_id,
            user_id=user_id,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            event_metadata=metadata,
        )

        db.add(event)

        return event