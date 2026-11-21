"""Safety checks of backend/seed_demo.py, without a database.

A fake session stands in for PostgreSQL: it answers current_database()
and the alembic_version query, and fails the test on anything else, so
these tests prove which statements run before the seed may write. No
test here opens a real connection.
"""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy.exc import IntegrityError

import seed_demo
from app.core.config import Settings
from seed_demo import (
    SeedConflict,
    SeedRefused,
    check_database_name,
    check_password,
    check_schema_version,
    expected_migration_heads,
    verify_connected_database,
)


GOOD_PASSWORD = "demo-pass-1234"
HEAD = "f00dfeedcafe"
# Shaped like a bcrypt hash; it must never reach the CLI's output.
FAKE_PASSWORD_HASH = "$2b$12$FakeHashForLeakTestOnlyxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


class FakeSession:
    def __init__(self, database="restaurant_os_demo", versions=(HEAD,), identity_error=None, version_error=None):
        self.database = database
        self.versions = versions
        self.identity_error = identity_error
        self.version_error = version_error
        self.queries: list[str] = []
        self.committed = False
        self.rolled_back = False

    def scalar(self, statement):
        sql = str(statement)
        self.queries.append(sql)
        if "current_database()" in sql:
            if self.identity_error is not None:
                raise self.identity_error
            return self.database
        raise AssertionError(f"unexpected query: {sql}")

    def scalars(self, statement):
        sql = str(statement)
        self.queries.append(sql)
        if "alembic_version" in sql:
            if self.version_error is not None:
                raise self.version_error
            return SimpleNamespace(all=lambda: list(self.versions))
        raise AssertionError(f"unexpected query: {sql}")

    def execute(self, *args, **kwargs):
        raise AssertionError("a refused run must not execute anything else")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    @contextmanager
    def begin_nested(self):
        yield

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class LockingSession(FakeSession):
    """Also accepts the advisory lock, the first statement of a permitted seed."""

    def execute(self, statement, params=None):
        sql = str(statement)
        assert "pg_advisory_xact_lock" in sql, f"unexpected statement: {sql}"
        self.queries.append(sql)


# --- Database name --------------------------------------------------------

@pytest.mark.parametrize("name", ["restaurant_os_dev", "restaurant_os_demo", "portfolio_demo", "r2_dev"])
def test_dev_and_demo_databases_are_accepted(name):
    assert check_database_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        None,
        "",
        "restaurant_os",
        "restaurant_os_test",
        "restaurant_os_dev_test",
        "restaurant_os_demo_test",
        "restaurant_os_dev_backup",
        "restaurant_os_development",
        "demo",
        "dev",
        "postgres",
        "_dev",
        "_demo",
        "Restaurant_OS_dev",
        "restaurant-os_dev",
        "restaurant os_demo",
        " restaurant_os_dev",
        "restaurant_os_dev\n",
        "restaurant_os_dev;drop",
        "restaurant_os_démo",
        42,
    ],
)
def test_every_other_database_is_refused(name):
    with pytest.raises(SeedRefused):
        check_database_name(name)


@pytest.mark.parametrize("name", ["restaurant_os_test", "restaurant_os_demo_test"])
def test_a_test_database_is_accepted_only_with_explicit_permission(name):
    with pytest.raises(SeedRefused, match="test databases are never seeded"):
        check_database_name(name)

    assert check_database_name(name, allow_test_database=True) == name


@pytest.mark.parametrize("name", ["restaurant_os", "postgres", "Restaurant_OS_test", "_test", None])
def test_test_permission_allows_nothing_else(name):
    with pytest.raises(SeedRefused):
        check_database_name(name, allow_test_database=True)


def test_test_permission_is_keyword_only():
    with pytest.raises(TypeError):
        check_database_name("restaurant_os_test", True)


# --- Connected database ---------------------------------------------------

def test_the_connected_database_must_be_the_configured_one():
    assert verify_connected_database(FakeSession("restaurant_os_demo"), "restaurant_os_demo") == "restaurant_os_demo"

    with pytest.raises(SeedRefused, match="connected to 'restaurant_os'"):
        verify_connected_database(FakeSession("restaurant_os"), "restaurant_os_demo")


