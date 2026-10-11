import uuid

import pytest
from psycopg.errors import LockNotAvailable
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.repositories.order_repository import OrderRepository
from tests.factories import create_order, create_restaurant


def _lock_nowait_from_another_connection(db_engine, order_id):
    """Try to lock the order row from a separate connection without waiting."""

    with db_engine.connect() as other:
        try:
            other.execute(
                text("SELECT id FROM orders WHERE id = :id FOR UPDATE NOWAIT"),
                {"id": order_id},
            )
        finally:
            other.rollback()


def test_returns_order_of_same_restaurant(db_session, restaurant):
    order = create_order(db_session, restaurant)
    db_session.expunge_all()

    found = OrderRepository(db_session).get_for_restaurant(
        order.id,
        restaurant.restaurant.id,
    )

    assert found is not None
    assert found.id == order.id
    assert found.restaurant_id == restaurant.restaurant.id
    assert found.status == "OPEN"


def test_returns_none_for_unknown_order(db_session, restaurant):
    found = OrderRepository(db_session).get_for_restaurant(
        uuid.uuid4(),
        restaurant.restaurant.id,
    )

    assert found is None


def test_does_not_return_order_of_another_restaurant(db_session, restaurant):
    order = create_order(db_session, restaurant)
    other = create_restaurant(db_session, name="Other Restaurant")

    found = OrderRepository(db_session).get_for_restaurant(
        order.id,
        other.restaurant.id,
    )

    assert found is None


def test_for_update_reloads_state_already_in_session(db_session, restaurant):
    order = create_order(db_session, restaurant)

    # Change the row behind the ORM's back, as a concurrent transaction
    # that committed while we waited for the lock would.
    db_session.execute(
        text("UPDATE orders SET status = 'SENT' WHERE id = :id"),
        {"id": order.id},
    )

    found = OrderRepository(db_session).get_for_restaurant(
        order.id,
        restaurant.restaurant.id,
        for_update=True,
    )

    assert found is order
    assert found.status == "SENT"


def test_plain_lookup_does_not_lock_row(db_engine, committed_order):
    with Session(db_engine) as session:
        found = OrderRepository(session).get_for_restaurant(
            committed_order.order_id,
            committed_order.restaurant_id,
        )
        assert found is not None

        _lock_nowait_from_another_connection(
            db_engine,
            committed_order.order_id,
        )


def test_for_update_locks_row_until_transaction_ends(
    db_engine,
    committed_order,
):
    with Session(db_engine) as session:
        found = OrderRepository(session).get_for_restaurant(
            committed_order.order_id,
            committed_order.restaurant_id,
            for_update=True,
        )
        assert found is not None

        with pytest.raises(OperationalError) as exc_info:
            _lock_nowait_from_another_connection(
                db_engine,
                committed_order.order_id,
            )

        assert isinstance(exc_info.value.orig, LockNotAvailable)

        session.rollback()

    # The lock is released with the transaction.
    _lock_nowait_from_another_connection(db_engine, committed_order.order_id)
