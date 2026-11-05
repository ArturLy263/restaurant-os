"""Business days and the shared cash register."""

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.idempotency import (
    IDEMPOTENT_ROUTE_RESPONSES,
    REQUIRED_IDEMPOTENCY_ROUTE_RESPONSES,
    Idempotency,
    RequiredIdempotency,
)
from app.core.dependencies import AuthContext, get_auth_context, require_permission
from app.core.permissions import Permission
from app.db.dependencies import get_db
from app.db.models.cash import BusinessDay
from app.db.models.user import User
from app.schemas.cash import (
    BusinessDayReportResponse,
    BusinessDayResponse,
    CashMovementRequest,
    CashMovementResponse,
    CashRegisterResponse,
    CashShiftResponse,
    CloseShiftRequest,
    HandoverRequest,
    HandoverResponse,
    OpenBusinessDayRequest,
    OpeningCorrectionRequest,
    OpeningCorrectionResponse,
    OpenShiftRequest,
)
from app.services import cashier_views
from app.services.cash_service import CashService
from app.services.cashier_views import CashViewer


def _viewer(auth: AuthContext) -> CashViewer:
    return CashViewer(
        user_id=auth.user.id,
        sees_expected=Permission.CASH_DRAWER_VIEW_EXPECTED in auth.permissions,
    )

business_days_router = APIRouter(
    prefix="/api/v1/business-days",
    tags=["business days"],
)

router = APIRouter(
    prefix="/api/v1/cash",
    tags=["cash register"],
)


# --- Business days ---


@business_days_router.post(
    "",
    response_model=BusinessDayResponse,
    status_code=status.HTTP_201_CREATED,
    responses=IDEMPOTENT_ROUTE_RESPONSES,
)
def open_business_day(
    request: OpenBusinessDayRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.BUSINESS_DAYS_OPEN)),
    auth: AuthContext = Depends(get_auth_context),
    idempotency: Idempotency = Depends(),
):
    """Open the next business day, dated today (restaurant time) unless
    `business_date` is given: yesterday is allowed (a day opened after
    midnight), never the future, always after the latest day; earlier
    dates need business_days:open_past (403 otherwise)."""

    return idempotency.run(
        lambda: CashService.open_business_day(
            db=db,
            restaurant_id=current_user.restaurant_id,
            user_id=current_user.id,
            business_date=request.business_date,
            may_open_past=Permission.BUSINESS_DAYS_OPEN_PAST in auth.permissions,
        ),
        payload=request,
    )


@business_days_router.post(
    "/{business_day_id}/close",
    response_model=BusinessDayResponse,
    responses=IDEMPOTENT_ROUTE_RESPONSES,
)
def close_business_day(
    business_day_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.BUSINESS_DAYS_CLOSE)),
    idempotency: Idempotency = Depends(),
):
    """Close the day and freeze its report. Refused (409, with the list in
    `error.details`) while a cash shift is open or any order is active."""

    return idempotency.run(
        lambda: CashService.close_business_day(
            db=db,
            restaurant_id=current_user.restaurant_id,
            user_id=current_user.id,
            business_day_id=business_day_id,
        ),
    )


@business_days_router.get(
    "",
    response_model=list[BusinessDayResponse],
)
def list_business_days(
    limit: int = Query(30, ge=1, le=366),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.CASH_REPORTS_VIEW)),
):
    return db.scalars(
        select(BusinessDay)
        .where(BusinessDay.restaurant_id == current_user.restaurant_id)
        .order_by(BusinessDay.business_date.desc())
        .limit(limit)
    ).all()


@business_days_router.get(
    "/{business_day_id}/report",
    response_model=BusinessDayReportResponse,
)
def business_day_report(
    business_day_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.CASH_REPORTS_VIEW)),
):
    """Revenue (without tips), tips, refunds by method; every shift's
    drawer and closing; the handovers into the day. A closed day's report
    is the one frozen when it closed."""

    day = CashService.get_business_day(db, current_user.restaurant_id, business_day_id)
    return {
        "business_day": day,
        "report": CashService.business_day_report(db, current_user.restaurant_id, day),
    }


# --- Register and shifts ---


@router.get(
    "/register",
    response_model=CashRegisterResponse,
)
def get_register(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.CASH_SHIFTS_VIEW)),
    auth: AuthContext = Depends(get_auth_context),
):
    """The open business day, the open shift with its drawer, and the
    previous shift's closing (what the next cashier should receive).
    Expected cash only with cash_drawer:view_expected (blind count)."""

    return cashier_views.register_view(db, current_user.restaurant_id, _viewer(auth))


@router.post(
    "/shifts",
    response_model=CashShiftResponse,
    status_code=status.HTTP_201_CREATED,
    responses=REQUIRED_IDEMPOTENCY_ROUTE_RESPONSES,
)
def open_shift(
    request: OpenShiftRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.CASH_SHIFTS_OPEN)),
    auth: AuthContext = Depends(get_auth_context),
    idempotency: RequiredIdempotency = Depends(),
):
    """Open the register's shift with the cash counted (blind).

    After a closed shift this is step 2 of the handover: the incoming
    cashier confirms the cash physically received, which becomes the
    opening amount. If it differs from the previous shift's count,
    `handover_reason` is required; the difference is recorded on the
    handover (in this business day), never as a cash movement. The
    employee who closed the previous shift can't confirm it (409
    HANDOVER_SAME_USER) without cash_handovers:self_confirm."""

    def run():
        shift = CashService.open_shift(
            db=db,
            restaurant_id=current_user.restaurant_id,
            user_id=current_user.id,
            opening_amount=request.opening_amount,
            handover_reason=request.handover_reason,
            may_self_confirm=Permission.CASH_HANDOVERS_SELF_CONFIRM in auth.permissions,
        )
        return cashier_views.shift_detail(db, CashService.get_shift(db, current_user.restaurant_id, shift.id), _viewer(auth))

    return idempotency.run(run, payload=request)


