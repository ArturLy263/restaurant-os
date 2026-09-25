from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import (
    create_access_token,
    verify_password,
)
from app.db.models.user import User


class AuthService:

    @staticmethod
    def login(
        db: Session,
        email: str,
        password: str,
    ) -> str:

        statement = select(User).where(
            User.email == email,
            User.is_active.is_(True),
        )

        user = db.scalar(statement)

        if user is None:
            raise ValueError("Invalid email or password")

        if not verify_password(
            password,
            user.password_hash,
        ):
            raise ValueError("Invalid email or password")

        return create_access_token(
            user_id=user.id,
            restaurant_id=user.restaurant_id,
            role_id=user.role_id,
        )