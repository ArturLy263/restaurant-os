import uuid
from decimal import Decimal

import pytest

from app.db.models.modifier import Modifier, ModifierGroup
from app.db.models.preparation import Preparation
from app.services.order_service import OrderService
from tests.locking import assert_waits_for_order_lock


def _open_order(db_session, restaurant):
    return OrderService.create_order(
        db=db_session,
        restaurant_id=restaurant.restaurant.id,
        user_id=restaurant.user.id,
        table_id=restaurant.table.id,
        guest_count=2,
    )


def test_add_item_prices_modifiers_and_snapshots_them(db_session, restaurant):
    group = ModifierGroup(
        restaurant_id=restaurant.restaurant.id,
        name="Extras",
        min_selections=0,
        max_selections=3,
    )
    group.modifiers = [
        Modifier(name="Cheese", price_adjustment=Decimal("1.00")),
        Modifier(name="Bacon", price_adjustment=Decimal("2.00")),
    ]
    restaurant.product.modifier_groups.append(group)
    db_session.flush()

    order = _open_order(db_session, restaurant)

    item = OrderService.add_order_item(
        db=db_session,
        restaurant_id=restaurant.restaurant.id,
        user_id=restaurant.user.id,
        order_id=order.id,
        product_id=restaurant.product.id,
        quantity=2,
        modifier_ids=[modifier.id for modifier in group.modifiers],
    )

    assert item.unit_price == Decimal("12.50")
    assert item.total_price == Decimal("31.00")
    assert {
        (modifier.name_snapshot, modifier.price_snapshot)
        for modifier in item.modifiers
    } == {
        ("Cheese", Decimal("1.00")),
        ("Bacon", Decimal("2.00")),
    }


def test_send_order_routes_items_to_product_station(db_session, restaurant):
    order = _open_order(db_session, restaurant)

    item = OrderService.add_order_item(
        db=db_session,
        restaurant_id=restaurant.restaurant.id,
        user_id=restaurant.user.id,
        order_id=order.id,
        product_id=restaurant.product.id,
        quantity=1,
    )

    sent_order = OrderService.send_order(
        db=db_session,
        restaurant_id=restaurant.restaurant.id,
        user_id=restaurant.user.id,
        order_id=order.id,
    )

    preparations = (
        db_session.query(Preparation)
        .filter(Preparation.order_item_id == item.id)
        .all()
    )

    assert sent_order.status == "SENT"
    assert item.status == "SENT"
    assert len(preparations) == 1
    assert preparations[0].station_id == restaurant.station.id
    assert preparations[0].status == "PENDING"


@pytest.mark.parametrize(
    "call",
    [
        lambda db, ids: OrderService.add_order_item(
            db=db,
            restaurant_id=ids.restaurant_id,
            user_id=ids.user_id,
            order_id=ids.order_id,
            product_id=ids.product_id,
            quantity=1,
        ),
        lambda db, ids: OrderService.send_order(
            db=db,
            restaurant_id=ids.restaurant_id,
            user_id=ids.user_id,
            order_id=ids.order_id,
        ),
        lambda db, ids: OrderService.cancel_order_item(
            db=db,
            restaurant_id=ids.restaurant_id,
            user_id=ids.user_id,
            order_id=ids.order_id,
            item_id=uuid.uuid4(),
            reason="Lock test",
        ),
        lambda db, ids: OrderService.void_order(
            db=db,
            restaurant_id=ids.restaurant_id,
            user_id=ids.user_id,
            order_id=ids.order_id,
            reason="Lock test",
        ),
    ],
    ids=["add_order_item", "send_order", "cancel_order_item", "void_order"],
)
def test_order_writes_wait_for_order_row_lock(
    db_engine,
    committed_order,
    call,
):
    assert_waits_for_order_lock(
        db_engine,
        committed_order.order_id,
        lambda session: call(session, committed_order),
    )
