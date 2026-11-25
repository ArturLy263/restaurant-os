"""Sign-in hardening: throttling, no account enumeration, token revocation.

Runs against the rolled-back test database; nothing outlives a test.
"""

from datetime import datetime, timedelta, timezone

import pytest
from jose import jwt
from sqlalchemy import select, text

from app.core import security
from app.core.config import settings
from app.core.security import create_access_token, hash_password
from app.db.models.auth_security import LoginThrottle, RevokedToken
from app.services import auth_service
from tests.factories import TEST_USER_PASSWORD, auth_headers


LOGIN = "/api/v1/auth/login"


def login(client, email, password=TEST_USER_PASSWORD):
    return client.post(LOGIN, json={"email": email, "password": password})


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def clock(monkeypatch):
    """Controls the time the login service sees (naive UTC)."""

    state = {"now": datetime.now(timezone.utc).replace(tzinfo=None)}
    monkeypatch.setattr(auth_service, "_utcnow", lambda: state["now"])

    def advance(seconds):
        state["now"] += timedelta(seconds=seconds)

    return advance


# --- Email normalization ---


def test_login_ignores_email_capitalization_and_surrounding_spaces(db_client, restaurant):
    email = restaurant.user.email
    response = login(db_client, f"  {email.upper()}  ".replace("@EXAMPLE.COM", "@example.com"))
    assert response.status_code == 200, response.text


# --- Throttling ---


def test_repeated_failures_throttle_the_account_with_a_generic_429(db_client, restaurant, clock):
    email = restaurant.user.email
    limit = settings.login_max_failures_per_account
    for _ in range(limit):
        assert login(db_client, email, "wrong").status_code == 401

    # Blocked now, even with the right password (and whatever the case).
    blocked = login(db_client, email.upper())
    assert blocked.status_code == 429
    body = blocked.json()
    assert body["error"]["code"] == "TOO_MANY_LOGIN_ATTEMPTS"
    assert body["error"]["message"] == "Too many sign-in attempts. Try again later."
    assert int(blocked.headers["Retry-After"]) == settings.login_block_base_seconds


def test_the_block_lapses_on_its_own_and_a_success_clears_the_count(db_client, db_session, restaurant, clock):
    email = restaurant.user.email
    for _ in range(settings.login_max_failures_per_account):
        login(db_client, email, "wrong")
    assert login(db_client, email).status_code == 429

    clock(settings.login_block_base_seconds + 1)
    assert login(db_client, email).status_code == 200
    # The account's counter is gone; the client address keeps its own.
    rows = db_session.scalars(select(LoginThrottle)).all()
    assert len(rows) == 1 and rows[0].blocked_until is None


def test_blocks_grow_with_further_failures_but_never_beyond_the_maximum(db_client, restaurant, clock, monkeypatch):
    monkeypatch.setattr(settings, "login_block_max_seconds", 100)
    email = restaurant.user.email
    delays = []
    for _ in range(settings.login_max_failures_per_account + 4):
        response = login(db_client, email, "wrong")
        if response.status_code == 429:
            delays.append(int(response.headers["Retry-After"]))
            clock(delays[-1] + 1)  # wait it out, then fail again
            login(db_client, email, "wrong")
    assert delays == sorted(delays)
    assert max(delays) <= 100


def test_a_legitimate_sign_in_between_failures_resets_the_account(db_client, restaurant, clock):
    email = restaurant.user.email
    below = settings.login_max_failures_per_account - 1
    for _ in range(below):
        login(db_client, email, "wrong")
    assert login(db_client, email).status_code == 200
    for _ in range(below):
        assert login(db_client, email, "wrong").status_code == 401
    assert login(db_client, email).status_code == 200


def test_unknown_emails_are_throttled_exactly_like_known_ones(db_client, restaurant, clock):
    """The throttle is keyed on what was submitted: it says nothing about
    whether the account exists."""

    responses = {}
    for email in (restaurant.user.email, "nobody@example.com"):
        for _ in range(settings.login_max_failures_per_account):
            login(db_client, email, "wrong")
        responses[email] = login(db_client, email, "wrong")
    known, unknown = responses.values()
    assert known.status_code == unknown.status_code == 429
    assert known.json()["error"] == {**unknown.json()["error"], "request_id": known.json()["error"]["request_id"]}


def test_one_client_address_is_throttled_across_emails(db_client, restaurant, clock, monkeypatch):
    monkeypatch.setattr(settings, "login_max_failures_per_ip", 3)
    for index in range(3):
        assert login(db_client, f"someone-{index}@example.com", "wrong").status_code == 401
    # Another email from the same address, even the right account, waits.
    assert login(db_client, restaurant.user.email).status_code == 429


def test_throttling_can_be_turned_off(db_client, restaurant, monkeypatch):
    monkeypatch.setattr(settings, "login_throttle_enabled", False)
    for _ in range(settings.login_max_failures_per_account + 2):
        assert login(db_client, restaurant.user.email, "wrong").status_code == 401
    assert login(db_client, restaurant.user.email).status_code == 200


def test_throttle_rows_hold_no_email_or_address(db_client, db_session, restaurant):
    login(db_client, restaurant.user.email, "wrong")
    for row in db_session.scalars(select(LoginThrottle)).all():
        assert len(row.key_hash) == 64 and "@" not in row.key_hash


# --- No account enumeration ---


def test_unknown_email_and_wrong_password_answer_identically_after_the_same_work(db_client, restaurant, monkeypatch):
    calls = []
    real = security.bcrypt.checkpw
    monkeypatch.setattr(security.bcrypt, "checkpw", lambda *a: calls.append(1) or real(*a))

    wrong = login(db_client, restaurant.user.email, "wrong")
    unknown = login(db_client, "nobody@example.com", "wrong")

    # One bcrypt check each: an unknown email is not answered faster.
    assert calls == [1, 1]
    assert wrong.status_code == unknown.status_code == 401
    strip = lambda r: {**r.json()["error"], "request_id": None}  # noqa: E731
    assert strip(wrong) == strip(unknown)
    assert wrong.json()["detail"] == unknown.json()["detail"]


