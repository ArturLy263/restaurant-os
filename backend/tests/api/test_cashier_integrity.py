"""Stage 3.1 cashier financial integrity, through the API: refunds only on
settled bills, the two-party handover, mandatory idempotency keys, blind
cash counts, splitting adjusted bills by items, business-date rules,
typed reports, and the database refusing writes into closed periods."""

import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models.bill import BillAdjustment, Refund
from app.db.models.cash import CashMovement, CashShiftHandover
from app.db.models.operational_event import OperationalEvent
from app.main import app
from tests.api.test_cashier import _second_table, open_till, till  # noqa: F401 - fixtures

# Till opens business days and shifts itself.
CASH_REGISTER_CLOSED = True


def _refunds(till):
    return till.db.scalars(select(Refund)).all()


# --- Part 1: refunds only on settled bills ---


def test_a_partly_paid_bill_is_not_refunded_and_stays_as_it_was(open_till):
    till = open_till
    order, _, bill = till.billed_order(till.burger, till.salad)  # 19.80
    payment = till.ok(till.pay(bill["id"], "10.00", tip_amount="1.00"), 201)

    for amount, tip in (("10.00", "0"), ("1.00", "0"), ("0", "1.00")):
        error = till.error(till.refund(payment["id"], amount, tip), 409, "BILL_NOT_SETTLED")
        assert "open" in error["message"]

    detail = till.bill(bill["id"])
    assert (detail["status"], detail["paid_total"], detail["amount_due"], detail["refunded_total"]) == (
        "OPEN", "10.00", "9.80", "0.00",
    )
    assert till.ok(till.call("GET", f"/api/v1/orders/{order['id']}", till.manager))["status"] == "SENT"
    assert _refunds(till) == []
    assert till.db.scalar(select(CashMovement).where(CashMovement.type == "CASH_REFUND")) is None
    # Settling the bill makes the payment refundable.
    till.ok(till.pay(bill["id"], "9.80", "CARD"), 201)
    till.ok(till.refund(payment["id"], "10.00"), 201)


def test_partial_and_full_refunds_of_a_settled_bill(open_till):
    till = open_till
    order, _, bill = till.billed_order()
    payment = till.ok(till.pay(bill["id"], "12.50", tip_amount="2.00"), 201)

    partial = till.ok(till.refund(payment["id"], "2.50"), 201)
    assert (partial["amount"], partial["tip_amount"]) == ("2.50", "0.00")
    till.ok(till.refund(payment["id"], "10.00", "2.00"), 201)  # the rest, tip included
    till.error(till.refund(payment["id"], "0.01"), 409, "REFUND_EXCEEDS_REFUNDABLE")

    detail = till.bill(bill["id"])
    # A refund never reopens the bill or the order.
    assert (detail["status"], detail["amount_due"], detail["refunded_total"], detail["refunded_tips_total"]) == (
        "PAID", "0.00", "12.50", "2.00",
    )
    assert till.ok(till.call("GET", f"/api/v1/orders/{order['id']}", till.manager))["status"] == "CLOSED"
    report = till.day_report(till.day["id"])
    assert (report["sales"]["total"], report["refunds"]["total"], report["revenue"]["total"]) == ("12.50", "12.50", "0.00")
    assert (report["tips"]["total"], report["refunded_tips"]["total"], report["net_tips"]["total"]) == ("2.00", "2.00", "0.00")


def test_a_refunded_tip_reduces_tips_and_never_revenue(open_till):
    till = open_till
    _, _, bill = till.billed_order()
    payment = till.ok(till.pay(bill["id"], "12.50", "CARD", tip_amount="3.00"), 201)

    till.ok(till.refund(payment["id"], "0", "3.00", reason="Tip typed twice", card_confirmed=True), 201)

    report = till.day_report(till.day["id"])
    assert report["revenue"] == {"CASH": "0.00", "CARD": "12.50", "total": "12.50"}
    assert report["net_tips"] == {"CASH": "0.00", "CARD": "0.00", "total": "0.00"}
    assert report["refunds"]["total"] == "0.00"


# --- Part 2: two-party handover ---


def _pending_handover(till, counted="112.50"):
    """Cashier 1 took a 12.50 cash payment and handed over (step 1)."""

    _, _, bill = till.billed_order()
    till.ok(till.pay(bill["id"], "12.50"), 201)
    closed = till.ok(till.call("POST", f"/api/v1/cash/shifts/{till.shift['id']}/handover", till.cashier,
                               {"counted_amount": counted}))
    return closed


