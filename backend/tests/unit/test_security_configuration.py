"""Startup configuration checks, API docs exposure, HTTP hardening and
error redaction. No database: nothing here opens a connection."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.db.database import engine
from app.main import create_app


UNREACHABLE = "postgresql+psycopg://restaurant:Str0ng-db-passw0rd-x@127.0.0.1:9/unreachable_test"
STRONG_SECRET = "unit-test-only-jwt-signing-key-4f9c2a-production-grade"


def config(**overrides):
    values = {"database_url": UNREACHABLE, "jwt_secret_key": STRONG_SECRET, **overrides}
    return Settings(_env_file=None, **values)


# --- JWT secret ---


@pytest.mark.parametrize(
    "secret",
    [
        "",
        "   ",
        "change_me_to_a_random_secret",  # the .env.example placeholder
        "CHANGEME-but-long-enough-0123456789",
        "replace-me-with-a-real-secret-please",
        "short-secret",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "abababababababababababababababababab",
    ],
)
def test_insecure_jwt_secrets_are_refused(secret):
    with pytest.raises(ValidationError) as caught:
        config(jwt_secret_key=secret)
    # The error names the setting, never its value or other settings.
    message = str(caught.value)
    assert "JWT_SECRET_KEY" in message
    if secret.strip():
        assert secret not in message
    assert UNREACHABLE not in message and "Str0ng-db-passw0rd-x" not in message


def test_production_needs_a_longer_secret():
    thirty = "k7Qp2Lx9-Vb4Nz8Rt1Wc6Ym3Hd5Gs0Fa"[:30]
    assert config(jwt_secret_key=thirty).environment == "development"
    with pytest.raises(ValidationError, match="at least 32 characters"):
        config(environment="production", jwt_secret_key=thirty)
    assert config(environment="production").environment == "production"


def test_the_secret_is_never_in_the_settings_repr():
    assert STRONG_SECRET not in repr(config())


# --- Database password in production ---


@pytest.mark.parametrize(
    "password",
    ["change_me", "postgres", "restaurant", "short1", "example-password-123"],
)
def test_production_refuses_placeholder_or_weak_database_passwords(password):
    url = f"postgresql+psycopg://restaurant:{password}@db:5432/restaurant_os"
    assert config(database_url=url).environment == "development"
    with pytest.raises(ValidationError) as caught:
        config(environment="production", database_url=url)
    assert "DATABASE_URL" in str(caught.value) and password not in str(caught.value)


def test_production_refuses_a_wildcard_cors_origin():
    with pytest.raises(ValidationError, match="CORS_ALLOWED_ORIGINS"):
        config(environment="production", cors_allowed_origins=["*"])


def test_throttle_settings_are_bounded():
    with pytest.raises(ValidationError):
        config(login_max_failures_per_account=0)
    with pytest.raises(ValidationError):
        config(login_block_base_seconds=600, login_block_max_seconds=60)


# --- API docs ---


def test_docs_are_served_outside_production():
    client = TestClient(create_app(config()))
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 200, path


def test_production_hides_the_docs_but_keeps_the_api():
    client = TestClient(create_app(config(environment="production")))
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path
    assert client.get("/health").json() == {"status": "ok"}
    # Routes still answer (and still require authentication).
    assert client.get("/api/v1/me").status_code == 401


def test_docs_can_be_enabled_explicitly_in_production():
    client = TestClient(create_app(config(environment="production", api_docs_enabled=True)))
    assert client.get("/openapi.json").status_code == 200


# --- Security headers ---


def test_api_responses_carry_security_headers_and_no_hsts_by_default():
    response = TestClient(create_app(config())).get("/api/v1/me")
    headers = response.headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Content-Security-Policy"].startswith("default-src 'none'")
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store"
    assert "Strict-Transport-Security" not in headers
    # Errors are hardened too, and keep their request ID.
    assert headers["X-Request-ID"]


def test_hsts_only_when_configured():
    response = TestClient(create_app(config(hsts_max_age_seconds=31536000))).get("/health")
    assert response.headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"


def test_the_docs_page_gets_a_policy_that_lets_it_load():
    response = TestClient(create_app(config())).get("/docs")
    assert "https://cdn.jsdelivr.net" in response.headers["Content-Security-Policy"]


def test_no_cors_by_default_and_only_listed_origins_when_configured():
    preflight = {"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"}
    default = TestClient(create_app(config())).options("/api/v1/auth/login", headers=preflight)
    assert "access-control-allow-origin" not in default.headers

    client = TestClient(create_app(config(cors_allowed_origins=["https://pos.example"])))
    allowed = client.options("/api/v1/auth/login", headers={**preflight, "Origin": "https://pos.example"})
    assert allowed.headers["access-control-allow-origin"] == "https://pos.example"
    refused = client.options("/api/v1/auth/login", headers=preflight)
    assert refused.headers.get("access-control-allow-origin") != "https://evil.example"


# --- Request size ---


def test_oversized_bodies_are_refused_with_413_announced_or_streamed():
    client = TestClient(create_app(config(max_request_body_bytes=1024)))
    big = {"email": "a@example.com", "password": "x" * 2000}
    response = client.post("/api/v1/auth/login", json=big)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"

    def chunks():
        yield b'{"email": "a@example.com", "password": "'
        yield b"x" * 2000
        yield b'"}'

    streamed = client.post(
        "/api/v1/auth/login", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert streamed.status_code == 413


# --- Validation errors ---


def test_validation_errors_never_echo_submitted_values():
    client = TestClient(create_app(config()))
    secret = "hunter2-very-secret-password"
    response = client.post("/api/v1/auth/login", json={"email": "not-an-email", "password": secret})
    assert response.status_code == 422
    assert secret not in response.text and "not-an-email" not in response.text
    errors = response.json()["detail"]
    assert errors and all(set(error) == {"loc", "msg", "type"} for error in errors)
    assert ["body", "email"] in [error["loc"] for error in errors]

    missing = client.post("/api/v1/auth/login", json={"password": secret})
    assert secret not in missing.text
    assert missing.json()["detail"][0]["type"] == "missing"


# --- Logging ---


def test_the_application_engine_hides_sql_parameters():
    assert engine.hide_parameters is True
