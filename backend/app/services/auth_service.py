from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import status
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ApiError, ErrorCode
from app.core.security import (
    access_token_for,
    verify_password,
    verify_password_for_unknown_user,
)
from app.db.models.auth_security import RevokedToken
from app.db.models.user import User
from app.db.unit_of_work import commit
from app.services import login_throttle


INVALID_CREDENTIALS_MESSAGE = "Invalid email or password"
TOO_MANY_ATTEMPTS_MESSAGE = "Too many sign-in attempts. Try again later."


def _utcnow() -> datetime:
    # Naive UTC, like every timestamp column.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AuthService:
    @staticmethod
    def login(
        db: Session,
        email: str,
        password: str,
        client_address: str | None,
        config: Settings,
    ) -> str:
        """A new access token, or ApiError 401 INVALID_CREDENTIALS (the same
        for an unknown email, a wrong password and an inactive account) or
        429 TOO_MANY_LOGIN_ATTEMPTS (keyed on what was submitted, so it says
        nothing about whether the account exists)."""

        now = _utcnow()
        keys = login_throttle.keys_for(email, client_address, config)

        if config.login_throttle_enabled:
            retry_after = login_throttle.retry_after_seconds(db, keys, now)
            if retry_after:
                raise ApiError(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    code=ErrorCode.TOO_MANY_LOGIN_ATTEMPTS,
                    message=TOO_MANY_ATTEMPTS_MESSAGE,
                    headers={"Retry-After": str(retry_after)},
                )

        normalized = login_throttle.normalize_email(email)
        users = db.scalars(
            select(User)
            .where(func.lower(User.email) == normalized, User.is_active.is_(True))
            .limit(2)
        ).all()
        # Exactly one account, else none: never guess between two emails
        # that differ only in capitalization.
        user = users[0] if len(users) == 1 else None

        if user is None:
            verified = verify_password_for_unknown_user(password)
        else:
            verified = verify_password(password, user.password_hash)

        if not verified:
            if config.login_throttle_enabled:
                login_throttle.record_failure(db, keys, now, config)
                commit(db)
            raise ApiError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                code=ErrorCode.INVALID_CREDENTIALS,
                message=INVALID_CREDENTIALS_MESSAGE,
            )

        if config.login_throttle_enabled:
            login_throttle.record_success(db, keys[0], now, config.login_window_seconds)
        token = access_token_for(user)
        commit(db)
        return token

    @staticmethod
    def revoke(db: Session, user_id: UUID, jti: UUID, expires_at: datetime | None, config: Settings) -> None:
        """Ends one access token (sign-out) until it would have expired."""

        now = _utcnow()
        until = (
            expires_at.astimezone(timezone.utc).replace(tzinfo=None)
            if expires_at is not None
            else now + timedelta(minutes=config.access_token_expire_minutes)
        )
        db.execute(delete(RevokedToken).where(RevokedToken.expires_at < now))
        if db.get(RevokedToken, jti) is None:
            db.add(RevokedToken(jti=jti, user_id=user_id, expires_at=until))
        commit(db)
