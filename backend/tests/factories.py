from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy.orm import Session

from app.core.security import create_access_token, hash_password
from app.db.models.category import Category
from app.db.models.product import Product
from app.db.models.restaurant import Restaurant
from app.db.models.role import Role
from app.db.models.station import Station
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


def auth_headers(user: User) -> dict[str, str]:
    token = create_access_token(
        user_id=user.id,
        restaurant_id=user.restaurant_id,
        role_id=user.role_id,
    )

    return {"Authorization": f"Bearer {token}"}
