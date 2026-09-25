import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.db.dependencies import get_db
from app.db.models.user import User
from app.schemas.bill import (
    CreateBillResponse,
    PaymentRequest,
    PaymentResponse,
)
from app.services.bill_service import BillService
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
)
def create_bill(
    order_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return BillService.create_bill(
        db=db,
        restaurant_id=current_user.restaurant_id,
        order_id=order_id,
    )


@router.post(
    "/{bill_id}/payments",
    response_model=PaymentResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_payment(
    bill_id: uuid.UUID,
    request: PaymentRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return BillService.create_payment(
        db=db,
        restaurant_id=current_user.restaurant_id,
        bill_id=bill_id,
        amount=request.amount,
        method=request.method,
    )

@router.get(
    "/{bill_id}",
    response_model=BillResponse,
)
def get_bill(
    bill_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return BillService.get_bill(
        db=db,
        restaurant_id=current_user.restaurant_id,
        bill_id=bill_id,
    )