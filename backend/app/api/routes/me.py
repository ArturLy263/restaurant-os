from fastapi import APIRouter, Depends

from app.core.dependencies import AuthContext, get_auth_context
from app.schemas.auth import CurrentUserResponse, CurrentUserRole


router = APIRouter(
    prefix="/api/v1",
    tags=["auth"],
)


@router.get(
    "/me",
    response_model=CurrentUserResponse,
)
def get_me(context: AuthContext = Depends(get_auth_context)):
    """The authenticated user, their role and their permissions.

    Needs authentication only, so that a user whose role grants nothing can
    still see why.
    """

    user = context.user
    role = context.role

    return CurrentUserResponse(
        id=user.id,
        name=user.name,
        email=user.email,
        restaurant_id=user.restaurant_id,
        role=(
            CurrentUserRole(id=role.id, name=role.name)
            if role is not None
            else None
        ),
        permissions=sorted(context.permissions),
    )
