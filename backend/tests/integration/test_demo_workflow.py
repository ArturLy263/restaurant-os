"""The portfolio demo, end to end, through the HTTP API.

Seeds the demo restaurant (backend/seed_demo.py) into this test's
rolled-back session on the *_test database, signs in with the seeded demo
accounts, and plays the whole workflow the frontend drives:

    waiter POS -> kitchen/bar queues -> cashier bill, equal split, cash and
    card -> cash out -> blind count and two-person handover -> second shift
    with a difference -> manager closes the day -> frozen report.

Every step goes through the real routes, permissions, idempotency and
business rules; nothing is written directly. Nothing outlives the test.
"""

import uuid
from decimal import Decimal

import pytest

from seed_demo import EMAIL_DOMAIN, seed_demo


PASSWORD = "integration-demo-password"


class Demo:
    def __init__(self, client):
        self.client = client
        self.tokens: dict[str, str] = {}

    def login(self, name: str) -> str:
        response = self.client.post(
            "/api/v1/auth/login",
            json={"email": f"{name}@{EMAIL_DOMAIN}", "password": PASSWORD},
        )
        assert response.status_code == 200, response.text
        self.tokens[name] = response.json()["access_token"]
        return name

    def call(self, method: str, path: str, who: str, body=None, *, key: bool = True, expect: int | None = None):
        headers = {"Authorization": f"Bearer {self.tokens[who]}"}
        if key and method != "GET":
            headers["Idempotency-Key"] = str(uuid.uuid4())
        response = self.client.request(method, path, json=body, headers=headers)
        if expect is not None:
            assert response.status_code == expect, f"{method} {path} as {who}: {response.status_code} {response.text}"
        return response

    def ok(self, method: str, path: str, who: str, body=None, expect: int = 200):
        return self.call(method, path, who, body, expect=expect).json()


@pytest.fixture
def demo(db_client, db_session):
    seed_demo(db_session, PASSWORD, allow_test_database=True)
    demo = Demo(db_client)
    for name in ("cashier1", "cashier2", "waiter", "kitchen", "bar", "manager"):
        demo.login(name)
    return demo


def product(menu: dict, name: str) -> dict:
    return next(p for category in menu["categories"] for p in category["products"] if p["name"] == name)


def modifier(item: dict, group: str, name: str) -> str:
    groups = {g["name"]: g for g in item["modifier_groups"]}
    return next(m["id"] for m in groups[group]["modifiers"] if m["name"] == name)


def table_by_number(tables: list[dict], number: int) -> dict:
    return next(t for t in tables if t["number"] == number)


