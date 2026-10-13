from uuid import UUID

from fastapi import Depends, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import ApiError, ErrorCode
from app.db.dependencies import get_db
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