import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.idempotency import IDEMPOTENT_ROUTE_RESPONSES, Idempotency
from app.core.dependencies import require_permission
from app.core.permissions import Permission
from app.db.dependencies import get_db
from app.db.models.user import User
from app.schemas.bill import (
    BillDetailResponse,
    BillResponse,
    CreateBillResponse,
    CreatePaymentRequest,
    EqualSplitResponse,
    PaymentDetailResponse,
)
from app.schemas.order import CancelOrderItemRequest
from app.services import cashier_views
from app.services.bill_service import MAX_SPLIT_PARTS, BillService
from app.services.order_service import OrderService


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
    response_model=PaymentDetailResponse,
    status_code=status.HTTP_201_CREATED,
    responses=IDEMPOTENT_ROUTE_RESPONSES,
)
def create_payment(
    bill_id: uuid.UUID,
    request: CreatePaymentRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_permission(Permission.PAYMENTS_CREATE)
    ),
    idempotency: Idempotency = Depends(),
):
    """Take a payment (CASH, or CARD confirmed by the cashier after the
    external terminal approved it) in the open cash shift of the open
    business day, with an optional tip and optionally for given items."""

    return idempotency.run(
        lambda: BillService.create_payment(
            db=db,
            restaurant_id=current_user.restaurant_id,
            user_id=current_user.id,
            bill_id=bill_id,
            amount=request.amount,
            method=request.method,
            tip_amount=request.tip_amount,
            order_item_ids=request.order_item_ids,
            external_reference=request.external_reference,
            card_confirmed=request.card_confirmed,
        ),
        payload=request,
    )


@router.get(
    "/{bill_id}",
    response_model=BillDetailResponse,
)
def get_bill(
    bill_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_permission(Permission.BILLS_VIEW)
    ),
):
    return cashier_views.bill_detail(db, current_user.restaurant_id, bill_id)


@router.get(
    "/{bill_id}/split",
    response_model=EqualSplitResponse,
)
def split_bill_equally(
    bill_id: uuid.UUID,
    parts: int = Query(ge=2, le=MAX_SPLIT_PARTS),
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_permission(Permission.BILLS_VIEW)
    ),
):
    """The amount due split into `parts` equal shares to the cent."""

    bill = BillService.get_bill(db, current_user.restaurant_id, bill_id)
    shares = BillService.equal_split(db, current_user.restaurant_id, bill_id, parts)
    return {"bill_id": bill.id, "amount_due": bill.amount_due, "shares": shares}


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