def test_the_outgoing_cashier_cannot_confirm_their_own_handover(open_till):
    till = open_till
    _pending_handover(till)

    till.error(till.open_shift("112.50", user=till.cashier), 409, "HANDOVER_SAME_USER")
    assert till.register()["shift"] is None
    assert till.db.scalar(select(CashShiftHandover)) is None
    incoming = till.ok(till.open_shift("112.50", user=till.cashier2), 201)
    assert incoming["handover"]["self_confirmed"] is False


def test_a_manager_may_confirm_their_own_handover_and_it_shows(open_till):
    till = open_till
    till.ok(till.close_shift(till.shift["id"], "100.00", user=till.manager))

    incoming = till.ok(till.open_shift("100.00", user=till.manager), 201)

    handover = incoming["handover"]
    assert handover["outgoing_user"]["id"] == handover["incoming_user"]["id"] == str(till.manager.id)
    assert handover["self_confirmed"] is True
    event = till.db.scalar(select(OperationalEvent).where(OperationalEvent.event_type == "cash_shift.handover_recorded"))
    assert event.event_metadata["self_confirmed"] is True
    assert till.day_report(till.day["id"])["handovers"][0]["self_confirmed"] is True


@pytest.mark.parametrize(
    ("received", "difference"),
    [("150.00", "0.00"), ("130.00", "-20.00"), ("165.00", "15.00")],
    ids=["exact", "lower", "higher"],
)
def test_two_party_handover_of_150(open_till, received, difference):
    """The outgoing cashier counts 150.00, the incoming one counts what
    they physically received."""

    till = open_till
    path = f"/api/v1/cash/shifts/{till.shift['id']}/movements"
    till.ok(till.call("POST", path, json={"type": "CASH_IN", "amount": "50.00", "reason": "Float"}), 201)
    till.ok(till.call("POST", f"/api/v1/cash/shifts/{till.shift['id']}/handover", till.cashier, {"counted_amount": "150.00"}))

    if difference != "0.00":
        error = till.error(till.open_shift(received, user=till.cashier2), 422, "HANDOVER_REASON_REQUIRED")
        # Blind: neither count is revealed by the refusal.
        assert "150" not in error["message"] and received not in error["message"]
        assert till.register()["shift"] is None
    incoming = till.ok(till.open_shift(received, "Counted twice" if difference != "0.00" else None, user=till.cashier2), 201)

    handover = incoming["handover"]
    assert (handover["handed_over_amount"], handover["received_amount"], handover["difference"]) == ("150.00", received, difference)
    assert (handover["reason"] is None) == (difference == "0.00")
    assert (handover["outgoing_user"]["id"], handover["incoming_user"]["id"]) == (str(till.cashier.id), str(till.cashier2.id))
    assert (handover["from_shift_id"], handover["to_shift_id"], handover["business_day_id"]) == (
        till.shift["id"], incoming["id"], till.day["id"],
    )
    assert handover["created_at"]
    # The incoming shift opens with what was received; no movement.
    view = till.shift_view(incoming["id"])
    assert (view["opening_amount"], view["expected_cash"], view["movements"]) == (received, received, [])
    report = till.day_report(till.day["id"])
    assert report["handover_difference_total"] == difference
    assert report["revenue"]["total"] == "0.00"
    assert {m.type for m in till.db.scalars(select(CashMovement)).all()} == {"CASH_IN"}
    # The outgoing shift's closing is untouched.
    assert till.shift_view(till.shift["id"])["closing"]["counted_amount"] == "150.00"


def test_while_a_handover_is_pending_no_money_moves_and_nothing_is_recorded(open_till):
    till = open_till
    _, _, earlier = till.billed_order(table=_second_table(till))
    earlier_payment = till.ok(till.pay(earlier["id"], "12.50"), 201)  # settles it
    _pending_handover(till, counted="125.00")
    _, _, bill = till.billed_order()

    till.error(till.pay(bill["id"], "12.50", user=till.cashier2), 409, "NO_OPEN_CASH_SHIFT")
    till.error(till.refund(earlier_payment["id"], "1.00"), 409, "NO_OPEN_CASH_SHIFT")
    till.error(till.call("POST", f"/api/v1/cash/shifts/{till.shift['id']}/movements",
                         json={"type": "CASH_IN", "amount": "1.00", "reason": "x"}), 409, "CASH_SHIFT_NOT_OPEN")
    assert till.bill(bill["id"])["payments"] == []
    assert _refunds(till) == []
    pending = till.register(till.cashier2)["previous_closing"]
    assert pending["handover_pending"] is True

    # The pending period ends the moment the incoming cashier confirms.
    incoming = till.ok(till.open_shift("125.00", user=till.cashier2), 201)
    paid = till.ok(till.pay(bill["id"], "12.50", user=till.cashier2), 201)
    assert paid["cash_shift_id"] == incoming["id"]
    assert till.register(till.cashier2)["previous_closing"]["handover_pending"] is False


