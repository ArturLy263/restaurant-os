import uuid

from pydantic import BaseModel

from app.schemas.money import Money


class MenuStationResponse(BaseModel):
    id: uuid.UUID
    name: str
    type: str
    is_active: bool


class MenuModifierResponse(BaseModel):
    id: uuid.UUID
    name: str
    price_adjustment: Money


class MenuModifierGroupResponse(BaseModel):
    id: uuid.UUID
    name: str
    min_selections: int
    max_selections: int | None
    is_required: bool
    # Active modifiers only.
    modifiers: list[MenuModifierResponse]


class MenuProductResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    price: Money
    # Where the product is prepared once sent; null if it has no station.
    station: MenuStationResponse | None
    # Whether the product can be ordered and sent now. If not, the reason:
    # PRODUCT_INACTIVE, PRODUCT_HAS_NO_STATION or STATION_INACTIVE (the
    # error codes adding or sending it would return).
    is_available: bool
    unavailable_reason: str | None
    modifier_groups: list[MenuModifierGroupResponse]


class MenuCategoryResponse(BaseModel):
    id: uuid.UUID
    name: str
    display_order: int
    products: list[MenuProductResponse]


class MenuResponse(BaseModel):
    # ISO 4217 code of the restaurant's prices, e.g. "EUR".
    currency: str
    # Active categories, in display order.
    categories: list[MenuCategoryResponse]
