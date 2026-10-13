import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.idempotency import IDEMPOTENT_ROUTE_RESPONSES, Idempotency
from app.core.dependencies import get_current_user
from app.db.dependencies import get_db
from app.db.models.user import User
from app.schemas.order import (
    AddOrderItemRequest,
    CreateOrderRequest,
    OrderDetailResponse,
    OrderItemResponse,
    OrderResponse,
    CancelOrderItemRequest,
    VoidOrderRequest,
)
from app.services.order_service import OrderService


router = APIRouter(
    prefix="/api/v1/orders",
    tags=["orders"],
)


@router.post(
    "",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
    responses=IDEMPOTENT_ROUTE_RESPONSES,
)
def create_order(
    request: CreateOrderRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency: Idempotency = Depends(),
):
    return idempotency.run(
        lambda: OrderService.create_order(
            db=db,
            restaurant_id=current_user.restaurant_id,
            user_id=current_user.id,
            table_id=request.table_id,
            guest_count=request.guest_count,
        ),
        payload=request,
    )


@router.post(
    "/{order_id}/items",
    response_model=OrderItemResponse,
    status_code=201,
)
def add_order_item(
    order_id: uuid.UUID,
    request: AddOrderItemRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order_item = OrderService.add_order_item(
        db=db,
        restaurant_id=current_user.restaurant_id,
        user_id=current_user.id,
        order_id=order_id,
        product_id=request.product_id,
        quantity=request.quantity,
        modifier_ids=request.modifier_ids,
    )

    return order_item

@router.post(
    "/{order_id}/send",
    response_model=OrderDetailResponse,
    responses=IDEMPOTENT_ROUTE_RESPONSES,
)
def send_order(
    order_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency: Idempotency = Depends(),
):
    return idempotency.run(
        lambda: OrderService.send_order(
            db=db,
            restaurant_id=current_user.restaurant_id,
            user_id=current_user.id,
            order_id=order_id,
        ),
    )

@router.get(
    "/{order_id}",
    response_model=OrderDetailResponse,
)
def get_order(
    order_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = OrderService.get_order(
        db=db,
        restaurant_id=current_user.restaurant_id,
        order_id=order_id,
    )

    return order
@router.post(
    "/{order_id}/items/{item_id}/cancel",
    response_model=OrderItemResponse,
    responses=IDEMPOTENT_ROUTE_RESPONSES,
)
def cancel_order_item(
    order_id: uuid.UUID,
    item_id: uuid.UUID,
    request: CancelOrderItemRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency: Idempotency = Depends(),
):
    return idempotency.run(
        lambda: OrderService.cancel_order_item(
            db=db,
            restaurant_id=current_user.restaurant_id,
            user_id=current_user.id,
            order_id=order_id,
            item_id=item_id,
            reason=request.reason,
        ),
        payload=request,
    )


@router.post(
    "/{order_id}/void",
    response_model=OrderDetailResponse,
    responses=IDEMPOTENT_ROUTE_RESPONSES,
)
def void_order(
    order_id: uuid.UUID,
    request: VoidOrderRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency: Idempotency = Depends(),
):
    return idempotency.run(
        lambda: OrderService.void_order(
            db=db,
            restaurant_id=current_user.restaurant_id,
            user_id=current_user.id,
            order_id=order_id,
            reason=request.reason,
        ),
        payload=request,
    )
