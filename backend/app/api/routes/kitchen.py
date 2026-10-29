import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.dependencies import (
    AuthContext,
    get_auth_context,
    require_permission,
)
from app.core.permissions import Permission
from app.db.dependencies import get_db
from app.db.models.user import User
from app.schemas.kitchen import PreparationQueueItem, StationResponse
from app.services.order_service import OrderService


router = APIRouter(
    prefix="/api/v1/stations",
    tags=["kitchen"],
)


@router.get(
    "",
    response_model=list[StationResponse],
)
def get_stations(
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_permission(Permission.PREPARATIONS_VIEW)
    ),
    auth: AuthContext = Depends(get_auth_context),
):
    """The stations whose queue the user may view: every station of the
    restaurant, or only the assigned ones (station scope)."""

    return OrderService.get_visible_stations(
        db=db,
        restaurant_id=current_user.restaurant_id,
        station_scope=auth.station_scope,
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
    auth: AuthContext = Depends(get_auth_context),
):
    return OrderService.get_station_preparations(
        db=db,
        restaurant_id=current_user.restaurant_id,
        station_id=station_id,
        station_scope=auth.station_scope,
    )