def test_the_demo_workflow_end_to_end(demo):
    # 1-3. cashier1 opens the business day and a shift with 150.00.
    day = demo.ok("POST", "/api/v1/business-days", "cashier1", {}, expect=201)
    shift1 = demo.ok("POST", "/api/v1/cash/shifts", "cashier1", {"opening_amount": "150.00"}, expect=201)
    assert shift1["handover"] is None

    # 4-7. The waiter opens table 3 and sends burger (Doneness + Cheese),
    # pizza and two Coca-Colas.
    tables = demo.ok("GET", "/api/v1/tables", "waiter")
    table3 = table_by_number(tables, 3)
    assert table3["status"] == "AVAILABLE" and table3["active_order"] is None
    menu = demo.ok("GET", "/api/v1/menu", "waiter")
    burger, pizza, coke = (product(menu, n) for n in ("Classic burger", "Pizza Margherita", "Coca-Cola"))

    order = demo.ok("POST", "/api/v1/orders", "waiter", {"table_id": table3["id"], "guest_count": 2}, expect=201)
    path = f"/api/v1/orders/{order['id']}"
    # A required group cannot be skipped.
    skipped = demo.call("POST", f"{path}/items", "waiter", {"product_id": burger["id"], "quantity": 1}, expect=400)
    assert skipped.json()["error"]["code"] == "MODIFIER_SELECTION_INVALID"
    demo.ok(
        "POST",
        f"{path}/items",
        "waiter",
        {
            "product_id": burger["id"],
            "quantity": 1,
            "modifier_ids": [modifier(burger, "Doneness", "Medium"), modifier(burger, "Burger extras", "Cheese")],
        },
        expect=201,
    )
    demo.ok("POST", f"{path}/items", "waiter", {"product_id": pizza["id"], "quantity": 1}, expect=201)
    demo.ok("POST", f"{path}/items", "waiter", {"product_id": coke["id"], "quantity": 2}, expect=201)
    sent = demo.ok("POST", f"{path}/send", "waiter")
    assert sent["status"] == "SENT"
    assert {item["preparation"]["station_name"] for item in sent["items"]} == {"Kitchen", "Bar"}

    # 8. Food reaches the Kitchen, drinks the Bar; each station sees only its own.
    kitchen_stations = demo.ok("GET", "/api/v1/stations", "kitchen")
    bar_stations = demo.ok("GET", "/api/v1/stations", "bar")
    assert [s["name"] for s in kitchen_stations] == ["Kitchen"]
    assert [s["name"] for s in bar_stations] == ["Bar"]
    kitchen_id, bar_id = kitchen_stations[0]["id"], bar_stations[0]["id"]
    kitchen_queue = demo.ok("GET", f"/api/v1/stations/{kitchen_id}/preparations", "kitchen")
    bar_queue = demo.ok("GET", f"/api/v1/stations/{bar_id}/preparations", "bar")
    assert sorted(p["product_name"] for p in kitchen_queue) == ["Classic burger", "Pizza Margherita"]
    assert [(p["product_name"], p["quantity"], p["table_number"]) for p in bar_queue] == [("Coca-Cola", 2, 3)]
    assert sorted(m["name"] for m in next(p for p in kitchen_queue if p["product_name"] == "Classic burger")["modifiers"]) == [
        "Cheese",
        "Medium",
    ]
    demo.call("GET", f"/api/v1/stations/{bar_id}/preparations", "kitchen", expect=403)

    # 9. Start -> ready at the stations; the waiter serves from the POS.
    for who, queue in (("kitchen", kitchen_queue), ("bar", bar_queue)):
        for preparation in queue:
            demo.ok("POST", f"/api/v1/preparations/{preparation['id']}/start", who)
            demo.ok("POST", f"/api/v1/preparations/{preparation['id']}/ready", who)
    # A station cannot work another station's dish.
    demo.call("POST", f"/api/v1/preparations/{kitchen_queue[0]['id']}/serve", "bar", expect=403)
    serve_key = str(uuid.uuid4())
    for preparation in kitchen_queue + bar_queue:
        headers = {"Authorization": f"Bearer {demo.tokens['waiter']}", "Idempotency-Key": serve_key if preparation is bar_queue[0] else str(uuid.uuid4())}
        response = demo.client.post(f"/api/v1/preparations/{preparation['id']}/serve", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "SERVED"
    # A repeated tap with the same key replays instead of failing.
    replay = demo.client.post(
        f"/api/v1/preparations/{bar_queue[0]['id']}/serve",
        headers={"Authorization": f"Bearer {demo.tokens['waiter']}", "Idempotency-Key": serve_key},
    )
    assert replay.status_code == 200 and replay.json()["status"] == "SERVED"
    assert demo.ok("GET", f"/api/v1/stations/{kitchen_id}/preparations", "kitchen") == []
    assert demo.ok("GET", f"/api/v1/stations/{bar_id}/preparations", "bar") == []

    # 10-11. cashier1 finds the order with what is due (14.50 + 11.00 + 2 x 2.80).
    payable = demo.ok("GET", "/api/v1/cashier/orders?view=payable", "cashier1")
    row = next(o for o in payable if o["order_id"] == order["id"])
    assert (row["table_number"], row["bill_id"], row["amount_due"]) == (3, None, "31.10")
    # The waiter cannot take money.
    demo.call("POST", f"/api/v1/bills/order/{order['id']}", "waiter", expect=403)

    # The day cannot close while a shift and this order are open.
    refused = demo.call("POST", f"/api/v1/business-days/{day['id']}/close", "manager", expect=409)
    assert refused.json()["error"]["code"] == "CASH_SHIFT_STILL_OPEN"

    # 12-14. Bill, equal split in two, one share cash and one card.
    bill = demo.ok("POST", f"/api/v1/bills/order/{order['id']}", "cashier1", expect=201)
    split = demo.ok("GET", f"/api/v1/bills/{bill['id']}/split?parts=2", "cashier1")
    assert split["shares"] == ["15.55", "15.55"]
    demo.ok("POST", f"/api/v1/bills/{bill['id']}/payments", "cashier1", {"amount": split["shares"][0], "method": "CASH"}, expect=201)
    after_first = demo.ok("GET", f"/api/v1/bills/{bill['id']}", "cashier1")
    assert (after_first["status"], after_first["amount_due"]) == ("OPEN", "15.55")
    unconfirmed = demo.call(
        "POST", f"/api/v1/bills/{bill['id']}/payments", "cashier1", {"amount": split["shares"][1], "method": "CARD"}
    )
    assert unconfirmed.status_code == 422, unconfirmed.text
    assert unconfirmed.json()["error"]["code"] == "CARD_CONFIRMATION_REQUIRED"
    demo.ok(
        "POST",
        f"/api/v1/bills/{bill['id']}/payments",
        "cashier1",
        {"amount": split["shares"][1], "method": "CARD", "card_confirmed": True, "external_reference": "A-1029"},
        expect=201,
    )

    # 15. The order is closed and table 3 is available again.
    paid = demo.ok("GET", f"/api/v1/bills/{bill['id']}", "cashier1")
    assert (paid["status"], paid["amount_due"], paid["order"]["status"]) == ("PAID", "0.00", "CLOSED")
    table3 = table_by_number(demo.ok("GET", "/api/v1/tables", "waiter"), 3)
    assert table3["status"] == "AVAILABLE" and table3["active_order"] is None
    assert all(o["order_id"] != order["id"] for o in demo.ok("GET", "/api/v1/cashier/orders?view=payable", "cashier1"))
    assert any(o["order_id"] == order["id"] for o in demo.ok("GET", "/api/v1/cashier/orders?view=settled", "cashier1"))

    # 16. A cash out of 20.00.
    demo.ok(
        "POST",
        f"/api/v1/cash/shifts/{shift1['id']}/movements",
        "cashier1",
        {"type": "CASH_OUT", "amount": "20.00", "reason": "Supplier paid in cash"},
        expect=201,
    )

    # 17. Blind count: the cashier never sees the expected cash
    # (150.00 + 15.55 cash - 20.00 = 145.55), and hands over what they counted.
    register = demo.ok("GET", "/api/v1/cash/register", "cashier1")
    assert register["shift"]["expected_cash"] is None and register["shift"]["expected_visible"] is False
    closed1 = demo.ok(
        "POST", f"/api/v1/cash/shifts/{shift1['id']}/handover", "cashier1", {"counted_amount": "145.55"}
    )
    assert closed1["status"] == "CLOSED"
    assert closed1["closing"]["expected_amount"] is None

    # 18. Only another employee receives it: cashier2 opens the next shift.
    same = demo.call("POST", "/api/v1/cash/shifts", "cashier1", {"opening_amount": "145.55"}, expect=409)
    assert same.json()["error"]["code"] == "HANDOVER_SAME_USER"
    shift2 = demo.ok("POST", "/api/v1/cash/shifts", "cashier2", {"opening_amount": "145.55"}, expect=201)
    handover = shift2["handover"]
    assert handover["outgoing_user"]["name"] == "Demo Cashier One"
    assert handover["incoming_user"]["name"] == "Demo Cashier Two"
    assert (handover["handed_over_amount"], handover["received_amount"], handover["difference"]) == ("145.55", "145.55", "0.00")
    assert handover["self_confirmed"] is False

    # 19. The second shift closes 5.55 short. The blind count is recorded
    # as submitted (no refusal could reveal the expected cash) and, being
    # final, is flagged as needing an explanation.
    closed2 = demo.ok("POST", f"/api/v1/cash/shifts/{shift2['id']}/close", "cashier2", {"counted_amount": "140.00"})
    assert closed2["closing"]["explanation_required"] is True
    assert closed2["closing"]["difference"] is None and closed2["closing"]["expected_amount"] is None
    demo.call("POST", f"/api/v1/cash/shifts/{shift2['id']}/close", "cashier2", {"counted_amount": "145.55"}, expect=409)
    # The day cannot close until the difference is explained.
    blocked = demo.call("POST", f"/api/v1/business-days/{day['id']}/close", "manager", expect=409)
    assert blocked.json()["error"]["code"] == "CASH_DIFFERENCE_UNEXPLAINED"
    demo.ok(
        "POST",
        f"/api/v1/cash/shifts/{shift2['id']}/closing-explanation",
        "cashier2",
        {"explanation": "Coins miscounted"},
        expect=201,
    )

    # 20-21. Cashiers cannot close the day; the manager can.
    demo.call("POST", f"/api/v1/business-days/{day['id']}/close", "cashier1", expect=403)
    demo.call("GET", f"/api/v1/business-days/{day['id']}/report", "cashier1", expect=403)
    closed_day = demo.ok("POST", f"/api/v1/business-days/{day['id']}/close", "manager")
    assert closed_day["status"] == "CLOSED"

    # 22. The frozen report: revenue by method, tips, shift drawers, the
    # handover and the difference.
    response = demo.ok("GET", f"/api/v1/business-days/{day['id']}/report", "manager")
    assert response["business_day"]["status"] == "CLOSED"
    report = response["report"]
    assert report["revenue"] == {"CASH": "15.55", "CARD": "15.55", "total": "31.10"}
    assert report["net_tips"]["total"] == "0.00"
    assert report["payments_count"] == 2
    assert [s["opened_by"] for s in report["shifts"]] == ["Demo Cashier One", "Demo Cashier Two"]
    first, second = report["shifts"]
    assert first["drawer"]["cash_out"] == "20.00"
    assert first["drawer"]["expected"] == "145.55"
    assert first["closing"]["difference"] == "0.00"
    assert second["closing"]["expected_amount"] == "145.55"
    assert second["closing"]["counted_amount"] == "140.00"
    assert second["closing"]["difference"] == "-5.55"
    assert second["closing"]["explanation"] == "Coins miscounted"
    assert len(report["handovers"]) == 1
    assert report["handovers"][0]["outgoing_user"] == "Demo Cashier One"
    assert report["handovers"][0]["incoming_user"] == "Demo Cashier Two"
    assert Decimal(report["closing_difference_total"]) == Decimal("-5.55")

    # Nothing more can be paid into the closed day.
    assert demo.ok("GET", "/api/v1/cash/register", "manager")["business_day"] is None
