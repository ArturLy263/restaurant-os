"""The database keeps the blind-count invariants even if a service were
bypassed (migration b8d4e6f2a1c3): a business day with an unexplained
difference cannot close, explanations are append-only and only written
while the day is open."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.models.cash import CashCountExplanation
from tests.api.test_cashier import open_till, till  # noqa: F401 - fixtures


# The till fixtures open their own business day and shift.
CASH_REGISTER_CLOSED = True


def test_the_database_refuses_to_close_a_day_with_an_unexplained_difference(open_till):
    till = open_till
    till.ok(till.close_shift(till.shift["id"], "90.00"))  # 10.00 short, no explanation

    with pytest.raises(DBAPIError, match="unexplained cash differences"):
        with till.db.begin_nested():
            till.db.execute(
                text("UPDATE business_days SET status = 'CLOSED', closed_at = now() WHERE id = :id"),
                {"id": till.day["id"]},
            )


def test_explanations_are_append_only_and_only_written_in_an_open_day(open_till):
    till = open_till
    till.ok(till.close_shift(till.shift["id"], "90.00"))
    path = f"/api/v1/cash/shifts/{till.shift['id']}/closing-explanation"
    till.ok(till.call("POST", path, till.cashier, {"explanation": "Gave change twice"}), 201)

    for statement in (
        "UPDATE cash_count_explanations SET explanation = 'rewritten'",
        "DELETE FROM cash_count_explanations",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            with till.db.begin_nested():
                till.db.execute(text(statement))

    till.ok(till.call("POST", f"/api/v1/business-days/{till.day['id']}/close", till.manager))
    # A second explanation row (kind HANDOVER) for the closed day's shift.
    with pytest.raises(DBAPIError, match="business day is open"):
        with till.db.begin_nested():
            till.db.add(CashCountExplanation(
                restaurant_id=till.restaurant.restaurant.id,
                cash_shift_id=till.shift["id"],
                kind="HANDOVER",
                explanation="late",
                user_id=till.manager.id,
            ))
            till.db.flush()


def test_a_manager_may_explain_a_cashiers_difference(open_till):
    till = open_till
    till.ok(till.close_shift(till.shift["id"], "101.00"))  # 1.00 over
    path = f"/api/v1/cash/shifts/{till.shift['id']}/closing-explanation"
    explained = till.ok(till.call("POST", path, till.manager, {"explanation": "Customer left a coin"}), 201)
    assert explained["user_id"] == str(till.manager.id)
    till.ok(till.call("POST", f"/api/v1/business-days/{till.day['id']}/close", till.manager))


def test_another_restaurants_shift_cannot_be_explained(open_till, db_session):
    from tests.factories import create_restaurant, create_user, auth_headers

    till = open_till
    till.ok(till.close_shift(till.shift["id"], "90.00"))
    other = create_restaurant(db_session, name="Other")
    intruder = create_user(db_session, other, "MANAGER")
    response = till.client.post(
        f"/api/v1/cash/shifts/{till.shift['id']}/closing-explanation",
        json={"explanation": "x"},
        headers={**auth_headers(intruder), "Idempotency-Key": "k-intruder-1"},
    )
    assert (response.status_code, response.json()["error"]["code"]) == (404, "CASH_SHIFT_NOT_FOUND")
