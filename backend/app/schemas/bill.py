import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    Field,
    StringConstraints,
)

from app.payments.providers import check_external_reference
from app.schemas.money import Money, MoneyInput


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



# --- Cashier module (Stage 3) ---

Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
OptionalText = Annotated[
    Annotated[str, StringConstraints(max_length=500)] | None,
    BeforeValidator(lambda value: (value.strip() or None) if isinstance(value, str) else value),
]
TerminalReference = Annotated[
    Annotated[str, StringConstraints(max_length=100)] | None,
    AfterValidator(check_external_reference),
]


class CreatePaymentRequest(PaymentRequest):
    """A payment for a bill, in the open cash shift.

    `amount` still accepts a JSON number for one release (deprecated;
    send a decimal string). Everything new takes decimal strings only.
    """

    method: Literal["CASH", "CARD"]
    # The guest's tip on top of `amount`: never owed, never revenue.
    tip_amount: MoneyInput = Decimal("0")
    # Pay exactly these items (split by items); `amount` must equal
    # their total.
    order_item_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    # CARD only: the terminal's receipt or transaction number, never card
    # data.
    external_reference: TerminalReference = None
    # CARD: the cashier confirms the external terminal approved it.
    card_confirmed: bool = False


class PaymentDetailResponse(PaymentResponse):
    tip_amount: Money
    currency: str
    user_id: uuid.UUID | None
    cash_shift_id: uuid.UUID | None
    business_day_id: uuid.UUID | None
    # CASH_RECEIVED, or MANUAL_CARD_TERMINAL: confirmed by the cashier,
    # not verified with the bank. Null for payments before Stage 3.
    confirmation_method: str | None
    confirmed_at: datetime | None
    external_reference: str | None


class RefundRequest(BaseModel):
    """Money returned for a payment: `amount` of the bill money and/or
    `tip_amount` of its tip (at least one positive)."""

    amount: MoneyInput = Decimal("0")
    tip_amount: MoneyInput = Decimal("0")
    reason: Reason
    external_reference: TerminalReference = None
    card_confirmed: bool = False


class RefundResponse(BaseModel):
    id: uuid.UUID
    payment_id: uuid.UUID
    cash_shift_id: uuid.UUID
    business_day_id: uuid.UUID
    amount: Money
    tip_amount: Money
    currency: str
    method: str
    confirmation_method: str
    external_reference: str | None
    reason: str
    user_id: uuid.UUID
    created_at: datetime


class EqualSplitResponse(BaseModel):
    bill_id: uuid.UUID
    amount_due: Money
    # Add up to amount_due exactly; the leftover cents go to the first.
    shares: list[Money]


class BillLineModifier(BaseModel):
    name: str
    price: Money


class BillLine(BaseModel):
    """An order item as billed: its snapshots at the time it was added."""

    order_item_id: uuid.UUID
    product_name: str | None
    quantity: int
    unit_price: Money
    total_price: Money
    status: str
    notes: str | None
    modifiers: list[BillLineModifier]
    # The payment that paid this item when the bill was split by items.
    paid_by_payment_id: uuid.UUID | None


class BillRefund(BaseModel):
    id: uuid.UUID
    amount: Money
    tip_amount: Money
    method: str
    reason: str
    user_name: str | None
    created_at: datetime


class BillPayment(BaseModel):
    id: uuid.UUID
    amount: Money
    tip_amount: Money
    method: str
    confirmation_method: str | None
    external_reference: str | None
    user_name: str | None
    created_at: datetime
    refunded_amount: Money
    refunded_tip_amount: Money
    refunds: list[BillRefund]


class BillOrderSummary(BaseModel):
    id: uuid.UUID
    status: str
    table_number: int
    table_name: str | None
    guest_count: int
    waiter_name: str | None


class BillDetailResponse(BillResponse):
    """A bill with its lines, payments, refunds and tips. Tips and refunds
    never change what is owed (`amount_due`)."""

    currency: str
    order: BillOrderSummary
    lines: list[BillLine]
    payments: list[BillPayment]
    tips_total: Money
    refunded_total: Money
    refunded_tips_total: Money


class CashierOrderResponse(BaseModel):
    """An order in the cashier's list: what is billed, paid and due."""

    order_id: uuid.UUID
    order_status: str
    table_number: int
    table_name: str | None
    guest_count: int
    waiter_name: str | None
    created_at: datetime
    items_count: int
    # Sum of the order's uncancelled items (before a bill exists).
    items_total: Money
    bill_id: uuid.UUID | None
    bill_status: str | None
    paid_total: Money
    amount_due: Money
