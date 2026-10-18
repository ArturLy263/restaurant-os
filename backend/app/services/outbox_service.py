import uuid

from sqlalchemy.orm import Session

from app.db.models.outbox_event import OutboxEvent
from app.schemas.outbox import OutboxPayload


class OutboxService:

    @staticmethod
    def record(
        db: Session,
        restaurant_id: uuid.UUID,
        aggregate_type: str,
        aggregate_id: uuid.UUID,
        payload: OutboxPayload,
    ) -> OutboxEvent:
        """Add an outbox event to the caller's unit of work.

        Never flushes or commits: the event is written when the service
        ends its unit of work, in the same transaction as the business
        change, so a rollback discards both and a failed insert fails the
        whole change.
        """

        event = OutboxEvent(
            restaurant_id=restaurant_id,
            event_type=payload.event_type,
            schema_version=payload.schema_version,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            payload=payload.model_dump(mode="json"),
        )

        db.add(event)

        return event
