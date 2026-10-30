import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, Field, field_validator


def _blank_to_none(value):
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


# A waiter's note for the kitchen or bar; surrounding whitespace is
# removed and an empty note means none.
ItemNotes = Annotated[
    Annotated[str, Field(max_length=500)] | None,
    BeforeValidator(_blank_to_none),
]


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
    notes: ItemNotes = None


class UpdateOrderItemRequest(BaseModel):
    """Changes to an item not sent yet. Omitted fields stay unchanged;
    `notes: null` (or an empty note) removes the note."""

    quantity: int | None = Field(default=None, gt=0)
    notes: ItemNotes = None


class OrderItemPreparationResponse(BaseModel):
    id: uuid.UUID
    station_id: uuid.UUID
    # The station's current name.
    station_name: str
    status: str

    @classmethod
    def from_preparation(cls, preparation):
        return cls(
            id=preparation.id,
            station_id=preparation.station_id,
            station_name=preparation.station.name,
            status=preparation.status,
        )


class OrderItemModifierResponse(BaseModel):
    id: uuid.UUID
    modifier_id: uuid.UUID
    # Read from the OrderItemModifier snapshot columns, exposed as name/price.
    name: str = Field(validation_alias="name_snapshot")
    price: float = Field(validation_alias="price_snapshot")


class OrderItemResponse(BaseModel):
    id: uuid.UUID
    order_id: uuid.UUID
    product_id: uuid.UUID
    # The product's name when the item was added; null for items added
    # before name snapshots (migration a3d9e7c1f5b2).
    product_name: str | None = Field(default=None, validation_alias="product_name_snapshot")
    quantity: int
    unit_price: float
    total_price: float
    status: str
    created_at: datetime
    cancelled_at: datetime | None = None
    modifiers: list[OrderItemModifierResponse] = []
    notes: str | None = None
    # Where and how far the item is being prepared; null until sent.
    preparation: OrderItemPreparationResponse | None = None

    @field_validator("preparation", mode="before")
    @classmethod
    def _preparation(cls, value):
        if value is None or isinstance(value, (dict, OrderItemPreparationResponse)):
            return value
        return OrderItemPreparationResponse.from_preparation(value)


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


class VoidOrderRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)
