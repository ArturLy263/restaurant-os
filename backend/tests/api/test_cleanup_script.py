"""scripts/cleanup_historical_test_data.py finds what it should (dry run).

Only the read-only finders run here, on the rolled-back test session; the
--apply path calls the same services the API tests cover.
"""

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

from app.db.models.order import OrderItem
from app.db.models.preparation import Preparation
from tests.api.test_tenant_consistency import trigger_disabled
from tests.factories import create_order, create_restaurant


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "cleanup_historical_test_data.py"


@pytest.fixture(scope="module")
def cleanup():
    spec = importlib.util.spec_from_file_location("cleanup_historical_test_data", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mine(findings, restaurant):
    return [f for f in findings if f.restaurant_id == restaurant.restaurant.id]


def test_finds_unfinished_preparations_of_paid_orders(pos, cleanup):
    order_id, burger_id, coke_id, bill_id = pos.billed_order()
    pos.start_preparation(burger_id, "start", "ready", "serve")
    assert pos.pay(bill_id, "15.00").status_code == 201

    findings = mine(cleanup.find_all(pos.db), pos.restaurant)

    assert [(f.row_id, f.action) for f in findings] == [
        (pos.preparation(coke_id).id, "discard preparation")
    ]
    assert str(findings[0].order_id) == order_id


def test_open_orders_are_not_reported(pos, cleanup):
    pos.billed_order()

    assert mine(cleanup.find_all(pos.db), pos.restaurant) == []


def test_finds_order_items_of_another_restaurants_product(db_session, restaurant, cleanup):
    other = create_restaurant(db_session, name="Other Restaurant")
    order = create_order(db_session, restaurant, status="SENT")
    pending, served = (
        OrderItem(
            order_id=order.id,
            product_id=other.product.id,
            quantity=1,
            unit_price=Decimal("12.50"),
            total_price=Decimal("12.50"),
            status="SENT",
        )
        for _ in range(2)
    )
    with trigger_disabled(db_session, "order_items", "trg_order_items_product_restaurant"):
        db_session.add_all([pending, served])
    with trigger_disabled(db_session, "preparations", "trg_preparations_station_restaurant"):
        db_session.add_all(
            [
                Preparation(order_item_id=pending.id, station_id=other.station.id, status="PENDING"),
                Preparation(order_item_id=served.id, station_id=other.station.id, status="SERVED"),
            ]
        )

    findings = {f.row_id: f.action for f in mine(cleanup.find_all(db_session), restaurant)}

    # A served dish cannot be cancelled; it is left for a manual decision.
    assert findings == {pending.id: "cancel order item", served.id: None}
