import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.idempotency import IDEMPOTENT_ROUTE_RESPONSES, Idempotency
from app.core.dependencies import require_permission
from app.core.permissions import Permission
from app.db.dependencies import get_db
from app.db.models.user import User
from app.schemas.bill import (
    CreateBillResponse,
    PaymentRequest,
    PaymentResponse,
)
from app.schemas.order import CancelOrderItemRequest
from app.services.bill_service import BillService
from app.services.order_service import OrderService
from app.schemas.bill import (
    BillResponse,
    CreateBillResponse,
    PaymentRequest,
    PaymentResponse,
)


router = APIRouter(
    prefix="/api/v1/bills",
    tags=["bills"],
)


@router.post(
    "/order/{order_id}",
    response_model=CreateBillResponse,
    status_code=status.HTTP_201_CREATED,
    responses=IDEMPOTENT_ROUTE_RESPONSES,
)
def create_bill(
    order_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_permission(Permission.BILLS_CREATE)
    ),
    idempotency: Idempotency = Depends(),
):
    return idempotency.run(
        lambda: BillService.create_bill(
            db=db,
            restaurant_id=current_user.restaurant_id,
            user_id=current_user.id,
            order_id=order_id,
        ),
    )


@router.post(
    "/{bill_id}/payments",
    response_model=PaymentResponse,
    status_code=status.HTTP_201_CREATED,
    responses=IDEMPOTENT_ROUTE_RESPONSES,
)
def create_payment(
    bill_id: uuid.UUID,
    request: PaymentRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_permission(Permission.PAYMENTS_CREATE)
    ),
    idempotency: Idempotency = Depends(),
):
    return idempotency.run(
        lambda: BillService.create_payment(
            db=db,
            restaurant_id=current_user.restaurant_id,
            user_id=current_user.id,
            bill_id=bill_id,
            amount=request.amount,
            method=request.method,
        ),
        payload=request,
    )

@router.get(
    "/{bill_id}",
    response_model=BillResponse,
)
def get_bill(
    bill_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_permission(Permission.BILLS_VIEW)
    ),
):
    return BillService.get_bill(
        db=db,
        restaurant_id=current_user.restaurant_id,
        bill_id=bill_id,
    )


@router.post(
    "/{bill_id}/items/{item_id}/cancel",
    response_model=BillResponse,
    responses=IDEMPOTENT_ROUTE_RESPONSES,
)
def cancel_billed_item(
    bill_id: uuid.UUID,
    item_id: uuid.UUID,
    request: CancelOrderItemRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_permission(Permission.BILLS_ADJUST)
    ),
    idempotency: Idempotency = Depends(),
):
    """Cancel an item of an issued bill, as an ITEM_CANCELLATION adjustment.

    The financial correction for billed items; the order endpoint refuses
    to cancel items once the order has a bill.
    """

    return idempotency.run(
        lambda: OrderService.cancel_billed_item(
            db=db,
            restaurant_id=current_user.restaurant_id,
            user_id=current_user.id,
            bill_id=bill_id,
            item_id=item_id,
            reason=request.reason,
        ),
        payload=request,
    )
