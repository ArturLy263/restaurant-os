"""Role-based permissions (Epic C1) and GET /api/v1/me (Epic C3)."""

import re
import uuid

import pytest

from app.core.permissions import ROLE_PERMISSIONS, Permission
from app.core.request_id import REQUEST_ID_HEADER
from app.main import app
from tests.factories import (
    assign_station,
    auth_headers,
    create_restaurant,
    create_station,
    create_user,
)


ROLES = ["WAITER", "KITCHEN", "BAR", "CASHIER", "MANAGER", "ADMIN"]

# Users whose role grants nothing.
UNRECOGNIZED_ROLES = ["SOMMELIER", "waiter", "Manager", ""]

ALL_ROLES = set(ROLES)
STATION_STAFF = {"KITCHEN", "BAR"}
MANAGEMENT = {"MANAGER", "ADMIN"}

_ID = "00000000-0000-4000-8000-000000000001"

# The policy, written out per endpoint (independently of ROLE_PERMISSIONS):
# (method, path, body, permission, roles allowed).
ENDPOINTS = [
    ("POST", "/api/v1/orders", {"table_id": _ID, "guest_count": 1},
     "orders:create", {"WAITER"} | MANAGEMENT),
    ("POST", f"/api/v1/orders/{_ID}/items", {"product_id": _ID, "quantity": 1},
     "orders:add_items", {"WAITER"} | MANAGEMENT),
    ("POST", f"/api/v1/orders/{_ID}/send", None,
     "orders:send", {"WAITER"} | MANAGEMENT),
    ("GET", f"/api/v1/orders/{_ID}", None,
     "orders:view", {"WAITER", "CASHIER"} | MANAGEMENT),
    ("POST", f"/api/v1/orders/{_ID}/items/{_ID}/cancel", {"reason": "x"},
     "orders:cancel_items", {"WAITER"} | MANAGEMENT),
    ("POST", f"/api/v1/orders/{_ID}/void", {"reason": "x"},
     "orders:void", {"WAITER"} | MANAGEMENT),
    ("GET", "/api/v1/stations", None,
     "preparations:view", STATION_STAFF | MANAGEMENT),
    ("GET", f"/api/v1/stations/{_ID}/preparations", None,
     "preparations:view", STATION_STAFF | MANAGEMENT),
    ("POST", f"/api/v1/preparations/{_ID}/start", None,
     "preparations:update", STATION_STAFF | MANAGEMENT),
    ("POST", f"/api/v1/preparations/{_ID}/ready", None,
     "preparations:update", STATION_STAFF | MANAGEMENT),
    ("POST", f"/api/v1/preparations/{_ID}/serve", None,
     "preparations:serve", {"WAITER"} | STATION_STAFF | MANAGEMENT),
    ("POST", f"/api/v1/bills/order/{_ID}", None,
     "bills:create", {"CASHIER"} | MANAGEMENT),
    ("GET", f"/api/v1/bills/{_ID}", None,
     "bills:view", {"CASHIER"} | MANAGEMENT),
    ("POST", f"/api/v1/bills/{_ID}/payments", {"amount": "1.00", "method": "CASH"},
     "payments:create", {"CASHIER"} | MANAGEMENT),
    ("POST", f"/api/v1/bills/{_ID}/items/{_ID}/cancel", {"reason": "x"},
     "bills:adjust", {"CASHIER"} | MANAGEMENT),
    ("GET", "/api/v1/events", None,
     "events:view", MANAGEMENT),
]

PUBLIC_ROUTES = {("POST", "/api/v1/auth/login"), ("GET", "/health")}
AUTHENTICATED_ONLY_ROUTES = {("GET", "/api/v1/me")}

# Permissions without an endpoint of their own: orders:manage_all lifts the
# creator-only order rule; preparations:all_stations lifts station scope
# (waiters serve from every station); menu and table management APIs do
# not exist yet.
EXPECTED_ROLE_PERMISSIONS = {
    role: {
        permission
        for *_, permission, roles in ENDPOINTS
        if role in roles
    }
    | (
        {"orders:manage_all", "menu:manage", "tables:manage"}
        if role in MANAGEMENT
        else set()
    )
    | (
        {"preparations:all_stations"}
        if role in MANAGEMENT | {"WAITER"}
        else set()
    )
    for role in ROLES
}

# Roles limited to their assigned stations, and the station type of each.
STATION_ROLE_TYPES = {"KITCHEN": "KITCHEN", "BAR": "BAR"}


