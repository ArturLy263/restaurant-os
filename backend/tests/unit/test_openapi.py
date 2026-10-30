"""OpenAPI documentation of the idempotent routes."""

import pytest

from app.main import app


IDEMPOTENT_OPERATIONS = [
    "/api/v1/orders",
    "/api/v1/orders/{order_id}/send",
    "/api/v1/orders/{order_id}/items/{item_id}/cancel",
    "/api/v1/orders/{order_id}/void",
    "/api/v1/bills/{bill_id}/payments",
    "/api/v1/bills/{bill_id}/items/{item_id}/cancel",
    "/api/v1/preparations/{preparation_id}/discard",
    "/api/v1/orders/{order_id}/items",
    "/api/v1/bills/order/{order_id}",
    "/api/v1/preparations/{preparation_id}/start",
    "/api/v1/preparations/{preparation_id}/ready",
    "/api/v1/preparations/{preparation_id}/serve",
]

VALIDATION_ERROR_CONTENT = {
    "application/json": {
        "schema": {"$ref": "#/components/schemas/HTTPValidationError"},
    },
}


@pytest.fixture(scope="module")
def schema():
    return app.openapi()


@pytest.mark.parametrize("path", IDEMPOTENT_OPERATIONS)
def test_idempotent_route_documents_validation_errors(schema, path):
    operation = schema["paths"][path]["post"]

    # Regression: declaring 422 must not drop FastAPI's validation schema.
    assert operation["responses"]["422"]["content"] == VALIDATION_ERROR_CONTENT
    assert "IDEMPOTENCY_KEY_REUSED" in operation["responses"]["422"]["description"]
    assert "IDEMPOTENCY_KEY_INVALID" in operation["responses"]["400"]["description"]
    assert {
        parameter["name"]
        for parameter in operation["parameters"]
        if parameter["in"] == "header"
    } == {"Idempotency-Key"}


def test_validation_error_schema_matches_other_routes(schema):
    """Same 422 schema as a route that keeps FastAPI's default entry."""

    default = schema["paths"]["/api/v1/orders/{order_id}/items"]["post"]

    assert default["responses"]["422"]["content"] == VALIDATION_ERROR_CONTENT
    assert {"HTTPValidationError", "ValidationError"} <= set(
        schema["components"]["schemas"]
    )


def test_item_update_documents_its_idempotency_key(schema):
    operation = schema["paths"]["/api/v1/orders/{order_id}/items/{item_id}"]["patch"]

    assert operation["responses"]["422"]["content"] == VALIDATION_ERROR_CONTENT
    assert {
        parameter["name"]
        for parameter in operation["parameters"]
        if parameter["in"] == "header"
    } == {"Idempotency-Key"}


def test_menu_prices_are_documented_as_decimal_strings(schema):
    components = schema["components"]["schemas"]

    for model, field in (
        ("MenuProductResponse", "price"),
        ("MenuModifierResponse", "price_adjustment"),
    ):
        documented = components[model]["properties"][field]
        assert documented["type"] == "string"
        assert documented["pattern"] == r"^-?\d{1,8}\.\d{2}$"