# --- Part 3: mandatory idempotency keys ---


def _money_requests(till, bill, payment):
    shift = till.shift["id"]
    return [
        ("POST", f"/api/v1/bills/{bill['id']}/payments", till.cashier, {"amount": "1.00", "method": "CASH"}),
        ("POST", f"/api/v1/payments/{payment['id']}/refunds", till.manager, {"amount": "1.00", "reason": "x"}),
        ("POST", f"/api/v1/cash/shifts/{shift}/movements", till.cashier, {"type": "CASH_IN", "amount": "1.00", "reason": "x"}),
        ("POST", f"/api/v1/cash/shifts/{shift}/opening-corrections", till.manager, {"opening_amount": "90.00", "reason": "x"}),
        ("POST", f"/api/v1/cash/shifts/{shift}/close", till.cashier, {"counted_amount": "100.00", "explanation": "x"}),
        ("POST", f"/api/v1/cash/shifts/{shift}/handover", till.cashier, {"counted_amount": "100.00", "closing_explanation": "x"}),
        ("POST", "/api/v1/cash/shifts", till.cashier2, {"opening_amount": "100.00"}),
    ]


def test_money_and_shift_operations_without_a_key_are_refused_before_running(open_till):
    till = open_till
    _, _, paid_bill = till.billed_order(table=_second_table(till))
    payment = till.ok(till.pay(paid_bill["id"], "12.50"), 201)
    _, _, bill = till.billed_order()
    before = till.shift_view(till.shift["id"])

    for method, path, user, body in _money_requests(till, bill, payment):
        error = till.error(till.call(method, path, user, body, key=None), 428, "IDEMPOTENCY_KEY_REQUIRED")
        assert "Idempotency-Key" in error["message"]
        till.error(till.call(method, path, user, body, key="has space"), 400, "IDEMPOTENCY_KEY_INVALID")

    after = till.shift_view(till.shift["id"])
    assert after == before
    assert till.bill(bill["id"])["payments"] == [] and _refunds(till) == []
    # Business days keep the key optional.
    till.error(till.call("POST", f"/api/v1/business-days/{till.day['id']}/close", till.manager, key=None),
               409, "CASH_SHIFT_STILL_OPEN")


@pytest.mark.parametrize("operation", ["refund", "movement", "close", "handover", "open"])
def test_a_retried_operation_replays_and_a_reused_key_is_refused(open_till, operation):
    till = open_till
    _, _, bill = till.billed_order()
    payment = till.ok(till.pay(bill["id"], "12.50"), 201)
    shift = till.shift["id"]
    if operation == "open":
        till.ok(till.close_shift(shift, "112.50"))
    method, path, user, body, changed = {
        "refund": ("POST", f"/api/v1/payments/{payment['id']}/refunds", till.manager,
                   {"amount": "2.00", "reason": "x"}, {"amount": "3.00", "reason": "x"}),
        "movement": ("POST", f"/api/v1/cash/shifts/{shift}/movements", till.cashier,
                     {"type": "CASH_OUT", "amount": "5.00", "reason": "Drop"}, {"type": "CASH_OUT", "amount": "6.00", "reason": "Drop"}),
        "close": ("POST", f"/api/v1/cash/shifts/{shift}/close", till.cashier,
                  {"counted_amount": "112.50"}, {"counted_amount": "112.00", "explanation": "x"}),
        "handover": ("POST", f"/api/v1/cash/shifts/{shift}/handover", till.cashier,
                     {"counted_amount": "112.50"}, {"counted_amount": "112.00", "closing_explanation": "x"}),
        "open": ("POST", "/api/v1/cash/shifts", till.cashier2,
                 {"opening_amount": "112.50"}, {"opening_amount": "110.00", "handover_reason": "x"}),
    }[operation]

    first = till.call(method, path, user, body, key=f"{operation}-1")
    again = till.call(method, path, user, body, key=f"{operation}-1")
    other = till.call(method, path, user, changed, key=f"{operation}-1")

    assert first.status_code in (200, 201), first.text
    assert (again.status_code, again.json()) == (first.status_code, first.json())
    assert again.headers["Idempotent-Replayed"] == "true"
    till.error(other, 422, "IDEMPOTENCY_KEY_REUSED")
    counts = {
        "refund": lambda: len(_refunds(till)),
        "movement": lambda: len(till.db.scalars(select(CashMovement).where(CashMovement.type == "CASH_OUT")).all()),
        "close": lambda: int(till.shift_view(shift)["status"] == "CLOSED"),
        "handover": lambda: int(till.shift_view(shift)["status"] == "CLOSED"),
        "open": lambda: len(till.db.scalars(select(CashShiftHandover)).all()),
    }
    assert counts[operation]() == 1


