from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.dependencies import require_permission
from app.core.permissions import Permission
from app.db.dependencies import get_db
from app.db.models.user import User
from app.schemas.bill import CashierOrderResponse
from app.services import cashier_views

router = APIRouter(
    prefix="/api/v1/cashier",
    tags=["cashier"],
)


@router.get(
    "/orders",
    response_model=list[CashierOrderResponse],
)
def list_cashier_orders(
    view: Literal["payable", "settled"] = Query("payable"),
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_permission(Permission.BILLS_VIEW)
    ),
):
    """`payable`: every active order with its bill and amount due.
    `settled`: the latest paid bills (for refunds)."""

    return cashier_views.cashier_orders(db, current_user.restaurant_id, view)
