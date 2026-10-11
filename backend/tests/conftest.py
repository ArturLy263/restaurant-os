"""
Shared pytest fixtures.

The environment is configured here, before any `app` module is imported,
so the application engine is bound to the isolated test database and
never to the development database.

Database-backed tests run against PostgreSQL in "<dev database>_test"
(or TEST_DATABASE_URL). The database is created if missing and migrated
with Alembic once per session. Each test runs inside a transaction that
is rolled back afterwards; service-level commit() calls only release a
SAVEPOINT, so no test data is ever persisted.
"""

import os
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from dotenv import dotenv_values

from tests.db_url import resolve_test_database_url


REPO_ROOT = Path(__file__).resolve().parents[2]

_dev_database_url = (
    os.environ.get("DATABASE_URL")
    or dotenv_values(REPO_ROOT / ".env").get("DATABASE_URL")
)

TEST_DATABASE_URL = resolve_test_database_url(
    dev_database_url=_dev_database_url,
    test_database_url=os.environ.get("TEST_DATABASE_URL"),
)

# Environment variables take precedence over .env in pydantic-settings.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["JWT_SECRET_KEY"] = "test-only-jwt-secret-key"
os.environ["ADMIN_PASSWORD"] = "test-only-admin-password"


import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, delete, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.db.database import engine  # noqa: E402
from app.db.dependencies import get_db  # noqa: E402
from app.db.models.bill import Bill  # noqa: E402
from app.main import app  # noqa: E402

from tests.factories import create_order, create_restaurant  # noqa: E402


def _ensure_test_database_exists() -> None:
    url = make_url(TEST_DATABASE_URL)
    database_name = url.database

    assert database_name.endswith("_test")

    maintenance_engine = create_engine(
        url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )

    try:
        with maintenance_engine.connect() as connection:
            exists = connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": database_name},
            )

            if not exists:
                connection.execute(
                    text(f'CREATE DATABASE "{database_name}"')
                )
    finally:
        maintenance_engine.dispose()


_database_unavailable_reason: str | None = None


@pytest.fixture(scope="session")
def db_engine():
    global _database_unavailable_reason

    test_database_name = make_url(TEST_DATABASE_URL).database

    if engine.url.database != test_database_name:
        pytest.exit(
            f"Application engine points at '{engine.url.database}', "
            f"expected test database '{test_database_name}'. Aborting.",
            returncode=1,
        )

    # A skipped session fixture is retried by every test; fail fast instead.
    if _database_unavailable_reason is not None:
        pytest.skip(_database_unavailable_reason)

    try:
        _ensure_test_database_exists()
    except OperationalError as exc:
        error = str(exc.orig or exc).strip().splitlines()[-1]
        _database_unavailable_reason = (
            "PostgreSQL test database unavailable at "
            f"{make_url(TEST_DATABASE_URL).render_as_string()}: {error}"
        )
        pytest.skip(_database_unavailable_reason)

    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")

    return engine


@pytest.fixture
def db_session(db_engine):
    connection = db_engine.connect()
    transaction = connection.begin()

    session = Session(
        bind=connection,
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )

    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def client():
    """API client for tests that never reach the database."""
    return TestClient(app)


@pytest.fixture
def db_client(db_session):
    """API client whose requests share the test's rolled-back session."""

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def restaurant(db_session):
    return create_restaurant(db_session)


@pytest.fixture
def committed_order(db_engine):
    """An OPEN order committed so that other connections can see and lock it.

    Row-lock tests need a second connection, which cannot see the
    rolled-back `db_session` transaction. The rows are deleted afterwards.
    """

    with Session(db_engine) as setup:
        restaurant = create_restaurant(setup, name="Committed Restaurant")
        order = create_order(setup, restaurant)
        setup.commit()

        # Deletion order respects foreign keys.
        created = [
            (type(row), row.id)
            for row in (
                order,
                restaurant.product,
                restaurant.category,
                restaurant.table,
                restaurant.station,
                restaurant.user,
                restaurant.role,
                restaurant.restaurant,
            )
        ]
        ids = SimpleNamespace(
            order_id=order.id,
            restaurant_id=restaurant.restaurant.id,
            user_id=restaurant.user.id,
            product_id=restaurant.product.id,
        )

    try:
        yield ids
    finally:
        with Session(db_engine) as teardown:
            for model, row_id in created:
                teardown.execute(delete(model).where(model.id == row_id))

            teardown.commit()


@pytest.fixture
def committed_bill(db_engine, committed_order):
    """An OPEN bill for `committed_order`, committed for row-lock tests.

    Torn down before `committed_order`, so the bill is deleted first.
    """

    with Session(db_engine) as setup:
        bill = Bill(
            order_id=committed_order.order_id,
            subtotal=Decimal("12.50"),
            total=Decimal("12.50"),
            status="OPEN",
        )
        setup.add(bill)
        setup.commit()
        bill_id = bill.id

    try:
        yield SimpleNamespace(**vars(committed_order), bill_id=bill_id)
    finally:
        with Session(db_engine) as teardown:
            teardown.execute(delete(Bill).where(Bill.id == bill_id))
            teardown.commit()
