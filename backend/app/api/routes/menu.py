from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.dependencies import require_permission
from app.core.permissions import Permission
from app.db.dependencies import get_db
from app.db.models.user import User
from app.schemas.menu import MenuResponse
from app.services.menu_service import MenuService


router = APIRouter(
    prefix="/api/v1/menu",
    tags=["menu"],
)


@router.get(
    "",
    response_model=MenuResponse,
)
def get_menu(
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_permission(Permission.ORDERS_VIEW)
    ),
):
    """The menu for taking orders; prices are decimal strings."""

    return MenuService.get_menu(
        db=db,
        restaurant_id=current_user.restaurant_id,
    )
