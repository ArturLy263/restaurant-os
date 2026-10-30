import uuid

from sqlalchemy.orm import Session, selectinload

from app.core.errors import ErrorCode
from app.db.models.category import Category
from app.db.models.modifier import ModifierGroup
from app.db.models.product import Product
from app.db.models.restaurant import Restaurant
from app.db.models.station import Station
from app.schemas.menu import (
    MenuCategoryResponse,
    MenuModifierGroupResponse,
    MenuModifierResponse,
    MenuProductResponse,
    MenuResponse,
    MenuStationResponse,
)


class MenuService:

    @staticmethod
    def get_menu(
        db: Session,
        restaurant_id: uuid.UUID,
    ) -> MenuResponse:
        """The restaurant's menu for taking orders: active categories in
        display order, each with all its products (unavailable ones are
        listed with the reason) and their modifier groups."""

        restaurant = db.get(Restaurant, restaurant_id)

        categories = (
            db.query(Category)
            .filter(
                Category.restaurant_id == restaurant_id,
                Category.is_active.is_(True),
            )
            .order_by(
                Category.display_order.asc(),
                Category.name.asc(),
                Category.id.asc(),
            )
            .all()
        )

        products_by_category: dict[uuid.UUID, list[Product]] = {}
        if categories:
            for product in (
                db.query(Product)
                .filter(
                    Product.category_id.in_(
                        [category.id for category in categories]
                    )
                )
                .options(
                    selectinload(Product.modifier_groups).selectinload(
                        ModifierGroup.modifiers
                    )
                )
                .order_by(Product.name.asc(), Product.id.asc())
            ):
                products_by_category.setdefault(product.category_id, []).append(
                    product
                )

        # Another restaurant's station counts as none, as when sending.
        stations = {
            station.id: station
            for station in db.query(Station).filter(
                Station.restaurant_id == restaurant_id
            )
        }

        def modifier_groups(product: Product) -> list[MenuModifierGroupResponse]:
            return [
                MenuModifierGroupResponse(
                    id=group.id,
                    name=group.name,
                    min_selections=group.min_selections,
                    max_selections=group.max_selections,
                    is_required=group.is_required,
                    modifiers=[
                        MenuModifierResponse(
                            id=modifier.id,
                            name=modifier.name,
                            price_adjustment=modifier.price_adjustment,
                        )
                        for modifier in sorted(
                            group.modifiers,
                            key=lambda modifier: (modifier.created_at, modifier.name),
                        )
                        if modifier.is_active
                    ],
                )
                for group in sorted(
                    product.modifier_groups,
                    key=lambda group: (group.name, str(group.id)),
                )
                if group.restaurant_id == restaurant_id
            ]

        def menu_product(product: Product) -> MenuProductResponse:
            station = stations.get(product.station_id) if product.station_id else None

            if not product.is_active:
                reason = ErrorCode.PRODUCT_INACTIVE
            elif station is None:
                reason = ErrorCode.PRODUCT_HAS_NO_STATION
            elif not station.is_active:
                reason = ErrorCode.STATION_INACTIVE
            else:
                reason = None

            return MenuProductResponse(
                id=product.id,
                name=product.name,
                description=product.description,
                price=product.price,
                station=(
                    MenuStationResponse(
                        id=station.id,
                        name=station.name,
                        type=station.type,
                        is_active=station.is_active,
                    )
                    if station
                    else None
                ),
                is_available=reason is None,
                unavailable_reason=str(reason) if reason else None,
                modifier_groups=modifier_groups(product),
            )

        return MenuResponse(
            currency=(restaurant.currency if restaurant and restaurant.currency else "EUR"),
            categories=[
                MenuCategoryResponse(
                    id=category.id,
                    name=category.name,
                    display_order=category.display_order,
                    products=[
                        menu_product(product)
                        for product in products_by_category.get(category.id, [])
                    ],
                )
                for category in categories
            ],
        )
