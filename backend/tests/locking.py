"""Helpers for tests that need a real row lock held by another connection."""

import pytest
from psycopg.errors import LockNotAvailable
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session


def assert_waits_for_order_lock(db_engine, order_id, call):
    """Assert that `call(session)` blocks on the order's row lock.

    Another connection holds the order row with FOR KEY SHARE. That
    conflicts with the service's FOR UPDATE, but not with the KEY SHARE
    locks taken by foreign-key checks nor with plain UPDATEs of the order's
    non-key columns, so only an explicit order lock makes the call time out.
    """

    with db_engine.connect() as holder:
        holder.execute(
            text("SELECT id FROM orders WHERE id = :id FOR KEY SHARE"),
            {"id": order_id},
        )

        # Service commits only release a SAVEPOINT; nothing is persisted.
        with db_engine.connect() as connection:
            transaction = connection.begin()
            session = Session(
                bind=connection,
                join_transaction_mode="create_savepoint",
            )

            try:
                session.execute(text("SET LOCAL lock_timeout = '200ms'"))

                with pytest.raises(OperationalError) as exc_info:
                    call(session)

                assert isinstance(exc_info.value.orig, LockNotAvailable)
            finally:
                session.close()
                transaction.rollback()

        holder.rollback()
