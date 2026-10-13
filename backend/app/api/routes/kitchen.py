import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.dependencies import require_permission
from app.core.permissions import Permission
from app.db.dependencies import get_db
from app.db.models.user import User
from app.schemas.kitchen import PreparationQueueItem
from app.services.order_service import OrderService


router = APIRouter(
    prefix="/api/v1/stations",
    tags=["kitchen"],
)


@router.get(
    "/{station_id}/preparations",
    response_model=list[PreparationQueueItem],
)
def get_station_preparations(
    station_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_permission(Permission.PREPARATIONS_VIEW)
    ),
):
    return OrderService.get_station_preparations(
        db=db,
        restaurant_id=current_user.restaurant_id,
        station_id=station_id,
    )