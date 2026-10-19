"""Station event streams (Epic D4.1): WebSocket authentication and scope.

Uses committed data and the real application (no session override): a
stream checks authorization on its own short sessions, again while it is
open, so revoking access must be visible to other connections.
"""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_access_token
from app.db.models.station import Station
from app.main import app
from app.realtime.hub import StationEventHub
from tests.factories import assign_station, create_station, create_user


WAIT_SECONDS = 5


def token_for(user_id, restaurant_id, role_id):
    return create_access_token(
        user_id=user_id,
        restaurant_id=restaurant_id,
        role_id=role_id,
    )


@pytest.fixture
def hub(monkeypatch):
    hub = StationEventHub(max_connections=50, queue_size=10)
    monkeypatch.setattr(app.state, "station_events", hub)
    return hub


@pytest.fixture
def floor(db_engine, committed_restaurants, hub):
    """Restaurant A with Kitchen, Grill, Bar and an inactive kitchen
    station and one user per case; restaurant B with its own users."""

    a_id = committed_restaurants("A").restaurant_id
    b_id = committed_restaurants("B").restaurant_id
    a = SimpleNamespace(restaurant=SimpleNamespace(id=a_id))
    b = SimpleNamespace(restaurant=SimpleNamespace(id=b_id))

    with Session(db_engine) as db:
        kitchen = db.query(Station).filter_by(restaurant_id=a_id, name="Kitchen").one()
        grill = create_station(db, a, "Grill", "KITCHEN")
        bar = create_station(db, a, "Bar", "BAR")
        closed = create_station(db, a, "Closed", "KITCHEN")
        closed.is_active = False
        b_kitchen = db.query(Station).filter_by(restaurant_id=b_id, name="Kitchen").one()

        users = {
            "cook": create_user(db, a, "KITCHEN"),
            "barman": create_user(db, a, "BAR"),
            "unassigned_cook": create_user(db, a, "KITCHEN"),
            "cook_on_bar": create_user(db, a, "KITCHEN"),
            "cook_on_closed": create_user(db, a, "KITCHEN"),
            "inactive_cook": create_user(db, a, "KITCHEN"),
            "waiter": create_user(db, a, "WAITER"),
            "cashier": create_user(db, a, "CASHIER"),
            "manager": create_user(db, a, "MANAGER"),
            "b_manager": create_user(db, b, "MANAGER"),
            "b_cook": create_user(db, b, "KITCHEN"),
        }
        assign_station(db, users["cook"], kitchen)
        assign_station(db, users["barman"], bar)
        assign_station(db, users["cook_on_bar"], bar)
        assign_station(db, users["cook_on_closed"], closed)
        assign_station(db, users["inactive_cook"], kitchen)
        # A row pointing at another restaurant's station grants nothing.
        assign_station(db, users["b_cook"], kitchen)
        users["inactive_cook"].is_active = False

        tokens = {
            name: token_for(user.id, user.restaurant_id, user.role_id)
            for name, user in users.items()
        }
        ids = {name: user.id for name, user in users.items()}
        stations = SimpleNamespace(
            kitchen=kitchen.id,
            grill=grill.id,
            bar=bar.id,
            closed=closed.id,
            b_kitchen=b_kitchen.id,
        )
        db.commit()

    return SimpleNamespace(
        a_id=a_id,
        b_id=b_id,
        stations=stations,
        tokens=tokens,
        ids=ids,
        engine=db_engine,
        client=TestClient(app),
    )


def connect(floor, station_id):
    return floor.client.websocket_connect(f"/api/v1/stations/{station_id}/events")


def authenticate(ws, token, **extra):
    ws.send_json({"type": "authenticate", "token": token, **extra})


def receive(ws):
    """The next server message, failing (not hanging) if none arrives."""

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(ws.receive).result(timeout=WAIT_SECONDS)
    except FutureTimeout:
        pytest.fail("Timed out waiting for a server message")
    finally:
        pool.shutdown(wait=False)