def call(client, endpoint, headers):
    method, path, body, *_ = endpoint
    return client.request(method, path, json=body, headers=headers)


def assert_permission_denied(response, permission):
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["error"]["code"] == "PERMISSION_DENIED"
    assert body["error"]["message"] == f"Missing permission: {permission}"
    assert body["detail"] == body["error"]["message"]
    assert body["error"]["request_id"] == response.headers[REQUEST_ID_HEADER]


@pytest.fixture
def staff(db_session, restaurant):
    """One user per role in `restaurant` (ADMIN is the factory's user)."""

    users = {
        role: create_user(db_session, restaurant, role)
        for role in ROLES
        if role != "ADMIN"
    }
    users["ADMIN"] = restaurant.user

    return users


def headers_for(user):
    return auth_headers(user)


# --- The endpoint map is complete ---


def test_every_route_is_mapped_to_a_permission():
    """A new endpoint fails this test until its access is decided here."""

    routes = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }
    mapped = {
        (method, path.replace(_ID, "{id}"))
        for method, path, *_ in ENDPOINTS
    }
    normalized = {
        (method, _normalize(path))
        for method, path in routes - PUBLIC_ROUTES - AUTHENTICATED_ONLY_ROUTES
    }

    assert normalized == mapped
    assert PUBLIC_ROUTES | AUTHENTICATED_ONLY_ROUTES <= routes


def _normalize(path):
    return re.sub(r"\{[a-z_]+\}", "{id}", path)


def test_role_permissions_match_the_policy():
    assert {
        role.value: {str(permission) for permission in permissions}
        for role, permissions in ROLE_PERMISSIONS.items()
    } == EXPECTED_ROLE_PERMISSIONS


# --- Allowed and forbidden, every role x every endpoint ---


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize(
    "endpoint",
    ENDPOINTS,
    ids=[f"{method} {path.replace(_ID, '{id}')}" for method, path, *_ in ENDPOINTS],
)
def test_endpoint_access_by_role(db_client, staff, role, endpoint):
    *_, permission, allowed_roles = endpoint

    response = call(db_client, endpoint, headers_for(staff[role]))

    if role in allowed_roles:
        # Authorized: the request reaches the endpoint, which then finds
        # nothing with these made-up IDs (or lists no events), or, for a
        # station role, refuses a station it is not assigned to.
        assert response.status_code in (200, 403, 404), response.text
        if response.status_code == 404:
            assert response.json()["error"]["code"].endswith("_NOT_FOUND")
        if response.status_code == 403:
            assert role in STATION_ROLE_TYPES
            assert response.json()["error"]["code"] == "STATION_ACCESS_DENIED"
    else:
        assert_permission_denied(response, permission)


@pytest.mark.parametrize("role_name", UNRECOGNIZED_ROLES)
def test_unrecognized_role_is_denied_everything(
    db_client, db_session, restaurant, role_name
):
    user = create_user(db_session, restaurant, role_name)

    for endpoint in ENDPOINTS:
        assert_permission_denied(
            call(db_client, endpoint, headers_for(user)),
            endpoint[3],
        )


def test_role_of_another_restaurant_is_not_honoured(
    db_client, db_session, restaurant
):
    other = create_restaurant(db_session, name="Other Restaurant")
    user = create_user(db_session, restaurant, "WAITER")
    user.role_id = other.role.id  # ADMIN, but of the other restaurant
    db_session.flush()

    response = db_client.post(
        "/api/v1/orders",
        json={"table_id": str(restaurant.table.id), "guest_count": 1},
        headers=headers_for(user),
    )
    assert_permission_denied(response, "orders:create")

    me = db_client.get("/api/v1/me", headers=headers_for(user)).json()
    assert me["role"] is None
    assert me["permissions"] == []