def test_the_dummy_hash_is_computed_once():
    assert security._dummy_hash() is security._dummy_hash()


def test_an_inactive_account_answers_like_a_wrong_password(db_client, db_session, restaurant):
    restaurant.user.is_active = False
    db_session.flush()
    response = login(db_client, restaurant.user.email)
    assert (response.status_code, response.json()["error"]["code"]) == (401, "INVALID_CREDENTIALS")


# --- Token revocation ---


def test_signing_out_revokes_that_token_only(db_client, db_session, restaurant):
    first = login(db_client, restaurant.user.email).json()["access_token"]
    second = login(db_client, restaurant.user.email).json()["access_token"]

    assert db_client.post("/api/v1/auth/logout", headers=bearer(first)).status_code == 204
    assert db_client.get("/api/v1/me", headers=bearer(first)).status_code == 401
    assert db_client.get("/api/v1/me", headers=bearer(second)).status_code == 200
    # Only the token id is kept, never the token.
    row = db_session.scalar(select(RevokedToken))
    claims = jwt.get_unverified_claims(first)
    assert str(row.jti) == claims["jti"] and first not in str(row.__dict__)
    # Signing out with a revoked token is refused like any invalid token.
    assert db_client.post("/api/v1/auth/logout", headers=bearer(first)).status_code == 401


def test_a_password_change_ends_every_earlier_session(db_client, db_session, restaurant):
    old = login(db_client, restaurant.user.email).json()["access_token"]
    version = restaurant.user.token_version

    # Any change of password_hash bumps the version (database trigger), so
    # it applies to every way passwords are reset.
    db_session.execute(
        text("UPDATE users SET password_hash = :h WHERE id = :id"),
        {"h": hash_password("a-brand-new-password"), "id": restaurant.user.id},
    )
    db_session.expire_all()
    assert db_session.get(type(restaurant.user), restaurant.user.id).token_version == version + 1

    assert db_client.get("/api/v1/me", headers=bearer(old)).status_code == 401
    new = login(db_client, restaurant.user.email, "a-brand-new-password").json()["access_token"]
    assert db_client.get("/api/v1/me", headers=bearer(new)).status_code == 200


def test_other_updates_keep_sessions(db_client, db_session, restaurant):
    token = login(db_client, restaurant.user.email).json()["access_token"]
    db_session.execute(text("UPDATE users SET name = 'Renamed' WHERE id = :id"), {"id": restaurant.user.id})
    assert db_client.get("/api/v1/me", headers=bearer(token)).status_code == 200


def _token(**claims):
    payload = {"exp": datetime.now(timezone.utc) + timedelta(minutes=5), **claims}
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def test_tokens_without_a_version_or_id_or_with_a_stale_version_are_refused(db_client, restaurant):
    user = restaurant.user
    base = {"sub": str(user.id), "restaurant_id": str(user.restaurant_id), "role_id": str(user.role_id)}
    for claims in (
        base,  # issued before revocation existed
        {**base, "ver": user.token_version},  # no jti
        {**base, "jti": "not-a-uuid", "ver": user.token_version},
        {**base, "jti": "6f1d0b54-8d5c-4c39-9a51-0e1f0a7c2b11", "ver": user.token_version + 1},
        {**base, "jti": "6f1d0b54-8d5c-4c39-9a51-0e1f0a7c2b11", "ver": True},
    ):
        assert db_client.get("/api/v1/me", headers=bearer(_token(**claims))).status_code == 401, claims


def test_an_expired_token_is_refused(db_client, restaurant):
    user = restaurant.user
    expired = jwt.encode(
        {
            "sub": str(user.id), "ver": user.token_version, "jti": "6f1d0b54-8d5c-4c39-9a51-0e1f0a7c2b11",
            "exp": datetime.now(timezone.utc) - timedelta(seconds=1),
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    assert db_client.get("/api/v1/me", headers=bearer(expired)).status_code == 401


def test_a_token_signed_with_another_algorithm_is_refused(db_client, restaurant):
    user = restaurant.user
    claims = {"sub": str(user.id), "ver": 0, "jti": "6f1d0b54-8d5c-4c39-9a51-0e1f0a7c2b11"}
    forged = jwt.encode(claims, settings.jwt_secret_key, algorithm="HS512")
    assert db_client.get("/api/v1/me", headers=bearer(forged)).status_code == 401


def test_a_valid_token_still_works_and_deactivation_blocks_it_at_once(db_client, db_session, restaurant):
    headers = auth_headers(restaurant.user)
    assert db_client.get("/api/v1/me", headers=headers).status_code == 200
    restaurant.user.is_active = False
    db_session.flush()
    response = db_client.get("/api/v1/me", headers=headers)
    assert (response.status_code, response.json()["error"]["code"]) == (403, "USER_INACTIVE")


def test_create_access_token_requires_the_version():
    with pytest.raises(TypeError):
        create_access_token(user_id=None, restaurant_id=None, role_id=None)  # type: ignore[call-arg]


def test_a_token_without_an_expiry_is_refused(db_client, restaurant):
    user = restaurant.user
    claims = {"sub": str(user.id), "ver": user.token_version, "jti": "6f1d0b54-8d5c-4c39-9a51-0e1f0a7c2b11"}
    endless = jwt.encode(claims, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    assert db_client.get("/api/v1/me", headers=bearer(endless)).status_code == 401