# --- Part 4: blind cash count ---


def _values(document):
    """Every (key, value) pair in a JSON document, recursively."""

    if isinstance(document, dict):
        for key, value in document.items():
            yield key, value
            yield from _values(value)
    elif isinstance(document, list):
        for value in document:
            yield from _values(value)


# Fields a cashier always gets as null (a closing's difference too, below).
HIDDEN_FROM_CASHIERS = {"expected", "expected_amount", "expected_cash", "previous_expected_amount", "drawer"}


def _assert_blind(document, *secrets):
    """No expected-cash field has a value, and none of `secrets` (amounts
    that would reveal it) appears anywhere in the document."""

    text = json.dumps(document)
    for secret in secrets:
        assert secret not in text, (secret, text)
    for key, value in _values(document):
        if key in HIDDEN_FROM_CASHIERS:
            assert value is None, (key, value)
        if key == "closing" and value is not None:
            assert value["difference"] is None, value


def test_cashiers_count_blind_and_managers_see_the_expected_cash(open_till):
    till = open_till
    # Expected: 100 + 37.85 cash sale + 0.40 tip = 138.25 (a figure that
    # appears nowhere else).
    _, _, bill = till.billed_order(till.salad, till.salad, till.salad, till.salad, till.salad)  # 36.50
    _, _, bill2 = till.billed_order(till.salad, table=_second_table(till))  # 7.30
    till.ok(till.pay(bill["id"], "36.50", tip_amount="0.40"), 201)
    till.ok(till.pay(bill2["id"], "1.35"), 201)
    shift_path = f"/api/v1/cash/shifts/{till.shift['id']}"

    for user in (till.cashier, till.cashier2):
        _assert_blind(till.ok(till.call("GET", shift_path, user)), "138.25")
        _assert_blind(till.register(user), "138.25")
    shift = till.ok(till.call("GET", shift_path))
    assert (shift["expected_cash"], shift["drawer"], shift["expected_visible"]) == (None, None, False)
    managed = till.shift_view(till.shift["id"])
    assert (managed["expected_cash"], managed["drawer"]["expected"], managed["expected_visible"]) == ("138.25", "138.25", True)
    # A refused cash-out doesn't reveal the drawer either.
    error = till.error(till.call("POST", f"{shift_path}/movements", json={"type": "CASH_OUT", "amount": "500.00", "reason": "x"}),
                       409, "INSUFFICIENT_CASH")
    assert "138.25" not in error["message"]

    # The outgoing cashier counts (blind) and sees their own count only.
    closed = till.ok(till.call("POST", f"{shift_path}/handover", till.cashier, {"counted_amount": "138.00", "closing_explanation": "short"}))
    _assert_blind(closed, "138.25", "-0.25")
    assert closed["closing"]["counted_amount"] == "138.00"
    # The incoming cashier sees neither the expected cash nor the count
    # they are about to check.
    for document in (till.ok(till.call("GET", shift_path, till.cashier2)), till.register(till.cashier2)):
        _assert_blind(document, "138.25", "138.00", "-0.25")
    assert till.register(till.cashier2)["previous_closing"]["counted_amount"] is None
    assert till.register(till.cashier)["previous_closing"]["counted_amount"] == "138.00"
    manager_register = till.register(till.manager)["previous_closing"]
    assert (manager_register["expected_amount"], manager_register["counted_amount"]) == ("138.25", "138.00")

    # After the incoming count, the counts are no secret; expected cash is.
    incoming = till.ok(till.open_shift("138.00", user=till.cashier2), 201)
    _assert_blind(incoming, "138.25")
    assert till.ok(till.call("GET", shift_path, till.cashier2))["closing"]["counted_amount"] == "138.00"
    _assert_blind(till.ok(till.call("GET", "/api/v1/cash/handovers", till.cashier2)), "138.25")
    assert till.ok(till.call("GET", "/api/v1/cash/handovers", till.manager))[0]["previous_expected_amount"] == "138.25"
    # Reports stay management only.
    till.error(till.call("GET", f"/api/v1/business-days/{till.day['id']}/report", till.cashier2), 403, "PERMISSION_DENIED")