@pytest.mark.parametrize("connected", [None, "", 42])
def test_an_unreadable_identity_is_refused(connected):
    with pytest.raises(SeedRefused, match="cannot verify"):
        verify_connected_database(FakeSession(connected), "restaurant_os_demo")


def test_a_failing_identity_query_is_refused():
    session = FakeSession(identity_error=RuntimeError("connection lost"))

    with pytest.raises(SeedRefused, match="cannot verify"):
        verify_connected_database(session, "restaurant_os_demo")


def test_a_connected_test_database_is_refused_even_when_configured():
    with pytest.raises(SeedRefused, match="test databases"):
        verify_connected_database(FakeSession("restaurant_os_test"), "restaurant_os_test")


# --- Schema version -------------------------------------------------------

def test_a_database_at_the_code_head_is_accepted():
    check_schema_version(FakeSession(versions=(HEAD,)), {HEAD})


@pytest.mark.parametrize("versions", [(), ("older",), (HEAD, "other")])
def test_a_database_at_another_version_is_refused(versions):
    with pytest.raises(SeedRefused, match="alembic upgrade head"):
        check_schema_version(FakeSession(versions=versions), {HEAD})


def test_an_unmigrated_database_is_refused():
    session = FakeSession(version_error=RuntimeError('relation "alembic_version" does not exist'))

    with pytest.raises(SeedRefused, match="cannot read the database's migration version"):
        check_schema_version(session, {HEAD})


def test_no_code_head_is_refused():
    with pytest.raises(SeedRefused):
        check_schema_version(FakeSession(), set())


def test_the_code_has_exactly_one_migration_head():
    # Read from the migration files; no database involved.
    assert len(expected_migration_heads()) == 1


# --- Password -------------------------------------------------------------

@pytest.mark.parametrize("password", [GOOD_PASSWORD, SecretStr(GOOD_PASSWORD), "x" * 72, "é" * 36])
def test_a_usable_password_is_accepted(password):
    assert check_password(password)


@pytest.mark.parametrize(
    "password",
    [None, "", "        ", "short", "change_me", "CHANGE_ME", "password", "x" * 73, "é" * 37, SecretStr("")],
)
def test_an_unusable_password_is_refused(password):
    with pytest.raises(SeedRefused) as refused:
        check_password(password)

    if isinstance(password, str) and len(password) > 3:
        assert password not in str(refused.value)


# --- seed_demo() called directly ------------------------------------------

@pytest.mark.parametrize(
    "session",
    [
        FakeSession("restaurant_os"),
        FakeSession("postgres"),
        FakeSession("restaurant_os_test"),
        FakeSession("Restaurant_OS_dev"),
        FakeSession(None),
        FakeSession(identity_error=RuntimeError("down")),
    ],
    ids=["original database", "postgres", "test database", "not strict", "identity unknown", "identity error"],
)
def test_a_direct_call_on_an_unsafe_database_refuses_before_locking(monkeypatch, session):
    monkeypatch.setattr(seed_demo, "_Seeder", _must_not_construct_seeder)

    with pytest.raises(SeedRefused):
        seed_demo.seed_demo(session, GOOD_PASSWORD)

    # Only the identity query ran: no advisory lock, no write.
    assert session.queries == ["SELECT current_database()"]


def test_a_direct_call_checks_the_password_before_the_database(monkeypatch):
    session = FakeSession("restaurant_os")
    monkeypatch.setattr(seed_demo, "_Seeder", _must_not_construct_seeder)

    with pytest.raises(SeedRefused, match="DEMO_PASSWORD"):
        seed_demo.seed_demo(session, "short")

    assert session.queries == []


@pytest.mark.parametrize("name", ["restaurant_os_dev", "restaurant_os_demo"])
def test_a_direct_call_on_a_dev_or_demo_database_locks_and_seeds(monkeypatch, name):
    _use_code_heads(monkeypatch)
    seeded = _record_seeder(monkeypatch)
    session = LockingSession(name)

    seed_demo.seed_demo(session, GOOD_PASSWORD)

    _assert_checked_once_then_locked(session)
    assert seeded == [True]


