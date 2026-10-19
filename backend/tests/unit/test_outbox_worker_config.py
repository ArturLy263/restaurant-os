"""Outbox worker configuration and process safety; no database needed.

Subprocesses get an unreachable DATABASE_URL, so nothing here can connect
anywhere.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.workers.outbox_worker import OutboxWorker


BACKEND = Path(__file__).resolve().parents[2]
UNREACHABLE_DATABASE_URL = "postgresql+psycopg://nobody:nothing@127.0.0.1:9/unreachable_test"

REQUIRED = {
    "database_url": UNREACHABLE_DATABASE_URL,
    "jwt_secret_key": "test",
    "admin_password": "test",
}


def isolated_env(**extra):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("OUTBOX_")
    }
    env.update(DATABASE_URL=UNREACHABLE_DATABASE_URL, **extra)
    return env


def test_defaults_are_safe():
    config = Settings(**REQUIRED)

    assert config.outbox_worker_batch_size == 100
    assert config.outbox_worker_poll_interval_seconds == 1.0
    assert config.outbox_worker_restaurant_id is None
    assert config.outbox_max_attempts == 10
    assert config.outbox_publish_timeout_seconds == 30.0
    assert config.outbox_retention_days == 7
    assert config.outbox_cleanup_batch_size == 1000


@pytest.mark.parametrize(
    "name, value",
    [
        ("outbox_worker_batch_size", 0),
        ("outbox_worker_batch_size", 1001),
        ("outbox_worker_poll_interval_seconds", 0),
        ("outbox_worker_poll_interval_seconds", -1),
        ("outbox_worker_poll_interval_seconds", 301),
        ("outbox_worker_restaurant_id", "not-a-uuid"),
        ("outbox_max_attempts", 0),
        ("outbox_max_attempts", 101),
        ("outbox_publish_timeout_seconds", 0),
        ("outbox_publish_timeout_seconds", -5),
        ("outbox_publish_timeout_seconds", 301),
        ("outbox_retention_days", 0),
        ("outbox_retention_days", 3651),
        ("outbox_cleanup_batch_size", 0),
        ("outbox_cleanup_batch_size", 10001),
    ],
)
def test_unsafe_values_are_rejected(name, value):
    with pytest.raises(ValidationError, match=name):
        Settings(**REQUIRED, **{name: value})


def test_values_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("OUTBOX_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("OUTBOX_PUBLISH_TIMEOUT_SECONDS", "2.5")

    config = Settings(**REQUIRED)

    assert config.outbox_max_attempts == 3
    assert config.outbox_publish_timeout_seconds == 2.5

    monkeypatch.setenv("OUTBOX_MAX_ATTEMPTS", "0")
    with pytest.raises(ValidationError):
        Settings(**REQUIRED)


@pytest.mark.parametrize(
    "options",
    [
        {"batch_size": 0},
        {"max_attempts": 0},
        {"publish_timeout": 0},
        {"publish_timeout": -1},
    ],
)
def test_worker_rejects_unsafe_values(options):
    with pytest.raises(ValueError):
        OutboxWorker(None, None, **options)


def test_runner_refuses_to_start_with_an_unsafe_setting():
    result = subprocess.run(
        [sys.executable, "-m", "app.workers.outbox_worker"],
        cwd=BACKEND,
        env=isolated_env(OUTBOX_PUBLISH_TIMEOUT_SECONDS="0"),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode != 0
    assert "outbox_publish_timeout_seconds" in result.stderr
    assert "Outbox worker started" not in result.stderr


def test_runner_rejects_unknown_commands():
    result = subprocess.run(
        [sys.executable, "-m", "app.workers.outbox_worker", "purge-everything"],
        cwd=BACKEND,
        env=isolated_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 2
    assert "invalid choice" in result.stderr


def test_importing_the_application_starts_no_worker():
    check = (
        "import signal, threading\n"
        "import app.main, app.workers.outbox_worker\n"
        "assert threading.active_count() == 1, threading.enumerate()\n"
        "assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL\n"
        "assert signal.getsignal(signal.SIGINT) is signal.default_int_handler\n"
        "print('idle')\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", check],
        cwd=BACKEND,
        env=isolated_env(JWT_SECRET_KEY="test", ADMIN_PASSWORD="test"),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "idle"


def test_importing_the_application_starts_no_worker_even_when_enabled():
    """Station delivery starts with the server's lifespan, not on import."""

    check = (
        "import threading\n"
        "import app.main\n"
        "assert app.main.settings.outbox_websocket_delivery is True\n"
        "assert threading.active_count() == 1, threading.enumerate()\n"
        "print('idle')\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", check],
        cwd=BACKEND,
        env=isolated_env(
            JWT_SECRET_KEY="test",
            ADMIN_PASSWORD="test",
            OUTBOX_WEBSOCKET_DELIVERY="true",
        ),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "idle"


def test_standalone_runner_refuses_to_deliver_when_the_api_does():
    result = subprocess.run(
        [sys.executable, "-m", "app.workers.outbox_worker", "run"],
        cwd=BACKEND,
        env=isolated_env(OUTBOX_WEBSOCKET_DELIVERY="true"),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 2
    assert "OUTBOX_WEBSOCKET_DELIVERY is enabled" in result.stderr
    assert "Outbox worker started" not in result.stderr
