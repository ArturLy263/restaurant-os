import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.bill import OptionalText, Reason
from app.schemas.money import Money, MoneyInput


class OpenBusinessDayRequest(BaseModel):
    # Defaults to today in the restaurant's time zone.
    business_date: date | None = None


class BusinessDayResponse(BaseModel):
    id: uuid.UUID
    business_date: date
    status: str
    opened_by_user_id: uuid.UUID
    opened_at: datetime
    closed_by_user_id: uuid.UUID | None
    closed_at: datetime | None


class BusinessDayReportResponse(BaseModel):
    """A business day's report: frozen at closing for a closed day."""

    business_day: BusinessDayResponse
    report: dict[str, Any]


class OpenShiftRequest(BaseModel):
    # The cash the opening cashier counted in the drawer.
    opening_amount: MoneyInput
    # Required when it differs from the previous shift's count.
    handover_reason: OptionalText = None


class CloseShiftRequest(BaseModel):
    counted_amount: MoneyInput
    # Required when the count differs from the expected cash.
    explanation: OptionalText = None


class HandoverRequest(BaseModel):
    """Close the open shift and open the next one in one step."""

    counted_amount: MoneyInput
    closing_explanation: OptionalText = None
    # What the incoming cashier counted: the new shift's opening amount.
    received_amount: MoneyInput
    handover_reason: OptionalText = None


class OpeningCorrectionRequest(BaseModel):
    opening_amount: MoneyInput
    reason: Reason


class CashMovementRequest(BaseModel):
    type: Literal["CASH_IN", "CASH_OUT"]
    amount: MoneyInput = Field(gt=Decimal("0"))
    reason: Reason


class UserRef(BaseModel):
    id: uuid.UUID
    name: str | None


class OpeningCorrectionResponse(BaseModel):
    id: uuid.UUID
    cash_shift_id: uuid.UUID
    previous_amount: Money
    new_amount: Money
    reason: str
    user: UserRef
    created_at: datetime


class CashMovementResponse(BaseModel):
    id: uuid.UUID
    cash_shift_id: uuid.UUID
    type: str
    amount: Money
    reason: str | None
    payment_id: uuid.UUID | None
    refund_id: uuid.UUID | None
    user: UserRef
    created_at: datetime


class HandoverResponse(BaseModel):
    id: uuid.UUID
    cash_register_id: uuid.UUID
    business_day_id: uuid.UUID
    from_shift_id: uuid.UUID
    to_shift_id: uuid.UUID
    # The outgoing shift's expected cash and count (handed over), the
    # incoming cashier's count (received) and received - handed over.
    previous_expected_amount: Money
    handed_over_amount: Money
    received_amount: Money
    difference: Money
    reason: str | None
    outgoing_user: UserRef
    incoming_user: UserRef
    created_at: datetime


class ShiftClosingResponse(BaseModel):
    expected_amount: Money
    counted_amount: Money
    difference: Money
    explanation: str | None
    user: UserRef
    created_at: datetime


class CashShiftResponse(BaseModel):
    id: uuid.UUID
    cash_register_id: uuid.UUID
    business_day_id: uuid.UUID
    status: str
    currency: str
    # As counted when opening; corrections are listed below and give the
    # effective opening amount used for expected cash.
    opening_amount: Money
    effective_opening_amount: Money
    opened_by: UserRef
    opened_at: datetime
    closed_at: datetime | None
    # The drawer now (open shift) or at closing.
    expected_cash: Money
    drawer: dict[str, Any]
    corrections: list[OpeningCorrectionResponse]
    movements: list[CashMovementResponse]
    handover: HandoverResponse | None
    closing: ShiftClosingResponse | None


class PreviousClosingResponse(BaseModel):
    """The register's last closed shift: what the next cashier receives."""

    cash_shift_id: uuid.UUID
    business_day_id: uuid.UUID
    expected_amount: Money
    counted_amount: Money
    closed_at: datetime
    closed_by: UserRef


class CashRegisterResponse(BaseModel):
    register_id: uuid.UUID | None
    business_day: BusinessDayResponse | None
    shift: CashShiftResponse | None
    previous_closing: PreviousClosingResponse | None
