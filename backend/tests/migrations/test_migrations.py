"""
Migration safety checks.

Destructive tests run only against `migration_database`: an empty database
created for the test and dropped afterwards. Each Alembic command gets a
connection whose identity has just been verified, so env.py never falls
back to DATABASE_URL.

Limitations of the models-versus-migrations comparison (Alembic
autogenerate): it compares tables, columns, column types, nullability,
indexes, unique constraints and foreign keys. It does not compare server
defaults (not enabled here), CHECK constraints, triggers, functions,
sequences or other objects created with raw SQL, and it cannot detect
data migrations that are wrong. A renamed column or table shows up as a
drop plus an add.
"""

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.db import models  # noqa: F401 - registers every model
from app.db.database import Base


ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"
SCRIPT = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))


def migrate(database, operation, revision):
    """Run `alembic <operation> <revision>` on the verified disposable DB."""

    with database.begin() as connection:
        config = Config(str(ALEMBIC_INI))
        config.attributes["connection"] = connection
        operation(config, revision)


def current_revision(database):
    with database.begin() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def schema_snapshot(database):
    """Tables, columns, keys, indexes, triggers, functions and enum types
    in the public schema."""

    with database.begin() as connection:
        inspector = inspect(connection)
        snapshot = {}

        for table in sorted(inspector.get_table_names()):
            snapshot[table] = {
                "columns": [
                    (column["name"], str(column["type"]), column["nullable"])
                    for column in inspector.get_columns(table)
                ],
                "primary_key": inspector.get_pk_constraint(table),
                "foreign_keys": sorted(
                    (fk["name"], tuple(fk["constrained_columns"]))
                    for fk in inspector.get_foreign_keys(table)
                ),
                "unique": sorted(
                    (uc["name"], tuple(uc["column_names"]))
                    for uc in inspector.get_unique_constraints(table)
                ),
                "indexes": sorted(
                    (ix["name"], tuple(ix["column_names"]), ix["unique"])
                    for ix in inspector.get_indexes(table)
                ),
            }

        # Raw-SQL objects the inspector does not see (e.g. the tenant
        # consistency triggers of e1b7d3a9c4f2).
        snapshot["<triggers>"] = sorted(
            connection.execute(
                text(
                    "SELECT c.relname, t.tgname FROM pg_trigger t "
                    "JOIN pg_class c ON c.oid = t.tgrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND NOT t.tgisinternal"
                )
            ).all()
        )
        snapshot["<functions>"] = sorted(
            connection.scalars(
                text(
                    "SELECT p.proname FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = 'public'"
                )
            )
        )

        snapshot["<enum types>"] = sorted(
            connection.scalars(
                text(
                    "SELECT t.typname FROM pg_type t "
                    "JOIN pg_namespace n ON n.oid = t.typnamespace "
                    "WHERE n.nspname = 'public' AND t.typtype = 'e'"
                )
            )
        )

        return snapshot


def migration_chain():
    """Revisions from the first to the head, oldest first."""

    return list(reversed(list(SCRIPT.walk_revisions("base", "heads"))))


def test_migration_history_has_a_single_head():
    assert len(SCRIPT.get_heads()) == 1


def test_upgrade_to_head_and_downgrade_to_base(migration_database):
    empty = schema_snapshot(migration_database)

    migrate(migration_database, command.upgrade, "head")

    assert current_revision(migration_database) == SCRIPT.get_current_head()
    assert set(Base.metadata.tables) <= set(schema_snapshot(migration_database))

    migrate(migration_database, command.downgrade, "base")

    assert current_revision(migration_database) is None
    after_downgrade = schema_snapshot(migration_database)
    after_downgrade.pop("alembic_version")
    assert after_downgrade == empty

    migrate(migration_database, command.upgrade, "head")

    assert current_revision(migration_database) == SCRIPT.get_current_head()


def test_each_migration_downgrades_cleanly(migration_database):
    """Upgrade one step, downgrade it, upgrade again — for every revision.

    The schema after each downgrade must equal the schema before the
    upgrade, so every downgrade() exactly reverses its upgrade().
    """

    for revision in migration_chain():
        before = schema_snapshot(migration_database)

        migrate(migration_database, command.upgrade, revision.revision)
        migrate(
            migration_database,
            command.downgrade,
            revision.down_revision or "base",
        )

        after = schema_snapshot(migration_database)
        # alembic_version appears after the first upgrade and stays.
        if "alembic_version" not in before:
            after.pop("alembic_version")

        assert after == before, f"downgrade of {revision.revision}"

        migrate(migration_database, command.upgrade, revision.revision)

    assert current_revision(migration_database) == SCRIPT.get_current_head()


