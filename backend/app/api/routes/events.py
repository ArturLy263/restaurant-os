import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.dependencies import get_db
from app.core.dependencies import get_current_user
from app.db.models.operational_event import OperationalEvent
from app.schemas.event import OperationalEventResponse


router = APIRouter(
    prefix="/api/v1/events",
    tags=["events"],
)


@router.get(
    "",
    response_model=list[OperationalEventResponse],
)
def get_events(
    entity_type: str | None = Query(default=None),
    entity_id: uuid.UUID | None = Query(default=None),
    event_type: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    query = (
        db.query(OperationalEvent)
        .filter(
            OperationalEvent.restaurant_id == current_user.restaurant_id
        )
    )

    if entity_type is not None:
        query = query.filter(
            OperationalEvent.entity_type == entity_type
        )

    if entity_id is not None:
        query = query.filter(
            OperationalEvent.entity_id == entity_id
        )

    if event_type is not None:
        query = query.filter(
            OperationalEvent.event_type == event_type
        )

    return (
        query
        .order_by(OperationalEvent.created_at.desc())
        .limit(limit)
        .all()
    )