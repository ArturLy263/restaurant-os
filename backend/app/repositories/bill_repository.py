import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models.bill import Bill


class BillRepository:
    """Bill queries scoped by their order.

    A bill is guarded by its order's row lock: callers lock the order
    first (OrderRepository.get_for_restaurant(..., for_update=True)) and
    then read the bill here. Tenant checks happen on the order.
    The repository never commits.
    """

    def __init__(self, db: Session):
        self.db = db

    def get_for_order(self, order_id: uuid.UUID) -> Bill | None:

        # Always reload the bill with its payments and adjustments, so
        # amounts read after taking the order lock are never stale copies
        # loaded earlier in the session.
        statement = (
            select(Bill)
            .where(Bill.order_id == order_id)
            .options(
                selectinload(Bill.payments),
                selectinload(Bill.adjustments),
            )
            .execution_options(populate_existing=True)
        )

        return self.db.scalar(statement)