def close_of(ws):
    message = receive(ws)
    assert message["type"] == "websocket.close", message
    return message["code"], message.get("reason")


def receive_json(ws):
    message = receive(ws)
    assert message["type"] == "websocket.send", message
    return json.loads(message["text"])


def subscribe(floor, station_id, token):
    """An open, authorized stream (a context manager)."""

    session = connect(floor, station_id)
    ws = session.__enter__()
    authenticate(ws, token)
    assert receive_json(ws) == {"type": "subscribed", "station_id": str(station_id)}
    return session, ws


def wait_until(condition, what):
    deadline = time.monotonic() + WAIT_SECONDS
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail(f"Timed out waiting for {what}")
        time.sleep(0.01)


def rejection(floor, station_id, token=None, **extra):
    with connect(floor, station_id) as ws:
        if token is not None:
            authenticate(ws, token, **extra)
        return close_of(ws)


# --- Authorized connections ---


@pytest.mark.parametrize(
    "user, station",
    [
        ("cook", "kitchen"),
        ("barman", "bar"),
        # Managers see every station of their restaurant, active or not,
        # as on GET /stations/{id}/preparations.
        ("manager", "grill"),
        ("manager", "closed"),
    ],
)
def test_authorized_user_subscribes_to_a_station(floor, hub, user, station):
    station_id = getattr(floor.stations, station)

    with connect(floor, station_id) as ws:
        authenticate(ws, floor.tokens[user])

        assert receive_json(ws) == {
            "type": "subscribed",
            "station_id": str(station_id),
        }
        [subscription] = hub.station_subscribers(floor.a_id, station_id)
        assert subscription.user_id == floor.ids[user]
        assert subscription.restaurant_id == floor.a_id

    wait_until(lambda: hub.connection_count == 0, "cleanup")


def test_disconnect_removes_the_subscription(floor, hub):
    first = subscribe(floor, floor.stations.kitchen, floor.tokens["cook"])
    second = subscribe(floor, floor.stations.kitchen, floor.tokens["manager"])
    assert hub.connection_count == 2

    first[0].__exit__(None, None, None)
    wait_until(lambda: hub.connection_count == 1, "first cleanup")
    [left] = hub.station_subscribers(floor.a_id, floor.stations.kitchen)
    assert left.user_id == floor.ids["manager"]

    second[0].__exit__(None, None, None)
    wait_until(lambda: hub.connection_count == 0, "second cleanup")
    assert hub.restaurant_subscribers(floor.a_id) == []


def test_client_messages_after_authentication_are_ignored(floor, hub):
    session, ws = subscribe(floor, floor.stations.kitchen, floor.tokens["cook"])

    ws.send_json({"type": "subscribe", "station_id": str(floor.stations.bar)})
    ws.send_text("noise")

    assert hub.station_subscribers(floor.a_id, floor.stations.bar) == []
    assert len(hub.station_subscribers(floor.a_id, floor.stations.kitchen)) == 1
    session.__exit__(None, None, None)


# --- Authentication ---


def test_connection_without_authentication_times_out(floor, hub, monkeypatch):
    monkeypatch.setattr(settings, "websocket_auth_timeout_seconds", 0.2)

    assert rejection(floor, floor.stations.kitchen) == (4401, "NOT_AUTHENTICATED")
    assert hub.connection_count == 0


@pytest.mark.parametrize(
    "first_message",
    [
        "not json",
        '["authenticate"]',
        '{"type": "hello", "token": "x"}',
        '{"type": "authenticate"}',
        '{"type": "authenticate", "token": ""}',
        '{"type": "authenticate", "token": 42}',
    ],
)
def test_malformed_authentication_is_rejected(floor, hub, first_message):
    with connect(floor, floor.stations.kitchen) as ws:
        ws.send_text(first_message)
        assert close_of(ws) == (4401, "NOT_AUTHENTICATED")


def test_binary_authentication_is_rejected(floor, hub):
    with connect(floor, floor.stations.kitchen) as ws:
        ws.send_bytes(b'{"type": "authenticate"}')
        assert close_of(ws) == (4401, "NOT_AUTHENTICATED")