# --- Part 5: the database refuses writes into closed periods ---


def test_the_database_refuses_ledger_rows_in_a_closed_shift(open_till, db_session):
    till = open_till
    till.ok(till.close_shift(till.shift["id"], "100.00"))
    db_session.commit()  # keep the closed shift when the insert below fails

    movement = CashMovement(
        restaurant_id=till.restaurant.restaurant.id,
        cash_shift_id=till.shift["id"],
        type="CASH_IN",
        amount=Decimal("5.00"),
        reason="after the close",
        user_id=till.cashier.id,
        created_at=datetime.utcnow(),
    )
    db_session.add(movement)
    with pytest.raises(IntegrityError, match="open cash shift of an open business day"):
        db_session.flush()
    db_session.rollback()
    assert till.shift_view(till.shift["id"])["movements"] == []


# --- Part 6: paying adjusted bills by items ---


def _add_item_to_billed_order(till, order, product):
    added = till.ok(till.call("POST", f"/api/v1/orders/{order['id']}/items", till.manager,
                              {"product_id": str(product.id), "quantity": 1}), 201)
    till.ok(till.call("POST", f"/api/v1/orders/{order['id']}/send", till.manager))
    return added


def test_items_of_an_adjusted_bill_are_paid_by_items_to_the_cent(open_till):
    till = open_till
    order, (burger, salad), bill = till.billed_order(till.burger, till.salad)  # 19.80
    added = _add_item_to_billed_order(till, order, till.salad)  # +7.30 ITEM_ADDITION
    till.ok(till.call("POST", f"/api/v1/bills/{bill['id']}/items/{burger['id']}/cancel", till.manager,
                      {"reason": "Cold"}))  # -12.50 ITEM_CANCELLATION
    assert till.bill(bill["id"])["amount_due"] == "14.60"

    till.error(till.pay(bill["id"], "12.50", order_item_ids=[burger["id"]]), 409, "ORDER_ITEM_NOT_PAYABLE")
    till.error(till.pay(bill["id"], "14.60", order_item_ids=[salad["id"]]), 422, "PAYMENT_AMOUNT_MISMATCH")
    first = till.ok(till.pay(bill["id"], "7.30", order_item_ids=[salad["id"]]), 201)
    till.error(till.pay(bill["id"], "7.30", "CARD", order_item_ids=[salad["id"]]), 409, "ORDER_ITEM_ALREADY_PAID")
    second = till.ok(till.pay(bill["id"], "7.30", "CARD", order_item_ids=[added["id"]]), 201)

    detail = till.bill(bill["id"])
    assert (detail["status"], detail["paid_total"], detail["amount_due"]) == ("PAID", "14.60", "0.00")
    paid_by = {line["order_item_id"]: line["paid_by_payment_id"] for line in detail["lines"]}
    assert paid_by == {burger["id"]: None, salad["id"]: first["id"], added["id"]: second["id"]}


def test_items_and_amounts_mix_without_overpaying(open_till):
    till = open_till
    _, (burger, salad), bill = till.billed_order(till.burger, till.salad)  # 19.80
    till.ok(till.pay(bill["id"], "13.00"), 201)  # 6.80 due, nothing allocated

    # The salad (7.30) would pay more than is due.
    till.error(till.pay(bill["id"], "7.30", order_item_ids=[salad["id"]]), 409, "PAYMENT_EXCEEDS_AMOUNT_DUE")
    till.ok(till.pay(bill["id"], "6.80"), 201)
    assert till.bill(bill["id"])["status"] == "PAID"


def test_an_adjustment_of_no_single_item_blocks_paying_by_items(open_till):
    till = open_till
    _, (burger, salad), bill = till.billed_order(till.burger, till.salad)  # 19.80
    # A bill-level adjustment (no API writes one yet; e.g. a discount).
    till.db.add(BillAdjustment(
        restaurant_id=till.restaurant.restaurant.id, bill_id=bill["id"], order_item_id=None,
        type="DISCOUNT", amount=Decimal("-1.99"), reason="Loyalty", user_id=till.manager.id,
    ))
    till.db.commit()  # survives the refused request's rollback

    till.error(till.pay(bill["id"], "12.50", order_item_ids=[burger["id"]]), 409, "ITEM_SPLIT_UNAVAILABLE")
    assert till.bill(bill["id"])["payments"] == []
    # Paying by amount still settles it exactly.
    till.ok(till.pay(bill["id"], "17.81"), 201)
    assert till.bill(bill["id"])["status"] == "PAID"