def test_models_match_migrations(migration_database):
    """Equivalent of `alembic check`: no autogenerate diff at head."""

    migrate(migration_database, command.upgrade, "head")

    with migration_database.begin() as connection:
        differences = compare_metadata(
            MigrationContext.configure(connection),
            Base.metadata,
        )

    assert differences == [], (
        "SQLAlchemy models and migrations differ; create a migration with "
        "`alembic revision --autogenerate`:\n"
        + "\n".join(map(str, differences))
    )


def test_outbox_delivery_tracking_keeps_existing_events(migration_database):
    """a7d3e5f1c9b4 adds delivery columns without changing existing events,
    and its downgrade removes them without losing the events."""

    migrate(migration_database, command.upgrade, "f3a9c1e7b5d2")

    event = {
        "id": "8b0f4c1e-1d2a-4c3b-9e8f-0a1b2c3d4e5f",
        "restaurant_id": "1f2e3d4c-5b6a-4978-8a9b-0c1d2e3f4a5b",
        "aggregate_id": "2a3b4c5d-6e7f-4a8b-9c0d-1e2f3a4b5c6d",
    }
    original_columns = (
        "id, restaurant_id, event_type, schema_version, aggregate_type, "
        "aggregate_id, payload::text, created_at, published_at"
    )

    with migration_database.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO restaurants (id, name, timezone, currency, "
                "is_active, created_at, updated_at) VALUES (:r, 'Migrated', "
                "'UTC', 'EUR', true, now(), now())"
            ),
            {"r": event["restaurant_id"]},
        )
        connection.execute(
            text(
                "INSERT INTO outbox_events (id, restaurant_id, event_type, "
                "schema_version, aggregate_type, aggregate_id, payload) "
                "VALUES (:id, :restaurant_id, 'order.sent', 1, 'order', "
                ":aggregate_id, '{\"order_id\": \"x\"}')"
            ),
            event,
        )
        before = connection.execute(
            text(f"SELECT {original_columns} FROM outbox_events")
        ).one()

    migrate(migration_database, command.upgrade, "a7d3e5f1c9b4")

    with migration_database.begin() as connection:
        assert connection.execute(
            text(f"SELECT {original_columns} FROM outbox_events")
        ).one() == before
        attempts, last_attempt_at, next_attempt_at, last_error = connection.execute(
            text(
                "SELECT attempts, last_attempt_at, next_attempt_at, last_error "
                "FROM outbox_events"
            )
        ).one()
        assert (attempts, last_attempt_at, last_error) == (0, None, None)
        assert next_attempt_at is not None

    migrate(migration_database, command.downgrade, "f3a9c1e7b5d2")

    with migration_database.begin() as connection:
        assert connection.execute(
            text(f"SELECT {original_columns} FROM outbox_events")
        ).one() == before


def test_outbox_dead_letter_migration_keeps_existing_events(migration_database):
    """c5e8a2d4f6b1 adds dead_lettered_at (NULL for every existing event)
    and its CHECK, which existing delivered and failing rows satisfy; the
    downgrade drops them without losing events."""

    migrate(migration_database, command.upgrade, "a7d3e5f1c9b4")

    restaurant_id = "3c4d5e6f-7a8b-4c9d-8e0f-1a2b3c4d5e6f"
    columns = (
        "id, restaurant_id, event_type, schema_version, aggregate_type, "
        "aggregate_id, payload::text, created_at, published_at, attempts, "
        "last_attempt_at, next_attempt_at, last_error"
    )

    with migration_database.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO restaurants (id, name, timezone, currency, "
                "is_active, created_at, updated_at) VALUES (:r, 'Migrated', "
                "'UTC', 'EUR', true, now(), now())"
            ),
            {"r": restaurant_id},
        )
        for event_id, published, attempts, error in (
            ("4d5e6f7a-8b9c-4d0e-9f1a-2b3c4d5e6f7a", "now()", 1, None),
            ("5e6f7a8b-9c0d-4e1f-8a2b-3c4d5e6f7a8b", "NULL", 7, "RuntimeError: down"),
        ):
            connection.execute(
                text(
                    "INSERT INTO outbox_events (id, restaurant_id, event_type, "
                    "schema_version, aggregate_type, aggregate_id, payload, "
                    f"published_at, attempts, last_attempt_at, last_error) "
                    f"VALUES (:id, :r, 'order.sent', 1, 'order', :id, "
                    f"'{{\"order_id\": \"x\"}}', {published}, :attempts, now(), :error)"
                ),
                {"id": event_id, "r": restaurant_id, "attempts": attempts, "error": error},
            )
        before = connection.execute(
            text(f"SELECT {columns} FROM outbox_events ORDER BY id")
        ).all()

    migrate(migration_database, command.upgrade, "c5e8a2d4f6b1")

    with migration_database.begin() as connection:
        assert connection.execute(
            text(f"SELECT {columns} FROM outbox_events ORDER BY id")
        ).all() == before
        assert connection.scalars(
            text("SELECT dead_lettered_at FROM outbox_events")
        ).all() == [None, None]

    migrate(migration_database, command.downgrade, "a7d3e5f1c9b4")

    with migration_database.begin() as connection:
        assert connection.execute(
            text(f"SELECT {columns} FROM outbox_events ORDER BY id")
        ).all() == before