@router.get(
    "/shifts/{shift_id}",
    response_model=CashShiftResponse,
)
def get_shift(
    shift_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.CASH_SHIFTS_VIEW)),
    auth: AuthContext = Depends(get_auth_context),
):
    return cashier_views.shift_detail(db, CashService.get_shift(db, current_user.restaurant_id, shift_id), _viewer(auth))


@router.post(
    "/shifts/{shift_id}/close",
    response_model=CashShiftResponse,
    responses=REQUIRED_IDEMPOTENCY_ROUTE_RESPONSES,
)
def close_shift(
    shift_id: uuid.UUID,
    request: CloseShiftRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.CASH_SHIFTS_CLOSE)),
    auth: AuthContext = Depends(get_auth_context),
    idempotency: RequiredIdempotency = Depends(),
):
    """Close the shift with the cash counted; `explanation` is required
    when it differs from the expected cash. Orders, tables, bills and the
    business day are not affected."""

    def run():
        CashService.close_shift(
            db=db,
            restaurant_id=current_user.restaurant_id,
            user_id=current_user.id,
            shift_id=shift_id,
            counted_amount=request.counted_amount,
            explanation=request.explanation,
        )
        return cashier_views.shift_detail(db, CashService.get_shift(db, current_user.restaurant_id, shift_id), _viewer(auth))

    return idempotency.run(run, payload=request)


@router.post(
    "/shifts/{shift_id}/handover",
    response_model=CashShiftResponse,
    responses=REQUIRED_IDEMPOTENCY_ROUTE_RESPONSES,
)
def hand_over_shift(
    shift_id: uuid.UUID,
    request: HandoverRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.CASH_SHIFTS_CLOSE)),
    auth: AuthContext = Depends(get_auth_context),
    idempotency: RequiredIdempotency = Depends(),
):
    """Step 1 of a two-party handover: the outgoing cashier counts the
    drawer and closes the shift under their own name; returns the closed
    shift. Step 2 is the incoming cashier opening the next shift
    (POST /cash/shifts) with the cash they received, under their own name.
    Until then payments are refused. The former one-step body
    (`received_amount`, `handover_reason`) is refused (422)."""

    def run():
        CashService.hand_over(
            db=db,
            restaurant_id=current_user.restaurant_id,
            user_id=current_user.id,
            shift_id=shift_id,
            counted_amount=request.counted_amount,
            closing_explanation=request.closing_explanation,
        )
        return cashier_views.shift_detail(db, CashService.get_shift(db, current_user.restaurant_id, shift_id), _viewer(auth))

    return idempotency.run(run, payload=request)


@router.post(
    "/shifts/{shift_id}/opening-corrections",
    response_model=OpeningCorrectionResponse,
    status_code=status.HTTP_201_CREATED,
    responses=REQUIRED_IDEMPOTENCY_ROUTE_RESPONSES,
)
def correct_opening_amount(
    shift_id: uuid.UUID,
    request: OpeningCorrectionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.CASH_OPENING_BALANCE_CORRECT)),
    auth: AuthContext = Depends(get_auth_context),
    idempotency: RequiredIdempotency = Depends(),
):
    """Correct a mistyped opening amount of an open shift (manager). The
    original amount and every correction are kept; no cash movement."""

    def run():
        correction = CashService.correct_opening_amount(
            db=db,
            restaurant_id=current_user.restaurant_id,
            user_id=current_user.id,
            shift_id=shift_id,
            new_amount=request.opening_amount,
            reason=request.reason,
        )
        return {
            **{f: getattr(correction, f) for f in (
                "id", "cash_shift_id", "previous_amount", "new_amount", "reason", "created_at",
            )},
            "user": {"id": current_user.id, "name": current_user.name},
        }

    return idempotency.run(run, payload=request)


@router.post(
    "/shifts/{shift_id}/movements",
    response_model=CashMovementResponse,
    status_code=status.HTTP_201_CREATED,
    responses=REQUIRED_IDEMPOTENCY_ROUTE_RESPONSES,
)
def record_cash_movement(
    shift_id: uuid.UUID,
    request: CashMovementRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.CASH_MOVEMENTS_CREATE)),
    auth: AuthContext = Depends(get_auth_context),
    idempotency: RequiredIdempotency = Depends(),
):
    """Cash really put into (CASH_IN) or taken out of (CASH_OUT) the drawer."""

    def run():
        movement = CashService.record_movement(
            db=db,
            restaurant_id=current_user.restaurant_id,
            user_id=current_user.id,
            shift_id=shift_id,
            movement_type=request.type,
            amount=request.amount,
            reason=request.reason,
        )
        return {
            **{f: getattr(movement, f) for f in (
                "id", "cash_shift_id", "type", "amount", "reason", "payment_id", "refund_id",
                "created_at",
            )},
            "user": {"id": current_user.id, "name": current_user.name},
        }

    return idempotency.run(run, payload=request)


@router.get(
    "/handovers",
    response_model=list[HandoverResponse],
)
def list_handovers(
    limit: int = Query(30, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.CASH_SHIFTS_VIEW)),
    auth: AuthContext = Depends(get_auth_context),
):
    """The register's handovers, newest first, with their differences."""

    return cashier_views.handovers(
        db, CashService.handovers(db, current_user.restaurant_id, limit), _viewer(auth)
    )
