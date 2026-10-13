from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.dependencies import AuthContext, get_auth_context
from app.db.dependencies import get_db
from app.db.models.station import Station
from app.schemas.auth import (
    CurrentUserResponse,
    CurrentUserRole,
    CurrentUserStation,
)


router = APIRouter(
    prefix="/api/v1",
    tags=["auth"],
)


@router.get(
    "/me",
    response_model=CurrentUserResponse,
)
def get_me(
    context: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
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
        stations=_stations(db, context.station_scope),
    )


def _stations(db, station_scope) -> list[CurrentUserStation] | None:
    if station_scope is None:
        return None

    if not station_scope:
        return []

    stations = db.scalars(
        select(Station)
        .where(Station.id.in_(station_scope))
        .order_by(Station.name, Station.id)
    )

    return [
        CurrentUserStation(id=station.id, name=station.name, type=station.type)
        for station in stations
    ]
