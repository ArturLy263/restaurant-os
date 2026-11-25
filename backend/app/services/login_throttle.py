"""Login throttling shared by every API process (rows in login_throttles).

Failed sign-ins are counted per key, a normalized email and the client
address, within `login_window_seconds`. When a key reaches its limit it is
blocked for `login_block_base_seconds`, doubling with every further failure
up to `login_block_max_seconds`; the block then lapses on its own, so an
attacker can delay an account's sign-in but never lock it permanently. A
successful sign-in clears the email's counter.

Keys are stored as sha256 hashes, so the table holds no email or address.
The client address is the one the ASGI server reports: behind a reverse
proxy, run uvicorn with --proxy-headers and --forwarded-allow-ips set to
that proxy only (see docs/security.md), never trusting X-Forwarded-For
from arbitrary clients.
"""

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models.auth_security import LoginThrottle


@dataclass(frozen=True)
class ThrottleKey:
    kind: str  # "email" or "ip"
    value: str
    limit: int

    @property
    def hash(self) -> str:
        return hashlib.sha256(f"{self.kind}:{self.value}".encode("utf-8")).hexdigest()


def normalize_email(email: str) -> str:
    """The form emails are compared and throttled in: trimmed, lower case
    (so changing capitalization neither finds another account nor escapes
    the throttle)."""

    return email.strip().lower()


def keys_for(email: str, client_address: str | None, config: Settings) -> list[ThrottleKey]:
    return [
        ThrottleKey("email", normalize_email(email), config.login_max_failures_per_account),
        ThrottleKey("ip", client_address or "unknown", config.login_max_failures_per_ip),
    ]


def retry_after_seconds(db: Session, keys: list[ThrottleKey], now: datetime) -> int:
    """Seconds until every key may try again; 0 when none is blocked."""

    blocked_until = db.scalars(
        select(LoginThrottle.blocked_until).where(
            LoginThrottle.key_hash.in_([key.hash for key in keys]),
            LoginThrottle.blocked_until > now,
        )
    ).all()
    if not blocked_until:
        return 0
    return max(1, math.ceil((max(blocked_until) - now).total_seconds()))


def record_failure(db: Session, keys: list[ThrottleKey], now: datetime, config: Settings) -> None:
    """Counts one failed sign-in for every key (row-locked, so concurrent
    failures are all counted)."""

    window = timedelta(seconds=config.login_window_seconds)
    for key in keys:
        db.execute(
            insert(LoginThrottle)
            .values(key_hash=key.hash, failures=0, window_started_at=now, updated_at=now)
            .on_conflict_do_nothing(index_elements=[LoginThrottle.key_hash])
        )
        row = db.scalar(
            select(LoginThrottle).where(LoginThrottle.key_hash == key.hash).with_for_update()
        )
        if row.window_started_at + window <= now and (row.blocked_until is None or row.blocked_until <= now):
            # A new window: earlier failures no longer count.
            row.failures = 0
            row.window_started_at = now
            row.blocked_until = None
        row.failures += 1
        row.updated_at = now
        if row.failures >= key.limit:
            over = row.failures - key.limit
            delay = min(
                config.login_block_base_seconds * (2 ** min(over, 20)),
                config.login_block_max_seconds,
            )
            row.blocked_until = now + timedelta(seconds=delay)
    db.flush()


def record_success(db: Session, email_key: ThrottleKey, now: datetime, window_seconds: int) -> None:
    """Clears the account's counter; also purges rows idle for a day."""

    db.execute(delete(LoginThrottle).where(LoginThrottle.key_hash == email_key.hash))
    stale = now - timedelta(seconds=max(window_seconds, 86400))
    db.execute(
        delete(LoginThrottle).where(
            LoginThrottle.updated_at < stale,
            (LoginThrottle.blocked_until.is_(None)) | (LoginThrottle.blocked_until < now),
        )
    )
    db.flush()
