from decimal import Decimal

import pytest

from app.db.models.bill import Bill, BillAdjustment
from app.db.models.operational_event import OperationalEvent
from app.db.models.order import Order, OrderItem
from app.db.models.preparation import Preparation
from app.db.models.product import Product
from app.db.models.table import Table
from tests.factories import auth_headers


class Pos:
    """Drives an order through the API and reads back database state."""

    def __init__(self, client, db_session, restaurant, coke):
        self.client = client
        self.db = db_session
        self.restaurant = restaurant
        self.burger = restaurant.product
        self.coke = coke
        self.headers = auth_headers(restaurant.user)

    # --- API calls ---

    def create_order(self):
        return self.client.post(
            "/api/v1/orders",
            json={
                "table_id": str(self.restaurant.table.id),
                "guest_count": 2,
            },
            headers=self.headers,
        )

    def add_item(self, order_id, product):
        return self.client.post(
            f"/api/v1/orders/{order_id}/items",
            json={"product_id": str(product.id), "quantity": 1},
            headers=self.headers,
        )

    def send(self, order_id):
        return self.client.post(
            f"/api/v1/orders/{order_id}/send",
            headers=self.headers,
        )

    def create_bill(self, order_id):
        return self.client.post(
            f"/api/v1/bills/order/{order_id}",
            headers=self.headers,
        )

    def get_bill(self, bill_id):
        return self.client.get(
            f"/api/v1/bills/{bill_id}",
            headers=self.headers,
        )

    def pay(self, bill_id, amount, method="CASH"):
        return self.client.post(
            f"/api/v1/bills/{bill_id}/payments",
            json={"amount": amount, "method": method},
            headers=self.headers,
        )

    def cancel(self, order_id, item_id, reason="Customer changed mind"):
        return self.client.post(
            f"/api/v1/orders/{order_id}/items/{item_id}/cancel",
            json={"reason": reason},
            headers=self.headers,
        )

    def void(self, order_id, reason="Nothing left to bill"):
        return self.client.post(
            f"/api/v1/orders/{order_id}/void",
            json={"reason": reason},
            headers=self.headers,
        )

    def start_preparation(self, item_id, *actions):
        preparation = self.preparation(item_id)

        for action in actions:
            response = self.client.post(
                f"/api/v1/preparations/{preparation.id}/{action}",
                headers=self.headers,
            )
            assert response.status_code == 200

    # --- Scenario setup ---

    def open_order(self):
        """OPEN order with a Burger (12.50) and a Coke (2.50)."""

        order = self.create_order()
        assert order.status_code == 201
        order_id = order.json()["id"]

        burger = self.add_item(order_id, self.burger)
        coke = self.add_item(order_id, self.coke)
        assert burger.status_code == 201
        assert coke.status_code == 201

        return order_id, burger.json()["id"], coke.json()["id"]

    def sent_order(self):
        order_id, burger_id, coke_id = self.open_order()
        assert self.send(order_id).status_code == 200

        return order_id, burger_id, coke_id

    def billed_order(self):
        """SENT order with an OPEN 15.00 bill."""

        order_id, burger_id, coke_id = self.sent_order()

        bill = self.create_bill(order_id)
        assert bill.status_code == 201
        assert bill.json()["total"] == "15.00"

        return order_id, burger_id, coke_id, bill.json()["id"]

    # --- Database state ---

    def _fresh(self):
        self.db.expire_all()
        return self.db

    def order(self, order_id):
        return self._fresh().get(Order, order_id)

    def item(self, item_id):
        return self._fresh().get(OrderItem, item_id)

    def bill(self, bill_id):
        return self._fresh().get(Bill, bill_id)

    def table(self):
        return self._fresh().get(Table, self.restaurant.table.id)

    def preparation(self, item_id):
        return (
            self._fresh()
            .query(Preparation)
            .filter(Preparation.order_item_id == item_id)
            .one()
        )

    def adjustments(self, bill_id):
        return (
            self._fresh()
            .query(BillAdjustment)
            .filter(BillAdjustment.bill_id == bill_id)
            .all()
        )

    def events(self, event_type):
        return (
            self._fresh()
            .query(OperationalEvent)
            .filter(
                OperationalEvent.restaurant_id == self.restaurant.restaurant.id,
                OperationalEvent.event_type == event_type,
            )
            .all()
        )

    def assert_bill_matches_items(self, bill_id):
        """
        Financial invariant: original total + applied cancellation
        adjustments == total of the order's non-cancelled items.
        """

        bill = self.bill(bill_id)

        cancellations = sum(
            (
                adjustment.amount
                for adjustment in bill.adjustments
                if adjustment.status == "APPLIED"
                and adjustment.type == "ITEM_CANCELLATION"
            ),
            Decimal("0.00"),
        )

        active_items_total = sum(
            (
                item.total_price
                for item in bill.order.items
                if item.status != "CANCELLED"
            ),
            Decimal("0.00"),
        )

        assert bill.total + cancellations == active_items_total
        assert bill.adjusted_total == active_items_total


@pytest.fixture
def coke(db_session, restaurant):
    product = Product(
        category_id=restaurant.category.id,
        name="Coca-Cola",
        price=Decimal("2.50"),
        station_id=restaurant.station.id,
    )
    db_session.add(product)
    db_session.flush()

    return product


@pytest.fixture
def pos(db_client, db_session, restaurant, coke):
    return Pos(db_client, db_session, restaurant, coke)
