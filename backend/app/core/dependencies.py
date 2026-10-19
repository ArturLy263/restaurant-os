from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from fastapi import Depends, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import ApiError, ErrorCode
from app.core.permissions import (
    Permission,
    permissions_for_role,
    station_type_for_role,
)
from app.db.dependencies import get_db
from app.db.models.role import Role
from app.db.models.station import Station
from app.db.models.station_assignment import StationAssignment
from app.db.models.user import User


security = HTTPBearer()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    return authenticate_token(db, credentials.credentials).user


@dataclass(frozen=True)
class AuthenticatedToken:
    user: User
    # The token's `exp` (rejected from then on); None if it has none.
    expires_at: datetime | None


def authenticate_token(db: Session, token: str) -> AuthenticatedToken:
    """The active user an access token belongs to.

    Raises ApiError 401 NOT_AUTHENTICATED for a missing, invalid, expired
    or unknown-user token, and 403 USER_INACTIVE for an inactive user.
    Shared by HTTP requests and WebSocket connections.
    """

    credentials_exception = ApiError(
        status_code=status.HTTP_401_UNAUTHORIZED,
        code=ErrorCode.NOT_AUTHENTICATED,
        message="Could not validate credentials",
        headers={
            "WWW-Authenticate": "Bearer",
        },
    )

    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )

        user_id = payload.get("sub")

        if user_id is None:
            raise credentials_exception

        user_uuid = UUID(user_id)

        # Past `exp` values were already rejected by jwt.decode.
        exp = payload.get("exp")
        expires_at = (
            None
            if exp is None
            else datetime.fromtimestamp(int(exp), tz=timezone.utc)
        )

    except (JWTError, ValueError, TypeError, OverflowError):
        raise credentials_exception

    user = db.get(User, user_uuid)

    if user is None:
        raise credentials_exception

    if not user.is_active:
        raise ApiError(
            status_code=status.HTTP_403_FORBIDDEN,
            code=ErrorCode.USER_INACTIVE,
            message="User is inactive",
        )

    return AuthenticatedToken(user=user, expires_at=expires_at)


@dataclass(frozen=True)
class AuthContext:
    """The authenticated user with their role and permissions."""

    user: User
    role: Role | None
    permissions: frozenset[Permission]
    # Stations whose preparations the user may work; None means all of the
    # restaurant's stations. Empty for users without stations.
    station_scope: frozenset[UUID] | None


def get_auth_context(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AuthContext:
    return build_auth_context(db, user)


def build_auth_context(db: Session, user: User) -> AuthContext:
    """The user's role, permissions and station scope, read now."""

    # The role is read from the database on every request (not from the
    # token's role_id claim), so a changed role applies immediately.
    role = db.get(Role, user.role_id)

    # A role of another restaurant is never honoured.
    if role is not None and role.restaurant_id != user.restaurant_id:
        role = None

    role_name = role.name if role is not None else None
    permissions = permissions_for_role(role_name)

    return AuthContext(
        user=user,
        role=role,
        permissions=permissions,
        station_scope=_station_scope(db, user, role_name, permissions),
    )


def _station_scope(
    db: Session,
    user: User,
    role_name: str | None,
    permissions: frozenset[Permission],
) -> frozenset[UUID] | None:
    if Permission.PREPARATIONS_ALL_STATIONS in permissions:
        return None

    station_type = station_type_for_role(role_name)

    # Fails closed: no station role, no stations.
    if station_type is None:
        return frozenset()

    # Assignments to another restaurant's, an inactive or another type of
    # station grant nothing.
    return frozenset(
        db.scalars(
            select(StationAssignment.station_id)
            .join(Station, Station.id == StationAssignment.station_id)
            .where(
                StationAssignment.user_id == user.id,
                Station.restaurant_id == user.restaurant_id,
                Station.type == station_type,
                Station.is_active.is_(True),
            )
        )
    )


def require_permission(permission: Permission) -> Callable[..., User]:
    """Dependency: the current user, if their role grants `permission`.

    Checked before the endpoint looks anything up, so a refusal reveals
    nothing about the requested resource.
    """

    def dependency(context: AuthContext = Depends(get_auth_context)) -> User:
        if permission not in context.permissions:
            raise ApiError(
                status_code=status.HTTP_403_FORBIDDEN,
                code=ErrorCode.PERMISSION_DENIED,
                message=f"Missing permission: {permission}",
            )

        return context.user

    return dependency
