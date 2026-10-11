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
from sqlalchemy import inspect, text

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
    """Tables, columns, keys, indexes and enum types in the public schema."""

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