def test_tenant_consistency_triggers_keep_and_report_existing_rows(
    migration_database,
):
    """e1b7d3a9c4f2 installs the tenant consistency triggers without
    changing existing rows that violate them, reports those rows, rejects
    new violations, and its downgrade removes only the triggers."""

    import logging
    from decimal import Decimal

    from sqlalchemy.orm import Session

    from app.db.models.order import OrderItem
    from app.db.models.preparation import Preparation
    from app.db.models.product import Product
    from tests.factories import create_order, create_restaurant

    migrate(migration_database, command.upgrade, "c5e8a2d4f6b1")

    with migration_database.begin() as connection:
        db = Session(bind=connection)
        own = create_restaurant(db, name="Own")
        other = create_restaurant(db, name="Other")

        misrouted = Product(
            category_id=own.category.id,
            name="Misrouted",
            price=Decimal("1.00"),
            station_id=other.station.id,
        )
        order = create_order(db, own, status="SENT")
        db.add(misrouted)
        db.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=other.product.id,
            quantity=1,
            unit_price=Decimal("12.50"),
            total_price=Decimal("12.50"),
            status="SENT",
        )
        db.add(item)
        db.flush()
        preparation = Preparation(
            order_item_id=item.id,
            station_id=other.station.id,
            status="PENDING",
        )
        db.add(preparation)
        db.flush()
        ids = {
            "ck_products_station_restaurant": [misrouted.id],
            "ck_order_items_product_restaurant": [item.id],
            "ck_preparations_station_restaurant": [preparation.id],
        }
        restaurants = (own, other)
        db.close()

    tables = ("products", "order_items", "preparations")

    def rows():
        with migration_database.begin() as connection:
            return {
                table: connection.execute(
                    text(f"SELECT * FROM {table} ORDER BY id")
                ).all()
                for table in tables
            }

    before = rows()

    # env.py's fileConfig removes handlers from alembic's loggers, so the
    # report is captured on the logger object itself.
    reports = []
    logger = logging.getLogger("alembic.tenant_consistency")
    logger.warning = lambda message, *args: reports.append(message % args)
    try:
        migrate(migration_database, command.upgrade, "e1b7d3a9c4f2")
    finally:
        del logger.warning

    assert rows() == before
    reported = "\n".join(reports)
    for constraint, row_ids in ids.items():
        assert constraint in reported
        assert all(str(row_id) in reported for row_id in row_ids)

    module = SCRIPT.get_revision("e1b7d3a9c4f2").module
    with migration_database.begin() as connection:
        assert module.historical_violations(connection) == ids

    own, other = restaurants
    with migration_database.begin() as connection:
        # Unrelated updates of the existing rows keep working.
        connection.execute(
            text("UPDATE preparations SET status = 'CANCELLED' WHERE id = :id"),
            {"id": ids["ck_preparations_station_restaurant"][0]},
        )
        connection.execute(
            text("UPDATE order_items SET status = 'CANCELLED' WHERE id = :id"),
            {"id": ids["ck_order_items_product_restaurant"][0]},
        )
        connection.execute(
            text("UPDATE products SET name = 'Renamed' WHERE id = :id"),
            {"id": ids["ck_products_station_restaurant"][0]},
        )

    # New violations are rejected.
    with pytest.raises(IntegrityError) as error:
        with migration_database.begin() as connection:
            connection.execute(
                text("UPDATE products SET station_id = :s WHERE id = :p"),
                {"s": other.station.id, "p": own.product.id},
            )

    assert error.value.orig.diag.constraint_name == "ck_products_station_restaurant"

    migrate(migration_database, command.downgrade, "c5e8a2d4f6b1")

    with migration_database.begin() as connection:
        assert connection.scalar(
            text(
                "SELECT count(*) FROM pg_trigger "
                "WHERE tgname LIKE 'trg_%_restaurant'"
            )
        ) == 0
        # Without the triggers the same update is accepted again.
        connection.execute(
            text("UPDATE products SET station_id = :s WHERE id = :p"),
            {"s": other.station.id, "p": own.product.id},
        )
