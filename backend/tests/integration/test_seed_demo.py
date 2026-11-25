"""backend/seed_demo.py on the PostgreSQL test database.

seed_demo() runs inside the rolled-back test session (its commit is only
a savepoint), so nothing outlives a test. It refuses this *_test database
unless called with allow_test_database=True, which only seed() below
passes; the command line never does. Both refusals are checked against
the live connection.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal

import pytest
from sqlalchemy import delete, event, func, select, text

from app.core.permissions import ROLE_PERMISSIONS, RoleName
from app.core.security import verify_password
from app.db.models.cash import BusinessDay, CashRegister, CashShift
from app.db.models.category import Category
from app.db.models.modifier import Modifier, ModifierGroup, product_modifier_groups
from app.db.models.order import Order
from app.db.models.product import Product
from app.db.models.restaurant import Restaurant
from app.db.models.role import Role
from app.db.models.station import Station
from app.db.models.station_assignment import StationAssignment
from app.db.models.table import Table
from app.db.models.user import User
from seed_demo import (
    ADMIN_EMAIL,
    EMAIL_DOMAIN,
    RESTAURANT_NAME,
    USERS,
    SeedConflict,
    SeedRefused,
    check_schema_version,
    expected_migration_heads,
    seed_demo,
    verify_connected_database,
)
from tests.factories import create_restaurant


PASSWORD = "integration-demo-password"

EXPECTED_COUNTS = {
    "roles": 6,
    "users": 7,
    "stations": 2,
    "station assignments": 2,
    "tables": 8,
    "categories": 4,
    "products": 16,
    "modifier groups": 4,
    "modifiers": 11,
    "product modifier groups": 5,
}

EXPECTED_CREATED = {
    "restaurant": 1,
    "role": 6,
    "station": 2,
    "user": 7,
    "station assignment": 2,
    "table": 8,
    "category": 4,
    "modifier group": 4,
    "modifier": 11,
    "product": 16,
    "product modifier group": 5,
}


# --- Helpers --------------------------------------------------------------

def seed(db, password: str = PASSWORD):
    """seed_demo() on this *_test database, with the explicit test-only permission."""

    return seed_demo(db, password, allow_test_database=True)


def demo_restaurant(db) -> Restaurant | None:
    return db.scalar(select(Restaurant).where(Restaurant.name == RESTAURANT_NAME))


def demo_user(db, email: str) -> User:
    return db.scalar(select(User).where(User.email == email))


def demo_product(db, name: str) -> Product:
    return db.scalar(
        select(Product).join(Category, Category.id == Product.category_id)
        .join(Restaurant, Restaurant.id == Category.restaurant_id)
        .where(Restaurant.name == RESTAURANT_NAME, Product.name == name)
    )


def counts(db, restaurant_id) -> dict[str, int]:
    def count(statement) -> int:
        return db.scalar(select(func.count()).select_from(statement.subquery()))

    link = product_modifier_groups.c
    return {
        "roles": count(select(Role.id).where(Role.restaurant_id == restaurant_id)),
        "users": count(select(User.id).where(User.restaurant_id == restaurant_id)),
        "stations": count(select(Station.id).where(Station.restaurant_id == restaurant_id)),
        "station assignments": count(
            select(StationAssignment.user_id)
            .join(User, User.id == StationAssignment.user_id)
            .where(User.restaurant_id == restaurant_id)
        ),
        "tables": count(select(Table.id).where(Table.restaurant_id == restaurant_id)),
        "categories": count(select(Category.id).where(Category.restaurant_id == restaurant_id)),
        "products": count(
            select(Product.id).join(Category, Category.id == Product.category_id)
            .where(Category.restaurant_id == restaurant_id)
        ),
        "modifier groups": count(select(ModifierGroup.id).where(ModifierGroup.restaurant_id == restaurant_id)),
        "modifiers": count(
            select(Modifier.id).join(ModifierGroup, ModifierGroup.id == Modifier.group_id)
            .where(ModifierGroup.restaurant_id == restaurant_id)
        ),
        "product modifier groups": count(
            select(link.product_id)
            .join(ModifierGroup, ModifierGroup.id == link.modifier_group_id)
            .where(ModifierGroup.restaurant_id == restaurant_id)
        ),
    }


def snapshot(db, restaurant_id) -> dict:
    """Every demo row's identity and the user columns a rerun must keep."""

    link = product_modifier_groups.c
    return {
        "users": sorted(
            (u.email, u.id, u.name, u.role_id, u.password_hash)
            for u in db.scalars(select(User).where(User.restaurant_id == restaurant_id))
        ),
        "roles": sorted(db.scalars(select(Role.id).where(Role.restaurant_id == restaurant_id))),
        "stations": sorted(db.scalars(select(Station.id).where(Station.restaurant_id == restaurant_id))),
        "tables": sorted(db.scalars(select(Table.id).where(Table.restaurant_id == restaurant_id))),
        "products": sorted(
            (p.id, p.price, p.is_active)
            for p in db.scalars(
                select(Product).join(Category, Category.id == Product.category_id)
                .where(Category.restaurant_id == restaurant_id)
            )
        ),
        "modifiers": sorted(
            db.scalars(
                select(Modifier.id).join(ModifierGroup, ModifierGroup.id == Modifier.group_id)
                .where(ModifierGroup.restaurant_id == restaurant_id)
            )
        ),
        "links": sorted(
            db.execute(
                select(link.product_id, link.modifier_group_id)
                .join(ModifierGroup, ModifierGroup.id == link.modifier_group_id)
                .where(ModifierGroup.restaurant_id == restaurant_id)
            ).all()
        ),
    }


