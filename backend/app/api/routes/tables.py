from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.dependencies import require_permission
from app.core.permissions import Permission
from app.db.dependencies import get_db
from app.db.models.user import User
from app.schemas.table import TableResponse
from app.services.table_service import TableService


router = APIRouter(
    prefix="/api/v1/tables",
    tags=["tables"],
)


@router.get(
    "",
    response_model=list[TableResponse],
)
def list_tables(
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_permission(Permission.ORDERS_VIEW)
    ),
):
    """The restaurant's active tables with their active order, if any."""

    return TableService.list_tables(
        db=db,
        restaurant_id=current_user.restaurant_id,
    )
