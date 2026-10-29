"""Contracts of the core POS operations: create order, add item, send,
mark ready and pay (plus creating the bill they need).

Each response is checked against the OpenAPI schema the API publishes
(exact fields, required fields, JSON types, UUID and timestamp formats),
so the documented contract and the real responses cannot drift apart,
and the operations, status codes and response models are pinned: changing
one of them must be a deliberate change of this file. Error responses use
the error envelope with a request id.
"""

import uuid
from datetime import datetime
from decimal import Decimal

import pytest

from app.core.request_id import REQUEST_ID_HEADER
from app.main import app


@pytest.fixture(scope="module")
def openapi():
    return app.openapi()


# (method, path) -> (success status, response model, request model)
CORE_OPERATIONS = {
    ("post", "/api/v1/orders"): ("201", "OrderResponse", "CreateOrderRequest"),
    ("post", "/api/v1/orders/{order_id}/items"): ("201", "OrderItemResponse", "AddOrderItemRequest"),
    ("post", "/api/v1/orders/{order_id}/send"): ("200", "OrderDetailResponse", None),
    ("post", "/api/v1/preparations/{preparation_id}/ready"): ("200", "PreparationResponse", None),
    ("post", "/api/v1/bills/order/{order_id}"): ("201", "CreateBillResponse", None),
    ("post", "/api/v1/bills/{bill_id}/payments"): ("201", "PaymentResponse", "PaymentRequest"),
}

JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}


