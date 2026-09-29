import uuid

import pytest
from jose import jwt

from app.core.config import settings
from app.core.security import (
    create_access_token,
    hash_password,
    verify_password,
)


def test_hash_and_verify_password():
    password_hash = hash_password("correct-password")

    assert password_hash != "correct-password"
    assert verify_password("correct-password", password_hash)
    assert not verify_password("wrong-password", password_hash)


def test_hash_password_rejects_more_than_72_bytes():
    with pytest.raises(ValueError):
        hash_password("x" * 73)


def test_access_token_contains_user_claims():
    user_id = uuid.uuid4()
    restaurant_id = uuid.uuid4()
    role_id = uuid.uuid4()

    token = create_access_token(
        user_id=user_id,
        restaurant_id=restaurant_id,
        role_id=role_id,
    )

    payload = jwt.decode(
        token,
        settings.jwt_secret_key,
        algorithms=[settings.jwt_algorithm],
    )

    assert payload["sub"] == str(user_id)
    assert payload["restaurant_id"] == str(restaurant_id)
    assert payload["role_id"] == str(role_id)
    assert "exp" in payload


def test_tests_use_isolated_settings():
    assert settings.database_url.endswith("_test")
    assert settings.jwt_secret_key == "test-only-jwt-secret-key"
