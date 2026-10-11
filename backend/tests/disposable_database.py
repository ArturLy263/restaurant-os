"""Disposable PostgreSQL databases for destructive migration tests.

Each database is created under a fresh random name and tagged with a random
token in its comment. Before every destructive step (migrating, dropping)
the effective target is re-checked against both the name and the token, so
only a database created by this module in this process can be affected —
never the development database or the shared test database.
"""

import re
import secrets
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool


NAME_PREFIX = "restaurantos_disposable_"
NAME_PATTERN = re.compile(r"restaurantos_disposable_[0-9a-f]{16}_test")
COMMENT_PREFIX = "RestaurantOS disposable migration test database "

_DATABASE_COMMENT = text(
    "SELECT shobj_description(oid, 'pg_database') "
    "FROM pg_database WHERE datname = :name"
)


class UnsafeDatabaseTarget(RuntimeError):
    """Raised instead of touching a database that is not provably disposable."""


def assert_disposable_name(name: str, protected_urls: Iterable[str]) -> None:
    if not NAME_PATTERN.fullmatch(name):
        raise UnsafeDatabaseTarget(
            f"'{name}' is not a disposable test database name."
        )

    for url in protected_urls:
        if url and make_url(url).database == name:
            raise UnsafeDatabaseTarget(
                f"'{name}' is a protected database (development or test)."
            )


@dataclass(frozen=True)
class DisposableDatabase:
    engine: Engine
    name: str
    token: str

    @contextmanager
    def begin(self) -> Iterator[Connection]:
        """A transaction on a connection verified to reach this database."""

        with self.engine.begin() as connection:
            self.verify(connection)
            yield connection

    def verify(self, connection: Connection) -> None:
        current = connection.scalar(text("SELECT current_database()"))
        comment = connection.scalar(_DATABASE_COMMENT, {"name": current})

        if current != self.name or comment != COMMENT_PREFIX + self.token:
            raise UnsafeDatabaseTarget(
                f"Connected to '{current}', which is not the disposable "
                f"database '{self.name}' created by this test run."
            )


@contextmanager
def disposable_database(
    server_url: str,
    protected_urls: Iterable[str],
) -> Iterator[DisposableDatabase]:
    """Create an empty database on `server_url`'s server, drop it afterwards."""

    protected_urls = list(protected_urls)
    name = f"{NAME_PREFIX}{secrets.token_hex(8)}_test"
    token = secrets.token_hex(16)
    assert_disposable_name(name, protected_urls)

    url = make_url(server_url)
    maintenance = create_engine(
        url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    engine = None
    created = False

    try:
        with maintenance.connect() as connection:
            # Fails if the name is taken, so an existing database is never
            # adopted (and later dropped).
            connection.execute(text(f'CREATE DATABASE "{name}"'))
            created = True
            connection.execute(
                text(
                    f'COMMENT ON DATABASE "{name}" '
                    f"IS '{COMMENT_PREFIX}{token}'"
                )
            )

        engine = create_engine(url.set(database=name), poolclass=NullPool)
        database = DisposableDatabase(engine=engine, name=name, token=token)

        with database.begin() as connection:
            tables = connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            )

        if tables:
            raise UnsafeDatabaseTarget(f"New database '{name}' is not empty.")

        yield database
    finally:
        if engine is not None:
            engine.dispose()

        try:
            if created:
                with maintenance.connect() as connection:
                    _drop(connection, name, token, protected_urls)
        finally:
            maintenance.dispose()


def _drop(
    connection: Connection,
    name: str,
    token: str,
    protected_urls: list[str],
) -> None:
    assert_disposable_name(name, protected_urls)

    comment = connection.scalar(_DATABASE_COMMENT, {"name": name})

    if comment != COMMENT_PREFIX + token:
        raise UnsafeDatabaseTarget(
            f"Refusing to drop '{name}': it is not tagged as the "
            "disposable database created by this test run."
        )

    connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
