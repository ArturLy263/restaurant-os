import uuid

from app.core.security import create_access_token
from tests.factories import TEST_USER_PASSWORD, auth_headers


# --- No database required: rejected before any query runs ---


def test_protected_route_requires_token(client):
    response = client.get(f"/api/v1/orders/{uuid.uuid4()}")

    assert response.status_code == 401


def test_protected_route_rejects_malformed_token(client):
    response = client.get(
        f"/api/v1/orders/{uuid.uuid4()}",
        headers={"Authorization": "Bearer not-a-jwt"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Could not validate credentials"


# --- PostgreSQL required ---


def test_login_returns_token(db_client, restaurant):
    response = db_client.post(
        "/api/v1/auth/login",
        json={
            "email": restaurant.user.email,
            "password": TEST_USER_PASSWORD,
        },
    )

    assert response.status_code == 200
    assert response.json()["token_type"] == "bearer"
    assert response.json()["access_token"]


def test_login_rejects_wrong_password(db_client, restaurant):
    response = db_client.post(
        "/api/v1/auth/login",
        json={
            "email": restaurant.user.email,
            "password": "wrong-password",
        },
    )

    assert response.status_code == 401


def test_token_for_unknown_user_is_rejected(db_client, restaurant):
    token = create_access_token(
        user_id=uuid.uuid4(),
        restaurant_id=restaurant.restaurant.id,
        role_id=restaurant.role.id,
    )

    response = db_client.get(
        f"/api/v1/orders/{uuid.uuid4()}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401


def test_inactive_user_is_rejected(db_client, db_session, restaurant):
    restaurant.user.is_active = False
    db_session.flush()

    response = db_client.get(
        f"/api/v1/orders/{uuid.uuid4()}",
        headers=auth_headers(restaurant.user),
    )

    assert response.status_code == 403
