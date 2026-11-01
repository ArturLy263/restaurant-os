"""The cashier module through the API: business days, the shared register's
shifts and handovers, payments (cash, manual card, tips, splits), refunds
and reports (docs/stage-3-cashier-plan.md)."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.models.bill import Payment
from app.db.models.cash import CashMovement, CashShiftHandover
from app.db.models.operational_event import OperationalEvent
from app.db.models.outbox_event import OutboxEvent
from app.db.models.product import Product
from tests.factories import auth_headers, create_restaurant, create_user

# These tests open business days and shifts themselves.
CASH_REGISTER_CLOSED = True

DAY_1 = "2026-10-31"
DAY_2 = "2026-11-01"


class Till:
    """Drives the cashier API as several employees of one restaurant."""

    def __init__(self, client, db, restaurant):
        self.client = client
        self.db = db
        self.restaurant = restaurant
        self.manager = restaurant.user  # ADMIN: every permission
        self.cashier = create_user(db, restaurant, "CASHIER")
        self.cashier2 = create_user(db, restaurant, "CASHIER")
        self.waiter = create_user(db, restaurant, "WAITER")
        self.burger = restaurant.product  # 12.50
        self.salad = Product(
            category_id=restaurant.category.id,
            name="Salad",
            price=Decimal("7.30"),
            station_id=restaurant.station.id,
        )
        db.add(self.salad)
        db.flush()

    def call(self, method, path, user=None, json=None, key=None):
        headers = auth_headers(user or self.cashier)
        if key:
            headers["Idempotency-Key"] = key
        return self.client.request(method, path, json=json, headers=headers)

    def ok(self, response, status=200):
        assert response.status_code == status, response.text
        return response.json()

    def error(self, response, status, code):
        assert response.status_code == status, response.text
        assert response.json()["error"]["code"] == code, response.text
        return response.json()["error"]

    # --- set-up ---

    def open_day(self, business_date=DAY_2, user=None):
        return self.ok(self.call("POST", "/api/v1/business-days", user, {"business_date": business_date}), 201)

    def open_shift(self, amount="100.00", reason=None, user=None):
        body = {"opening_amount": amount}
        if reason is not None:
            body["handover_reason"] = reason
        return self.call("POST", "/api/v1/cash/shifts", user, body)

    def close_shift(self, shift_id, counted, explanation=None, user=None):
        body = {"counted_amount": counted}
        if explanation is not None:
            body["explanation"] = explanation
        return self.call("POST", f"/api/v1/cash/shifts/{shift_id}/close", user, body)

    def register(self, user=None):
        return self.ok(self.call("GET", "/api/v1/cash/register", user))

    def billed_order(self, *products, table=None):
        """An order of `products` (default one burger), sent and billed."""

        products = products or (self.burger,)
        headers = auth_headers(self.manager)
        order = self.client.post(
            "/api/v1/orders",
            json={"table_id": str((table or self.restaurant.table).id), "guest_count": 2},
            headers=headers,
        ).json()
        items = [
            self.client.post(
                f"/api/v1/orders/{order['id']}/items",
                json={"product_id": str(product.id), "quantity": 1},
                headers=headers,
            ).json()
            for product in products
        ]
        assert self.client.post(f"/api/v1/orders/{order['id']}/send", headers=headers).status_code == 200
        bill = self.ok(self.call("POST", f"/api/v1/bills/order/{order['id']}"), 201)
        return order, items, bill

    def pay(self, bill_id, amount, method="CASH", user=None, key=None, **extra):
        body = {"amount": amount, "method": method, **extra}
        if method == "CARD":
            body.setdefault("card_confirmed", True)
        return self.call("POST", f"/api/v1/bills/{bill_id}/payments", user, body, key)

    def refund(self, payment_id, amount="0", tip="0", reason="Guest complaint", user=None, **extra):
        body = {"amount": amount, "tip_amount": tip, "reason": reason, **extra}
        return self.call("POST", f"/api/v1/payments/{payment_id}/refunds", user or self.manager, body)

    def bill(self, bill_id):
        return self.ok(self.call("GET", f"/api/v1/bills/{bill_id}"))

    def day_report(self, day_id):
        return self.ok(self.call("GET", f"/api/v1/business-days/{day_id}/report", self.manager))["report"]


@pytest.fixture
def till(db_client, db_session, restaurant):
    return Till(db_client, db_session, restaurant)


@pytest.fixture
def open_till(till):
    """A business day with an open shift (cashier 1, opened with 100.00)."""

    till.day = till.open_day()
    till.shift = till.ok(till.open_shift("100.00"), 201)
    return till


# --- Business days and shifts ---


def test_orders_need_no_business_day_but_payments_need_a_day_and_a_shift(till):
    order, _, bill = till.billed_order()  # decision F: no day needed

    till.error(till.pay(bill["id"], "12.50"), 409, "NO_OPEN_BUSINESS_DAY")
    till.error(till.open_shift(), 409, "NO_OPEN_BUSINESS_DAY")
    till.open_day()
    till.error(till.pay(bill["id"], "12.50"), 409, "NO_OPEN_CASH_SHIFT")
    till.ok(till.open_shift(), 201)
    till.ok(till.pay(bill["id"], "12.50"), 201)


def test_one_open_day_and_one_open_shift_at_a_time(till):
    day = till.open_day(DAY_2)
    till.error(till.call("POST", "/api/v1/business-days", json={"business_date": "2026-11-02"}), 409, "BUSINESS_DAY_ALREADY_OPEN")
    shift = till.ok(till.open_shift(), 201)
    till.error(till.open_shift(user=till.cashier2), 409, "CASH_SHIFT_ALREADY_OPEN")

    till.ok(till.close_shift(shift["id"], "100.00"))
    till.ok(till.call("POST", f"/api/v1/business-days/{day['id']}/close", till.manager))
    # A closed day is never reopened.
    till.error(till.call("POST", "/api/v1/business-days", json={"business_date": DAY_2}), 409, "BUSINESS_DAY_EXISTS")


def test_several_cashiers_work_one_shift_and_each_is_recorded(open_till):
    till = open_till
    _, _, bill = till.billed_order(till.burger, till.salad)

    first = till.ok(till.pay(bill["id"], "10.00", user=till.cashier), 201)
    second = till.ok(till.pay(bill["id"], "9.80", "CARD", user=till.cashier2), 201)

    assert first["cash_shift_id"] == second["cash_shift_id"] == till.shift["id"]
    assert (first["user_id"], second["user_id"]) == (str(till.cashier.id), str(till.cashier2.id))
    assert till.shift["opened_by"]["id"] == str(till.cashier.id)


def test_closing_a_shift_leaves_orders_tables_and_bills_working(open_till):
    till = open_till
    order, _, bill = till.billed_order()
    till.ok(till.pay(bill["id"], "5.00"), 201)
    till.ok(till.close_shift(till.shift["id"], "105.00"))

    # The partly paid bill and its order and table are untouched.
    assert till.bill(bill["id"])["status"] == "OPEN"
    assert till.ok(till.call("GET", f"/api/v1/orders/{order['id']}", till.manager))["status"] == "SENT"
    assert till.call("GET", "/api/v1/tables", till.manager).json()[0]["status"] == "OCCUPIED"
    # No shift: no payment; the next shift takes the rest.
    till.error(till.pay(bill["id"], "7.50"), 409, "NO_OPEN_CASH_SHIFT")
    till.ok(till.open_shift("105.00", user=till.cashier2), 201)
    till.ok(till.pay(bill["id"], "7.50", user=till.cashier2), 201)
    assert till.bill(bill["id"])["status"] == "PAID"


def test_closing_needs_an_explanation_for_a_difference(open_till):
    till = open_till
    _, _, bill = till.billed_order()
    till.ok(till.pay(bill["id"], "12.50"), 201)

    till.error(till.close_shift(till.shift["id"], "110.00"), 422, "CLOSING_EXPLANATION_REQUIRED")
    closed = till.ok(till.close_shift(till.shift["id"], "110.00", "Short 2.50, unclear"))

    assert closed["closing"] == {
        "expected_amount": "112.50",
        "counted_amount": "110.00",
        "difference": "-2.50",
        "explanation": "Short 2.50, unclear",
        "user": {"id": str(till.cashier.id), "name": till.cashier.name},
        "created_at": closed["closing"]["created_at"],
    }
    till.error(till.close_shift(till.shift["id"], "110.00", "again"), 409, "CASH_SHIFT_NOT_OPEN")


# --- Opening balance corrections ---


def test_manager_corrects_a_mistyped_opening_amount(open_till):
    till = open_till
    shift_id = till.shift["id"]
    path = f"/api/v1/cash/shifts/{shift_id}/opening-corrections"

    till.error(till.call("POST", path, till.cashier, {"opening_amount": "150.00", "reason": "typo"}), 403, "PERMISSION_DENIED")
    till.error(till.call("POST", path, till.manager, {"opening_amount": "150.00"}), 422, "VALIDATION_ERROR")
    first = till.ok(till.call("POST", path, till.manager, {"opening_amount": "150.00", "reason": "Typed 100, drawer had 150"}), 201)
    second = till.ok(till.call("POST", path, till.manager, {"opening_amount": "140.00", "reason": "Recount"}), 201)
    till.error(till.call("POST", path, till.manager, {"opening_amount": "140.00", "reason": "x"}), 409, "OPENING_AMOUNT_UNCHANGED")

    shift = till.ok(till.call("GET", f"/api/v1/cash/shifts/{shift_id}"))
    assert shift["status"] == "OPEN"
    assert (shift["opening_amount"], shift["effective_opening_amount"], shift["expected_cash"]) == ("100.00", "140.00", "140.00")
    assert [(c["previous_amount"], c["new_amount"]) for c in shift["corrections"]] == [("100.00", "150.00"), ("150.00", "140.00")]
    assert first["user"]["id"] == second["user"]["id"] == str(till.manager.id)
    # Not a deposit: no cash movement, no revenue.
    assert shift["movements"] == []
    assert till.db.scalar(select(OperationalEvent.event_metadata).where(
        OperationalEvent.event_type == "cash_opening_balance.corrected"
    ).order_by(OperationalEvent.created_at))["original_amount"] == "100.00"


def test_corrections_need_an_open_shift(open_till):
    till = open_till
    till.ok(till.close_shift(till.shift["id"], "100.00"))
    till.error(
        till.call("POST", f"/api/v1/cash/shifts/{till.shift['id']}/opening-corrections", till.manager,
                  {"opening_amount": "150.00", "reason": "late"}),
        409, "CASH_SHIFT_NOT_OPEN",
    )


# --- Cash movements ---


def test_cash_in_and_out_change_expected_cash_and_never_overdraw(open_till):
    till = open_till
    path = f"/api/v1/cash/shifts/{till.shift['id']}/movements"
    till.ok(till.call("POST", path, json={"type": "CASH_IN", "amount": "20.00", "reason": "Change from safe"}), 201)
    till.ok(till.call("POST", path, json={"type": "CASH_OUT", "amount": "50.00", "reason": "Safe drop"}), 201)
    till.error(till.call("POST", path, json={"type": "CASH_OUT", "amount": "70.01", "reason": "x"}), 409, "INSUFFICIENT_CASH")
    till.error(till.call("POST", path, json={"type": "CASH_REFUND", "amount": "1.00", "reason": "x"}), 422, "VALIDATION_ERROR")
    till.error(till.call("POST", path, json={"type": "CASH_IN", "amount": 5, "reason": "x"}), 422, "VALIDATION_ERROR")
    till.error(till.call("POST", path, json={"type": "CASH_IN", "amount": "0", "reason": "x"}), 422, "VALIDATION_ERROR")
    till.error(till.call("POST", path, till.waiter, {"type": "CASH_IN", "amount": "1.00", "reason": "x"}), 403, "PERMISSION_DENIED")

    shift = till.ok(till.call("GET", f"/api/v1/cash/shifts/{till.shift['id']}"))
    assert shift["expected_cash"] == "70.00"
    assert [(m["type"], m["amount"], m["user"]["id"]) for m in shift["movements"]] == [
        ("CASH_IN", "20.00", str(till.cashier.id)),
        ("CASH_OUT", "50.00", str(till.cashier.id)),
    ]


# --- Payments ---


def test_partial_mixed_payments_settle_the_bill_and_close_the_order(open_till):
    till = open_till
    order, _, bill = till.billed_order(till.burger, till.salad)  # 19.80

    till.ok(till.pay(bill["id"], "10.00"), 201)
    card = till.ok(till.pay(bill["id"], "9.80", "CARD", external_reference="AUTH 483920"), 201)
    till.error(till.pay(bill["id"], "0.01"), 409, "BILL_NOT_OPEN")

    assert (card["confirmation_method"], card["external_reference"]) == ("MANUAL_CARD_TERMINAL", "AUTH 483920")
    detail = till.bill(bill["id"])
    assert (detail["status"], detail["paid_total"], detail["amount_due"]) == ("PAID", "19.80", "0.00")
    # Decision A: settling the bill closes the order and frees the table.
    assert till.ok(till.call("GET", f"/api/v1/orders/{order['id']}", till.manager))["status"] == "CLOSED"
    assert till.call("GET", "/api/v1/tables", till.manager).json()[0]["status"] == "AVAILABLE"


def test_a_card_payment_needs_the_cashiers_confirmation_and_no_card_data(open_till):
    till = open_till
    _, _, bill = till.billed_order()

    till.error(till.pay(bill["id"], "12.50", "CARD", card_confirmed=False), 422, "CARD_CONFIRMATION_REQUIRED")
    till.error(till.pay(bill["id"], "12.50", "CARD", external_reference="4111 1111 1111 1111"), 422, "VALIDATION_ERROR")
    till.error(till.pay(bill["id"], "12.50", "CASH", external_reference="REF1"), 422, "VALIDATION_ERROR")
    till.error(till.pay(bill["id"], "12.50", "VOUCHER"), 422, "VALIDATION_ERROR")
    assert till.bill(bill["id"])["payments"] == []


def test_tips_are_kept_apart_from_what_is_owed_and_from_revenue(open_till):
    till = open_till
    _, _, bill = till.billed_order(till.burger, till.salad)  # 19.80

    till.ok(till.pay(bill["id"], "10.00", tip_amount="1.50"), 201)
    till.ok(till.pay(bill["id"], "9.80", "CARD", tip_amount="2.00"), 201)
    till.error(till.pay(bill["id"], "1.00", tip_amount="1.00"), 409, "BILL_NOT_OPEN")

    detail = till.bill(bill["id"])
    assert (detail["paid_total"], detail["amount_due"], detail["tips_total"]) == ("19.80", "0.00", "3.50")
    report = till.day_report(till.day["id"])
    assert report["revenue"] == {"CASH": "10.00", "CARD": "9.80", "total": "19.80"}
    assert report["tips"] == {"CASH": "1.50", "CARD": "2.00", "total": "3.50"}
    # The cash tip went into the drawer, as its own line.
    drawer = till.ok(till.call("GET", f"/api/v1/cash/shifts/{till.shift['id']}"))["drawer"]
    assert (drawer["cash_sales"], drawer["cash_tips"], drawer["expected"]) == ("10.00", "1.50", "111.50")


def test_amounts_are_decimal_strings(open_till):
    till = open_till
    _, _, bill = till.billed_order()
    till.error(till.pay(bill["id"], "1.00", tip_amount=0.5), 422, "VALIDATION_ERROR")
    till.error(till.pay(bill["id"], "1.00", tip_amount="0.505"), 422, "VALIDATION_ERROR")
    # `amount` keeps accepting a JSON number for one release (decision D).
    payment = till.ok(till.pay(bill["id"], 1.25), 201)
    assert (payment["amount"], payment["tip_amount"]) == ("1.25", "0.00")


def test_a_payment_is_recorded_once_per_idempotency_key(open_till):
    till = open_till
    _, _, bill = till.billed_order()

    first = till.pay(bill["id"], "5.00", key="pay-1")
    again = till.pay(bill["id"], "5.00", key="pay-1")
    other = till.pay(bill["id"], "6.00", key="pay-1")

    assert (first.status_code, again.status_code) == (201, 201)
    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.json()["id"] == first.json()["id"]
    till.error(other, 422, "IDEMPOTENCY_KEY_REUSED")
    assert till.bill(bill["id"])["paid_total"] == "5.00"


# --- Splitting ---


def test_equal_split_allocates_leftover_cents_to_the_first_shares(open_till):
    till = open_till
    _, _, bill = till.billed_order()  # 12.50
    till.ok(till.pay(bill["id"], "2.50"), 201)  # 10.00 due

    split = till.ok(till.call("GET", f"/api/v1/bills/{bill['id']}/split?parts=3"))
    assert split["shares"] == ["3.34", "3.33", "3.33"]
    assert sum(Decimal(s) for s in split["shares"]) == Decimal(split["amount_due"]) == Decimal("10.00")
    for share, method in zip(split["shares"], ("CASH", "CARD", "CASH")):
        till.ok(till.pay(bill["id"], share, method), 201)
    assert till.bill(bill["id"])["status"] == "PAID"
    till.error(till.call("GET", f"/api/v1/bills/{bill['id']}/split?parts=2"), 409, "BILL_NOT_OPEN")
    till.error(till.call("GET", f"/api/v1/bills/{bill['id']}/split?parts=1"), 422, "VALIDATION_ERROR")


@pytest.mark.parametrize(
    ("due", "parts", "shares"),
    [
        ("0.05", 2, ["0.03", "0.02"]),
        ("10.00", 7, ["1.43", "1.43", "1.43", "1.43", "1.43", "1.43", "1.42"]),
        ("12.50", 2, ["6.25", "6.25"]),
    ],
)
def test_equal_split_is_exact_to_the_cent(open_till, due, parts, shares):
    till = open_till
    _, _, bill = till.billed_order()
    paid = Decimal("12.50") - Decimal(due)
    if paid:
        till.ok(till.pay(bill["id"], f"{paid:.2f}"), 201)
    split = till.ok(till.call("GET", f"/api/v1/bills/{bill['id']}/split?parts={parts}"))
    assert split["shares"] == shares
    assert sum(Decimal(s) for s in shares) == Decimal(due)


def test_split_by_items_pays_each_item_once(open_till):
    till = open_till
    _, (burger, salad), bill = till.billed_order(till.burger, till.salad)

    till.error(till.pay(bill["id"], "12.00", order_item_ids=[burger["id"]]), 422, "PAYMENT_AMOUNT_MISMATCH")
    payment = till.ok(till.pay(bill["id"], "12.50", order_item_ids=[burger["id"]]), 201)
    till.error(till.pay(bill["id"], "12.50", "CARD", order_item_ids=[burger["id"]]), 409, "ORDER_ITEM_ALREADY_PAID")
    till.ok(till.pay(bill["id"], "7.30", "CARD", order_item_ids=[salad["id"]]), 201)

    lines = {line["order_item_id"]: line["paid_by_payment_id"] for line in till.bill(bill["id"])["lines"]}
    assert lines[burger["id"]] == payment["id"]
    assert till.bill(bill["id"])["status"] == "PAID"


# --- Refunds ---


def test_refunds_keep_the_payment_and_split_bill_money_from_tips(open_till):
    till = open_till
    _, _, bill = till.billed_order()
    payment = till.ok(till.pay(bill["id"], "12.50", tip_amount="2.00"), 201)

    till.error(till.refund(payment["id"], "12.51"), 409, "REFUND_EXCEEDS_REFUNDABLE")
    till.error(till.refund(payment["id"], "0", "2.01"), 409, "REFUND_EXCEEDS_REFUNDABLE")
    till.error(till.refund(payment["id"], "0", "0"), 422, "VALIDATION_ERROR")
    till.error(till.refund(payment["id"], "1.00", user=till.cashier), 403, "PERMISSION_DENIED")
    till.error(till.refund(payment["id"], "1.00", reason=" "), 422, "VALIDATION_ERROR")
    till.ok(till.refund(payment["id"], "5.00"), 201)
    till.ok(till.refund(payment["id"], "0", "2.00", reason="Tip returned"), 201)
    till.error(till.refund(payment["id"], "7.51"), 409, "REFUND_EXCEEDS_REFUNDABLE")
    till.ok(till.refund(payment["id"], "7.50"), 201)

    detail = till.bill(bill["id"])
    assert detail["status"] == "PAID"  # a refund never reopens a bill
    assert (detail["payments"][0]["amount"], detail["refunded_total"], detail["refunded_tips_total"]) == ("12.50", "12.50", "2.00")
    report = till.day_report(till.day["id"])
    # The tip refund reduces tips only; revenue drops by the bill refunds.
    assert report["revenue"]["total"] == "0.00"
    assert report["net_tips"]["total"] == "0.00"
    assert report["refunds"]["CASH"] == "12.50" and report["refunded_tips"]["CASH"] == "2.00"
    # Cash left the drawer: 100 + 12.50 + 2.00 tip - 14.50 refunded.
    assert till.ok(till.call("GET", f"/api/v1/cash/shifts/{till.shift['id']}"))["expected_cash"] == "100.00"


def test_card_refunds_are_confirmed_by_hand_and_cash_refunds_need_cash(open_till):
    till = open_till
    _, _, bill = till.billed_order()
    card = till.ok(till.pay(bill["id"], "12.50", "CARD"), 201)

    till.error(till.refund(card["id"], "12.50"), 422, "CARD_CONFIRMATION_REQUIRED")
    refund = till.ok(till.refund(card["id"], "12.50", card_confirmed=True, external_reference="RFND-1"), 201)
    assert (refund["method"], refund["confirmation_method"]) == ("CARD", "MANUAL_CARD_TERMINAL")
    # A card refund doesn't touch the drawer.
    assert till.ok(till.call("GET", f"/api/v1/cash/shifts/{till.shift['id']}"))["expected_cash"] == "100.00"

    _, _, bill2 = till.billed_order(table=_second_table(till))
    cash = till.ok(till.pay(bill2["id"], "12.50"), 201)
    path = f"/api/v1/cash/shifts/{till.shift['id']}/movements"
    till.ok(till.call("POST", path, json={"type": "CASH_OUT", "amount": "110.00", "reason": "Safe drop"}), 201)
    till.error(till.refund(cash["id"], "12.50"), 409, "INSUFFICIENT_CASH")


def test_a_refund_belongs_to_the_shift_and_day_it_is_made_in(till):
    day1 = till.open_day(DAY_1)
    shift1 = till.ok(till.open_shift("100.00"), 201)
    _, _, bill = till.billed_order()
    payment = till.ok(till.pay(bill["id"], "12.50"), 201)
    till.ok(till.close_shift(shift1["id"], "112.50"))
    till.ok(till.call("POST", f"/api/v1/business-days/{day1['id']}/close", till.manager))
    frozen = till.day_report(day1["id"])

    day2 = till.open_day(DAY_2)
    shift2 = till.ok(till.open_shift("112.50"), 201)
    refund = till.ok(till.refund(payment["id"], "2.50"), 201)

    assert (refund["cash_shift_id"], refund["business_day_id"]) == (shift2["id"], day2["id"])
    assert till.day_report(day1["id"]) == frozen
    assert frozen["revenue"]["total"] == "12.50"
    assert till.day_report(day2["id"])["revenue"]["total"] == "-2.50"
    assert till.ok(till.call("GET", f"/api/v1/cash/shifts/{shift2['id']}"))["expected_cash"] == "110.00"


# --- Handovers ---


def _close_and_hand_over(till, counted, received, reason=None):
    shift = till.register()["shift"]
    till.ok(till.close_shift(shift["id"], counted, None if counted == shift["expected_cash"] else "count"))
    return till.open_shift(received, reason, user=till.cashier2)


def test_exact_handover_needs_no_reason(open_till):
    till = open_till
    incoming = till.ok(_close_and_hand_over(till, "100.00", "100.00"), 201)
    handover = incoming["handover"]
    assert (handover["handed_over_amount"], handover["received_amount"], handover["difference"], handover["reason"]) == (
        "100.00", "100.00", "0.00", None,
    )
    assert handover["incoming_user"]["id"] == str(till.cashier2.id)
    assert handover["outgoing_user"]["id"] == str(till.cashier.id)


@pytest.mark.parametrize(("received", "difference"), [("130.00", "-20.00"), ("155.00", "5.00")])
def test_handover_discrepancy_is_recorded_with_its_reason(open_till, received, difference):
    till = open_till
    _, _, bill = till.billed_order()
    till.ok(till.pay(bill["id"], "12.50"), 201)
    path = f"/api/v1/cash/shifts/{till.shift['id']}/movements"
    till.ok(till.call("POST", path, json={"type": "CASH_IN", "amount": "37.50", "reason": "Float top-up"}), 201)
    # Expected 150.00, counted 150.00 by the outgoing cashier.
    till.ok(till.close_shift(till.shift["id"], "150.00"))

    till.error(till.open_shift(received, user=till.cashier2), 422, "HANDOVER_REASON_REQUIRED")
    incoming = till.ok(till.open_shift(received, "Recounted twice", user=till.cashier2), 201)

    handover = incoming["handover"]
    assert (
        handover["previous_expected_amount"], handover["handed_over_amount"],
        handover["received_amount"], handover["difference"], handover["reason"],
    ) == ("150.00", "150.00", received, difference, "Recounted twice")
    # The new shift opens with what was received; no movement, nothing
    # else changes, and payments go on.
    assert (incoming["opening_amount"], incoming["expected_cash"], incoming["movements"]) == (received, received, [])
    assert till.db.scalar(select(CashMovement).where(CashMovement.cash_shift_id == incoming["id"])) is None
    _, _, bill2 = till.billed_order(table=_second_table(till))
    till.ok(till.pay(bill2["id"], "12.50", user=till.cashier2), 201)
    report = till.day_report(till.day["id"])
    assert report["handover_difference_total"] == difference
    assert report["revenue"]["total"] == "25.00"
    assert [h["difference"] for h in report["handovers"]] == [difference]
    assert till.ok(till.call("GET", "/api/v1/cash/handovers"))[0]["id"] == handover["id"]
    event = till.db.scalar(select(OperationalEvent).where(OperationalEvent.event_type == "cash_shift.handover_recorded"))
    assert event.event_metadata["difference"] == difference


def test_cross_day_handover_belongs_to_the_new_day_and_leaves_the_old_report(till):
    day1 = till.open_day(DAY_1)
    shift1 = till.ok(till.open_shift("100.00"), 201)
    till.ok(till.close_shift(shift1["id"], "150.00", "Found 50 extra"))
    till.ok(till.call("POST", f"/api/v1/business-days/{day1['id']}/close", till.manager))
    frozen = till.day_report(day1["id"])

    day2 = till.open_day(DAY_2)
    shift2 = till.ok(till.open_shift("130.00", "Only 130 in the drawer", user=till.cashier2), 201)

    handover = shift2["handover"]
    assert (handover["from_shift_id"], handover["business_day_id"], handover["difference"]) == (shift1["id"], day2["id"], "-20.00")
    assert till.day_report(day1["id"]) == frozen and frozen["handovers"] == []
    report2 = till.day_report(day2["id"])
    assert [(h["handed_over_amount"], h["received_amount"]) for h in report2["handovers"]] == [("150.00", "130.00")]
    assert shift2["expected_cash"] == "130.00"


def test_consecutive_handovers_chain_and_reports_count_nothing_twice(open_till):
    till = open_till
    totals = []
    # Three shifts in one day, each taking one 12.50 cash payment.
    for counted, received, cashier in (("112.50", "110.00", till.cashier2), ("122.50", "122.50", till.cashier)):
        _, _, bill = till.billed_order()
        till.ok(till.pay(bill["id"], "12.50"), 201)
        shift = till.register()["shift"]
        totals.append(shift["expected_cash"])
        till.ok(till.close_shift(shift["id"], counted, None if counted == shift["expected_cash"] else "x"))
        reason = None if received == counted else "short"
        till.ok(till.open_shift(received, reason, user=cashier), 201)
    _, _, bill = till.billed_order()
    till.ok(till.pay(bill["id"], "12.50"), 201)
    last = till.register()["shift"]

    assert totals == ["112.50", "122.50"]
    assert last["expected_cash"] == "135.00"
    handovers = till.db.scalars(select(CashShiftHandover).order_by(CashShiftHandover.created_at)).all()
    assert [(h.handed_over_amount, h.received_amount, h.difference) for h in handovers] == [
        (Decimal("112.50"), Decimal("110.00"), Decimal("-2.50")),
        (Decimal("122.50"), Decimal("122.50"), Decimal("0.00")),
    ]
    assert handovers[0].to_shift_id == handovers[1].from_shift_id
    report = till.day_report(till.day["id"])
    assert report["revenue"] == {"CASH": "37.50", "CARD": "0.00", "total": "37.50"}
    assert len(report["shifts"]) == 3 and report["handover_difference_total"] == "-2.50"
    assert sum(Decimal(s["revenue"]["total"]) for s in report["shifts"]) == Decimal("37.50")


def test_atomic_handover_leaves_no_moment_without_a_shift(open_till):
    till = open_till
    _, _, bill = till.billed_order()
    till.ok(till.pay(bill["id"], "12.50"), 201)
    path = f"/api/v1/cash/shifts/{till.shift['id']}/handover"

    till.error(till.call("POST", path, till.cashier2, {"counted_amount": "112.50", "received_amount": "110.00"}), 422, "HANDOVER_REASON_REQUIRED")
    assert till.register()["shift"]["id"] == till.shift["id"]  # nothing changed
    incoming = till.ok(till.call("POST", path, till.cashier2, {
        "counted_amount": "112.50", "received_amount": "110.00", "handover_reason": "2.50 short",
    }), 201)

    assert incoming["handover"]["from_shift_id"] == till.shift["id"]
    assert till.ok(till.call("GET", f"/api/v1/cash/shifts/{till.shift['id']}"))["status"] == "CLOSED"
    assert till.register()["shift"]["id"] == incoming["id"]


# --- Closing the business day ---


def test_business_day_close_lists_every_blocking_order(open_till):
    till = open_till
    order1, _, bill1 = till.billed_order()
    till.ok(till.pay(bill1["id"], "5.00"), 201)
    order2 = till.client.post(
        "/api/v1/orders",
        json={"table_id": str(_second_table(till).id), "guest_count": 1},
        headers=auth_headers(till.manager),
    ).json()
    close = f"/api/v1/business-days/{till.day['id']}/close"

    till.error(till.call("POST", close, till.cashier), 403, "PERMISSION_DENIED")
    error = till.error(till.call("POST", close, till.manager), 409, "CASH_SHIFT_STILL_OPEN")
    assert error["details"] == {"cash_shift_ids": [till.shift["id"]]}
    till.ok(till.close_shift(till.shift["id"], "105.00"))
    error = till.error(till.call("POST", close, till.manager), 409, "BUSINESS_DAY_HAS_OPEN_ORDERS")
    assert error["details"]["orders"] == [
        {"order_id": order1["id"], "order_status": "SENT", "table_number": 1, "table_name": None,
         "bill_id": bill1["id"], "bill_status": "OPEN", "outstanding_amount": "7.50"},
        {"order_id": order2["id"], "order_status": "OPEN", "table_number": 2, "table_name": None,
         "bill_id": None, "bill_status": None, "outstanding_amount": "0.00"},
    ]
    # Nothing was closed, cancelled or carried over.
    assert till.ok(till.call("GET", f"/api/v1/orders/{order2['id']}", till.manager))["status"] == "OPEN"
    assert till.bill(bill1["id"])["status"] == "OPEN"


def test_a_closed_day_report_is_frozen(open_till):
    till = open_till
    _, _, bill = till.billed_order()
    till.ok(till.pay(bill["id"], "12.50", tip_amount="1.00"), 201)
    till.ok(till.close_shift(till.shift["id"], "113.50"))
    day = till.ok(till.call("POST", f"/api/v1/business-days/{till.day['id']}/close", till.manager))

    report = till.day_report(till.day["id"])
    assert day["status"] == "CLOSED"
    assert (report["revenue"]["total"], report["tips"]["total"], report["shifts"][0]["closing"]["difference"]) == (
        "12.50", "1.00", "0.00",
    )
    till.error(till.call("POST", f"/api/v1/business-days/{till.day['id']}/close", till.manager), 409, "BUSINESS_DAY_NOT_OPEN")


# --- Events, tenancy and access ---


def test_financial_events_are_in_the_outbox(open_till):
    till = open_till
    _, _, bill = till.billed_order()
    payment = till.ok(till.pay(bill["id"], "12.50"), 201)
    till.ok(till.refund(payment["id"], "1.00"), 201)

    types = set(till.db.scalars(select(OutboxEvent.event_type)).all())
    assert {
        "business_day.opened", "cash_shift.opened", "bill.created", "payment.completed", "payment.refunded",
    } <= types


def test_another_restaurants_cash_records_look_missing(open_till, db_session, db_client):
    till = open_till
    _, _, bill = till.billed_order()
    payment = till.ok(till.pay(bill["id"], "12.50"), 201)
    other = create_restaurant(db_session, "Other")
    stranger = create_user(db_session, other, "MANAGER")

    for method, path, body in (
        ("GET", f"/api/v1/bills/{bill['id']}", None),
        ("POST", f"/api/v1/bills/{bill['id']}/payments", {"amount": "1.00", "method": "CASH"}),
        ("GET", f"/api/v1/bills/{bill['id']}/split?parts=2", None),
        ("POST", f"/api/v1/payments/{payment['id']}/refunds", {"amount": "1.00", "reason": "x"}),
        ("GET", f"/api/v1/cash/shifts/{till.shift['id']}", None),
        ("POST", f"/api/v1/cash/shifts/{till.shift['id']}/close", {"counted_amount": "1.00", "explanation": "x"}),
        ("POST", f"/api/v1/cash/shifts/{till.shift['id']}/movements", {"type": "CASH_IN", "amount": "1.00", "reason": "x"}),
        ("POST", f"/api/v1/cash/shifts/{till.shift['id']}/opening-corrections", {"opening_amount": "1.00", "reason": "x"}),
        ("GET", f"/api/v1/business-days/{till.day['id']}/report", None),
        ("POST", f"/api/v1/business-days/{till.day['id']}/close", None),
    ):
        response = till.call(method, path, stranger, body)
        assert response.status_code == 404, (path, response.text)
    assert till.ok(till.call("GET", "/api/v1/cash/register", stranger))["shift"] is None
    assert till.ok(till.call("GET", "/api/v1/cashier/orders", stranger)) == []
    assert till.db.scalar(select(Payment.amount).where(Payment.id == payment["id"])) == Decimal("12.50")


def test_waiters_take_no_payments_and_cashiers_see_no_reports(open_till):
    till = open_till
    _, _, bill = till.billed_order()
    till.error(till.pay(bill["id"], "12.50", user=till.waiter), 403, "PERMISSION_DENIED")
    till.error(till.call("GET", f"/api/v1/business-days/{till.day['id']}/report"), 403, "PERMISSION_DENIED")
    till.error(till.call("GET", "/api/v1/cash/register", till.waiter), 403, "PERMISSION_DENIED")


def test_cashier_order_list_shows_what_is_due(open_till):
    till = open_till
    _, _, bill = till.billed_order(till.burger, till.salad)
    till.ok(till.pay(bill["id"], "4.80"), 201)

    payable = till.ok(till.call("GET", "/api/v1/cashier/orders"))
    assert [(o["table_number"], o["bill_status"], o["paid_total"], o["amount_due"]) for o in payable] == [
        (1, "OPEN", "4.80", "15.00"),
    ]
    till.ok(till.pay(bill["id"], "15.00"), 201)
    assert till.ok(till.call("GET", "/api/v1/cashier/orders")) == []
    assert [o["bill_id"] for o in till.ok(till.call("GET", "/api/v1/cashier/orders?view=settled"))] == [bill["id"]]


def _second_table(till):
    from app.db.models.table import Table

    table = till.db.scalar(select(Table).where(Table.restaurant_id == till.restaurant.restaurant.id, Table.number == 2))
    if table is None:
        table = Table(restaurant_id=till.restaurant.restaurant.id, number=2, capacity=2)
        till.db.add(table)
        till.db.flush()
    return table
