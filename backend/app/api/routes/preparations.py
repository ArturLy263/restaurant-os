import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.idempotency import IDEMPOTENT_ROUTE_RESPONSES, Idempotency
from app.core.dependencies import (
    AuthContext,
    get_auth_context,
    require_permission,
)
from app.core.permissions import Permission
from app.db.dependencies import get_db
from app.db.models.user import User
from app.schemas.preparation import (
    DiscardPreparationRequest,
    PreparationResponse,
)
from app.services.order_service import OrderService


router = APIRouter(
    prefix="/api/v1/preparations",
    tags=["preparations"],
)


@router.post(
    "/{preparation_id}/start",
    response_model=PreparationResponse,
)
def start_preparation(
    preparation_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_permission(Permission.PREPARATIONS_UPDATE)
    ),
    auth: AuthContext = Depends(get_auth_context),
):
    return OrderService.update_preparation_status(
        db=db,
        restaurant_id=current_user.restaurant_id,
        user_id=current_user.id,
        preparation_id=preparation_id,
        new_status="IN_PROGRESS",
        station_scope=auth.station_scope,
    )


@router.post(
    "/{preparation_id}/ready",
    response_model=PreparationResponse,
)
def mark_preparation_ready(
    preparation_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_permission(Permission.PREPARATIONS_UPDATE)
    ),
    auth: AuthContext = Depends(get_auth_context),
):
    return OrderService.update_preparation_status(
        db=db,
        restaurant_id=current_user.restaurant_id,
        user_id=current_user.id,
        preparation_id=preparation_id,
        new_status="READY",
        station_scope=auth.station_scope,
    )


@router.post(
    "/{preparation_id}/serve",
    response_model=PreparationResponse,
)
def serve_preparation(
    preparation_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_permission(Permission.PREPARATIONS_SERVE)
    ),
    auth: AuthContext = Depends(get_auth_context),
):
    return OrderService.update_preparation_status(
        db=db,
        restaurant_id=current_user.restaurant_id,
        user_id=current_user.id,
        preparation_id=preparation_id,
        new_status="SERVED",
        station_scope=auth.station_scope,
    )


@router.post(
    "/{preparation_id}/discard",
    response_model=PreparationResponse,
    responses=IDEMPOTENT_ROUTE_RESPONSES,
)
def discard_preparation(
    preparation_id: uuid.UUID,
    request: DiscardPreparationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_permission(Permission.PREPARATIONS_DISCARD)
    ),
    auth: AuthContext = Depends(get_auth_context),
    idempotency: Idempotency = Depends(),
):
    """Cancel an unfinished preparation of a fully paid (CLOSED) order,
    e.g. when the guest left; no bill or item changes."""

    return idempotency.run(
        lambda: OrderService.discard_preparation(
            db=db,
            restaurant_id=current_user.restaurant_id,
            user_id=current_user.id,
            preparation_id=preparation_id,
            reason=request.reason,
            station_scope=auth.station_scope,
        ),
        payload=request,
    )