def test_a_direct_call_on_a_test_database_needs_explicit_permission(monkeypatch):
    _use_code_heads(monkeypatch)
    seeded = _record_seeder(monkeypatch)

    with pytest.raises(SeedRefused, match="test databases are never seeded"):
        seed_demo.seed_demo(LockingSession("restaurant_os_test"), GOOD_PASSWORD)
    assert seeded == []

    session = LockingSession("restaurant_os_test")
    seed_demo.seed_demo(session, GOOD_PASSWORD, allow_test_database=True)
    _assert_checked_once_then_locked(session)
    assert seeded == [True]


def test_a_direct_call_on_another_database_than_the_configured_one_is_refused(monkeypatch):
    _use_code_heads(monkeypatch)
    monkeypatch.setattr(seed_demo, "_Seeder", _must_not_construct_seeder)
    session = FakeSession("portfolio_demo")

    with pytest.raises(SeedRefused, match="DATABASE_URL names 'restaurant_os_demo'"):
        seed_demo.seed_demo(session, GOOD_PASSWORD, configured_database="restaurant_os_demo")

    assert session.queries == ["SELECT current_database()"]


# A database at another migration than the code's single head.
@pytest.mark.parametrize(
    "session",
    [
        FakeSession(version_error=RuntimeError('relation "alembic_version" does not exist')),
        FakeSession(versions=()),
        FakeSession(versions=("older",)),
        FakeSession(versions=(HEAD, "other")),
    ],
    ids=["migration table missing", "no version row", "behind head", "several heads in the database"],
)
def test_a_direct_call_on_a_database_at_another_migration_refuses_before_locking(monkeypatch, session):
    _use_code_heads(monkeypatch)
    monkeypatch.setattr(seed_demo, "_Seeder", _must_not_construct_seeder)

    with pytest.raises(SeedRefused, match="alembic upgrade head"):
        seed_demo.seed_demo(session, GOOD_PASSWORD)

    # FakeSession.execute fails the test, so neither the lock nor an insert ran.
    assert session.queries == ["SELECT current_database()", "SELECT version_num FROM alembic_version"]
    assert not session.committed


@pytest.mark.parametrize(
    ("heads", "message"),
    [
        (frozenset(), "the code has no Alembic head"),
        (frozenset({HEAD, "other"}), "several Alembic heads"),
    ],
    ids=["no code head", "several code heads"],
)
def test_a_direct_call_refuses_unless_the_code_has_one_head(monkeypatch, heads, message):
    _use_code_heads(monkeypatch, heads)
    monkeypatch.setattr(seed_demo, "_Seeder", _must_not_construct_seeder)
    session = FakeSession()

    with pytest.raises(SeedRefused, match=message):
        seed_demo.seed_demo(session, GOOD_PASSWORD)

    assert session.queries == ["SELECT current_database()"]


def test_a_direct_call_refuses_when_the_code_heads_cannot_be_read(monkeypatch):
    def unreadable():
        raise FileNotFoundError("alembic.ini")

    monkeypatch.setattr(seed_demo, "expected_migration_heads", unreadable)
    monkeypatch.setattr(seed_demo, "_Seeder", _must_not_construct_seeder)
    session = FakeSession()

    with pytest.raises(SeedRefused, match=r"cannot read the code's Alembic heads \(FileNotFoundError\)"):
        seed_demo.seed_demo(session, GOOD_PASSWORD)

    assert session.queries == ["SELECT current_database()"]


def test_test_permission_does_not_open_the_original_database(monkeypatch):
    monkeypatch.setattr(seed_demo, "_Seeder", _must_not_construct_seeder)
    session = FakeSession("restaurant_os")

    with pytest.raises(SeedRefused):
        seed_demo.seed_demo(session, GOOD_PASSWORD, allow_test_database=True)

    assert session.queries == ["SELECT current_database()"]


def test_seed_demo_test_permission_is_keyword_only():
    with pytest.raises(TypeError):
        seed_demo.seed_demo(FakeSession("restaurant_os_test"), GOOD_PASSWORD, True)


# --- Order of the checks (run) --------------------------------------------

