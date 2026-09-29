import pytest

from tests.db_url import resolve_test_database_url


DEV_URL = "postgresql+psycopg://user:secret@localhost:5433/restaurant_os"


def test_derives_test_database_from_dev_url():
    url = resolve_test_database_url(DEV_URL, None)

    assert url == (
        "postgresql+psycopg://user:secret@localhost:5433/restaurant_os_test"
    )


def test_explicit_test_database_url_wins():
    explicit = "postgresql+psycopg://user:secret@db:5432/other_test"

    assert resolve_test_database_url(DEV_URL, explicit) == explicit


def test_rejects_database_without_test_suffix():
    with pytest.raises(RuntimeError, match="must end with '_test'"):
        resolve_test_database_url(DEV_URL, DEV_URL)


def test_rejects_test_url_equal_to_dev_url():
    dev_url = "postgresql+psycopg://user:secret@localhost:5433/app_test"

    with pytest.raises(RuntimeError, match="same as the development"):
        resolve_test_database_url(dev_url, dev_url)


def test_requires_some_database_url():
    with pytest.raises(RuntimeError, match="Cannot configure tests"):
        resolve_test_database_url(None, None)
