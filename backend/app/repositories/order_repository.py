import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.order import Order


class OrderRepository:
    """Tenant-scoped order queries.

    The repository never commits; the calling service owns the
    transaction, so a row lock taken here is held until that commit.
    """

    def __init__(self, db: Session):
        self.db = db

    def get_for_restaurant(
        self,
        order_id: uuid.UUID,
        restaurant_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> Order | None:

        statement = select(Order).where(
            Order.id == order_id,
            Order.restaurant_id == restaurant_id,
        )

        if for_update:
            # Refresh an already-loaded order with the state read under
            # the lock, which may have changed while waiting for it.
            statement = (
                statement
                .with_for_update()
                .execution_options(populate_existing=True)
            )

        return self.db.scalar(statement)