def test_role_change_applies_to_existing_tokens(db_client, db_session, restaurant):
    """The role is read per request, not taken from the token."""

    waiter = create_user(db_session, restaurant, "WAITER")
    headers = headers_for(waiter)
    cashier_role = create_user(db_session, restaurant, "CASHIER").role_id

    get_bill = ("GET", "/api/v1/bills/" + _ID, None)
    assert_permission_denied(call(db_client, get_bill, headers), "bills:view")

    waiter.role_id = cashier_role
    db_session.flush()

    response = call(db_client, get_bill, headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "BILL_NOT_FOUND"


# --- Unauthenticated ---


@pytest.mark.parametrize(
    "endpoint",
    ENDPOINTS + [("GET", "/api/v1/me", None, None, set())],
    ids=lambda endpoint: f"{endpoint[0]} {endpoint[1].replace(_ID, '{id}')}",
)
@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer not-a-jwt"}],
    ids=["no token", "invalid token"],
)
def test_unauthenticated_requests_are_rejected(client, endpoint, headers):
    response = call(client, endpoint, headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "NOT_AUTHENTICATED"


# --- Real workflows per role ---


def test_waiter_opens_adds_sends_and_serves(pos, staff):
    pos.headers = headers_for(staff["WAITER"])

    order_id, burger_id, _ = pos.sent_order()

    preparation = pos.preparation(burger_id)
    preparation.status = "READY"
    pos.db.flush()

    served = pos.client.post(
        f"/api/v1/preparations/{preparation.id}/serve",
        headers=pos.headers,
    )
    assert served.status_code == 200

    assert_permission_denied(pos.create_bill(order_id), "bills:create")


def test_cashier_bills_and_takes_payment_on_a_waiters_order(pos, staff):
    pos.headers = headers_for(staff["WAITER"])
    order_id, *_ = pos.sent_order()

    pos.headers = headers_for(staff["CASHIER"])
    assert_permission_denied(pos.create_order(), "orders:create")
    assert_permission_denied(pos.send(order_id), "orders:send")

    bill = pos.create_bill(order_id)
    assert bill.status_code == 201
    assert pos.get_bill(bill.json()["id"]).status_code == 200

    payment = pos.pay(bill.json()["id"], "15.00")
    assert payment.status_code == 201
    assert pos.order(order_id).status == "CLOSED"


@pytest.mark.parametrize("role", sorted(STATION_STAFF))
def test_station_staff_work_the_preparation_queue(
    pos, staff, role, db_session, restaurant
):
    # The burger is prepared at a station of the role's type, assigned to
    # the user.
    station = create_station(
        db_session, restaurant, f"{role} station", STATION_ROLE_TYPES[role]
    )
    pos.burger.station_id = station.id
    db_session.flush()
    assign_station(db_session, staff[role], station)

    pos.headers = headers_for(staff["WAITER"])
    order_id, burger_id, _ = pos.sent_order()
    preparation_id = pos.preparation(burger_id).id

    headers = headers_for(staff[role])

    queue = pos.client.get(
        f"/api/v1/stations/{station.id}/preparations",
        headers=headers,
    )
    assert queue.status_code == 200
    assert str(preparation_id) in {item["id"] for item in queue.json()}

    for action, expected in (("start", "IN_PROGRESS"), ("ready", "READY")):
        response = pos.client.post(
            f"/api/v1/preparations/{preparation_id}/{action}",
            headers=headers,
        )
        assert response.status_code == 200
        assert response.json()["status"] == expected

    order = pos.client.get(f"/api/v1/orders/{order_id}", headers=headers)
    assert_permission_denied(order, "orders:view")


def test_manager_reads_operational_events(pos, staff):
    pos.headers = headers_for(staff["WAITER"])
    order_id, *_ = pos.open_order()

    response = pos.client.get(
        "/api/v1/events",
        params={"entity_id": order_id},
        headers=headers_for(staff["MANAGER"]),
    )

    assert response.status_code == 200
    assert [event["event_type"] for event in response.json()] == ["order.created"]

    assert_permission_denied(
        pos.client.get("/api/v1/events", headers=headers_for(staff["WAITER"])),
        "events:view",
    )


def test_order_ownership_still_applies_within_a_role(pos, staff, db_session, restaurant):
    """Permissions add to, not replace, the creator-only order rule."""

    pos.headers = headers_for(staff["WAITER"])
    order_id, burger_id, _ = pos.sent_order()

    colleague = create_user(db_session, restaurant, "WAITER")
    response = pos.client.post(
        f"/api/v1/orders/{order_id}/items/{burger_id}/cancel",
        json={"reason": "x"},
        headers=headers_for(colleague),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ORDER_ACCESS_DENIED"


# --- Cross-restaurant isolation ---


@pytest.fixture
def other_staff(db_session):
    other = create_restaurant(db_session, name="Other Restaurant")
    users = {
        role: create_user(db_session, other, role)
        for role in ("WAITER", "KITCHEN", "CASHIER", "MANAGER")
    }
    # Assigned to its own restaurant's kitchen, never to ours.
    assign_station(db_session, users["KITCHEN"], other.station)

    return users


def assert_same_error(foreign, missing, status_code=404):
    """A foreign resource is indistinguishable from a missing one."""

    assert foreign.status_code == missing.status_code == status_code
    assert foreign.json()["error"]["code"] == missing.json()["error"]["code"]
    assert foreign.json()["detail"] == missing.json()["detail"]


def test_other_restaurant_resources_look_missing(pos, staff, other_staff):
    pos.headers = headers_for(staff["WAITER"])
    order_id, burger_id, _ = pos.sent_order()
    pos.headers = headers_for(staff["CASHIER"])
    bill_id = pos.create_bill(order_id).json()["id"]
    preparation_id = pos.preparation(burger_id).id
    missing = str(uuid.uuid4())
    client = pos.client

    def both(role, method, template, body=None):
        headers = headers_for(other_staff[role])
        return (
            client.request(method, template.format(id=foreign_ids[template]), json=body, headers=headers),
            client.request(method, template.format(id=missing), json=body, headers=headers),
        )

    foreign_ids = {
        "/api/v1/orders/{id}": order_id,
        "/api/v1/orders/{id}/send": order_id,
        "/api/v1/bills/{id}": bill_id,
        "/api/v1/bills/{id}/payments": bill_id,
        "/api/v1/bills/order/{id}": order_id,
        "/api/v1/stations/{id}/preparations": pos.restaurant.station.id,
        "/api/v1/preparations/{id}/start": preparation_id,
    }

    assert_same_error(*both("WAITER", "GET", "/api/v1/orders/{id}"))
    assert_same_error(*both("WAITER", "POST", "/api/v1/orders/{id}/send"))
    assert_same_error(*both("CASHIER", "GET", "/api/v1/bills/{id}"))
    assert_same_error(
        *both(
            "CASHIER",
            "POST",
            "/api/v1/bills/{id}/payments",
            {"amount": "1.00", "method": "CASH"},
        )
    )
    assert_same_error(*both("CASHIER", "POST", "/api/v1/bills/order/{id}"))
    # Station scope answers first: the same refusal for any station the
    # user is not assigned to, existing or not.
    assert_same_error(
        *both("KITCHEN", "GET", "/api/v1/stations/{id}/preparations"),
        status_code=403,
    )
    assert_same_error(*both("KITCHEN", "POST", "/api/v1/preparations/{id}/start"))

    # Nothing changed in the first restaurant.
    assert pos.bill(bill_id).paid_total == 0
    assert pos.preparation(burger_id).status == "PENDING"


def test_events_are_limited_to_the_users_restaurant(pos, staff, other_staff):
    pos.headers = headers_for(staff["WAITER"])
    pos.open_order()

    response = pos.client.get(
        "/api/v1/events",
        headers=headers_for(other_staff["MANAGER"]),
    )

    assert response.status_code == 200
    assert response.json() == []


# --- GET /api/v1/me ---


@pytest.mark.parametrize("role", ROLES)
def test_me_returns_the_user_role_and_permissions(db_client, staff, role):
    user = staff[role]

    response = db_client.get("/api/v1/me", headers=headers_for(user))

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "id": str(user.id),
        "name": user.name,
        "email": user.email,
        "restaurant_id": str(user.restaurant_id),
        "role": {"id": str(user.role_id), "name": role},
        "permissions": sorted(EXPECTED_ROLE_PERMISSIONS[role]),
        # Waiters and management: every station; others: none assigned.
        "stations": (
            None if "preparations:all_stations" in EXPECTED_ROLE_PERMISSIONS[role]
            else []
        ),
    }


def test_me_exposes_no_secrets(db_client, staff):
    response = db_client.get("/api/v1/me", headers=headers_for(staff["WAITER"]))

    text = response.text.lower()
    for secret in ("password", "hash", "token", "$2b$"):
        assert secret not in text


def test_me_for_an_unrecognized_role(db_client, db_session, restaurant):
    user = create_user(db_session, restaurant, "SOMMELIER")

    body = db_client.get("/api/v1/me", headers=headers_for(user)).json()

    assert body["role"] == {"id": str(user.role_id), "name": "SOMMELIER"}
    assert body["permissions"] == []


def test_me_for_an_inactive_user(db_client, db_session, staff):
    staff["WAITER"].is_active = False
    db_session.flush()

    response = db_client.get("/api/v1/me", headers=headers_for(staff["WAITER"]))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "USER_INACTIVE"


def test_me_permissions_are_known_permission_values(db_client, staff):
    body = db_client.get("/api/v1/me", headers=headers_for(staff["ADMIN"])).json()

    assert set(body["permissions"]) == {str(p) for p in Permission}

