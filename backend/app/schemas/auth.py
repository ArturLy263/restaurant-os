import uuid

from pydantic import BaseModel, EmailStr


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"

class CurrentUserRole(BaseModel):
    id: uuid.UUID
    name: str


class CurrentUserStation(BaseModel):
    id: uuid.UUID
    name: str
    type: str


class CurrentUserResponse(BaseModel):
    id: uuid.UUID
    name: str
    email: str
    restaurant_id: uuid.UUID
    # None if the user's role is not a role of their restaurant.
    role: CurrentUserRole | None
    # What the backend allows this user to do, sorted; empty for a role
    # that is not recognized. Clients may use it to adapt their UI, but the
    # backend checks every request itself.
    permissions: list[str]
    # Stations whose preparations the user may work: null means every
    # station of the restaurant, [] none.
    stations: list[CurrentUserStation] | None