def demo_restaurants(db) -> int:
    return db.scalar(
        select(func.count()).select_from(Restaurant).where(Restaurant.name == RESTAURANT_NAME)
    )


@contextmanager
def recorded_statements(db) -> Iterator[list[str]]:
    connection = db.connection()
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(connection, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(connection, "before_cursor_execute", record)


def assert_no_update_or_delete(statements: list[str]) -> None:
    writes = [s for s in statements if s.lstrip().upper().startswith(("UPDATE", "DELETE"))]
    assert writes == []


def login(client, email: str, password: str = PASSWORD):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def headers_for(client, email: str) -> dict[str, str]:
    response = login(client, email)
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


# --- First run ------------------------------------------------------------

def test_first_run_creates_the_complete_demo(db_session):
    report = seed(db_session, PASSWORD)

    assert report.created == EXPECTED_CREATED
    assert report.existing == {}

    restaurant = demo_restaurant(db_session)
    assert (restaurant.timezone, restaurant.currency) == ("Europe/Madrid", "EUR")
    assert counts(db_session, restaurant.id) == EXPECTED_COUNTS


def test_first_run_creates_no_orders_bills_days_or_shifts(db_session):
    seed(db_session, PASSWORD)
    restaurant_id = demo_restaurant(db_session).id

    for model in (Order, BusinessDay, CashRegister, CashShift):
        assert db_session.scalar(
            select(func.count()).select_from(model).where(model.restaurant_id == restaurant_id)
        ) == 0, model.__name__


def test_every_account_gets_its_role_station_and_own_password_hash(db_session):
    seed(db_session, PASSWORD)

    hashes = set()
    for spec in USERS:
        user = demo_user(db_session, spec.email)
        assert user.email.endswith(f"@{EMAIL_DOMAIN}")
        assert db_session.get(Role, user.role_id).name == spec.role
        assert verify_password(PASSWORD, user.password_hash)
        assert PASSWORD not in user.password_hash
        hashes.add(user.password_hash)

        stations = db_session.scalars(
            select(Station.name)
            .join(StationAssignment, StationAssignment.station_id == Station.id)
            .where(StationAssignment.user_id == user.id)
        ).all()
        assert stations == ([spec.station] if spec.station else [])

    assert len(hashes) == len(USERS)  # one salt per account


def test_money_is_stored_exactly(db_session):
    seed(db_session, PASSWORD)

    burger = demo_product(db_session, "Classic burger")
    oat = db_session.scalar(
        select(Modifier).join(ModifierGroup, ModifierGroup.id == Modifier.group_id)
        .where(ModifierGroup.restaurant_id == demo_restaurant(db_session).id, Modifier.name == "Oat milk")
    )

    assert burger.price == Decimal("13.50")
    assert oat.price_adjustment == Decimal("0.40")


# --- Repeated and partial runs --------------------------------------------

def test_a_repeated_run_creates_nothing_and_changes_nothing(db_session):
    seed(db_session, PASSWORD)
    restaurant_id = demo_restaurant(db_session).id
    before = snapshot(db_session, restaurant_id)

    with recorded_statements(db_session) as statements:
        report = seed(db_session, PASSWORD)

    assert report.created == {}
    assert report.existing == EXPECTED_CREATED
    assert [n for n in report.notices if n.startswith(("differs", "notice"))] == []
    assert snapshot(db_session, restaurant_id) == before
    assert_no_update_or_delete(statements)


def test_a_rerun_recreates_only_what_is_missing(db_session):
    seed(db_session, PASSWORD)
    restaurant_id = demo_restaurant(db_session).id
    kitchen_user = demo_user(db_session, f"kitchen@{EMAIL_DOMAIN}")
    burger = demo_product(db_session, "Classic burger")
    doneness = db_session.scalar(select(ModifierGroup).where(
        ModifierGroup.restaurant_id == restaurant_id, ModifierGroup.name == "Doneness"
    ))

    # The test removes rows; the seed never does.
    link = product_modifier_groups.c
    db_session.execute(delete(Table).where(Table.restaurant_id == restaurant_id, Table.number == 8))
    db_session.execute(delete(product_modifier_groups).where(
        link.product_id == burger.id, link.modifier_group_id == doneness.id
    ))
    db_session.execute(delete(StationAssignment).where(StationAssignment.user_id == kitchen_user.id))
    db_session.flush()
    db_session.expire_all()

    with recorded_statements(db_session) as statements:
        report = seed(db_session, PASSWORD)

    assert report.created == {"table": 1, "product modifier group": 1, "station assignment": 1}
    assert counts(db_session, restaurant_id) == EXPECTED_COUNTS
    assert_no_update_or_delete(statements)


def test_a_rerun_keeps_edited_rows_and_reports_them(db_session):
    seed(db_session, PASSWORD)
    cashier = demo_user(db_session, f"cashier1@{EMAIL_DOMAIN}")
    burger = demo_product(db_session, "Classic burger")
    cashier.name = "Renamed by hand"
    cashier.password_hash = "kept-as-is"
    burger.price = Decimal("15.00")
    db_session.flush()

    with recorded_statements(db_session) as statements:
        report = seed(db_session, PASSWORD)

    db_session.expire_all()
    assert demo_user(db_session, f"cashier1@{EMAIL_DOMAIN}").name == "Renamed by hand"
    assert demo_user(db_session, f"cashier1@{EMAIL_DOMAIN}").password_hash == "kept-as-is"
    assert db_session.get(Product, burger.id).price == Decimal("15.00")
    assert report.created == {}
    assert f"differs  user cashier1@{EMAIL_DOMAIN}: name (left unchanged)" in report.notices
    assert "differs  product Classic burger: price (left unchanged)" in report.notices
    assert_no_update_or_delete(statements)


def test_a_rerun_with_another_password_rotates_nothing(db_session):
    seed(db_session, PASSWORD)
    before = demo_user(db_session, ADMIN_EMAIL).password_hash

    seed(db_session, "another-demo-password")

    assert demo_user(db_session, ADMIN_EMAIL).password_hash == before


# --- Conflicts write nothing ----------------------------------------------

def test_a_demo_email_in_another_restaurant_aborts_with_nothing_written(db_session):
    other = create_restaurant(db_session, name="Not The Demo")
    db_session.add(User(
        restaurant_id=other.restaurant.id,
        role_id=other.role.id,
        name="Someone else",
        email=f"cashier2@{EMAIL_DOMAIN}",
        password_hash="x",
    ))
    db_session.flush()

    with pytest.raises(SeedConflict, match="another restaurant"):
        seed(db_session, PASSWORD)

    assert demo_restaurants(db_session) == 0
    assert db_session.scalar(
        select(func.count()).select_from(User).where(User.email.like(f"%@{EMAIL_DOMAIN}"))
    ) == 1  # only the conflicting account, untouched


def test_a_demo_account_with_another_role_aborts_with_nothing_written(db_session):
    seed(db_session, PASSWORD)
    restaurant_id = demo_restaurant(db_session).id
    waiter_role = db_session.scalar(select(Role).where(Role.restaurant_id == restaurant_id, Role.name == "WAITER"))
    demo_user(db_session, f"cashier1@{EMAIL_DOMAIN}").role_id = waiter_role.id
    # Missing rows the run would recreate if it went ahead.
    db_session.execute(delete(Table).where(Table.restaurant_id == restaurant_id, Table.number == 1))
    db_session.flush()
    before = snapshot(db_session, restaurant_id)

    with pytest.raises(SeedConflict, match="another role"):
        seed(db_session, PASSWORD)

    assert snapshot(db_session, restaurant_id) == before
    assert counts(db_session, restaurant_id)["tables"] == 7


def test_a_demo_admin_of_another_restaurant_aborts_with_nothing_written(db_session):
    other = create_restaurant(db_session, name="Not The Demo")
    db_session.add(User(
        restaurant_id=other.restaurant.id,
        role_id=other.role.id,  # ADMIN
        name="Someone else",
        email=ADMIN_EMAIL,
        password_hash="x",
    ))
    db_session.flush()

    with pytest.raises(SeedConflict, match="not 'RestaurantOS Demo Bistro'"):
        seed(db_session, PASSWORD)

    assert demo_restaurants(db_session) == 0
    assert counts(db_session, other.restaurant.id)["users"] == 2


def test_a_demo_named_restaurant_without_the_admin_is_refused(db_session):
    db_session.add(Restaurant(name=RESTAURANT_NAME))
    db_session.flush()

    with pytest.raises(SeedConflict, match="refusing to guess"):
        seed(db_session, PASSWORD)

    assert demo_restaurants(db_session) == 1
    assert db_session.scalar(
        select(func.count()).select_from(User).where(User.email.like(f"%@{EMAIL_DOMAIN}"))
    ) == 0


def test_an_unusable_password_is_refused_before_any_write(db_session):
    with recorded_statements(db_session) as statements:
        with pytest.raises(SeedRefused):
            seed(db_session, "short")

    assert statements == []
    assert demo_restaurants(db_session) == 0


# --- Database guard on the live connection --------------------------------

def test_the_guard_refuses_this_test_database(db_session):
    connected = db_session.scalar(text("SELECT current_database()"))
    assert connected.endswith("_test")

    with pytest.raises(SeedRefused, match="test databases are never seeded"):
        verify_connected_database(db_session, connected)


def test_seed_demo_without_test_permission_refuses_this_database_before_locking(db_session):
    with recorded_statements(db_session) as statements:
        with pytest.raises(SeedRefused, match="test databases are never seeded"):
            seed_demo(db_session, PASSWORD)

    assert statements == ["SELECT current_database()"]
    assert demo_restaurants(db_session) == 0


def test_the_migrated_test_database_passes_the_schema_check(db_session):
    check_schema_version(db_session, expected_migration_heads())


# --- Through the API ------------------------------------------------------

def test_every_demo_account_signs_in_with_the_permissions_of_its_role(db_session, db_client):
    seed(db_session, PASSWORD)

    for spec in USERS:
        me = db_client.get("/api/v1/me", headers=headers_for(db_client, spec.email))
        assert me.status_code == 200, me.text
        body = me.json()

        assert body["role"]["name"] == spec.role
        assert set(body["permissions"]) == set(ROLE_PERMISSIONS[RoleName(spec.role)])

        stations = body["stations"]
        if spec.role in ("KITCHEN", "BAR"):
            assert [s["name"] for s in stations] == [spec.station]
        elif spec.role == "CASHIER":
            assert stations == []
        else:  # admin, manager, waiter: every station
            assert stations is None


def test_a_wrong_password_is_rejected(db_session, db_client):
    seed(db_session, PASSWORD)

    assert login(db_client, ADMIN_EMAIL, "not-the-demo-password").status_code == 401


def test_the_kitchen_display_shows_only_the_assigned_station(db_session, db_client):
    seed(db_session, PASSWORD)

    kitchen = db_client.get("/api/v1/stations", headers=headers_for(db_client, f"kitchen@{EMAIL_DOMAIN}"))
    admin = db_client.get("/api/v1/stations", headers=headers_for(db_client, ADMIN_EMAIL))

    assert [s["name"] for s in kitchen.json()] == ["Kitchen"]
    assert sorted(s["name"] for s in admin.json()) == ["Bar", "Kitchen"]


def test_the_waiter_sees_the_demo_menu(db_session, db_client):
    seed(db_session, PASSWORD)

    menu = db_client.get("/api/v1/menu", headers=headers_for(db_client, f"waiter@{EMAIL_DOMAIN}"))
    assert menu.status_code == 200, menu.text
    body = menu.json()
    products = {p["name"]: p for c in body["categories"] for p in c["products"]}

    assert body["currency"] == "EUR"
    assert [c["name"] for c in body["categories"]] == ["Starters", "Mains", "Desserts", "Drinks"]
    assert len(products) == 16
    assert products["Seasonal fruit tart"]["is_available"] is False
    assert products["Classic burger"]["is_available"] is True
    doneness = next(g for g in products["Classic burger"]["modifier_groups"] if g["name"] == "Doneness")
    assert (doneness["is_required"], doneness["min_selections"], doneness["max_selections"]) == (True, 1, 1)
