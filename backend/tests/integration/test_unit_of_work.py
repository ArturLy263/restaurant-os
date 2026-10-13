"""Transaction ownership (app.db.unit_of_work) against real PostgreSQL.

Sessions here use the engine directly, so commits are real and visibility
is checked from a separate connection. Rows are deleted afterwards.
"""

import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models.restaurant import Restaurant
from app.db.unit_of_work import (
    OwnedTransactionCommitError,
    commit,
    owned_transaction,
)


@pytest.fixture
def names(db_engine):
    """Unique restaurant names to write; their rows are deleted afterwards."""

    created = []

    def new_name():
        name = f"uow-{uuid.uuid4()}"
        created.append(name)
        return name

    try:
        yield new_name
    finally:
        with Session(db_engine) as cleanup:
            cleanup.execute(
                delete(Restaurant).where(Restaurant.name.in_(created))
            )
            cleanup.commit()


def committed(db_engine, name) -> bool:
    """Whether `name` is visible to another connection."""

    with Session(db_engine) as other:
        return other.scalar(
            select(Restaurant.id).where(Restaurant.name == name)
        ) is not None


def test_direct_commit_inside_owned_transaction_is_refused(db_engine, names):
    name = names()

    with Session(db_engine) as db:
        with owned_transaction(db):
            db.add(Restaurant(name=name))
            db.flush()

            with pytest.raises(OwnedTransactionCommitError):
                db.commit()

        assert not committed(db_engine, name)
        db.rollback()

    assert not committed(db_engine, name)


def test_owner_commit_is_allowed(db_engine, names):
    name = names()

    with Session(db_engine) as db:
        with owned_transaction(db) as transaction:
            db.add(Restaurant(name=name))
            # A service ending its unit of work only flushes.
            commit(db)
            assert not committed(db_engine, name)

            transaction.commit()

    assert committed(db_engine, name)


def test_rollback_inside_owned_transaction_is_allowed(db_engine, names):
    name = names()

    with Session(db_engine) as db:
        with owned_transaction(db) as transaction:
            db.add(Restaurant(name=name))
            db.flush()
            db.rollback()

            # The owner can still end the (now empty) transaction.
            transaction.commit()

    assert not committed(db_engine, name)


def test_ordinary_commits_are_unaffected(db_engine, names):
    first, second = names(), names()

    with Session(db_engine) as db:
        db.add(Restaurant(name=first))
        db.commit()

        db.add(Restaurant(name=second))
        commit(db)

    assert committed(db_engine, first)
    assert committed(db_engine, second)


def test_ownership_ends_with_the_block_even_after_an_error(db_engine, names):
    name = names()

    with Session(db_engine) as db:
        with pytest.raises(ValueError):
            with owned_transaction(db):
                raise ValueError("operation failed")

        db.rollback()
        db.add(Restaurant(name=name))
        db.commit()

    assert committed(db_engine, name)


def test_owner_handle_flag_does_not_outlive_its_commit(db_engine, names):
    """After the owner commits, a later direct commit is refused again."""

    name = names()

    with Session(db_engine) as db:
        with owned_transaction(db) as transaction:
            transaction.commit()

            db.add(Restaurant(name=name))
            with pytest.raises(OwnedTransactionCommitError):
                db.commit()

            db.rollback()

    assert not committed(db_engine, name)


def test_transaction_cannot_be_owned_twice(db_session):
    with owned_transaction(db_session):
        with pytest.raises(RuntimeError, match="already owned"):
            with owned_transaction(db_session):
                pass
