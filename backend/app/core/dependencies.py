from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import ApiError, ErrorCode
from app.core.permissions import Permission, permissions_for_role
from app.db.dependencies import get_db
from app.db.models.role import Role
from app.db.models.user import User


security = HTTPBearer()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> User:

    credentials_exception = ApiError(
        status_code=status.HTTP_401_UNAUTHORIZED,
        code=ErrorCode.NOT_AUTHENTICATED,
        message="Could not validate credentials",
        headers={
            "WWW-Authenticate": "Bearer",
        },
    )

    token = credentials.credentials

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

    except (JWTError, ValueError):
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

    return user


@dataclass(frozen=True)
class AuthContext:
    """The authenticated user with their role and permissions."""

    user: User
    role: Role | None
    permissions: frozenset[Permission]


def get_auth_context(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AuthContext:
    # The role is read from the database on every request (not from the
    # token's role_id claim), so a changed role applies immediately.
    role = db.get(Role, user.role_id)

    # A role of another restaurant is never honoured.
    if role is not None and role.restaurant_id != user.restaurant_id:
        role = None

    return AuthContext(
        user=user,
        role=role,
        permissions=permissions_for_role(
            role.name if role is not None else None
        ),
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
