"""GET /api/v1/stations lists the stations whose queue a user may view.

The restaurant has two kitchen stations (Kitchen, Grill), a Bar and an
inactive kitchen station (Old Kitchen); another restaurant has its own.
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from app.db.models.station_assignment import StationAssignment
from tests.factories import (
    assign_station,
    auth_headers,
    create_restaurant,
    create_station,
    create_user,
)


@pytest.fixture
def site(db_client, db_session, restaurant):
    kitchen = restaurant.station  # type KITCHEN
    grill = create_station(db_session, restaurant, "Grill", "KITCHEN")
    bar = create_station(db_session, restaurant, "Bar", "BAR")
    old_kitchen = create_station(db_session, restaurant, "Old Kitchen", "KITCHEN")
    old_kitchen.is_active = False
    db_session.flush()

    other = create_restaurant(db_session, name="Other Restaurant")

    users = SimpleNamespace(
        cook=create_user(db_session, restaurant, "KITCHEN"),
        barman=create_user(db_session, restaurant, "BAR"),
        unassigned_cook=create_user(db_session, restaurant, "KITCHEN"),
        waiter=create_user(db_session, restaurant, "WAITER"),
        cashier=create_user(db_session, restaurant, "CASHIER"),
        manager=create_user(db_session, restaurant, "MANAGER"),
        admin=restaurant.user,
        other_admin=other.user,
    )
    assign_station(db_session, users.cook, kitchen)
    assign_station(db_session, users.cook, grill)
    assign_station(db_session, users.barman, bar)

    return SimpleNamespace(
        client=db_client,
        db=db_session,
        stations=SimpleNamespace(
            kitchen=kitchen,
            grill=grill,
            bar=bar,
            old_kitchen=old_kitchen,
            other=other.station,
        ),
        users=users,
    )


def stations(site, user):
    response = site.client.get("/api/v1/stations", headers=auth_headers(user))
    assert response.status_code == 200, response.text
    return response.json()


def names(site, user):
    return [station["name"] for station in stations(site, user)]


def queue_status(site, user, station):
    return site.client.get(
        f"/api/v1/stations/{station.id}/preparations",
        headers=auth_headers(user),
    ).status_code


# --- Who sees what ---


@pytest.mark.parametrize("user", ["manager", "admin"])
def test_management_lists_every_station_of_the_restaurant(site, user):
    listed = stations(site, getattr(site.users, user))

    # By name; inactive stations included, as their queues are viewable.
    assert listed == [
        {"id": str(site.stations.bar.id), "name": "Bar", "type": "BAR", "is_active": True},
        {"id": str(site.stations.grill.id), "name": "Grill", "type": "KITCHEN", "is_active": True},
        {"id": str(site.stations.kitchen.id), "name": "Kitchen", "type": "KITCHEN", "is_active": True},
        {
            "id": str(site.stations.old_kitchen.id),
            "name": "Old Kitchen",
            "type": "KITCHEN",
            "is_active": False,
        },
    ]


def test_kitchen_user_lists_only_their_assigned_stations(site):
    assert names(site, site.users.cook) == ["Grill", "Kitchen"]


def test_bar_user_lists_only_their_bar(site):
    assert names(site, site.users.barman) == ["Bar"]


def test_kitchen_user_without_assignments_lists_nothing(site):
    assert stations(site, site.users.unassigned_cook) == []


def test_invalid_assignments_list_nothing(site):
    cook = site.users.unassigned_cook
    assign_station(site.db, cook, site.stations.bar)  # another type
    assign_station(site.db, cook, site.stations.old_kitchen)  # inactive
    assign_station(site.db, cook, site.stations.other)  # another restaurant

    assert stations(site, cook) == []


def test_assignment_changes_apply_immediately(site):
    cook = site.users.cook
    site.db.execute(
        delete(StationAssignment).where(
            StationAssignment.user_id == cook.id,
            StationAssignment.station_id == site.stations.grill.id,
        )
    )
    site.db.flush()

    assert names(site, cook) == ["Kitchen"]


def test_other_restaurants_stations_are_never_listed(site):
    assert names(site, site.users.other_admin) == ["Kitchen"]
    assert stations(site, site.users.other_admin)[0]["id"] == str(site.stations.other.id)

    for user in vars(site.users).values():
        if user is site.users.other_admin:
            continue
        response = site.client.get("/api/v1/stations", headers=auth_headers(user))
        if response.status_code == 200:
            assert str(site.stations.other.id) not in {s["id"] for s in response.json()}


# --- The list matches what the queue allows ---


@pytest.mark.parametrize("user", ["cook", "barman", "unassigned_cook", "manager"])
def test_listed_stations_are_exactly_the_viewable_queues(site, user):
    user = getattr(site.users, user)
    listed = {station["id"] for station in stations(site, user)}

    for station in vars(site.stations).values():
        if station is site.stations.other:
            continue
        expected = 200 if str(station.id) in listed else 403
        assert queue_status(site, user, station) == expected, station.name


# --- Refusals ---


@pytest.mark.parametrize("user", ["waiter", "cashier"])
def test_roles_without_queue_access_are_refused(site, user):
    response = site.client.get(
        "/api/v1/stations",
        headers=auth_headers(getattr(site.users, user)),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"
    assert response.json()["error"]["message"] == "Missing permission: preparations:view"


def test_unauthenticated_request_is_refused(site):
    assert site.client.get("/api/v1/stations").status_code == 401
    response = site.client.get(
        "/api/v1/stations",
        headers={"Authorization": "Bearer not-a-token"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "NOT_AUTHENTICATED"