# --- Part 7: business dates ---


def test_a_cashier_opens_today_or_yesterday_only(till):
    yesterday = (till.today - timedelta(days=1)).isoformat()
    tomorrow = (till.today + timedelta(days=1)).isoformat()
    two_days_ago = (till.today - timedelta(days=2)).isoformat()
    path = "/api/v1/business-days"

    till.error(till.call("POST", path, json={"business_date": tomorrow}), 422, "INVALID_BUSINESS_DATE")
    till.error(till.call("POST", path, json={"business_date": two_days_ago}), 403, "PERMISSION_DENIED")
    # Late night: the day opened after midnight trades for yesterday.
    day = till.ok(till.call("POST", path, json={"business_date": yesterday}), 201)
    assert day["business_date"] == yesterday


def test_a_manager_may_backdate_and_dates_only_move_forward(till):
    three_days_ago = (till.today - timedelta(days=3)).isoformat()
    four_days_ago = (till.today - timedelta(days=4)).isoformat()
    path = "/api/v1/business-days"

    day = till.ok(till.call("POST", path, till.manager, {"business_date": three_days_ago}), 201)
    event = till.db.scalar(select(OperationalEvent).where(OperationalEvent.event_type == "business_day.opened"))
    assert (event.event_metadata["backdated"], event.event_metadata["restaurant_date"]) == (True, till.today.isoformat())
    till.ok(till.call("POST", f"{path}/{day['id']}/close", till.manager))

    till.error(till.call("POST", path, till.manager, {"business_date": four_days_ago}), 422, "INVALID_BUSINESS_DATE")
    till.error(till.call("POST", path, till.manager, {"business_date": three_days_ago}), 409, "BUSINESS_DAY_EXISTS")
    today = till.ok(till.call("POST", path, json={}), 201)
    assert today["business_date"] == till.today.isoformat()


# --- Parts 8 and 9: typed, frozen reports ---


def test_reports_and_drawers_are_typed_in_the_api_schema():
    schemas = app.openapi()["components"]["schemas"]

    report = schemas["BusinessDayReportResponse"]["properties"]["report"]
    assert report == {"$ref": "#/components/schemas/BusinessDayReport"}
    day = schemas["BusinessDayReport"]["properties"]
    assert day["shifts"]["items"] == {"$ref": "#/components/schemas/DayShiftReport"}
    assert day["handovers"]["items"] == {"$ref": "#/components/schemas/HandoverReport"}
    assert day["revenue"] == {"$ref": "#/components/schemas/MethodTotals"}
    drawer = schemas["CashShiftResponse"]["properties"]["drawer"]
    assert {"$ref": "#/components/schemas/DrawerReport"} in drawer["anyOf"]
    money = schemas["DrawerReport"]["properties"]["expected"]
    assert money["type"] == "string"


def test_a_closed_days_report_survives_later_financial_operations(till):
    day1 = till.open_day(till.DAY_1)
    shift1 = till.ok(till.open_shift("100.00"), 201)
    _, _, bill = till.billed_order()
    payment = till.ok(till.pay(bill["id"], "12.50", tip_amount="1.00"), 201)
    till.ok(till.close_shift(shift1["id"], "113.50"))
    till.ok(till.call("POST", f"/api/v1/business-days/{day1['id']}/close", till.manager))
    frozen = till.day_report(day1["id"])

    till.open_day(till.DAY_2)
    shift2 = till.ok(till.open_shift("110.00", "Counted 110", user=till.cashier2), 201)
    till.ok(till.refund(payment["id"], "12.50", "1.00"), 201)
    till.ok(till.call("POST", f"/api/v1/cash/shifts/{shift2['id']}/movements", till.cashier2,
                      {"type": "CASH_IN", "amount": "20.00", "reason": "Change"}), 201)
    _, _, bill2 = till.billed_order()
    till.ok(till.pay(bill2["id"], "12.50", user=till.cashier2), 201)

    assert till.day_report(day1["id"]) == frozen
    assert (frozen["revenue"]["total"], frozen["net_tips"]["total"], frozen["handovers"]) == ("12.50", "1.00", [])
    assert frozen["shifts"][0]["drawer"]["expected"] == "113.50"
