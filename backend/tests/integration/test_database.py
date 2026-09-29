from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.db.database import Base


ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"


def test_connected_to_isolated_test_database(db_session):
    database_name = db_session.scalar(text("SELECT current_database()"))

    assert database_name.endswith("_test")


def test_test_database_is_migrated_to_head(db_session):
    script = ScriptDirectory.from_config(
        Config(str(ALEMBIC_INI))
    )

    current_revision = db_session.scalar(
        text("SELECT version_num FROM alembic_version")
    )

    assert current_revision == script.get_current_head()


def test_migrations_create_every_model_table(db_session):
    tables = set(inspect(db_session.connection()).get_table_names())

    assert set(Base.metadata.tables) <= tables
