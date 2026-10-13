import uuid
from decimal import Decimal
from functools import cache
from types import SimpleNamespace

from sqlalchemy.orm import Session

from app.core.security import create_access_token, hash_password
from app.db.models.category import Category
from app.db.models.order import Order
from app.db.models.product import Product
from app.db.models.restaurant import Restaurant
from app.db.models.role import Role
from app.db.models.station import Station
from app.db.models.station_assignment import StationAssignment
from app.db.models.table import Table
from app.db.models.user import User


TEST_USER_PASSWORD = "test-user-password"


def create_restaurant(db: Session, name: str = "Test Restaurant"):
    """Create a minimal restaurant with one user, table, station and product."""

    restaurant = Restaurant(name=name)
    db.add(restaurant)
    db.flush()

    role = Role(restaurant_id=restaurant.id, name="ADMIN")
    db.add(role)
    db.flush()

    user = User(
        restaurant_id=restaurant.id,
        role_id=role.id,
        name="Test User",
        email=f"user-{restaurant.id}@example.com",
        password_hash=hash_password(TEST_USER_PASSWORD),
    )

    station = Station(
        restaurant_id=restaurant.id,
        name="Kitchen",
        type="KITCHEN",
    )

    table = Table(
        restaurant_id=restaurant.id,
        number=1,
        capacity=4,
    )

    category = Category(
        restaurant_id=restaurant.id,
        name="Food",
    )

    db.add_all([user, station, table, category])
    db.flush()

    product = Product(
        category_id=category.id,
        name="Burger",
        price=Decimal("12.50"),
        station_id=station.id,
    )
    db.add(product)
    db.flush()

    return SimpleNamespace(
        restaurant=restaurant,
        role=role,
        user=user,
        station=station,
        table=table,
        category=category,
        product=product,
    )


def create_order(db: Session, restaurant, status: str = "OPEN") -> Order:
    """Insert an order directly, bypassing OrderService side effects."""

    order = Order(
        restaurant_id=restaurant.restaurant.id,
        table_id=restaurant.table.id,
        user_id=restaurant.user.id,
        guest_count=2,
        status=status,
    )
    db.add(order)
    db.flush()

    return order


def auth_headers(user: User) -> dict[str, str]:
    token = create_access_token(
        user_id=user.id,
        restaurant_id=user.restaurant_id,
        role_id=user.role_id,
    )

    return {"Authorization": f"Bearer {token}"}


def create_user(db: Session, restaurant, role_name: str) -> User:
    """A user of `restaurant` with a (new or existing) role `role_name`."""

    role = (
        db.query(Role)
        .filter(
            Role.restaurant_id == restaurant.restaurant.id,
            Role.name == role_name,
        )
        .first()
    )

    if role is None:
        role = Role(restaurant_id=restaurant.restaurant.id, name=role_name)
        db.add(role)
        db.flush()

    user = User(
        restaurant_id=restaurant.restaurant.id,
        role_id=role.id,
        name=f"{role_name.title()} User",
        email=f"{role_name.lower()}-{uuid.uuid4()}@example.com",
        password_hash=_test_password_hash(),
    )
    db.add(user)
    db.flush()

    return user


@cache
def _test_password_hash() -> str:
    # bcrypt is deliberately slow; one hash serves every test user.
    return hash_password(TEST_USER_PASSWORD)


def assign_station(db: Session, user: User, station: Station) -> None:
    """Let a Kitchen or Bar user work `station` (station_assignments)."""

    db.add(StationAssignment(user_id=user.id, station_id=station.id))
    db.flush()


def create_station(db: Session, restaurant, name: str, station_type: str) -> Station:
    station = Station(
        restaurant_id=restaurant.restaurant.id,
        name=name,
        type=station_type,
    )
    db.add(station)
    db.flush()

    return station
