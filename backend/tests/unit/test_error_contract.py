"""The API error contract and request IDs (no database needed)."""

import logging
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.error_handlers import register_error_handlers
from app.api.idempotency import request_fingerprint
from app.core.errors import ApiError, ErrorCode
from app.core.request_id import REQUEST_ID_HEADER, RequestIdMiddleware


GENERATED_REQUEST_ID = re.compile(r"[0-9a-f]{32}")

SECRET = "postgresql://restaurant:s3cr3t-password@db.internal:5432/prod"


def assert_error(response, status_code, code, message=None):
    assert response.status_code == status_code
    body = response.json()

    assert set(body) == {"detail", "error"}
    assert set(body["error"]) == {"code", "message", "request_id"}
    assert body["error"]["code"] == code
    assert body["error"]["request_id"] == response.headers[REQUEST_ID_HEADER]

    if message is not None:
        assert body["error"]["message"] == message
        assert body["detail"] == message

    return body


@pytest.fixture
def failing_app():
    """A bare app with the production error handling and failing routes."""

    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)
    register_error_handlers(app)

    @app.get("/crash")
    def crash():
        raise RuntimeError(f"could not connect to {SECRET}")

    @app.get("/coded")
    def coded():
        raise ApiError(
            status_code=409,
            code=ErrorCode.ORDER_NOT_OPEN,
            message="Order is not open",
            headers={"X-Extra": "kept"},
        )

    return app


def test_unknown_route_uses_error_contract(client):
    response = client.get("/api/v1/does-not-exist")

    body = assert_error(response, 404, "NOT_FOUND", "Not Found")
    assert GENERATED_REQUEST_ID.fullmatch(body["error"]["request_id"])


def test_missing_token_is_not_authenticated(client):
    response = client.post("/api/v1/orders", json={})

    assert_error(response, 401, "NOT_AUTHENTICATED")


def test_malformed_token_keeps_detail(client):
    response = client.get(
        "/api/v1/orders/00000000-0000-0000-0000-000000000000",
        headers={"Authorization": "Bearer not-a-jwt"},
    )

    assert_error(
        response,
        401,
        "NOT_AUTHENTICATED",
        "Could not validate credentials",
    )
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_validation_error_keeps_field_errors_in_detail(client):
    response = client.post("/api/v1/auth/login", json={"email": "nope"})

    body = assert_error(
        response,
        422,
        "VALIDATION_ERROR",
    )
    assert body["error"]["message"] == "Request validation failed"
    assert {tuple(error["loc"]) for error in body["detail"]} == {
        ("body", "email"),
        ("body", "password"),
    }


def test_client_request_id_is_echoed(client):
    response = client.get(
        "/api/v1/does-not-exist",
        headers={REQUEST_ID_HEADER: "pos-7:retry.2"},
    )

    assert response.headers[REQUEST_ID_HEADER] == "pos-7:retry.2"
    assert response.json()["error"]["request_id"] == "pos-7:retry.2"


@pytest.mark.parametrize(
    "unsafe",
    ["x" * 129, "has space", "new\tline", "<script>"],
)
def test_unsafe_client_request_id_is_replaced(client, unsafe):
    response = client.get(
        "/api/v1/does-not-exist",
        headers={REQUEST_ID_HEADER: unsafe},
    )

    request_id = response.headers[REQUEST_ID_HEADER]
    assert GENERATED_REQUEST_ID.fullmatch(request_id)
    assert response.json()["error"]["request_id"] == request_id


def test_successful_responses_carry_request_id_and_unchanged_body(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert GENERATED_REQUEST_ID.fullmatch(response.headers[REQUEST_ID_HEADER])


def test_unexpected_error_hides_internals(failing_app, caplog, monkeypatch):
    # Alembic's logging.config.fileConfig (run by the database fixtures)
    # disables loggers that already exist, depending on test order.
    monkeypatch.setattr(logging.getLogger("app.errors"), "disabled", False)
    client = TestClient(failing_app, raise_server_exceptions=False)

    with caplog.at_level(logging.ERROR, logger="app.errors"):
        response = client.get(
            "/crash",
            headers={REQUEST_ID_HEADER: "req-500"},
        )

    body = assert_error(
        response,
        500,
        "INTERNAL_ERROR",
        "Internal server error",
    )
    assert body["error"]["request_id"] == "req-500"
    assert "s3cr3t" not in response.text
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text

    # The details are logged with the request ID for correlation.
    assert "request_id=req-500" in caplog.text
    assert "s3cr3t" in caplog.text


def test_api_error_keeps_code_status_and_headers(failing_app):
    response = TestClient(failing_app).get("/coded")

    assert_error(response, 409, "ORDER_NOT_OPEN", "Order is not open")
    assert response.headers["X-Extra"] == "kept"


def test_api_error_is_an_http_exception():
    from fastapi import HTTPException

    error = ApiError(404, ErrorCode.ORDER_NOT_FOUND, "Order not found")

    assert isinstance(error, HTTPException)
    assert error.detail == "Order not found"
    assert error.code == "ORDER_NOT_FOUND"


def test_error_codes_are_unique_and_match_their_names():
    for code in ErrorCode:
        assert code.value == code.name


# --- Request fingerprints ---


def test_fingerprint_ignores_key_order_and_amount_formatting():
    from decimal import Decimal

    assert request_fingerprint(
        "POST /x/{id}", {"id": "1"}, {"amount": Decimal("10"), "method": "CASH"}
    ) == request_fingerprint(
        "POST /x/{id}", {"id": "1"}, {"method": "CASH", "amount": Decimal("10.00")}
    )


@pytest.mark.parametrize(
    "other",
    [
        ("POST /y/{id}", {"id": "1"}, {"amount": "10"}),
        ("POST /x/{id}", {"id": "2"}, {"amount": "10"}),
        ("POST /x/{id}", {"id": "1"}, {"amount": "11"}),
        ("POST /x/{id}", {"id": "1"}, None),
    ],
    ids=["operation", "path", "body", "no body"],
)
def test_fingerprint_changes_with_the_request(other):
    assert request_fingerprint(
        "POST /x/{id}", {"id": "1"}, {"amount": "10"}
    ) != request_fingerprint(*other)
