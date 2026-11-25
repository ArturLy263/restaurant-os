import uuid
from datetime import datetime, timedelta, timezone
from functools import cache
from uuid import UUID

import bcrypt
from jose import jwt

from app.core.config import settings


def hash_password(password: str) -> str:
    password_bytes = password.encode("utf-8")

    if len(password_bytes) > 72:
        raise ValueError("Password cannot be longer than 72 bytes")

    hashed = bcrypt.hashpw(
        password_bytes,
        bcrypt.gensalt(),
    )

    return hashed.decode("utf-8")


def verify_password(
    plain_password: str,
    password_hash: str,
) -> bool:
    password_bytes = plain_password.encode("utf-8")

    if len(password_bytes) > 72:
        # Same cost as a real check, so length does not shortcut timing.
        bcrypt.checkpw(b"", _dummy_hash())
        return False

    return bcrypt.checkpw(
        password_bytes,
        password_hash.encode("utf-8"),
    )


@cache
def _dummy_hash() -> bytes:
    """A bcrypt hash of a random value, computed once per process with the
    same cost as real hashes; checked against for unknown accounts."""

    return bcrypt.hashpw(uuid.uuid4().bytes, bcrypt.gensalt())


def verify_password_for_unknown_user(plain_password: str) -> bool:
    """Spends the time of a real password check, then fails: sign-in takes
    as long for an unknown email as for a wrong password (no account
    enumeration by timing)."""

    bcrypt.checkpw(plain_password.encode("utf-8")[:72], _dummy_hash())
    return False


def create_access_token(
    user_id: UUID,
    restaurant_id: UUID,
    role_id: UUID,
    token_version: int,
) -> str:
    """A signed access token. `ver` must equal the user's current
    token_version and `jti` must not be revoked for it to be accepted
    (app.core.dependencies.authenticate_token)."""

    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.access_token_expire_minutes
    )

    payload = {
        "sub": str(user_id),
        "restaurant_id": str(restaurant_id),
        "role_id": str(role_id),
        "ver": token_version,
        "jti": str(uuid.uuid4()),
        "exp": expire,
    }

    return jwt.encode(
        payload,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def access_token_for(user) -> str:
    return create_access_token(
        user_id=user.id,
        restaurant_id=user.restaurant_id,
        role_id=user.role_id,
        token_version=user.token_version,
    )
