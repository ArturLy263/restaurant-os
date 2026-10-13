"""Station scope (Epic C2): Kitchen and Bar users work only their stations.

The restaurant here has two kitchen stations (Kitchen, Grill) and a Bar.
A sent order has a burger (Kitchen), a steak (Grill) and a beer (Bar).
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from app.core.request_id import REQUEST_ID_HEADER
from app.db.models.preparation import Preparation
from app.db.models.product import Product
from app.db.models.station_assignment import StationAssignment
from tests.factories import (
    assign_station,
    auth_headers,
    create_restaurant,
    create_station,
    create_user,
)


@pytest.fixture
def floor(pos, db_session, restaurant):
    kitchen = restaurant.station  # type KITCHEN
    grill = create_station(db_session, restaurant, "Grill", "KITCHEN")
    bar = create_station(db_session, restaurant, "Bar", "BAR")

    def product(name, station):
        item = Product(
            category_id=restaurant.category.id,
            name=name,
            price=Decimal("5.00"),
            station_id=station.id,
        )
        db_session.add(item)
        db_session.flush()
        return item

    steak, beer = product("Steak", grill), product("Beer", bar)

    users = SimpleNamespace(
        cook=create_user(db_session, restaurant, "KITCHEN"),
        grill_cook=create_user(db_session, restaurant, "KITCHEN"),
        barman=create_user(db_session, restaurant, "BAR"),
        unassigned_cook=create_user(db_session, restaurant, "KITCHEN"),
        waiter=create_user(db_session, restaurant, "WAITER"),
        manager=create_user(db_session, restaurant, "MANAGER"),
        admin=restaurant.user,
    )
    assign_station(db_session, users.cook, kitchen)
    assign_station(db_session, users.grill_cook, grill)
    assign_station(db_session, users.barman, bar)

    order_id = pos.create_order().json()["id"]
    item_ids = {}
    for name, item in (("burger", pos.burger), ("steak", steak), ("beer", beer)):
        response = pos.add_item(order_id, item)
        assert response.status_code == 201
        item_ids[name] = response.json()["id"]
    assert pos.send(order_id).status_code == 200

    return SimpleNamespace(
        pos=pos,
        db=db_session,
        restaurant=restaurant,
        stations=SimpleNamespace(kitchen=kitchen, grill=grill, bar=bar),
        users=users,
        order_id=order_id,
        preparations={
            name: pos.preparation(item_id).id
            for name, item_id in item_ids.items()
        },
    )


def queue(floor, user, station):
    return floor.pos.client.get(
        f"/api/v1/stations/{station.id}/preparations",
        headers=auth_headers(user),
    )


def act(floor, user, preparation_id, action):
    return floor.pos.client.post(
        f"/api/v1/preparations/{preparation_id}/{action}",
        headers=auth_headers(user),
    )


def status_of(floor, name):
    floor.db.expire_all()
    return floor.db.get(Preparation, floor.preparations[name]).status


def assert_station_denied(response):
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["error"]["code"] == "STATION_ACCESS_DENIED"
    assert body["error"]["message"] == "You are not assigned to this station"
    assert body["error"]["request_id"] == response.headers[REQUEST_ID_HEADER]


# --- Assigned stations work ---


def test_kitchen_user_works_their_kitchen_station(floor):
    cook = floor.users.cook

    response = queue(floor, cook, floor.stations.kitchen)

    assert response.status_code == 200
    # Only this station's preparations, nothing from the grill or bar.
    assert [item["id"] for item in response.json()] == [
        str(floor.preparations["burger"])
    ]

    for action, expected in (("start", "IN_PROGRESS"), ("ready", "READY")):
        response = act(floor, cook, floor.preparations["burger"], action)
        assert response.status_code == 200
        assert response.json()["status"] == expected

    # Audit attribution is unchanged: the cook acted.
    [started] = floor.pos.events("preparation.started")
    assert started.user_id == cook.id


def test_bar_user_works_their_bar_station(floor):
    barman = floor.users.barman

    response = queue(floor, barman, floor.stations.bar)

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [
        str(floor.preparations["beer"])
    ]

    for action in ("start", "ready", "serve"):
        assert act(floor, barman, floor.preparations["beer"], action).status_code == 200

    assert status_of(floor, "beer") == "SERVED"


# --- Other stations are refused, by queue and by direct ID ---


@pytest.mark.parametrize(
    ("user", "station", "preparation"),
    [
        ("cook", "grill", "steak"),        # same type, not assigned
        ("cook", "bar", "beer"),           # kitchen -> bar
        ("barman", "kitchen", "burger"),   # bar -> kitchen
        ("grill_cook", "kitchen", "burger"),
    ],
)
def test_unassigned_station_is_refused(floor, user, station, preparation):
    user = getattr(floor.users, user)

    assert_station_denied(queue(floor, user, getattr(floor.stations, station)))

    # Preparation IDs do not bypass the scope, for any transition.
    for action in ("start", "ready", "serve"):
        assert_station_denied(act(floor, user, floor.preparations[preparation], action))

    assert status_of(floor, preparation) == "PENDING"
    assert floor.pos.events("preparation.started") == []


def test_scope_is_checked_before_the_state_machine(floor):
    """A refused user learns nothing about the preparation's state."""

    response = act(floor, floor.users.barman, floor.preparations["burger"], "serve")

    assert_station_denied(response)


