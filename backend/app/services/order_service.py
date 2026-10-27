import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ApiError, ErrorCode
from app.core.permissions import Permission
from app.core.states import (
    BILL,
    ORDER,
    ORDER_ITEM,
    PREPARATION,
    PREPARATION_STEPS,
    TABLE,
)
from app.db.models.bill import Bill, BillAdjustment
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
from app.db.unit_of_work import commit
from app.repositories.bill_repository import BillRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.table_repository import TableRepository
from app.schemas.kitchen import PreparationQueueItem, PreparationQueueModifier
from app.schemas.outbox import (
    OrderSentV1,
    PreparationStatusChangedV1,
    preparation_status_changed,
    SentOrderItem,
)
from app.services.event_service import EventService
from app.services.outbox_service import OutboxService


# Orders take new items, and further sends, until they are closed or
# cancelled.
ORDER_STATUSES_ACCEPTING_ITEMS = ("OPEN", "SENT")

# A table has at most one order in these statuses; CLOSED and CANCELLED
# are terminal and free the table.
ACTIVE_ORDER_STATUSES = ("OPEN", "SENT")

ITEM_ADDITION_REASON = "Item added after bill was created"


class OrderService:

    @staticmethod
    def _ensure_order_accepts_items(order: Order) -> None:
        if order.status not in ORDER_STATUSES_ACCEPTING_ITEMS:
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.ORDER_NOT_OPEN,
                message="Order is not open",
            )

    @staticmethod
    def _ensure_can_modify(
        order: Order,
        user_id: uuid.UUID,
        can_modify_any_order: bool,
    ) -> None:
        """Only the order's creator may modify it, unless overridden.

        The override (Permission.ORDERS_MANAGE_ALL, e.g. managers) lifts
        only this rule; every state and financial rule still applies.
        """

        if order.user_id != user_id and not can_modify_any_order:
            raise ApiError(
                status_code=status.HTTP_403_FORBIDDEN,
                code=ErrorCode.ORDER_ACCESS_DENIED,
                message="You cannot modify this order",
            )

    @staticmethod
    def _ensure_station_access(
        station_id: uuid.UUID,
        station_scope: frozenset[uuid.UUID] | None,
    ) -> None:
        if station_scope is not None and station_id not in station_scope:
            raise ApiError(
                status_code=status.HTTP_403_FORBIDDEN,
                code=ErrorCode.STATION_ACCESS_DENIED,
                message="You are not assigned to this station",
            )

    @staticmethod
    def _apply_bill_adjustment(
        db: Session,
        bill: Bill,
        order: Order,
        item: OrderItem,
        adjustment_type: str,
        amount: Decimal,
        reason: str,
        user_id: uuid.UUID,
    ) -> BillAdjustment:
        """Record a signed adjustment on the bill and a bill.adjusted event.

        The original bill total stays unchanged. Flushes the adjustment, so
        an IntegrityError surfaces here, before the event is recorded.
        """

        adjustment = BillAdjustment(
            restaurant_id=order.restaurant_id,
            order_item_id=item.id,
            type=adjustment_type,
            amount=amount,
            status="APPLIED",
            reason=reason,
            user_id=user_id,
        )
        bill.adjustments.append(adjustment)
        db.flush()

        EventService.record(
            db=db,
            restaurant_id=order.restaurant_id,
            event_type="bill.adjusted",
            entity_type="bill",
            entity_id=bill.id,
            user_id=user_id,
            metadata={
                "adjustment_id": str(adjustment.id),
                "order_id": str(order.id),
                "order_created_by": str(order.user_id),
                "order_item_id": str(item.id),
                "type": adjustment.type,
                "amount": str(adjustment.amount),
                "reason": reason,
                "original_total": str(bill.total),
                "adjusted_total": str(bill.adjusted_total),
                "amount_due": str(bill.amount_due),
            },
        )

        return adjustment

    @staticmethod
    def create_order(
        db: Session,
        restaurant_id: uuid.UUID,
        user_id: uuid.UUID,
        table_id: uuid.UUID,
        guest_count: int,
    ) -> Order:

        # Lock the table row, so concurrent requests for the same table are
        # serialized: the second one sees the table OCCUPIED by the first.
        # Every change of a table's status (here, on void and on the final
        # payment) updates this row, so it cannot interleave either.
        table = TableRepository(db).get_for_restaurant(
            table_id,
            restaurant_id,
            for_update=True,
        )

        if not table:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.TABLE_NOT_FOUND,
                message="Table not found",
            )

        if table.status != "AVAILABLE":
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.TABLE_NOT_AVAILABLE,
                message="Table is not available",
            )

        # Belt and braces for tables whose status was left AVAILABLE while
        # an order is still active (checked under the table lock).
        has_active_order = db.query(
            db.query(Order)
            .filter(
                Order.table_id == table.id,
                Order.status.in_(ACTIVE_ORDER_STATUSES),
            )
            .exists()
        ).scalar()

        if has_active_order:
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.TABLE_HAS_ACTIVE_ORDER,
                message="Table already has an active order",
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

        TABLE.apply(table, "OCCUPIED")

        commit(db)
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
        *,
        can_modify_any_order: bool = False,
    ) -> OrderItem:

        modifier_ids = modifier_ids or []

        # Lock the order so an item cannot be added while it is being sent,
        # billed or paid.
        order = OrderRepository(db).get_for_restaurant(
            order_id,
            restaurant_id,
            for_update=True,
        )

        if not order:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.ORDER_NOT_FOUND,
                message="Order not found",
            )

        OrderService._ensure_can_modify(order, user_id, can_modify_any_order)

        OrderService._ensure_order_accepts_items(order)

        # Once a bill exists, a new item is charged through a positive
        # adjustment, which requires the bill to still be OPEN.
        bill = BillRepository(db).get_for_order(order.id)

        if bill is not None and bill.status != "OPEN":
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.BILL_NOT_OPEN,
                message=(
                    f"Cannot add an item to a "
                    f"{bill.status.lower()} bill"
                ),
            )

        if quantity <= 0:
            raise ApiError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code=ErrorCode.INVALID_QUANTITY,
                message="Quantity must be greater than zero",
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
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.PRODUCT_NOT_FOUND,
                message="Product not found",
            )

        if not product.is_active:
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.PRODUCT_INACTIVE,
                message="Product is not active",
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
                raise ApiError(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    code=ErrorCode.MODIFIER_INVALID,
                    message="One or more modifiers are invalid",
                )

            allowed_group_ids = {
                group.id
                for group in product.modifier_groups
            }

            for modifier in selected_modifiers:
                if modifier.group_id not in allowed_group_ids:
                    raise ApiError(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        code=ErrorCode.MODIFIER_INVALID,
                        message=(
                            f"Modifier '{modifier.name}' "
                            f"is not available for product '{product.name}'"
                        ),
                    )

        # Validate every group on the product, including groups with no
        # selected modifiers, so required groups cannot be skipped.
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

            min_selections = group.min_selections

            if group.is_required:
                min_selections = max(min_selections, 1)

            if selected_count < min_selections:
                raise ApiError(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    code=ErrorCode.MODIFIER_SELECTION_INVALID,
                    message=(
                        f"Modifier group '{group.name}' "
                        f"requires at least "
                        f"{min_selections} selection(s)"
                    ),
                )

            if (
                group.max_selections is not None
                and selected_count > group.max_selections
            ):
                raise ApiError(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    code=ErrorCode.MODIFIER_SELECTION_INVALID,
                    message=(
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

        item_event_metadata = {
            "order_id": str(order.id),
            "order_created_by": str(order.user_id),
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
        }

        if bill is not None:
            item_event_metadata["bill_id"] = str(bill.id)

        # The original bill total stays unchanged; the new item is charged
        # as a positive adjustment in the same transaction. A zero-priced
        # item has no financial effect and needs no adjustment.
        if bill is not None and total_price != Decimal("0.00"):
            adjustment = OrderService._apply_bill_adjustment(
                db=db,
                bill=bill,
                order=order,
                item=item,
                adjustment_type="ITEM_ADDITION",
                amount=total_price,
                reason=ITEM_ADDITION_REASON,
                user_id=user_id,
            )

            item_event_metadata["adjustment_id"] = str(adjustment.id)

        EventService.record(
            db=db,
            restaurant_id=restaurant_id,
            event_type="item.added",
            entity_type="order_item",
            entity_id=item.id,
            user_id=user_id,
            metadata=item_event_metadata,
        )

        commit(db)
        db.refresh(item)

        return item

    @staticmethod
    def send_order(
        db: Session,
        restaurant_id: uuid.UUID,
        user_id: uuid.UUID,
        order_id: uuid.UUID,
        *,
        can_modify_any_order: bool = False,
    ) -> Order:

        # Lock the order so concurrent sends cannot both pick up the same
        # ADDED items and create duplicate preparations.
        order = OrderRepository(db).get_for_restaurant(
            order_id,
            restaurant_id,
            for_update=True,
        )

        if not order:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.ORDER_NOT_FOUND,
                message="Order not found",
            )

        OrderService._ensure_can_modify(order, user_id, can_modify_any_order)

        # An order is sent in rounds: each send picks up only the items
        # added since the previous one.
        OrderService._ensure_order_accepts_items(order)

        # Re-read item statuses under the lock, so an item sent by a
        # concurrent request is never picked up again.
        items = (
            db.query(OrderItem)
            .filter(
                OrderItem.order_id == order.id,
            )
            .order_by(OrderItem.created_at, OrderItem.id)
            .populate_existing()
            .all()
        )

        if not items:
            raise ApiError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code=ErrorCode.ORDER_HAS_NO_ITEMS,
                message="Order has no items",
            )

        sent_item_ids: list[str] = []
        sent: list[tuple[OrderItem, Preparation]] = []

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
                raise ApiError(
                    status_code=status.HTTP_404_NOT_FOUND,
                    code=ErrorCode.PRODUCT_NOT_FOUND,
                    message=f"Product not found: {item.product_id}",
                )

            if not product.is_active:
                raise ApiError(
                    status_code=status.HTTP_409_CONFLICT,
                    code=ErrorCode.PRODUCT_INACTIVE,
                    message=f"Product is not active: {product.id}",
                )

            # Another restaurant's station counts as none, so its
            # existence is not revealed.
            station = (
                db.query(Station)
                .filter(
                    Station.id == product.station_id,
                    Station.restaurant_id == restaurant_id,
                )
                .first()
                if product.station_id
                else None
            )

            if station is None:
                raise ApiError(
                    status_code=status.HTTP_409_CONFLICT,
                    code=ErrorCode.PRODUCT_HAS_NO_STATION,
                    message=f"Product {product.id} has no station",
                )

            # Checked when sending, not when adding: the station may be
            # reactivated in between.
            if not station.is_active:
                raise ApiError(
                    status_code=status.HTTP_409_CONFLICT,
                    code=ErrorCode.STATION_INACTIVE,
                    message=f"Station {station.name} is inactive",
                )

            ORDER_ITEM.apply(item, "SENT")
            item.sent_at = datetime.utcnow()

            preparation = Preparation(
                order_item_id=item.id,
                station_id=station.id,
                status="PENDING",
            )

            db.add(preparation)
            sent_item_ids.append(str(item.id))
            sent.append((item, preparation))

        if not sent_item_ids:
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.ORDER_HAS_NO_ITEMS_TO_SEND,
                message="Order has no items ready to send",
            )

        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()

            violated_constraint = getattr(
                getattr(exc.orig, "diag", None),
                "constraint_name",
                None,
            )

            # Backstop: one of the items already has a preparation.
            if violated_constraint == "uq_preparations_order_item_id":
                raise ApiError(
                    status_code=status.HTTP_409_CONFLICT,
                    code=ErrorCode.ORDER_ITEM_ALREADY_SENT,
                    message="Order item has already been sent",
                ) from exc

            raise

        is_initial_send = order.status == "OPEN"
        ORDER.apply(order, "SENT")

        EventService.record(
            db=db,
            restaurant_id=restaurant_id,
            event_type="order.sent",
            entity_type="order",
            entity_id=order.id,
            user_id=user_id,
            metadata={
                "order_created_by": str(order.user_id),
                "items_count": len(sent_item_ids),
                "item_ids": sent_item_ids,
                "initial": is_initial_send,
            },
        )

        # Preparation ids exist since the flush above.
        OutboxService.record(
            db=db,
            restaurant_id=restaurant_id,
            payload=OrderSentV1(
                order_id=order.id,
                table_id=order.table_id,
                initial=is_initial_send,
                items=[
                    SentOrderItem(
                        order_item_id=item.id,
                        preparation_id=preparation.id,
                        station_id=preparation.station_id,
                        product_id=item.product_id,
                        quantity=item.quantity,
                    )
                    for item, preparation in sent
                ],
            ),
        )

        commit(db)
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
        *,
        can_modify_any_order: bool = False,
    ) -> OrderItem:
        # Lock the order so the cancellation cannot race a send, a new
        # item or a payment on its bill.
        order = OrderRepository(db).get_for_restaurant(
            order_id,
            restaurant_id,
            for_update=True,
        )

        if not order:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.ORDER_NOT_FOUND,
                message="Order not found",
            )

        # Check order status
        if order.status == "CLOSED":
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.ORDER_CLOSED,
                message="Cannot cancel items in a closed order",
            )

        if order.status == "CANCELLED":
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.ORDER_CANCELLED,
                message="Cannot cancel items in a cancelled order",
            )

        OrderService._ensure_can_modify(order, user_id, can_modify_any_order)

        # An issued bill is a financial document: its items are cancelled
        # through the bill (cancel_billed_item, Permission.BILLS_ADJUST),
        # never as a side effect of this order endpoint.
        if BillRepository(db).get_for_order(order.id) is not None:
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.ORDER_BILLED,
                message=(
                    "Order has a bill; cancel billed items through the bill"
                ),
            )

        return OrderService._cancel_item(
            db=db,
            order=order,
            bill=None,
            item_id=item_id,
            reason=reason,
            user_id=user_id,
        )

    @staticmethod
    def cancel_billed_item(
        db: Session,
        restaurant_id: uuid.UUID,
        user_id: uuid.UUID,
        bill_id: uuid.UUID,
        item_id: uuid.UUID,
        reason: str,
    ) -> Bill:
        """Cancel an item of an issued bill: the financial correction.

        Records an ITEM_CANCELLATION adjustment on the bill (its original
        total is unchanged), under the same bill rules as before: the bill
        must be OPEN and unpaid. Not limited to the order's creator; the
        caller needs Permission.BILLS_ADJUST.
        """

        bill = db.get(Bill, bill_id)
        order = db.get(Order, bill.order_id) if bill is not None else None

        # Another restaurant's bill is reported exactly like a missing one.
        if order is None or order.restaurant_id != restaurant_id:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.BILL_NOT_FOUND,
                message="Bill not found",
            )

        # Lock the order (and so its bill), then re-read the bill under the
        # lock, as every other order and bill write does.
        order = OrderRepository(db).get_for_restaurant(
            order.id,
            restaurant_id,
            for_update=True,
        )
        bill = BillRepository(db).get_for_order(order.id)

        OrderService._cancel_item(
            db=db,
            order=order,
            bill=bill,
            item_id=item_id,
            reason=reason,
            user_id=user_id,
        )

        db.refresh(bill)

        return bill

    @staticmethod
    def _cancel_item(
        db: Session,
        order: Order,
        bill: Bill | None,
        item_id: uuid.UUID,
        reason: str,
        user_id: uuid.UUID,
    ) -> OrderItem:
        """Cancel an item of the locked `order`; adjusts `bill` if given."""


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
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.ORDER_ITEM_NOT_FOUND,
                message="Order item not found",
            )

        if item.status == "CANCELLED":
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.ORDER_ITEM_ALREADY_CANCELLED,
                message="Order item is already cancelled",
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
                raise ApiError(
                    status_code=status.HTTP_409_CONFLICT,
                    code=ErrorCode.ORDER_ITEM_NOT_CANCELLABLE,
                    message=f"Cannot cancel item. Preparation is already {prep.status}",
                )

        # With a bill, the cancellation is recorded as a bill adjustment.
        # Validate the bill before changing anything.
        if bill is not None:
            if bill.status != "OPEN":
                raise ApiError(
                    status_code=status.HTTP_409_CONFLICT,
                    code=ErrorCode.BILL_NOT_OPEN,
                    message=(
                        f"Cannot cancel an order item on a "
                        f"{bill.status.lower()} bill"
                    ),
                )

            if bill.paid_total > Decimal("0.00"):
                raise ApiError(
                    status_code=status.HTTP_409_CONFLICT,
                    code=ErrorCode.BILL_HAS_PAYMENTS,
                    message=(
                        "Cannot cancel an order item after payment "
                        "has been made"
                    ),
                )

        now = datetime.utcnow()
        previous_item_status = item.status

        # Perform cancellation
        ORDER_ITEM.apply(item, "CANCELLED")
        item.cancelled_at = now

        cancelled_preps = []
        for prep in preparations:
            if prep.status in ("PENDING", "IN_PROGRESS"):
                prep_previous_status = prep.status
                PREPARATION.apply(prep, "CANCELLED")
                prep.cancelled_at = now
                cancelled_preps.append({
                    "preparation_id": str(prep.id),
                    "previous_status": prep_previous_status
                })

                EventService.record(
                    db=db,
                    restaurant_id=order.restaurant_id,
                    event_type="preparation.cancelled",
                    entity_type="preparation",
                    entity_id=prep.id,
                    user_id=user_id,
                    metadata={
                        "status": "CANCELLED",
                        "order_item_id": str(item.id),
                        "order_id": str(order.id),
                        "order_created_by": str(order.user_id),
                        "previous_status": prep_previous_status,
                        "reason": reason,
                    },
                )

                OutboxService.record(
                    db=db,
                    restaurant_id=order.restaurant_id,
                    payload=PreparationStatusChangedV1(
                        preparation_id=prep.id,
                        order_id=order.id,
                        order_item_id=item.id,
                        station_id=prep.station_id,
                        previous_status=prep_previous_status,
                        status="CANCELLED",
                    ),
                )

        item_event_metadata = {
            "order_id": str(order.id),
            "order_created_by": str(order.user_id),
            "previous_status": previous_item_status,
            "reason": reason,
            "cancelled_preparations": cancelled_preps,
            "total_price": str(item.total_price),
        }

        if bill is not None:
            item_event_metadata["bill_id"] = str(bill.id)

        # A zero-priced item has no financial effect, so it needs no
        # adjustment (and amount = 0 is rejected by the database).
        if bill is not None and item.total_price != Decimal("0.00"):
            # The original bill total stays unchanged; the cancellation is
            # recorded as a negative adjustment in the same transaction.
            try:
                adjustment = OrderService._apply_bill_adjustment(
                    db=db,
                    bill=bill,
                    order=order,
                    item=item,
                    adjustment_type="ITEM_CANCELLATION",
                    amount=-item.total_price,
                    reason=reason,
                    user_id=user_id,
                )
            except IntegrityError as exc:
                db.rollback()

                violated_constraint = getattr(
                    getattr(exc.orig, "diag", None),
                    "constraint_name",
                    None,
                )

                # Backstop: the partial unique index already holds an
                # APPLIED cancellation for this item.
                if (
                    violated_constraint
                    == "uq_bill_adjustments_applied_order_item_type"
                ):
                    raise ApiError(
                        status_code=status.HTTP_409_CONFLICT,
                        code=ErrorCode.ORDER_ITEM_ALREADY_CANCELLED,
                        message="Order item is already cancelled",
                    ) from exc

                raise

            item_event_metadata["adjustment_id"] = str(adjustment.id)

        EventService.record(
            db=db,
            restaurant_id=order.restaurant_id,
            event_type="item.cancelled",
            entity_type="order_item",
            entity_id=item.id,
            user_id=user_id,
            metadata=item_event_metadata,
        )

        commit(db)
        db.refresh(item)

        return item

    @staticmethod
    def void_order(
        db: Session,
        restaurant_id: uuid.UUID,
        user_id: uuid.UUID,
        order_id: uuid.UUID,
        reason: str,
        *,
        can_modify_any_order: bool = False,
        can_adjust_bills: bool = False,
    ) -> Order:
        """
        Explicitly cancel an order that has nothing left to bill.

        Voids an OPEN zero-balance bill if one exists and frees the table.
        Nothing is deleted.
        """

        # Lock the order so no item can be added and no payment taken
        # while it is being voided.
        order = OrderRepository(db).get_for_restaurant(
            order_id,
            restaurant_id,
            for_update=True,
        )

        if not order:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.ORDER_NOT_FOUND,
                message="Order not found",
            )

        if order.status == "CLOSED":
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.ORDER_CLOSED,
                message="Order is already closed",
            )

        if order.status == "CANCELLED":
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.ORDER_CANCELLED,
                message="Order is already cancelled",
            )

        OrderService._ensure_can_modify(order, user_id, can_modify_any_order)

        active_items_count = sum(
            1
            for item in order.items
            if item.status != "CANCELLED"
        )

        if active_items_count > 0:
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.ORDER_HAS_ACTIVE_ITEMS,
                message=(
                    f"Order still has {active_items_count} active "
                    f"item(s); cancel them before voiding the order"
                ),
            )

        bill = BillRepository(db).get_for_order(order.id)

        if bill is not None:
            # Voiding the order voids its bill, a financial document.
            if not can_adjust_bills:
                raise ApiError(
                    status_code=status.HTTP_403_FORBIDDEN,
                    code=ErrorCode.PERMISSION_DENIED,
                    message=f"Missing permission: {Permission.BILLS_ADJUST}",
                )

            if bill.status != "OPEN":
                raise ApiError(
                    status_code=status.HTTP_409_CONFLICT,
                    code=ErrorCode.BILL_NOT_OPEN,
                    message=f"Cannot void an order with a {bill.status.lower()} bill",
                )

            if bill.paid_total > Decimal("0.00"):
                raise ApiError(
                    status_code=status.HTTP_409_CONFLICT,
                    code=ErrorCode.BILL_HAS_PAYMENTS,
                    message="Cannot void an order after payment has been made",
                )

            if bill.amount_due != Decimal("0.00"):
                raise ApiError(
                    status_code=status.HTTP_409_CONFLICT,
                    code=ErrorCode.BILL_AMOUNT_DUE,
                    message="Cannot void an order while an amount is still due",
                )

        now = datetime.utcnow()
        previous_status = order.status

        if bill is not None:
            BILL.apply(bill, "VOID")
            bill.voided_at = now

            EventService.record(
                db=db,
                restaurant_id=restaurant_id,
                event_type="bill.voided",
                entity_type="bill",
                entity_id=bill.id,
                user_id=user_id,
                metadata={
                    "order_id": str(order.id),
                    "order_created_by": str(order.user_id),
                    "original_total": str(bill.total),
                    "adjustments_total": str(bill.adjustments_total),
                    "adjusted_total": str(bill.adjusted_total),
                    "reason": reason,
                },
            )

        ORDER.apply(order, "CANCELLED")
        order.cancelled_at = now

        table = db.get(Table, order.table_id)

        if table is not None:
            TABLE.apply(table, "AVAILABLE")

        EventService.record(
            db=db,
            restaurant_id=restaurant_id,
            event_type="order.cancelled",
            entity_type="order",
            entity_id=order.id,
            user_id=user_id,
            metadata={
                "order_created_by": str(order.user_id),
                "previous_status": previous_status,
                "bill_id": str(bill.id) if bill is not None else None,
                "table_id": str(order.table_id),
                "reason": reason,
            },
        )

        commit(db)
        db.refresh(order)

        return order

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
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.ORDER_NOT_FOUND,
                message="Order not found",
            )

        return order

    @staticmethod
    def _lock_preparation(
        db: Session,
        restaurant_id: uuid.UUID,
        preparation_id: uuid.UUID,
        *,
        station_scope: frozenset[uuid.UUID] | None,
    ) -> tuple[Order, Preparation]:
        """The restaurant's preparation and its order, with the order row
        locked and the preparation re-read under the lock.

        Raises 404 PREPARATION_NOT_FOUND (also for another restaurant's) or
        403 STATION_ACCESS_DENIED.
        """

        found = (
            db.query(OrderItem.order_id, Preparation.station_id)
            .join(
                Preparation,
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

        # Another restaurant's preparation is reported like a missing one.
        if found is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.PREPARATION_NOT_FOUND,
                message="Preparation not found",
            )

        order_id, station_id = found

        # Checked before taking the order lock; a preparation never moves
        # to another station.
        OrderService._ensure_station_access(station_id, station_scope)

        # Lock the order, as cancelling its items does, so a transition
        # cannot race a cancellation (e.g. revive a CANCELLED preparation),
        # then read the preparation's current status under the lock.
        order = OrderRepository(db).get_for_restaurant(
            order_id,
            restaurant_id,
            for_update=True,
        )
        preparation = (
            db.query(Preparation)
            .filter(Preparation.id == preparation_id)
            .populate_existing()
            .one()
        )

        return order, preparation

    @staticmethod
    def update_preparation_status(
        db: Session,
        restaurant_id: uuid.UUID,
        user_id: uuid.UUID,
        preparation_id: uuid.UUID,
        new_status: str,
        *,
        station_scope: frozenset[uuid.UUID] | None,
    ) -> Preparation:
        """Move a preparation to `new_status`.

        `station_scope` is the set of stations the user may work, or None
        for all; required, so a caller cannot forget it.
        """

        order, preparation = OrderService._lock_preparation(
            db,
            restaurant_id,
            preparation_id,
            station_scope=station_scope,
        )

        current_status = preparation.status

        expected_next_status = PREPARATION_STEPS.get(current_status)

        if expected_next_status != new_status:
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.INVALID_PREPARATION_TRANSITION,
                message=(
                    f"Invalid preparation transition: "
                    f"{current_status} -> {new_status}"
                ),
            )

        now = datetime.utcnow()

        PREPARATION.apply(preparation, new_status)

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
            raise ApiError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code=ErrorCode.BAD_REQUEST,
                message="Unsupported preparation status",
            )

        order_item = (
            db.query(OrderItem)
            .filter(
                OrderItem.id == preparation.order_item_id,
            )
            .first()
        )

        if not order_item:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.ORDER_ITEM_NOT_FOUND,
                message="Order item not found",
            )

        EventService.record(
            db=db,
            restaurant_id=restaurant_id,
            event_type=event_type,
            entity_type="preparation",
            entity_id=preparation.id,
            user_id=user_id,
            metadata={
                "status": new_status,
                "order_item_id": str(
                    preparation.order_item_id
                ),
                "order_id": str(order.id),
                "order_created_by": str(order.user_id),
            },
        )

        OutboxService.record(
            db=db,
            restaurant_id=restaurant_id,
            payload=PreparationStatusChangedV1(
                preparation_id=preparation.id,
                order_id=order.id,
                order_item_id=preparation.order_item_id,
                station_id=preparation.station_id,
                previous_status=current_status,
                status=new_status,
            ),
        )

        commit(db)
        db.refresh(preparation)

        return preparation

    @staticmethod
    def discard_preparation(
        db: Session,
        restaurant_id: uuid.UUID,
        user_id: uuid.UUID,
        preparation_id: uuid.UUID,
        reason: str,
        *,
        station_scope: frozenset[uuid.UUID] | None,
    ) -> Preparation:
        """Cancel an unfinished preparation of a fully paid order.

        Payment is not service: a CLOSED order's preparations stay in the
        queue until served, or until discarded here (e.g. the guest left).
        Only the preparation changes; its order item and the paid bill are
        left as they are (no refunds). The reason goes to the audit trail.
        """

        order, preparation = OrderService._lock_preparation(
            db,
            restaurant_id,
            preparation_id,
            station_scope=station_scope,
        )

        # Read under the order lock: only the final payment closes an
        # order, so a discard cannot overtake a payment in progress.
        if order.status != "CLOSED":
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.ORDER_NOT_CLOSED,
                message=(
                    "Only preparations of a fully paid, closed order can "
                    "be discarded"
                ),
            )

        previous_status = preparation.status

        if not PREPARATION.allows(previous_status, "CANCELLED"):
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.INVALID_PREPARATION_TRANSITION,
                message=(
                    f"Invalid preparation transition: "
                    f"{previous_status} -> CANCELLED"
                ),
            )

        PREPARATION.apply(preparation, "CANCELLED")
        preparation.cancelled_at = datetime.utcnow()

        EventService.record(
            db=db,
            restaurant_id=restaurant_id,
            event_type="preparation.discarded",
            entity_type="preparation",
            entity_id=preparation.id,
            user_id=user_id,
            metadata={
                "status": "CANCELLED",
                "previous_status": previous_status,
                "reason": reason,
                "order_item_id": str(preparation.order_item_id),
                "order_id": str(order.id),
                "station_id": str(preparation.station_id),
                "order_created_by": str(order.user_id),
            },
        )

        OutboxService.record(
            db=db,
            restaurant_id=restaurant_id,
            payload=preparation_status_changed(
                preparation_id=preparation.id,
                order_id=order.id,
                order_item_id=preparation.order_item_id,
                station_id=preparation.station_id,
                previous_status=previous_status,
                status="CANCELLED",
            ),
        )

        commit(db)
        db.refresh(preparation)

        return preparation

    @staticmethod
    def ensure_station_visible(
        db: Session,
        restaurant_id: uuid.UUID,
        station_id: uuid.UUID,
        *,
        station_scope: frozenset[uuid.UUID] | None,
    ) -> Station:
        """The restaurant's station, if `station_scope` includes it.

        Used by the station queue and the station event stream. Raises
        403 STATION_ACCESS_DENIED or 404 STATION_NOT_FOUND.
        """

        # Checked before looking the station up, so the answer is the same
        # for unassigned, missing and other restaurants' stations.
        OrderService._ensure_station_access(station_id, station_scope)

        station = (
            db.query(Station)
            .filter(
                Station.id == station_id,
                Station.restaurant_id == restaurant_id,
            )
            .first()
        )

        if not station:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.STATION_NOT_FOUND,
                message="Station not found",
            )

        return station

    @staticmethod
    def get_visible_stations(
        db: Session,
        restaurant_id: uuid.UUID,
        *,
        station_scope: frozenset[uuid.UUID] | None,
    ) -> list[Station]:
        """The restaurant's stations within `station_scope` (None: all of
        them, inactive ones included), by name."""

        if station_scope is not None and not station_scope:
            return []

        query = db.query(Station).filter(Station.restaurant_id == restaurant_id)

        if station_scope is not None:
            query = query.filter(Station.id.in_(station_scope))

        return query.order_by(Station.name.asc(), Station.id.asc()).all()

    @staticmethod
    def get_station_preparations(
        db: Session,
        restaurant_id: uuid.UUID,
        station_id: uuid.UUID,
        *,
        station_scope: frozenset[uuid.UUID] | None,
    ) -> list[PreparationQueueItem]:
        """The station's open preparations, oldest first, with what a
        display shows for each; see update_preparation_status for
        `station_scope`."""

        OrderService.ensure_station_visible(
            db,
            restaurant_id,
            station_id,
            station_scope=station_scope,
        )

        rows = (
            db.query(
                Preparation,
                OrderItem.order_id,
                OrderItem.quantity,
                Table.number,
                Table.name,
                Product.name,
                Order.status,
                Bill.status,
            )
            .join(
                OrderItem,
                Preparation.order_item_id == OrderItem.id,
            )
            .join(
                Order,
                OrderItem.order_id == Order.id,
            )
            .join(
                Table,
                Order.table_id == Table.id,
            )
            .join(
                Product,
                OrderItem.product_id == Product.id,
            )
            # At most one bill per order (bills.order_id is unique).
            .outerjoin(
                Bill,
                Bill.order_id == Order.id,
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
                Preparation.created_at.asc(),
                Preparation.id.asc(),
            )
            .all()
        )

        modifiers: dict[uuid.UUID, list[PreparationQueueModifier]] = {}

        if rows:
            for modifier in (
                db.query(OrderItemModifier)
                .filter(
                    OrderItemModifier.order_item_id.in_(
                        [preparation.order_item_id for preparation, *_ in rows]
                    )
                )
                # By name: an item's modifiers are stored in no
                # particular order.
                .order_by(
                    OrderItemModifier.name_snapshot.asc(),
                    OrderItemModifier.id.asc(),
                )
            ):
                modifiers.setdefault(modifier.order_item_id, []).append(
                    PreparationQueueModifier(
                        modifier_id=modifier.modifier_id,
                        name=modifier.name_snapshot,
                    )
                )

        return [
            PreparationQueueItem(
                id=preparation.id,
                order_item_id=preparation.order_item_id,
                station_id=preparation.station_id,
                status=preparation.status,
                created_at=preparation.created_at,
                started_at=preparation.started_at,
                ready_at=preparation.ready_at,
                order_id=order_id,
                table_number=table_number,
                table_name=table_name,
                product_name=product_name,
                quantity=quantity,
                modifiers=modifiers.get(preparation.order_item_id, []),
                order_status=order_status,
                bill_status=bill_status,
            )
            for (
                preparation,
                order_id,
                quantity,
                table_number,
                table_name,
                product_name,
                order_status,
                bill_status,
            ) in rows
        ]