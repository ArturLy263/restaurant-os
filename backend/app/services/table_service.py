import uuid

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models.order import Order, OrderItem
from app.db.models.table import Table
from app.schemas.table import TableActiveOrderResponse, TableResponse
from app.services.order_service import ACTIVE_ORDER_STATUSES


class TableService:

    @staticmethod
    def list_tables(
        db: Session,
        restaurant_id: uuid.UUID,
    ) -> list[TableResponse]:
        """The restaurant's active tables by number, each with its active
        (OPEN or SENT) order, if any."""

        tables = (
            db.query(Table)
            .filter(
                Table.restaurant_id == restaurant_id,
                Table.is_active.is_(True),
            )
            .order_by(Table.number.asc(), Table.id.asc())
            .all()
        )

        # Newest first: a table should have at most one active order, but
        # older data may hold several (no unique index enforces it); the
        # table then shows its most recent one.
        orders = (
            db.query(Order)
            .filter(
                Order.restaurant_id == restaurant_id,
                Order.status.in_(ACTIVE_ORDER_STATUSES),
            )
            .order_by(Order.created_at.desc(), Order.id.desc())
            .all()
        )

        active_orders: dict[uuid.UUID, Order] = {}
        for order in orders:
            active_orders.setdefault(order.table_id, order)

        counts: dict[tuple[uuid.UUID, str], int] = {}
        if active_orders:
            counts = {
                (order_id, item_status): count
                for order_id, item_status, count in (
                    db.query(OrderItem.order_id, OrderItem.status, func.count())
                    .filter(
                        OrderItem.order_id.in_(
                            [order.id for order in active_orders.values()]
                        )
                    )
                    .group_by(OrderItem.order_id, OrderItem.status)
                )
            }

        def active_order(table: Table) -> TableActiveOrderResponse | None:
            order = active_orders.get(table.id)
            if order is None:
                return None
            return TableActiveOrderResponse(
                id=order.id,
                status=order.status,
                user_id=order.user_id,
                guest_count=order.guest_count,
                created_at=order.created_at,
                unsent_items=counts.get((order.id, "ADDED"), 0),
                sent_items=counts.get((order.id, "SENT"), 0),
            )

        return [
            TableResponse(
                id=table.id,
                number=table.number,
                name=table.name,
                capacity=table.capacity,
                status=table.status,
                active_order=active_order(table),
            )
            for table in tables
        ]
