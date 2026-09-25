import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.db.dependencies import get_db
from app.db.models.user import User
from app.schemas.order import (
    AddOrderItemRequest,
    CreateOrderRequest,
    OrderDetailResponse,
    OrderItemResponse,
    OrderItemModifierResponse,
    OrderResponse,
    CancelOrderItemRequest,
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
)
def create_order(
    request: CreateOrderRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = OrderService.create_order(
        db=db,
        restaurant_id=current_user.restaurant_id,
        user_id=current_user.id,
        table_id=request.table_id,
        guest_count=request.guest_count,
    )

    return order


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

    return OrderItemResponse(
        id=order_item.id,
        order_id=order_item.order_id,
        product_id=order_item.product_id,
        quantity=order_item.quantity,
        unit_price=float(order_item.unit_price),
        total_price=float(order_item.total_price),
        status=order_item.status,
        created_at=order_item.created_at,
        modifiers=[
            OrderItemModifierResponse(
                id=modifier.id,
                modifier_id=modifier.modifier_id,
                name=modifier.name_snapshot,
                price=float(modifier.price_snapshot),
            )
            for modifier in order_item.modifiers
        ],
    )

@router.post(
    "/{order_id}/send",
    response_model=OrderDetailResponse,
)
def send_order(
    order_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = OrderService.send_order(
        db=db,
        restaurant_id=current_user.restaurant_id,
        user_id=current_user.id,
        order_id=order_id,
    )

    return order

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
)
def cancel_order_item(
    order_id: uuid.UUID,
    item_id: uuid.UUID,
    request: CancelOrderItemRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order_item = OrderService.cancel_order_item(
        db=db,
        restaurant_id=current_user.restaurant_id,
        user_id=current_user.id,
        order_id=order_id,
        item_id=item_id,
        reason=request.reason,
    )

    return OrderItemResponse(
        id=order_item.id,
        order_id=order_item.order_id,
        product_id=order_item.product_id,
        quantity=order_item.quantity,
        unit_price=float(order_item.unit_price),
        total_price=float(order_item.total_price),
        status=order_item.status,
        created_at=order_item.created_at,
        cancelled_at=order_item.cancelled_at,
        modifiers=[
            OrderItemModifierResponse(
                id=modifier.id,
                modifier_id=modifier.modifier_id,
                name=modifier.name_snapshot,
                price=float(modifier.price_snapshot),
            )
            for modifier in order_item.modifiers
        ],
    )
