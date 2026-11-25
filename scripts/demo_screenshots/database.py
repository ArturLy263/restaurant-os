"""Disposable demo database for the README screenshots.

Started by frontend/e2e/screenshots.mjs; not meant to be run by hand.

1. Creates a fresh database with tests/disposable_database.py (random name
   ending in _test, comment token; the development and test databases are
   refused), on the server of DATABASE_URL.
2. Migrates it to head and loads the demo restaurant with
   backend/seed_demo.py (seed_demo(..., allow_test_database=True), the
   same call the integration tests use).
3. Prints one JSON line {"ready": {"database": ..., "url": ...}}.
4. Drops the database when stdin closes (also when the runner crashes).

The demo password comes from SCREENSHOTS_PASSWORD and is never printed.
No other database is written.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "tests"))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from dotenv import dotenv_values  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from disposable_database import disposable_database  # noqa: E402


def main() -> None:
    password = os.environ["SCREENSHOTS_PASSWORD"]
    env = dotenv_values(ROOT / ".env")
    server_url = os.environ.get("DATABASE_URL") or env.get("DATABASE_URL")
    test_url = os.environ.get("TEST_DATABASE_URL") or env.get("TEST_DATABASE_URL")
    if not server_url:
        sys.exit("DATABASE_URL is not set (.env)")

    with disposable_database(
        server_url=server_url,
        protected_urls=[server_url, test_url or "", ""],
    ) as database:
        url = database.engine.url.render_as_string(hide_password=False)
        # The app's settings (and so seed_demo's imports) use this database.
        os.environ["DATABASE_URL"] = url

        with database.begin() as connection:
            config = Config(str(ROOT / "alembic.ini"))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

        from seed_demo import seed_demo  # noqa: E402  (after DATABASE_URL is set)

        with database.begin() as connection:
            with Session(bind=connection) as session:
                seed_demo(session, password, allow_test_database=True)
                session.flush()

        print(json.dumps({"ready": {"database": database.name, "url": url}}), flush=True)
        # Kept until the runner closes stdin.
        for _ in sys.stdin:
            pass


if __name__ == "__main__":
    main()