def test_state_machine_still_applies_within_scope(floor):
    response = act(floor, floor.users.cook, floor.preparations["burger"], "serve")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_PREPARATION_TRANSITION"
    assert status_of(floor, "burger") == "PENDING"


# --- Fail closed ---


def test_kitchen_user_without_assignments_has_no_station(floor):
    cook = floor.users.unassigned_cook

    for station in ("kitchen", "grill", "bar"):
        assert_station_denied(queue(floor, cook, getattr(floor.stations, station)))

    assert_station_denied(act(floor, cook, floor.preparations["burger"], "start"))

    me = floor.pos.client.get("/api/v1/me", headers=auth_headers(cook)).json()
    assert me["stations"] == []


def test_assignment_to_a_station_of_another_type_grants_nothing(floor):
    cook = floor.users.unassigned_cook
    assign_station(floor.db, cook, floor.stations.bar)

    assert_station_denied(queue(floor, cook, floor.stations.bar))
    assert_station_denied(act(floor, cook, floor.preparations["beer"], "start"))


def test_assignment_to_another_restaurants_station_grants_nothing(floor):
    other = create_restaurant(floor.db, name="Other Restaurant")
    cook = floor.users.unassigned_cook
    assign_station(floor.db, cook, other.station)  # a KITCHEN station

    assert_station_denied(queue(floor, cook, other.station))
    me = floor.pos.client.get("/api/v1/me", headers=auth_headers(cook)).json()
    assert me["stations"] == []


def test_inactive_station_grants_nothing(floor):
    floor.stations.kitchen.is_active = False
    floor.db.flush()

    assert_station_denied(queue(floor, floor.users.cook, floor.stations.kitchen))
    assert_station_denied(act(floor, floor.users.cook, floor.preparations["burger"], "start"))


@pytest.mark.parametrize("role_name", ["CASHIER", "SOMMELIER", "kitchen"])
def test_assignments_of_other_roles_grant_nothing(floor, role_name):
    """Only KITCHEN and BAR use assignments; other roles cannot gain
    preparation access through a stray assignment row."""

    user = create_user(floor.db, floor.restaurant, role_name)
    assign_station(floor.db, user, floor.stations.kitchen)

    response = queue(floor, user, floor.stations.kitchen)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"
    me = floor.pos.client.get("/api/v1/me", headers=auth_headers(user)).json()
    assert me["stations"] == []


def test_removed_assignment_applies_immediately(floor):
    cook = floor.users.cook
    assert queue(floor, cook, floor.stations.kitchen).status_code == 200

    floor.db.execute(
        delete(StationAssignment).where(StationAssignment.user_id == cook.id)
    )
    floor.db.flush()

    assert_station_denied(queue(floor, cook, floor.stations.kitchen))


# --- Cross-station roles ---


@pytest.mark.parametrize("user", ["manager", "admin"])
def test_management_works_every_station(floor, user):
    user = getattr(floor.users, user)

    for station in ("kitchen", "grill", "bar"):
        assert queue(floor, user, getattr(floor.stations, station)).status_code == 200

    for name in ("burger", "steak", "beer"):
        for action in ("start", "ready", "serve"):
            assert act(floor, user, floor.preparations[name], action).status_code == 200

    me = floor.pos.client.get("/api/v1/me", headers=auth_headers(user)).json()
    assert me["stations"] is None


def test_waiter_serves_from_every_station(floor):
    for name in ("burger", "steak", "beer"):
        preparation_id = floor.preparations[name]
        for action in ("start", "ready"):
            assert act(floor, floor.users.manager, preparation_id, action).status_code == 200

        response = act(floor, floor.users.waiter, preparation_id, "serve")
        assert response.status_code == 200

    # ...but has no queue access and no kitchen transitions.
    response = queue(floor, floor.users.waiter, floor.stations.kitchen)
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"


# --- /me and tenant isolation ---


def test_me_lists_the_assigned_stations(floor):
    cook = floor.users.cook
    assign_station(floor.db, cook, floor.stations.grill)
    assign_station(floor.db, cook, floor.stations.bar)  # wrong type: ignored

    me = floor.pos.client.get("/api/v1/me", headers=auth_headers(cook)).json()

    assert me["stations"] == [
        {"id": str(floor.stations.grill.id), "name": "Grill", "type": "KITCHEN"},
        {"id": str(floor.stations.kitchen.id), "name": "Kitchen", "type": "KITCHEN"},
    ]


def test_other_restaurants_station_staff_cannot_reach_ours(floor):
    other = create_restaurant(floor.db, name="Other Restaurant")
    foreign_cook = create_user(floor.db, other, "KITCHEN")
    assign_station(floor.db, foreign_cook, other.station)

    # Their own station works...
    assert queue(floor, foreign_cook, other.station).status_code == 200

    # ...ours does not, and our preparations look missing.
    assert_station_denied(queue(floor, foreign_cook, floor.stations.kitchen))
    response = act(floor, foreign_cook, floor.preparations["burger"], "start")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PREPARATION_NOT_FOUND"
    assert status_of(floor, "burger") == "PENDING"
