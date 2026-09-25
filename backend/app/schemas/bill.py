import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class CreateBillResponse(BaseModel):
    id: uuid.UUID
    order_id: uuid.UUID
    subtotal: Decimal
    total: Decimal
    status: str
    created_at: datetime


class PaymentRequest(BaseModel):
    amount: Decimal = Field(gt=0)
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
    subtotal: Decimal
    total: Decimal
    status: str
    created_at: datetime
    paid_at: datetime | None

