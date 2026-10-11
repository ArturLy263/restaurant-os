"""
The test-suite safety net itself: strict markers, automatic test
classification, REQUIRE_TEST_DATABASE and disposable database names.

The pytest runs happen in subprocesses pointed at a port nobody listens
on, so these tests need no PostgreSQL.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.disposable_database import UnsafeDatabaseTarget, assert_disposable_name


REPO_ROOT = Path(__file__).resolve().parents[3]
UNREACHABLE_TEST_DATABASE_URL = (
    "postgresql+psycopg://nobody:nothing@127.0.0.1:9/unreachable_test"
)
DATABASE_TEST = (
    "backend/tests/integration/test_database.py"
    "::test_connected_to_isolated_test_database"
)


def run_pytest(*args, **env):
    # conftest.py replaced DATABASE_URL with the test database URL in this
    # process; the child must not mistake it for the development database.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PYTEST_") and key != "DATABASE_URL"
    }
    environment["TEST_DATABASE_URL"] = UNREACHABLE_TEST_DATABASE_URL
    environment.update(env)

    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_database_tests_are_skipped_without_postgres_by_default():
    result = run_pytest(DATABASE_TEST, REQUIRE_TEST_DATABASE="0")

    assert result.returncode == 0, result.stdout
    assert "1 skipped" in result.stdout


def test_required_test_database_fails_when_postgres_is_unavailable():
    result = run_pytest(DATABASE_TEST, REQUIRE_TEST_DATABASE="1")

    # The database fixture fails, so pytest reports a setup error.
    assert result.returncode == pytest.ExitCode.TESTS_FAILED, result.stdout
    assert "1 error" in result.stdout
    assert "PostgreSQL test server unavailable" in result.stdout


@pytest.mark.parametrize(
    ("required", "exit_code", "outcome"),
    [
        ("0", pytest.ExitCode.OK, "1 skipped"),
        ("1", pytest.ExitCode.TESTS_FAILED, "1 error"),
    ],
)
def test_any_skipped_database_test_fails_when_required(
    tmp_path, required, exit_code, outcome
):
    # A database test skipped for any reason, not only an unreachable server.
    test_file = tmp_path / "test_skipping_database_test.py"
    test_file.write_text(
        "import pytest\n\n"
        "@pytest.fixture\n"
        "def db_engine():\n"
        "    pytest.skip('simulated skip')\n\n"
        "def test_database(db_engine):\n"
        "    pass\n"
    )

    result = run_pytest(
        "-c", str(REPO_ROOT / "pytest.ini"),
        "--rootdir", str(REPO_ROOT),
        "-p", "tests.conftest",
        str(test_file),
        REQUIRE_TEST_DATABASE=required,
    )

    assert result.returncode == exit_code, result.stdout
    assert outcome in result.stdout
    assert ("does not allow database tests to be skipped" in result.stdout) is (
        required == "1"
    )


def test_invalid_require_test_database_value_is_rejected():
    result = run_pytest(DATABASE_TEST, REQUIRE_TEST_DATABASE="yes")

    assert result.returncode != 0
    assert "REQUIRE_TEST_DATABASE must be" in result.stdout + result.stderr


def test_misspelled_marker_fails_collection(tmp_path):
    test_file = tmp_path / "test_typo.py"
    test_file.write_text(
        "import pytest\n\n"
        "@pytest.mark.integraton\n"
        "def test_typo():\n"
        "    pass\n"
    )

    result = run_pytest(
        "-c", str(REPO_ROOT / "pytest.ini"),
        "--rootdir", str(REPO_ROOT),
        str(test_file),
    )

    assert result.returncode == pytest.ExitCode.INTERRUPTED, result.stdout
    assert "'integraton' not found in `markers`" in result.stdout


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("unit", "backend/tests/api/test_health.py::test_health"),
        ("integration", DATABASE_TEST),
        (
            "migrations",
            "backend/tests/migrations/test_migrations.py"
            "::test_models_match_migrations",
        ),
    ],
)
def test_tests_are_classified_by_their_database_fixtures(marker, expected):
    result = run_pytest("--collect-only", "-q", "-m", marker)

    assert result.returncode == 0, result.stdout
    assert expected in result.stdout.splitlines()


def test_unit_selection_excludes_database_tests():
    result = run_pytest("--collect-only", "-q", "-m", "unit")

    collected = result.stdout.splitlines()

    assert result.returncode == 0, result.stdout
    assert DATABASE_TEST not in collected
    assert (
        "backend/tests/migrations/test_migrations.py"
        "::test_models_match_migrations"
    ) not in collected
    # Needs only the migration scripts, not a database.
    assert (
        "backend/tests/migrations/test_migrations.py"
        "::test_migration_history_has_a_single_head"
    ) in collected


@pytest.mark.parametrize(
    "name",
    [
        "restaurant_os",
        "restaurant_os_test",
        "postgres",
        "restaurantos_disposable_0123456789abcdef",
        "restaurantos_disposable_0123456789ABCDEF_test",
        'restaurantos_disposable_0123456789abcdef_test"; DROP DATABASE x; --',
    ],
)
def test_only_generated_disposable_names_are_accepted(name):
    with pytest.raises(UnsafeDatabaseTarget):
        assert_disposable_name(name, protected_urls=[])


def test_protected_database_names_are_refused():
    name = "restaurantos_disposable_0123456789abcdef_test"

    assert_disposable_name(name, protected_urls=[None])

    with pytest.raises(UnsafeDatabaseTarget, match="protected"):
        assert_disposable_name(
            name,
            protected_urls=[f"postgresql+psycopg://u:p@localhost:5433/{name}"],
        )
