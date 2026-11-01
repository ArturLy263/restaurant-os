import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.idempotency import IDEMPOTENT_ROUTE_RESPONSES, Idempotency
from app.core.dependencies import require_permission
from app.core.permissions import Permission
from app.db.dependencies import get_db
from app.db.models.user import User
from app.schemas.bill import RefundRequest, RefundResponse
from app.services.bill_service import BillService

router = APIRouter(
    prefix="/api/v1/payments",
    tags=["payments"],
)


@router.post(
    "/{payment_id}/refunds",
    response_model=RefundResponse,
    status_code=status.HTTP_201_CREATED,
    responses=IDEMPOTENT_ROUTE_RESPONSES,
)
def refund_payment(
    payment_id: uuid.UUID,
    request: RefundRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_permission(Permission.PAYMENTS_REFUND)
    ),
    idempotency: Idempotency = Depends(),
):
    """Return money for a payment the way it was paid, in the open shift.
    A card refund is run on the external terminal and confirmed here
    (card_confirmed); the payment and its bill are kept unchanged."""

    return idempotency.run(
        lambda: BillService.refund_payment(
            db=db,
            restaurant_id=current_user.restaurant_id,
            user_id=current_user.id,
            payment_id=payment_id,
            amount=request.amount,
            tip_amount=request.tip_amount,
            reason=request.reason,
            external_reference=request.external_reference,
            card_confirmed=request.card_confirmed,
        ),
        payload=request,
    )
