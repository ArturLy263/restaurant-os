import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.idempotency import IDEMPOTENT_ROUTE_RESPONSES, Idempotency
from app.core.dependencies import (
    AuthContext,
    get_auth_context,
    require_permission,
)
from app.core.permissions import Permission
from app.db.dependencies import get_db
from app.db.models.user import User
from app.schemas.order import (
    AddOrderItemRequest,
    CreateOrderRequest,
    OrderDetailResponse,
    OrderItemResponse,
    OrderResponse,
    CancelOrderItemRequest,
    UpdateOrderItemRequest,
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
    current_user: User = Depends(
        require_permission(Permission.ORDERS_CREATE)
    ),
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
    responses=IDEMPOTENT_ROUTE_RESPONSES,
)
def add_order_item(
    order_id: uuid.UUID,
    request: AddOrderItemRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_permission(Permission.ORDERS_ADD_ITEMS)
    ),
    auth: AuthContext = Depends(get_auth_context),
    idempotency: Idempotency = Depends(),
):
    # Without a key a retry adds the item again; with one it replays.
    return idempotency.run(
        lambda: OrderService.add_order_item(
            db=db,
            restaurant_id=current_user.restaurant_id,
            user_id=current_user.id,
            order_id=order_id,
            product_id=request.product_id,
            quantity=request.quantity,
            modifier_ids=request.modifier_ids,
            notes=request.notes,
            can_modify_any_order=(
                Permission.ORDERS_MANAGE_ALL in auth.permissions
            ),
        ),
        payload=request,
    )

@router.post(
    "/{order_id}/send",
    response_model=OrderDetailResponse,
    responses=IDEMPOTENT_ROUTE_RESPONSES,
)
def send_order(
    order_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_permission(Permission.ORDERS_SEND)
    ),
    auth: AuthContext = Depends(get_auth_context),
    idempotency: Idempotency = Depends(),
):
    return idempotency.run(
        lambda: OrderService.send_order(
            db=db,
            restaurant_id=current_user.restaurant_id,
            user_id=current_user.id,
            order_id=order_id,
            can_modify_any_order=(
                Permission.ORDERS_MANAGE_ALL in auth.permissions
            ),
        ),
    )

@router.get(
    "/{order_id}",
    response_model=OrderDetailResponse,
)
def get_order(
    order_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_permission(Permission.ORDERS_VIEW)
    ),
):
    order = OrderService.get_order(
        db=db,
        restaurant_id=current_user.restaurant_id,
        order_id=order_id,
    )

    return order
@router.patch(
    "/{order_id}/items/{item_id}",
    response_model=OrderItemResponse,
    responses=IDEMPOTENT_ROUTE_RESPONSES,
)
def update_order_item(
    order_id: uuid.UUID,
    item_id: uuid.UUID,
    request: UpdateOrderItemRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_permission(Permission.ORDERS_ADD_ITEMS)
    ),
    auth: AuthContext = Depends(get_auth_context),
    idempotency: Idempotency = Depends(),
):
    """Change the quantity or note of an item that was not sent yet."""

    return idempotency.run(
        lambda: OrderService.update_order_item(
            db=db,
            restaurant_id=current_user.restaurant_id,
            user_id=current_user.id,
            order_id=order_id,
            item_id=item_id,
            quantity=request.quantity,
            notes=request.notes,
            update_notes="notes" in request.model_fields_set,
            can_modify_any_order=(
                Permission.ORDERS_MANAGE_ALL in auth.permissions
            ),
        ),
        payload=request,
    )


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
    current_user: User = Depends(
        require_permission(Permission.ORDERS_CANCEL_ITEMS)
    ),
    auth: AuthContext = Depends(get_auth_context),
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
            can_modify_any_order=(
                Permission.ORDERS_MANAGE_ALL in auth.permissions
            ),
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
    current_user: User = Depends(
        require_permission(Permission.ORDERS_VOID)
    ),
    auth: AuthContext = Depends(get_auth_context),
    idempotency: Idempotency = Depends(),
):
    return idempotency.run(
        lambda: OrderService.void_order(
            db=db,
            restaurant_id=current_user.restaurant_id,
            user_id=current_user.id,
            order_id=order_id,
            reason=request.reason,
            can_modify_any_order=(
                Permission.ORDERS_MANAGE_ALL in auth.permissions
            ),
            can_adjust_bills=Permission.BILLS_ADJUST in auth.permissions,
        ),
        payload=request,
    )
