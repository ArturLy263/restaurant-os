import uuid

from app.core.security import hash_password
from app.core.config import settings
from app.db.database import SessionLocal
from app.db.models.restaurant import Restaurant
from app.db.models.role import Role
from app.db.models.user import User
from app.db.models.table import Table
from app.db.models.category import Category
from app.db.models.station import Station
from app.db.models.product import Product
from app.db.models.modifier import ModifierGroup, Modifier


def seed():
    db = SessionLocal()

    try:
        # -------------------------
        # Restaurant
        # -------------------------

        restaurant = Restaurant(
            name="RestaurantOS Demo",
            address="Valencia, Spain",
        )

        db.add(restaurant)
        db.flush()

        # -------------------------
        # Role
        # -------------------------

        admin_role = Role(
            restaurant_id=restaurant.id,
            name="ADMIN",
            description="Restaurant administrator",
        )

        db.add(admin_role)
        db.flush()

        # -------------------------
        # User
        # -------------------------

        admin = User(
            restaurant_id=restaurant.id,
            role_id=admin_role.id,
            name="Admin",
            email="admin@restaurantos.com",
            password_hash=hash_password(settings.admin_password),
        )

        db.add(admin)

        # -------------------------
        # Stations
        # -------------------------

        kitchen = Station(
            restaurant_id=restaurant.id,
            name="Kitchen",
            type="KITCHEN",
        )

        bar = Station(
            restaurant_id=restaurant.id,
            name="Bar",
            type="BAR",
        )

        db.add_all([kitchen, bar])
        db.flush()

        # -------------------------
        # Tables
        # -------------------------

        tables = [
            Table(
                restaurant_id=restaurant.id,
                number=1,
                capacity=2,
            ),
            Table(
                restaurant_id=restaurant.id,
                number=2,
                capacity=4,
            ),
            Table(
                restaurant_id=restaurant.id,
                number=3,
                capacity=6,
            ),
        ]

        db.add_all(tables)

        # -------------------------
        # Categories
        # -------------------------

        food_category = Category(
            restaurant_id=restaurant.id,
            name="Food",
            display_order=1,
        )

        drinks_category = Category(
            restaurant_id=restaurant.id,
            name="Drinks",
            display_order=2,
        )

        db.add_all([
            food_category,
            drinks_category,
        ])
        db.flush()

        # -------------------------
        # Products
        # -------------------------

        products = [
            Product(
                category_id=food_category.id,
                name="Burger",
                description="Classic restaurant burger",
                price=12.50,
                station_id=kitchen.id,
            ),
            Product(
                category_id=food_category.id,
                name="Pizza Margherita",
                description="Tomato, mozzarella and basil",
                price=10.00,
                station_id=kitchen.id,
            ),
            Product(
                category_id=drinks_category.id,
                name="Coca-Cola",
                description="330ml",
                price=2.50,
                station_id=bar.id,
            ),
            Product(
                category_id=drinks_category.id,
                name="Water",
                description="500ml",
                price=1.50,
                station_id=bar.id,
            ),
        ]

        db.add_all(products)
        db.flush()

        # -------------------------
        # Modifiers
        # -------------------------

        burger = products[0]

        extras_group = ModifierGroup(
            restaurant_id=restaurant.id,
            name="Extras",
            min_selections=0,
            max_selections=3,
            is_required=False,
        )

        db.add(extras_group)
        db.flush()

        modifiers = [
            Modifier(
                group_id=extras_group.id,
                name="Cheese",
                price_adjustment=1.00,
            ),
            Modifier(
                group_id=extras_group.id,
                name="Bacon",
                price_adjustment=2.00,
            ),
            Modifier(
                group_id=extras_group.id,
                name="Egg",
                price_adjustment=1.50,
            ),
        ]

        db.add_all(modifiers)

        # Connect Burger -> Extras
        burger.modifier_groups.append(extras_group)

        # -------------------------
        # Commit
        # -------------------------

        db.commit()

        print("✅ Seed completed")
        print(f"Restaurant ID: {restaurant.id}")
        print(f"Admin user ID: {admin.id}")
        print("Email: admin@restaurantos.com")
        print("Password: configured via ADMIN_PASSWORD in .env")
        print("")
        print("Modifiers:")
        print("  Burger → Extras")
        print("  Cheese +€1.00")
        print("  Bacon  +€2.00")
        print("  Egg    +€1.50")

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


if __name__ == "__main__":
    seed()