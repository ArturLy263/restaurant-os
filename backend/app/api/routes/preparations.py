import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.dependencies import require_permission
from app.core.permissions import Permission
from app.db.dependencies import get_db
from app.db.models.user import User
from app.schemas.preparation import PreparationResponse
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
):
    return OrderService.update_preparation_status(
        db=db,
        restaurant_id=current_user.restaurant_id,
        user_id=current_user.id,
        preparation_id=preparation_id,
        new_status="IN_PROGRESS",
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
):
    return OrderService.update_preparation_status(
        db=db,
        restaurant_id=current_user.restaurant_id,
        user_id=current_user.id,
        preparation_id=preparation_id,
        new_status="READY",
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
):
    return OrderService.update_preparation_status(
        db=db,
        restaurant_id=current_user.restaurant_id,
        user_id=current_user.id,
        preparation_id=preparation_id,
        new_status="SERVED",
    )