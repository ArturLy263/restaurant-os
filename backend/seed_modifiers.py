from decimal import Decimal

from app.db.database import SessionLocal
from app.db.models.user import User
from app.db.models.category import Category
from app.db.models.product import Product
from app.db.models.modifier import ModifierGroup, Modifier


def seed_modifiers():
    db = SessionLocal()

    try:
        # -------------------------
        # Find existing demo admin
        # -------------------------

        admin = (
            db.query(User)
            .filter(
                User.email == "admin@restaurantos.com",
            )
            .first()
        )

        if not admin:
            raise RuntimeError(
                "Demo admin user not found: admin@restaurantos.com"
            )

        restaurant_id = admin.restaurant_id

        print(f"Restaurant ID: {restaurant_id}")

        # -------------------------
        # Find Burger
        # -------------------------

        burger = (
            db.query(Product)
            .join(
                Category,
                Product.category_id == Category.id,
            )
            .filter(
                Product.name == "Burger",
                Category.restaurant_id == restaurant_id,
            )
            .first()
        )

        if not burger:
            raise RuntimeError(
                "Burger not found in the admin's restaurant"
            )

        print(f"Burger ID: {burger.id}")

        # -------------------------
        # Modifier Group
        # -------------------------

        extras_group = (
            db.query(ModifierGroup)
            .filter(
                ModifierGroup.restaurant_id == restaurant_id,
                ModifierGroup.name == "Extras",
            )
            .first()
        )

        if not extras_group:
            extras_group = ModifierGroup(
                restaurant_id=restaurant_id,
                name="Extras",
                min_selections=0,
                max_selections=3,
                is_required=False,
            )

            db.add(extras_group)
            db.flush()

            print("✅ Created modifier group: Extras")

        else:
            print(
                f"ℹ️ Modifier group already exists: "
                f"{extras_group.id}"
            )

        # -------------------------
        # Connect Burger -> Extras
        # -------------------------

        if extras_group not in burger.modifier_groups:
            burger.modifier_groups.append(extras_group)

            print(
                "✅ Connected Burger -> Extras"
            )
        else:
            print(
                "ℹ️ Burger is already connected to Extras"
            )

        # -------------------------
        # Modifiers
        # -------------------------

        modifier_data = [
            ("Cheese", Decimal("1.00")),
            ("Bacon", Decimal("2.00")),
            ("Egg", Decimal("1.50")),
        ]

        for name, price in modifier_data:

            modifier = (
                db.query(Modifier)
                .filter(
                    Modifier.group_id == extras_group.id,
                    Modifier.name == name,
                )
                .first()
            )

            if modifier:
                print(
                    f"ℹ️ Modifier already exists: "
                    f"{name} +€{modifier.price_adjustment}"
                )
                continue

            modifier = Modifier(
                group_id=extras_group.id,
                name=name,
                price_adjustment=price,
                is_active=True,
            )

            db.add(modifier)

            print(
                f"✅ Created modifier: "
                f"{name} +€{price}"
            )

        db.commit()

        print()
        print("🎉 Modifier seed completed")
        print()
        print("Burger modifiers:")
        print("  Cheese +€1.00")
        print("  Bacon  +€2.00")
        print("  Egg    +€1.50")

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


if __name__ == "__main__":
    seed_modifiers()