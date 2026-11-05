import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.bill import OptionalText, Reason
from app.schemas.money import Money, MoneyInput


class OpenBusinessDayRequest(BaseModel):
    # Defaults to today in the restaurant's time zone. Never in the future,
    # always after the latest business day; before yesterday only with
    # business_days:open_past.
    business_date: date | None = None


class BusinessDayResponse(BaseModel):
    id: uuid.UUID
    business_date: date
    status: str
    opened_by_user_id: uuid.UUID
    opened_at: datetime
    closed_by_user_id: uuid.UUID | None
    closed_at: datetime | None


# --- Reports (app.services.cash_ledger) ---
#
# Every amount is a two-decimal string. The same shapes are frozen as JSON
# in cash_shift_closings.summary and business_days.closing_summary.


class MethodTotals(BaseModel):
    CASH: Money
    CARD: Money
    total: Money


class DrawerReport(BaseModel):
    """What the drawer should hold: effective opening + cash sales + cash
    tips + cash in - cash out - cash refunds (tip included)."""

    # As counted at opening, and after the latest opening correction.
    opening_amount: Money
    effective_opening_amount: Money
    opening_corrections_count: int
    cash_sales: Money
    cash_tips: Money
    cash_in: Money
    cash_out: Money
    # Bill money and tips returned in cash.
    cash_refunds: Money
    expected: Money


class ClosingSummary(BaseModel):
    expected_amount: Money
    counted_amount: Money
    difference: Money
    explanation: str | None


class FlowsReport(BaseModel):
    """Payments and refunds of a period, per method. revenue = sales -
    refunds (bill money only); net_tips = tips - refunded_tips: a refunded
    tip reduces tips, never revenue."""

    sales: MethodTotals
    refunds: MethodTotals
    revenue: MethodTotals
    tips: MethodTotals
    refunded_tips: MethodTotals
    net_tips: MethodTotals
    payments_count: int
    refunds_count: int


class ShiftReport(FlowsReport):
    cash_shift_id: uuid.UUID
    business_day_id: uuid.UUID
    currency: str
    drawer: DrawerReport
    # Null while the shift is open.
    closing: ClosingSummary | None


class DayShiftReport(ShiftReport):
    status: str
    # The name of the employee who opened the shift.
    opened_by: str | None
    opened_at: datetime
    closed_at: datetime | None


class HandoverReport(BaseModel):
    id: uuid.UUID
    from_shift_id: uuid.UUID
    to_shift_id: uuid.UUID
    business_day_id: uuid.UUID
    previous_expected_amount: Money
    handed_over_amount: Money
    received_amount: Money
    difference: Money
    reason: str | None
    # Employee names.
    outgoing_user: str | None
    incoming_user: str | None
    # Null in reports frozen before Stage 3.1.
    self_confirmed: bool | None = None
    created_at: datetime


class BusinessDayReport(FlowsReport):
    """A business day: its payments and refunds (all shifts), each shift's
    drawer and closing, and the handovers into its shifts (a handover
    belongs to the incoming shift's day)."""

    business_day_id: uuid.UUID
    business_date: date
    currency: str
    shifts: list[DayShiftReport]
    handovers: list[HandoverReport]
    # Sum of the closings' count differences, and of the handovers'
    # received - handed over: neither is revenue nor expense.
    closing_difference_total: Money
    handover_difference_total: Money


class BusinessDayReportResponse(BaseModel):
    """A business day's report: frozen at closing for a closed day."""

    business_day: BusinessDayResponse
    report: BusinessDayReport


class OpenShiftRequest(BaseModel):
    """Open the register's next shift. After a closed shift this is the
    incoming cashier's confirmation of the handover (step 2): the cash
    physically received, counted blind."""

    # The cash the opening cashier counted in the drawer.
    opening_amount: MoneyInput
    # Required when it differs from the previous shift's count.
    handover_reason: OptionalText = None


class CloseShiftRequest(BaseModel):
    counted_amount: MoneyInput
    # Required when the count differs from the expected cash.
    explanation: OptionalText = None


class HandoverRequest(BaseModel):
    """Step 1 of a handover: the outgoing cashier counts and closes the
    shift. The incoming cashier confirms what they received by opening the
    next shift (OpenShiftRequest). Unknown fields are refused, so a client
    of the former one-step handover (received_amount, handover_reason)
    gets a 422 instead of a silently half-done handover."""

    model_config = ConfigDict(extra="forbid")

    counted_amount: MoneyInput
    closing_explanation: OptionalText = None


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
    # The outgoing shift's expected cash (null without
    # cash_drawer:view_expected: cashiers count blind) and count (handed
    # over), the incoming cashier's count (received) and received - handed
    # over.
    previous_expected_amount: Money | None
    handed_over_amount: Money
    received_amount: Money
    difference: Money
    reason: str | None
    # The employee who closed and counted the outgoing shift, and the one
    # who received and counted the cash.
    outgoing_user: UserRef
    incoming_user: UserRef
    # One person on both sides (only with cash_handovers:self_confirm).
    self_confirmed: bool
    created_at: datetime


class ShiftClosingResponse(BaseModel):
    """A closed shift's count. Without cash_drawer:view_expected the
    expected cash and the difference are null (blind count), and so is the
    count while its handover is pending, except for whoever counted it."""

    expected_amount: Money | None
    counted_amount: Money | None
    difference: Money | None
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
    # The drawer now (open shift) or at closing; null without
    # cash_drawer:view_expected (cashiers count blind).
    expected_cash: Money | None
    drawer: "DrawerReport | None"
    # Whether expected cash figures are shown to this user.
    expected_visible: bool
    corrections: list[OpeningCorrectionResponse]
    movements: list[CashMovementResponse]
    handover: HandoverResponse | None
    closing: ShiftClosingResponse | None


class PreviousClosingResponse(BaseModel):
    """The register's last closed shift: what the next cashier receives.

    The next cashier counts blind: without cash_drawer:view_expected the
    expected amount is null, and so is the count while the handover is
    pending (except for whoever counted it)."""

    cash_shift_id: uuid.UUID
    business_day_id: uuid.UUID
    expected_amount: Money | None
    counted_amount: Money | None
    closed_at: datetime
    closed_by: UserRef
    # True until the next shift opens and confirms the cash received.
    handover_pending: bool


class CashRegisterResponse(BaseModel):
    register_id: uuid.UUID | None
    business_day: BusinessDayResponse | None
    shift: CashShiftResponse | None
    previous_closing: PreviousClosingResponse | None