def resolve(openapi, schema):
    if "$ref" in schema:
        return openapi["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
    return schema


def check(openapi, value, schema, where):
    """Assert that `value` is valid for the OpenAPI `schema`."""

    schema = resolve(openapi, schema)
    if "anyOf" in schema:
        errors = []
        for option in schema["anyOf"]:
            try:
                check(openapi, value, option, where)
                return
            except AssertionError as error:
                errors.append(str(error))
        raise AssertionError(f"{where}: matches no option: {errors}")

    kind = schema.get("type")
    if kind is None:
        return
    assert isinstance(value, JSON_TYPES[kind]) and not (
        kind in ("integer", "number") and isinstance(value, bool)
    ), f"{where}: {value!r} is not {kind}"

    if kind == "string" and schema.get("format") == "uuid":
        uuid.UUID(value)
    if kind == "string" and schema.get("format") == "date-time":
        datetime.fromisoformat(value)
    if kind == "array":
        for index, item in enumerate(value):
            check(openapi, item, schema.get("items", {}), f"{where}[{index}]")
    if kind == "object" and "properties" in schema:
        assert set(value) == set(schema["properties"]), (
            f"{where}: fields {sorted(value)} != documented {sorted(schema['properties'])}"
        )
        missing = set(schema.get("required", [])) - set(value)
        assert not missing, f"{where}: missing required {missing}"
        for name, field in schema["properties"].items():
            check(openapi, value[name], field, f"{where}.{name}")


def check_response(openapi, response, method, path):
    status_code, model, _ = CORE_OPERATIONS[(method, path)]
    assert str(response.status_code) == status_code, response.text
    check(openapi, response.json(), {"$ref": f"#/components/schemas/{model}"}, model)
    assert response.headers[REQUEST_ID_HEADER]
    return response.json()


@pytest.mark.parametrize(("method", "path"), sorted(CORE_OPERATIONS))
def test_core_operations_are_documented_as_pinned(openapi, method, path):
    status_code, model, request_model = CORE_OPERATIONS[(method, path)]
    operation = openapi["paths"][path][method]

    success = [code for code in operation["responses"] if code.startswith("2")]
    assert success == [status_code]
    schema = operation["responses"][status_code]["content"]["application/json"]["schema"]
    assert schema == {"$ref": f"#/components/schemas/{model}"}

    body = operation.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
    assert body == ({"$ref": f"#/components/schemas/{request_model}"} if request_model else None)
    # Every core write is retry-safe with a key and authenticated.
    assert "Idempotency-Key" in {parameter["name"] for parameter in operation["parameters"]}
    assert "401" not in success


def test_core_flow_matches_the_published_contracts(pos, openapi):
    created = check_response(
        openapi,
        pos.client.post(
            "/api/v1/orders",
            json={"table_id": str(pos.restaurant.table.id), "guest_count": 2},
            headers=pos.headers,
        ),
        "post",
        "/api/v1/orders",
    )
    assert created["status"] == "OPEN"
    assert created["table_id"] == str(pos.restaurant.table.id)
    assert created["guest_count"] == 2
    order_id = created["id"]

    item = check_response(
        openapi,
        pos.client.post(
            f"/api/v1/orders/{order_id}/items",
            json={"product_id": str(pos.burger.id), "quantity": 2},
            headers=pos.headers,
        ),
        "post",
        "/api/v1/orders/{order_id}/items",
    )
    assert (item["order_id"], item["status"], item["quantity"]) == (order_id, "ADDED", 2)
    assert item["product_name"] == pos.burger.name
    # Item prices are JSON numbers (bill amounts are decimal strings).
    assert Decimal(str(item["unit_price"])) == Decimal("12.50")
    assert Decimal(str(item["total_price"])) == Decimal("25.00")

    sent = check_response(
        openapi,
        pos.client.post(f"/api/v1/orders/{order_id}/send", headers=pos.headers),
        "post",
        "/api/v1/orders/{order_id}/send",
    )
    assert sent["status"] == "SENT"
    assert [(i["id"], i["status"]) for i in sent["items"]] == [(item["id"], "SENT")]

    preparation_id = pos.preparation(item["id"]).id
    assert pos.client.post(f"/api/v1/preparations/{preparation_id}/start", headers=pos.headers).status_code == 200
    ready = check_response(
        openapi,
        pos.client.post(f"/api/v1/preparations/{preparation_id}/ready", headers=pos.headers),
        "post",
        "/api/v1/preparations/{preparation_id}/ready",
    )
    assert ready["status"] == "READY"
    assert ready["order_item_id"] == item["id"]
    assert ready["started_at"] and ready["ready_at"] and ready["served_at"] is None

    bill = check_response(
        openapi,
        pos.client.post(f"/api/v1/bills/order/{order_id}", headers=pos.headers),
        "post",
        "/api/v1/bills/order/{order_id}",
    )
    assert (bill["status"], bill["total"], bill["amount_due"]) == ("OPEN", "25.00", "25.00")

    partial = check_response(
        openapi,
        pos.client.post(
            f"/api/v1/bills/{bill['id']}/payments",
            json={"amount": "10.00", "method": "CASH"},
            headers=pos.headers,
        ),
        "post",
        "/api/v1/bills/{bill_id}/payments",
    )
    assert (partial["amount"], partial["method"], partial["status"]) == ("10.00", "CASH", "COMPLETED")
    assert pos.order(order_id).status == "SENT"

    final = check_response(
        openapi,
        pos.client.post(
            f"/api/v1/bills/{bill['id']}/payments",
            json={"amount": "15.00", "method": "CARD"},
            headers=pos.headers,
        ),
        "post",
        "/api/v1/bills/{bill_id}/payments",
    )
    assert final["amount"] == "15.00"
    assert pos.order(order_id).status == "CLOSED"
    assert pos.bill(bill["id"]).status == "PAID"


@pytest.mark.parametrize(
    ("path", "body", "status_code", "code"),
    [
        ("/api/v1/orders", {"table_id": str(uuid.uuid4()), "guest_count": 2}, 404, "TABLE_NOT_FOUND"),
        ("/api/v1/orders/{missing}/items", None, 404, "ORDER_NOT_FOUND"),
        ("/api/v1/orders/{missing}/send", None, 404, "ORDER_NOT_FOUND"),
        ("/api/v1/preparations/{missing}/ready", None, 404, "PREPARATION_NOT_FOUND"),
        ("/api/v1/bills/order/{missing}", None, 404, "ORDER_NOT_FOUND"),
        ("/api/v1/bills/{missing}/payments", {"amount": "1.00", "method": "CASH"}, 404, "BILL_NOT_FOUND"),
        ("/api/v1/orders", {"guest_count": 2}, 422, "VALIDATION_ERROR"),
    ],
)
def test_core_operation_errors_use_the_error_envelope(pos, path, body, status_code, code):
    if body is None and path.endswith("/items"):
        body = {"product_id": str(pos.burger.id), "quantity": 1}
    response = pos.client.post(path.format(missing=uuid.uuid4()), json=body, headers=pos.headers)

    assert response.status_code == status_code, response.text
    error = response.json()["error"]
    assert set(error) == {"code", "message", "request_id"}
    assert error["code"] == code
    assert error["request_id"] == response.headers[REQUEST_ID_HEADER]


def test_core_operations_require_authentication(pos):
    response = pos.client.post(
        "/api/v1/orders",
        json={"table_id": str(pos.restaurant.table.id), "guest_count": 2},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "NOT_AUTHENTICATED"
