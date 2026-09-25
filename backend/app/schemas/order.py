import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class CreateOrderRequest(BaseModel):
    table_id: uuid.UUID
    guest_count: int = Field(gt=0)


class OrderResponse(BaseModel):
    id: uuid.UUID
    restaurant_id: uuid.UUID
    table_id: uuid.UUID
    user_id: uuid.UUID
    guest_count: int
    status: str
    created_at: datetime


class AddOrderItemRequest(BaseModel):
    product_id: uuid.UUID
    quantity: int = Field(gt=0)
    modifier_ids: list[uuid.UUID] = Field(default_factory=list)


class OrderItemModifierResponse(BaseModel):
    id: uuid.UUID
    modifier_id: uuid.UUID
    name: str
    price: float


class OrderItemResponse(BaseModel):
    id: uuid.UUID
    order_id: uuid.UUID
    product_id: uuid.UUID
    quantity: int
    unit_price: float
    total_price: float
    status: str
    created_at: datetime
    cancelled_at: datetime | None = None
    modifiers: list[OrderItemModifierResponse] = []


class OrderDetailResponse(BaseModel):
    id: uuid.UUID
    restaurant_id: uuid.UUID
    table_id: uuid.UUID
    user_id: uuid.UUID
    guest_count: int
    status: str
    created_at: datetime
    items: list[OrderItemResponse]


class CancelOrderItemRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)
