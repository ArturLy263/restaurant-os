"""Disposable database for the Waiter POS / KDS end-to-end acceptance run.

Started by frontend/e2e/pos.e2e.mjs; not meant to be run by hand.

1. Creates a fresh database with tests/disposable_database.py (random name,
   comment token; the development and test databases are refused).
2. Migrates it to head and seeds two restaurants (see `seed`).
3. Prints one JSON line {"ready": {...}} with its URL and the seeded ids.
4. Answers SQL on stdin, one JSON request per line ({"sql": ..., "params":
   {...}}), each in its own committed transaction, with one JSON line
   {"rows": [[...], ...]} or {"error": "..."}.
5. Drops the database when stdin closes (also when the runner crashes).

The E2E password comes from E2E_PASSWORD; nothing is printed but ids.
"""

import json
import os
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "tests"))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from dotenv import dotenv_values  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from disposable_database import disposable_database  # noqa: E402


def seed(session: Session, password: str) -> dict:
    # Imported after the database exists: app.core.config reads settings.
    from app.core.security import hash_password
    from app.db.models.category import Category
    from app.db.models.modifier import Modifier, ModifierGroup
    from app.db.models.order import Order, OrderItem
    from app.db.models.product import Product
    from app.db.models.restaurant import Restaurant
    from app.db.models.role import Role
    from app.db.models.station import Station
    from app.db.models.station_assignment import StationAssignment
    from app.db.models.table import Table
    from app.db.models.user import User

    password_hash = hash_password(password)

    def restaurant(name: str, slug: str) -> dict:
        r = Restaurant(name=name, currency="EUR")
        session.add(r)
        session.flush()
        roles = {
            role: Role(restaurant_id=r.id, name=role)
            for role in ("WAITER", "KITCHEN", "BAR")
        }
        session.add_all(roles.values())
        kitchen = Station(restaurant_id=r.id, name="Kitchen", type="KITCHEN")
        bar = Station(restaurant_id=r.id, name="Bar", type="BAR")
        session.add_all([kitchen, bar])
        session.flush()
        users = {
            key: User(
                restaurant_id=r.id,
                role_id=roles[role].id,
                name=f"{label} ({slug})",
                email=f"{key}@{slug}.e2e.example.com",
                password_hash=password_hash,
            )
            for key, role, label in (
                ("waiter", "WAITER", "Wendy Waiter"),
                ("waiter2", "WAITER", "Walt Waiter"),
                ("cook", "KITCHEN", "Kim Cook"),
                ("barman", "BAR", "Bo Barman"),
            )
        }
        session.add_all(users.values())
        session.flush()
        session.add_all(
            [
                StationAssignment(user_id=users["cook"].id, station_id=kitchen.id),
                StationAssignment(user_id=users["barman"].id, station_id=bar.id),
            ]
        )
        tables = [
            Table(restaurant_id=r.id, number=number, name=name, capacity=capacity)
            for number, name, capacity in (
                (1, "Terrace", 4),
                (2, "Window", 2),
                (3, None, 6),
                (4, None, 4),
            )
        ]
        session.add_all(tables)
        food = Category(restaurant_id=r.id, name="Food", display_order=1)
        drinks = Category(restaurant_id=r.id, name="Drinks", display_order=2)
        session.add_all([food, drinks])
        session.flush()
        burger = Product(category_id=food.id, name="Burger", price=Decimal("12.50"), station_id=kitchen.id)
        fries = Product(category_id=food.id, name="Fries", price=Decimal("4.50"), station_id=kitchen.id)
        beer = Product(category_id=drinks.id, name="Beer", price=Decimal("4.00"), station_id=bar.id)
        cola = Product(category_id=drinks.id, name="Cola", price=Decimal("2.80"), station_id=bar.id)
        mojito = Product(category_id=drinks.id, name="Mojito", price=Decimal("8.00"), station_id=bar.id, is_active=False)
        session.add_all([burger, fries, beer, cola, mojito])
        session.flush()
        doneness = ModifierGroup(restaurant_id=r.id, name="Doneness", min_selections=1, max_selections=1, is_required=True)
        doneness.modifiers = [
            Modifier(name="Medium", price_adjustment=Decimal("0.00")),
            Modifier(name="Well done", price_adjustment=Decimal("0.00")),
        ]
        extras = ModifierGroup(restaurant_id=r.id, name="Extras", min_selections=0, max_selections=2)
        extras.modifiers = [
            Modifier(name="Cheese", price_adjustment=Decimal("1.10")),
            Modifier(name="Bacon", price_adjustment=Decimal("2.05")),
        ]
        burger.modifier_groups.extend([doneness, extras])
        session.flush()
        return {
            "restaurant_id": str(r.id),
            "kitchen_id": str(kitchen.id),
            "bar_id": str(bar.id),
            "users": {key: user.email for key, user in users.items()},
            "user_ids": {key: str(user.id) for key, user in users.items()},
            "tables": {table.number: str(table.id) for table in tables},
            "products": {p.name: str(p.id) for p in (burger, fries, beer, cola, mojito)},
            "modifiers": {m.name: str(m.id) for g in (doneness, extras) for m in g.modifiers},
        }

    main = restaurant("POS E2E Bistro", "bistro")
    other = restaurant("Other E2E Tenant", "other")

    # The other tenant has an active order with one unsent item, the
    # target of the cross-tenant checks.
    order = Order(
        restaurant_id=other["restaurant_id"],
        table_id=other["tables"][1],
        user_id=other["user_ids"]["waiter"],
        guest_count=2,
        status="OPEN",
    )
    session.add(order)
    session.execute(
        text("UPDATE tables SET status = 'OCCUPIED' WHERE id = :id"),
        {"id": other["tables"][1]},
    )
    session.flush()
    item = OrderItem(
        order_id=order.id,
        product_id=other["products"]["Fries"],
        product_name_snapshot="Fries",
        quantity=1,
        unit_price=Decimal("4.50"),
        total_price=Decimal("4.50"),
        status="ADDED",
    )
    session.add(item)
    session.flush()
    other["order_id"] = str(order.id)
    other["item_id"] = str(item.id)
    return {"main": main, "other": other}


def serve(database) -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            with database.begin() as connection:
                result = connection.execute(text(request["sql"]), request.get("params") or {})
                rows = [[_plain(v) for v in row] for row in result] if result.returns_rows else []
            reply = {"rows": rows}
        except Exception as exc:  # reported to the runner, which fails
            reply = {"error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(reply), flush=True)


def _plain(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def main() -> None:
    password = os.environ["E2E_PASSWORD"]
    env = dotenv_values(ROOT / ".env")
    dev_url = os.environ.get("DATABASE_URL") or env.get("DATABASE_URL")
    test_url = os.environ.get("TEST_DATABASE_URL") or env.get("TEST_DATABASE_URL")
    if not dev_url:
        sys.exit("DATABASE_URL is not set (.env)")

    with disposable_database(
        server_url=dev_url,
        protected_urls=[dev_url, test_url or "", ""],
    ) as database:
        url = database.engine.url.render_as_string(hide_password=False)
        # The app's settings (and so the seed's imports) use this database.
        os.environ["DATABASE_URL"] = url

        with database.begin() as connection:
            config = Config(str(ROOT / "alembic.ini"))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

        with database.begin() as connection:
            with Session(bind=connection) as session:
                ids = seed(session, password)
                session.flush()

        print(json.dumps({"ready": {"database": database.name, "url": url, **ids}}), flush=True)
        serve(database)


if __name__ == "__main__":
    main()
