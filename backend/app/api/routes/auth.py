from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.dependencies import authenticate_token, security
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
    http_request: Request,
    db: Session = Depends(get_db),
):
    """Sign in. 401 INVALID_CREDENTIALS for any wrong combination; 429
    TOO_MANY_LOGIN_ATTEMPTS (with Retry-After) while the email or the client
    address is throttled."""

    client = http_request.client
    token = AuthService.login(
        db=db,
        email=request.email,
        password=request.password,
        client_address=client.host if client else None,
        config=settings,
    )

    return TokenResponse(
        access_token=token,
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
)
def logout(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    """Revoke the access token this request carries: it is refused from
    now on, everywhere (HTTP and station streams). Other sessions of the
    user stay valid; a password change ends them all."""

    authenticated = authenticate_token(db, credentials.credentials)
    AuthService.revoke(
        db,
        user_id=authenticated.user.id,
        jti=authenticated.jti,
        expires_at=authenticated.expires_at,
        config=settings,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
