import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class BillAdjustmentResponse(BaseModel):
    id: uuid.UUID
    order_item_id: uuid.UUID | None
    type: str
    amount: Decimal
    status: str
    reason: str
    user_id: uuid.UUID | None
    created_at: datetime


class CreateBillResponse(BaseModel):
    id: uuid.UUID
    order_id: uuid.UUID
    # subtotal/total are the original bill amounts; the fields below are
    # derived from adjustments and payments.
    subtotal: Decimal
    total: Decimal
    status: str
    created_at: datetime
    voided_at: datetime | None
    adjustments_total: Decimal
    adjusted_total: Decimal
    paid_total: Decimal
    amount_due: Decimal
    adjustments: list[BillAdjustmentResponse]


class PaymentRequest(BaseModel):
    # Matches the Numeric(10, 2) money columns.
    amount: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    method: str


class PaymentResponse(BaseModel):
    id: uuid.UUID
    bill_id: uuid.UUID
    amount: Decimal
    method: str
    status: str
    created_at: datetime

class BillResponse(BaseModel):
    id: uuid.UUID
    order_id: uuid.UUID
    # subtotal/total are the original bill amounts; the fields below are
    # derived from adjustments and payments.
    subtotal: Decimal
    total: Decimal
    status: str
    created_at: datetime
    paid_at: datetime | None
    voided_at: datetime | None
    adjustments_total: Decimal
    adjusted_total: Decimal
    paid_total: Decimal
    amount_due: Decimal
    adjustments: list[BillAdjustmentResponse]