def test_a_refused_database_name_runs_no_query(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(seed_demo, "seed_demo", _must_not_seed)

    with pytest.raises(SeedRefused):
        seed_demo.run(session, "restaurant_os", GOOD_PASSWORD)

    assert session.queries == []
    assert session.rolled_back and not session.committed


@pytest.mark.parametrize(
    "session",
    [
        FakeSession("restaurant_os"),
        FakeSession(identity_error=RuntimeError("down")),
        FakeSession(versions=("older",)),
        FakeSession(version_error=RuntimeError("no table")),
    ],
    ids=["other database", "identity unknown", "behind head", "not migrated"],
)
def test_a_failed_check_never_reaches_the_seed(monkeypatch, session):
    _use_code_heads(monkeypatch)
    monkeypatch.setattr(seed_demo, "_Seeder", _must_not_construct_seeder)

    with pytest.raises(SeedRefused):
        seed_demo.run(session, "restaurant_os_demo", GOOD_PASSWORD)

    assert session.rolled_back and not session.committed


def test_a_checked_run_commits_and_a_dry_run_rolls_back(monkeypatch):
    _use_code_heads(monkeypatch)
    _record_seeder(monkeypatch)

    session = LockingSession("restaurant_os_demo")
    seed_demo.run(session, "restaurant_os_demo", GOOD_PASSWORD)
    assert session.committed and not session.rolled_back

    dry = LockingSession("restaurant_os_demo")
    seed_demo.run(dry, "restaurant_os_demo", GOOD_PASSWORD, dry_run=True)
    assert dry.rolled_back and not dry.committed


def test_a_run_checks_the_database_once(monkeypatch):
    _use_code_heads(monkeypatch)
    _record_seeder(monkeypatch)
    session = LockingSession("restaurant_os_demo")

    seed_demo.run(session, "restaurant_os_demo", GOOD_PASSWORD)

    _assert_checked_once_then_locked(session)


# --- Command line ---------------------------------------------------------

def test_the_cli_refuses_a_missing_password_before_connecting(monkeypatch, capsys):
    monkeypatch.setattr(seed_demo, "DemoSeedSettings", lambda: SimpleNamespace(demo_password=None))
    _forbid_sessions(monkeypatch)

    assert seed_demo.main([]) == 2
    assert "DEMO_PASSWORD" in capsys.readouterr().err


def test_the_cli_refuses_the_test_database_before_connecting(monkeypatch, capsys):
    # The test environment's DATABASE_URL names the *_test database.
    secret = "a-secret-demo-password"
    monkeypatch.setattr(
        seed_demo, "DemoSeedSettings", lambda: SimpleNamespace(demo_password=SecretStr(secret))
    )
    _forbid_sessions(monkeypatch)

    assert seed_demo.main([]) == 2

    output = capsys.readouterr()
    assert "test databases are never seeded" in output.err
    assert secret not in output.out + output.err


def test_the_cli_refuses_the_original_database_before_connecting(monkeypatch, capsys):
    _cli_environment(monkeypatch, database="restaurant_os")
    _forbid_sessions(monkeypatch)

    assert seed_demo.main([]) == 2
    assert "refusing to seed 'restaurant_os'" in capsys.readouterr().err


def test_the_cli_exits_3_on_a_seed_conflict(monkeypatch, capsys):
    session = _cli_environment(monkeypatch)

    def conflict(session, password, **kwargs):
        raise SeedConflict(f"cashier2@{seed_demo.EMAIL_DOMAIN} exists in another restaurant than the demo restaurant.")

    monkeypatch.setattr(seed_demo, "seed_demo", conflict)

    assert seed_demo.main([]) == 3
    output = capsys.readouterr()
    assert "conflict, nothing written" in output.err
    assert output.out == ""
    assert session.rolled_back and not session.committed


def test_the_cli_never_prints_a_database_error_message(monkeypatch, capsys):
    session = _cli_environment(monkeypatch)
    error = IntegrityError(
        "INSERT INTO users (name, email, password_hash) VALUES (%(name)s, %(email)s, %(password_hash)s)",
        {"name": "Demo Admin", "email": seed_demo.ADMIN_EMAIL, "password_hash": FAKE_PASSWORD_HASH},
        Exception("duplicate key value violates unique constraint"),
    )
    assert FAKE_PASSWORD_HASH in str(error)  # what would leak if it were printed

    def fail(session, password, **kwargs):
        raise error

    monkeypatch.setattr(seed_demo, "seed_demo", fail)

    assert seed_demo.main([]) == 1
    output = capsys.readouterr()
    assert "IntegrityError" in output.err
    for leaked in (FAKE_PASSWORD_HASH, "$2b$", "password_hash", GOOD_PASSWORD):
        assert leaked not in output.out + output.err
    assert session.rolled_back and not session.committed


def test_the_cli_exits_130_on_ctrl_c_without_a_traceback(monkeypatch, capsys):
    session = _cli_environment(monkeypatch)

    def interrupted(session, password, **kwargs):
        # Ctrl+C while a database error is being handled: the interrupt's
        # exception chain then carries the error and its bound parameters.
        try:
            raise IntegrityError(
                "INSERT INTO users (email, password_hash) VALUES (%(email)s, %(password_hash)s)",
                {"email": seed_demo.ADMIN_EMAIL, "password_hash": FAKE_PASSWORD_HASH},
                Exception("duplicate key value violates unique constraint"),
            )
        except IntegrityError:
            raise KeyboardInterrupt

    monkeypatch.setattr(seed_demo, "seed_demo", interrupted)

    assert seed_demo.main([]) == 130
    output = capsys.readouterr()
    assert output.err == "seed_demo: interrupted; an unfinished run is rolled back.\n"
    assert output.out == ""
    for leaked in (FAKE_PASSWORD_HASH, "$2b$", "password_hash", "INSERT", "Traceback", GOOD_PASSWORD):
        assert leaked not in output.out + output.err
    assert session.rolled_back and not session.committed


# --- Retired seed.py ------------------------------------------------------

def test_the_application_needs_no_admin_password():
    assert "admin_password" not in Settings.model_fields

    Settings(
        _env_file=None,
        database_url="postgresql+psycopg://nobody:nothing@127.0.0.1:9/unreachable_test",
        jwt_secret_key="test",
    )


def _must_not_seed(session, password):
    raise AssertionError("the seed ran although a check failed")


def _use_code_heads(monkeypatch, heads=frozenset({HEAD})):
    """The code's Alembic heads as seed_demo() reads them (no files read)."""

    monkeypatch.setattr(seed_demo, "expected_migration_heads", lambda: heads)


def _assert_checked_once_then_locked(session):
    """Identity and migration were each queried once, then the lock was taken."""

    assert session.queries[:2] == ["SELECT current_database()", "SELECT version_num FROM alembic_version"]
    assert "pg_advisory_xact_lock" in session.queries[2]
    assert len(session.queries) == 3


def _must_not_construct_seeder(*args, **kwargs):
    raise AssertionError("the seeder ran although the database was refused")


def _record_seeder(monkeypatch) -> list[bool]:
    seeded: list[bool] = []

    class RecordingSeeder:
        def __init__(self, session, password, report):
            pass

        def run(self):
            seeded.append(True)

    monkeypatch.setattr(seed_demo, "_Seeder", RecordingSeeder)
    return seeded


def _cli_environment(monkeypatch, database="restaurant_os_demo") -> FakeSession:
    """main() with a usable password, `database` as DATABASE_URL's name and a
    fake session at the code head; no real engine or connection is used."""

    import app.db.database

    session = FakeSession(database)
    monkeypatch.setattr(
        seed_demo, "DemoSeedSettings", lambda: SimpleNamespace(demo_password=SecretStr(GOOD_PASSWORD))
    )
    monkeypatch.setattr(app.db.database, "engine", SimpleNamespace(url=SimpleNamespace(database=database)))
    monkeypatch.setattr(app.db.database, "SessionLocal", lambda: session)
    _use_code_heads(monkeypatch)
    return session


def _forbid_sessions(monkeypatch):
    import app.db.database

    def refuse():
        raise AssertionError("a session was opened")

    monkeypatch.setattr(app.db.database, "SessionLocal", refuse)
