from sqlalchemy.engine import make_url


TEST_DATABASE_SUFFIX = "_test"


def resolve_test_database_url(
    dev_database_url: str | None,
    test_database_url: str | None,
) -> str:
    """
    Return the URL of the isolated test database.

    Uses TEST_DATABASE_URL when given, otherwise derives it from the
    development DATABASE_URL by appending "_test" to the database name.
    Refuses any URL that could point at the development database.
    """

    if test_database_url:
        url = make_url(test_database_url)
    elif dev_database_url:
        dev_url = make_url(dev_database_url)
        url = dev_url.set(
            database=f"{dev_url.database}{TEST_DATABASE_SUFFIX}"
        )
    else:
        raise RuntimeError(
            "Cannot configure tests: set TEST_DATABASE_URL, "
            "or DATABASE_URL in .env to derive it from."
        )

    if not url.database or not url.database.endswith(TEST_DATABASE_SUFFIX):
        raise RuntimeError(
            f"Refusing to run tests against database '{url.database}': "
            f"the test database name must end with '{TEST_DATABASE_SUFFIX}'."
        )

    if dev_database_url:
        dev_url = make_url(dev_database_url)

        if (
            dev_url.host == url.host
            and dev_url.port == url.port
            and dev_url.database == url.database
        ):
            raise RuntimeError(
                "Refusing to run tests: the test database is the same "
                "as the development database."
            )

    return url.render_as_string(hide_password=False)