def forged(floor, **claims):
    claims.setdefault("sub", str(floor.ids["cook"]))
    claims.setdefault("exp", datetime.now(timezone.utc) + timedelta(minutes=5))
    secret = claims.pop("secret", settings.jwt_secret_key)
    return jwt.encode(claims, secret, algorithm=settings.jwt_algorithm)


@pytest.mark.parametrize(
    "case",
    ["garbage", "wrong secret", "expired", "unknown user", "no subject", "no expiry"],
)
def test_invalid_credentials_are_rejected(floor, hub, case):
    token = {
        "garbage": lambda: "not-a-token",
        "wrong secret": lambda: forged(floor, secret="another-secret"),
        "expired": lambda: forged(
            floor, exp=datetime.now(timezone.utc) - timedelta(seconds=1)
        ),
        "unknown user": lambda: forged(floor, sub=str(uuid.uuid4())),
        "no subject": lambda: forged(floor, sub=None),
        "no expiry": lambda: jwt.encode(
            {"sub": str(floor.ids["cook"])},
            settings.jwt_secret_key,
            algorithm=settings.jwt_algorithm,
        ),
    }[case]()

    assert rejection(floor, floor.stations.kitchen, token) == (
        4401,
        "NOT_AUTHENTICATED",
    )
    assert hub.connection_count == 0


def test_inactive_user_is_rejected(floor, hub):
    assert rejection(
        floor, floor.stations.kitchen, floor.tokens["inactive_cook"]
    ) == (4403, "USER_INACTIVE")


# --- Authorization ---


@pytest.mark.parametrize("user", ["waiter", "cashier"])
def test_roles_without_preparations_view_are_rejected(floor, hub, user):
    assert rejection(floor, floor.stations.kitchen, floor.tokens[user]) == (
        4403,
        "PERMISSION_DENIED",
    )


@pytest.mark.parametrize(
    "user, station",
    [
        # Assigned elsewhere, or not at all.
        ("cook", "grill"),
        ("cook", "bar"),
        ("barman", "kitchen"),
        ("unassigned_cook", "kitchen"),
        # Assigned, but to a station of another type, or an inactive one.
        ("cook_on_bar", "bar"),
        ("cook_on_closed", "closed"),
    ],
)
def test_station_scope_is_enforced(floor, hub, user, station):
    assert rejection(floor, getattr(floor.stations, station), floor.tokens[user]) == (
        4403,
        "STATION_ACCESS_DENIED",
    )
    assert hub.connection_count == 0


def test_scoped_user_cannot_tell_missing_from_unassigned_stations(floor, hub):
    assert rejection(floor, uuid.uuid4(), floor.tokens["cook"]) == (
        4403,
        "STATION_ACCESS_DENIED",
    )


def test_other_restaurants_station_is_not_found_for_a_manager(floor, hub):
    assert rejection(floor, floor.stations.kitchen, floor.tokens["b_manager"]) == (
        4404,
        "STATION_NOT_FOUND",
    )
    assert rejection(floor, uuid.uuid4(), floor.tokens["manager"]) == (
        4404,
        "STATION_NOT_FOUND",
    )


def test_assignment_to_another_restaurants_station_grants_nothing(floor, hub):
    assert rejection(floor, floor.stations.kitchen, floor.tokens["b_cook"]) == (
        4403,
        "STATION_ACCESS_DENIED",
    )


def test_client_supplied_scope_is_ignored(floor, hub):
    """Tenant, role and stations come from the database only."""

    assert rejection(
        floor,
        floor.stations.kitchen,
        floor.tokens["b_manager"],
        restaurant_id=str(floor.a_id),
        role="ADMIN",
        stations=[str(floor.stations.kitchen)],
    ) == (4404, "STATION_NOT_FOUND")

    with connect(floor, floor.stations.b_kitchen) as ws:
        authenticate(ws, floor.tokens["b_manager"], restaurant_id=str(floor.a_id))
        assert receive_json(ws)["type"] == "subscribed"
        [subscription] = hub.restaurant_subscribers(floor.b_id)
        assert subscription.station_id == floor.stations.b_kitchen

    assert hub.restaurant_subscribers(floor.a_id) == []


