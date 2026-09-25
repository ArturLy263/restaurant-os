import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.db.models.category import Category
from app.db.models.modifier import (
    Modifier,
    ModifierGroup,
    OrderItemModifier,
)
from app.db.models.order import Order, OrderItem
from app.db.models.preparation import Preparation
from app.db.models.product import Product
from app.db.models.station import Station
from app.db.models.table import Table
from app.services.event_service import EventService


class OrderService:

    @staticmethod
    def create_order(
        db: Session,
        restaurant_id: uuid.UUID,
        user_id: uuid.UUID,
        table_id: uuid.UUID,
        guest_count: int,
    ) -> Order:

        table = (
            db.query(Table)
            .filter(
                Table.id == table_id,
                Table.restaurant_id == restaurant_id,
            )
            .first()
        )

        if not table:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Table not found",
            )

        if table.status != "AVAILABLE":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Table is not available",
            )

        order = Order(
            restaurant_id=restaurant_id,
            table_id=table_id,
            user_id=user_id,
            guest_count=guest_count,
            status="OPEN",
        )

        db.add(order)
        db.flush()

        EventService.record(
            db=db,
            restaurant_id=restaurant_id,
            event_type="order.created",
            entity_type="order",
            entity_id=order.id,
            user_id=user_id,
            metadata={
                "table_id": str(table_id),
                "guest_count": guest_count,
            },
        )

        table.status = "OCCUPIED"

        db.commit()
        db.refresh(order)

        return order

    @staticmethod
    def add_order_item(
        db: Session,
        restaurant_id: uuid.UUID,
        user_id: uuid.UUID,
        order_id: uuid.UUID,
        product_id: uuid.UUID,
        quantity: int,
        modifier_ids: list[uuid.UUID] | None = None,
    ) -> OrderItem:

        modifier_ids = modifier_ids or []

        order = (
            db.query(Order)
            .filter(
                Order.id == order_id,
                Order.restaurant_id == restaurant_id,
            )
            .first()
        )

        if not order:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Order not found",
            )

        if order.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You cannot modify this order",
            )

        if order.status != "OPEN":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Order is not open",
            )

        if quantity <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Quantity must be greater than zero",
            )

        product = (
            db.query(Product)
            .join(
                Category,
                Product.category_id == Category.id,
            )
            .filter(
                Product.id == product_id,
                Category.restaurant_id == restaurant_id,
            )
            .first()
        )

        if not product:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Product not found",
            )

        if not product.is_active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Product is not active",
            )

        selected_modifiers: list[Modifier] = []

        if modifier_ids:

            unique_modifier_ids = set(modifier_ids)

            selected_modifiers = (
                db.query(Modifier)
                .join(
                    ModifierGroup,
                    Modifier.group_id == ModifierGroup.id,
                )
                .filter(
                    Modifier.id.in_(unique_modifier_ids),
                    ModifierGroup.restaurant_id == restaurant_id,
                    Modifier.is_active.is_(True),
                )
                .all()
            )

            if len(selected_modifiers) != len(unique_modifier_ids):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="One or more modifiers are invalid",
                )

            allowed_group_ids = {
                group.id
                for group in product.modifier_groups
            }

            for modifier in selected_modifiers:
                if modifier.group_id not in allowed_group_ids:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            f"Modifier '{modifier.name}' "
                            f"is not available for product '{product.name}'"
                        ),
                    )

            selected_by_group: dict[uuid.UUID, int] = {}

            for modifier in selected_modifiers:
                selected_by_group[modifier.group_id] = (
                    selected_by_group.get(modifier.group_id, 0) + 1
                )

            for group in product.modifier_groups:
                selected_count = selected_by_group.get(
                    group.id,
                    0,
                )

                if selected_count < group.min_selections:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            f"Modifier group '{group.name}' "
                            f"requires at least "
                            f"{group.min_selections} selection(s)"
                        ),
                    )

                if (
                    group.max_selections is not None
                    and selected_count > group.max_selections
                ):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            f"Modifier group '{group.name}' "
                            f"allows at most "
                            f"{group.max_selections} selection(s)"
                        ),
                    )

        base_price = Decimal(str(product.price))

        modifier_total = sum(
            (
                Decimal(str(modifier.price_adjustment))
                for modifier in selected_modifiers
            ),
            Decimal("0.00"),
        )

        line_unit_price = base_price + modifier_total
        total_price = line_unit_price * quantity

        item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=quantity,
            unit_price=base_price,
            total_price=total_price,
            status="ADDED",
        )

        db.add(item)
        db.flush()

        for modifier in selected_modifiers:
            item_modifier = OrderItemModifier(
                order_item_id=item.id,
                modifier_id=modifier.id,
                name_snapshot=modifier.name,
                price_snapshot=modifier.price_adjustment,
            )

            db.add(item_modifier)

        EventService.record(
            db=db,
            restaurant_id=restaurant_id,
            event_type="item.added",
            entity_type="order_item",
            entity_id=item.id,
            user_id=user_id,
            metadata={
                "order_id": str(order.id),
                "product_id": str(product.id),
                "quantity": quantity,
                "unit_price": str(base_price),
                "total_price": str(total_price),
                "modifiers": [
                    {
                        "modifier_id": str(modifier.id),
                        "name": modifier.name,
                        "price": str(modifier.price_adjustment),
                    }
                    for modifier in selected_modifiers
                ],
            },
        )

        db.commit()
        db.refresh(item)

        return item

    @staticmethod
    def send_order(
        db: Session,
        restaurant_id: uuid.UUID,
        user_id: uuid.UUID,
        order_id: uuid.UUID,
    ) -> Order:

        order = (
            db.query(Order)
            .filter(
                Order.id == order_id,
                Order.restaurant_id == restaurant_id,
            )
            .first()
        )

        if not order:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Order not found",
            )

        if order.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You cannot modify this order",
            )

        if order.status != "OPEN":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Order is not open",
            )

        items = (
            db.query(OrderItem)
            .filter(
                OrderItem.order_id == order.id,
            )
            .all()
        )

        if not items:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Order has no items",
            )

        sent_items_count = 0

        for item in items:

            if item.status != "ADDED":
                continue

            product = (
                db.query(Product)
                .join(
                    Category,
                    Product.category_id == Category.id,
                )
                .filter(
                    Product.id == item.product_id,
                    Category.restaurant_id == restaurant_id,
                )
                .first()
            )

            if not product:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Product not found: {item.product_id}",
                )

            if not product.is_active:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Product is not active: {product.id}",
                )

            if not product.station_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Product {product.id} has no station",
                )

            item.status = "SENT"
            item.sent_at = datetime.utcnow()

            preparation = Preparation(
                order_item_id=item.id,
                station_id=product.station_id,
                status="PENDING",
            )

            db.add(preparation)
            sent_items_count += 1

        if sent_items_count == 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Order has no items ready to send",
            )

        order.status = "SENT"

        EventService.record(
            db=db,
            restaurant_id=restaurant_id,
            event_type="order.sent",
            entity_type="order",
            entity_id=order.id,
            user_id=user_id,
            metadata={
                "items_count": sent_items_count,
            },
        )

        db.commit()
        db.refresh(order)

        return order


    @staticmethod
    def cancel_order_item(
        db: Session,
        restaurant_id: uuid.UUID,
        user_id: uuid.UUID,
        order_id: uuid.UUID,
        item_id: uuid.UUID,
        reason: str,
    ) -> OrderItem:
        # Load the order to verify tenant and user
        order = (
            db.query(Order)
            .filter(
                Order.id == order_id,
                Order.restaurant_id == restaurant_id,
            )
            .first()
        )

        if not order:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Order not found",
            )

        # Check order status
        if order.status == "CLOSED":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot cancel items in a closed order",
            )

        # Ensure the user is authorized (matches order creator like in other methods)
        if order.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You cannot modify this order",
            )

        # Load the item with its preparations
        item = (
            db.query(OrderItem)
            .filter(
                OrderItem.id == item_id,
                OrderItem.order_id == order.id,
            )
            .first()
        )

        if not item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Order item not found",
            )

        if item.status == "CANCELLED":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Order item is already cancelled",
            )

        # Load preparations explicitly just in case
        preparations = (
            db.query(Preparation)
            .filter(
                Preparation.order_item_id == item.id,
            )
            .all()
        )

        # Check preparation states
        for prep in preparations:
            if prep.status in ("READY", "SERVED"):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Cannot cancel item. Preparation is already {prep.status}",
                )

        now = datetime.utcnow()
        previous_item_status = item.status

        # Perform cancellation
        item.status = "CANCELLED"
        item.cancelled_at = now

        cancelled_preps = []
        for prep in preparations:
            if prep.status in ("PENDING", "IN_PROGRESS"):
                prep_previous_status = prep.status
                prep.status = "CANCELLED"
                prep.cancelled_at = now
                cancelled_preps.append({
                    "preparation_id": str(prep.id),
                    "previous_status": prep_previous_status
                })

                EventService.record(
                    db=db,
                    restaurant_id=restaurant_id,
                    event_type="preparation.cancelled",
                    entity_type="preparation",
                    entity_id=prep.id,
                    user_id=user_id,
                    metadata={
                        "status": "CANCELLED",
                        "order_item_id": str(item.id),
                        "previous_status": prep_previous_status,
                        "reason": reason,
                    },
                )

        EventService.record(
            db=db,
            restaurant_id=restaurant_id,
            event_type="item.cancelled",
            entity_type="order_item",
            entity_id=item.id,
            user_id=user_id,
            metadata={
                "order_id": str(order.id),
                "previous_status": previous_item_status,
                "reason": reason,
                "cancelled_preparations": cancelled_preps,
                "total_price": str(item.total_price),
            },
        )

        db.commit()
        db.refresh(item)

        return item

    @staticmethod
    def get_order(
        db: Session,
        restaurant_id: uuid.UUID,
        order_id: uuid.UUID,
    ) -> Order:

        order = (
            db.query(Order)
            .filter(
                Order.id == order_id,
                Order.restaurant_id == restaurant_id,
            )
            .first()
        )

        if not order:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Order not found",
            )

        return order

    @staticmethod
    def update_preparation_status(
        db: Session,
        restaurant_id: uuid.UUID,
        preparation_id: uuid.UUID,
        new_status: str,
    ) -> Preparation:

        preparation = (
            db.query(Preparation)
            .join(
                OrderItem,
                Preparation.order_item_id == OrderItem.id,
            )
            .join(
                Order,
                OrderItem.order_id == Order.id,
            )
            .filter(
                Preparation.id == preparation_id,
                Order.restaurant_id == restaurant_id,
            )
            .first()
        )

        if not preparation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Preparation not found",
            )

        current_status = preparation.status

        valid_transitions = {
            "PENDING": "IN_PROGRESS",
            "IN_PROGRESS": "READY",
            "READY": "SERVED",
        }

        expected_next_status = valid_transitions.get(current_status)

        if expected_next_status != new_status:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Invalid preparation transition: "
                    f"{current_status} -> {new_status}"
                ),
            )

        now = datetime.utcnow()

        preparation.status = new_status

        if new_status == "IN_PROGRESS":
            preparation.started_at = now
            event_type = "preparation.started"

        elif new_status == "READY":
            preparation.ready_at = now
            event_type = "preparation.ready"

        elif new_status == "SERVED":
            preparation.served_at = now
            event_type = "preparation.served"

        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unsupported preparation status",
            )

        order_item = (
            db.query(OrderItem)
            .filter(
                OrderItem.id == preparation.order_item_id,
            )
            .first()
        )

        if not order_item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Order item not found",
            )

        EventService.record(
            db=db,
            restaurant_id=restaurant_id,
            event_type=event_type,
            entity_type="preparation",
            entity_id=preparation.id,
            metadata={
                "status": new_status,
                "order_item_id": str(
                    preparation.order_item_id
                ),
            },
        )

        db.commit()
        db.refresh(preparation)

        return preparation

    @staticmethod
    def get_station_preparations(
        db: Session,
        restaurant_id: uuid.UUID,
        station_id: uuid.UUID,
    ) -> list[Preparation]:

        station = (
            db.query(Station)
            .filter(
                Station.id == station_id,
                Station.restaurant_id == restaurant_id,
            )
            .first()
        )

        if not station:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Station not found",
            )

        preparations = (
            db.query(Preparation)
            .join(
                OrderItem,
                Preparation.order_item_id == OrderItem.id,
            )
            .join(
                Order,
                OrderItem.order_id == Order.id,
            )
            .filter(
                Preparation.station_id == station_id,
                Order.restaurant_id == restaurant_id,
                Preparation.status.in_(
                    [
                        "PENDING",
                        "IN_PROGRESS",
                        "READY",
                    ]
                ),
            )
            .order_by(
                Preparation.created_at.asc()
            )
            .all()
        )

        return preparations