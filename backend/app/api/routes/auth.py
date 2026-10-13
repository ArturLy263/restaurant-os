from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.errors import ApiError, ErrorCode
from app.db.dependencies import get_db
from app.schemas.auth import LoginRequest, TokenResponse
from app.services.auth_service import AuthService


router = APIRouter(
    prefix="/api/v1/auth",
    tags=["auth"],
)


@router.post(
    "/login",
    response_model=TokenResponse,
)
def login(
    request: LoginRequest,
    db: Session = Depends(get_db),
):
    try:
        token = AuthService.login(
            db=db,
            email=request.email,
            password=request.password,
        )
    except ValueError:
        raise ApiError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code=ErrorCode.INVALID_CREDENTIALS,
            message="Invalid email or password",
        )

    return TokenResponse(
        access_token=token,
    )