# --- While open ---


def revoke(floor, sql, **params):
    with floor.engine.begin() as connection:
        connection.execute(text(sql), params)


@pytest.mark.parametrize(
    "change, reason",
    [
        (
            "DELETE FROM station_assignments WHERE user_id = :u",
            "STATION_ACCESS_DENIED",
        ),
        ("UPDATE users SET is_active = false WHERE id = :u", "USER_INACTIVE"),
        (
            "UPDATE stations SET is_active = false WHERE id = :s",
            "STATION_ACCESS_DENIED",
        ),
        (
            "UPDATE users SET role_id = (SELECT id FROM roles WHERE "
            "restaurant_id = :r AND name = 'WAITER') WHERE id = :u",
            "PERMISSION_DENIED",
        ),
    ],
)
def test_revoked_access_closes_an_open_stream(floor, hub, monkeypatch, change, reason):
    monkeypatch.setattr(settings, "websocket_reauth_interval_seconds", 0.1)
    session, ws = subscribe(floor, floor.stations.kitchen, floor.tokens["cook"])

    revoke(floor, change, u=floor.ids["cook"], s=floor.stations.kitchen, r=floor.a_id)

    assert close_of(ws) == (4403, reason)
    wait_until(lambda: hub.connection_count == 0, "cleanup")
    session.__exit__(None, None, None)


def test_unchanged_access_keeps_the_stream_open(floor, hub, monkeypatch):
    monkeypatch.setattr(settings, "websocket_reauth_interval_seconds", 0.05)
    session, ws = subscribe(floor, floor.stations.kitchen, floor.tokens["cook"])

    time.sleep(0.3)  # several checks

    assert hub.connection_count == 1
    session.__exit__(None, None, None)
    wait_until(lambda: hub.connection_count == 0, "cleanup")


def test_stream_closes_when_the_token_expires(floor, hub):
    token = forged(floor, exp=datetime.now(timezone.utc) + timedelta(seconds=1.5))
    session, ws = subscribe(floor, floor.stations.kitchen, token)

    assert close_of(ws) == (4401, "NOT_AUTHENTICATED")
    wait_until(lambda: hub.connection_count == 0, "cleanup")
    session.__exit__(None, None, None)


def test_full_process_refuses_new_connections(floor, monkeypatch):
    small = StationEventHub(max_connections=1, queue_size=10)
    monkeypatch.setattr(app.state, "station_events", small)
    session, _ = subscribe(floor, floor.stations.kitchen, floor.tokens["cook"])

    assert rejection(floor, floor.stations.kitchen, floor.tokens["manager"]) == (
        1013,
        "TOO_MANY_CONNECTIONS",
    )

    session.__exit__(None, None, None)
    wait_until(lambda: small.connection_count == 0, "cleanup")
    second, _ = subscribe(floor, floor.stations.kitchen, floor.tokens["manager"])
    second.__exit__(None, None, None)


def test_logs_never_contain_the_token(floor, hub, caplog, monkeypatch):
    monkeypatch.setattr(logging.getLogger("app.realtime"), "disabled", False)
    caplog.set_level(logging.DEBUG)

    session, _ = subscribe(floor, floor.stations.kitchen, floor.tokens["cook"])
    session.__exit__(None, None, None)
    wait_until(lambda: hub.connection_count == 0, "cleanup")
    rejection(floor, floor.stations.bar, floor.tokens["cook"])
    rejection(floor, floor.stations.kitchen, "not-a-token-secret-value")

    assert "Station stream opened" in caplog.text
    assert "STATION_ACCESS_DENIED" in caplog.text
    assert floor.tokens["cook"] not in caplog.text
    assert "not-a-token-secret-value" not in caplog.text
