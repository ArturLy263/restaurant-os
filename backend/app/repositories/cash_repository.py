import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, selectinload

from app.db.models.cash import BusinessDay, CashRegister, CashShift


class CashRepository:
    """Locking reads of business days, the register and cash shifts.

    Locks are always taken in this order, so concurrent requests never
    wait on each other in a cycle: order row (payments and refunds, see
    OrderRepository) -> business day -> register -> cash shift.

    - A payment or refund locks the open day FOR SHARE (it can't close
      meanwhile) and the open shift FOR UPDATE (it can't close or hand
      over meanwhile; a close waits for the payment and then sees it).
    - Opening a shift or a handover locks the day FOR SHARE and the
      register FOR UPDATE (one opening at a time per register).
    - Closing a day locks the day FOR UPDATE.

    Every read re-checks the status under the lock (PostgreSQL re-evaluates
    the WHERE clause on rows changed meanwhile), so a row found here is
    in the state asked for until the transaction ends. The repository
    never commits.
    """

    def __init__(self, db: Session):
        self.db = db

    def lock_open_business_day(
        self,
        restaurant_id: uuid.UUID,
        *,
        exclusive: bool = False,
    ) -> BusinessDay | None:
        return self.db.scalar(
            select(BusinessDay)
            .where(
                BusinessDay.restaurant_id == restaurant_id,
                BusinessDay.status == "OPEN",
            )
            .with_for_update(read=not exclusive)
            .execution_options(populate_existing=True)
        )

    def lock_business_day(
        self,
        business_day_id: uuid.UUID,
        restaurant_id: uuid.UUID,
    ) -> BusinessDay | None:
        return self.db.scalar(
            select(BusinessDay)
            .where(
                BusinessDay.id == business_day_id,
                BusinessDay.restaurant_id == restaurant_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def active_register(self, restaurant_id: uuid.UUID) -> CashRegister | None:
        return self.db.scalar(
            select(CashRegister).where(
                CashRegister.restaurant_id == restaurant_id,
                CashRegister.is_active.is_(True),
            )
        )

    def lock_or_create_active_register(
        self,
        restaurant_id: uuid.UUID,
    ) -> CashRegister:
        """The restaurant's register, locked; created on first use (the
        partial unique index makes concurrent creation insert one row)."""

        self.db.execute(
            insert(CashRegister)
            .values(
                id=uuid.uuid4(),
                restaurant_id=restaurant_id,
                name="Main register",
                is_active=True,
                created_at=datetime.utcnow(),
            )
            .on_conflict_do_nothing(
                index_elements=["restaurant_id"],
                index_where=CashRegister.is_active,
            )
        )
        return self.db.scalar(
            select(CashRegister)
            .where(
                CashRegister.restaurant_id == restaurant_id,
                CashRegister.is_active.is_(True),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def open_shift(self, restaurant_id: uuid.UUID) -> CashShift | None:
        """The open shift of the restaurant's active register (unlocked)."""

        return self.db.scalar(self._open_shift_query(restaurant_id))

    def lock_open_shift(self, restaurant_id: uuid.UUID) -> CashShift | None:
        return self.db.scalar(
            self._open_shift_query(restaurant_id)
            .with_for_update(of=CashShift)
            .execution_options(populate_existing=True)
        )

    def lock_shift(
        self,
        shift_id: uuid.UUID,
        restaurant_id: uuid.UUID,
    ) -> CashShift | None:
        return self.db.scalar(
            select(CashShift)
            .where(
                CashShift.id == shift_id,
                CashShift.restaurant_id == restaurant_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def get_shift(
        self,
        shift_id: uuid.UUID,
        restaurant_id: uuid.UUID,
    ) -> CashShift | None:
        return self.db.scalar(
            select(CashShift)
            .where(
                CashShift.id == shift_id,
                CashShift.restaurant_id == restaurant_id,
            )
            .options(
                selectinload(CashShift.corrections),
                selectinload(CashShift.movements),
            )
            .execution_options(populate_existing=True)
        )

    def last_closed_shift(self, register_id: uuid.UUID) -> CashShift | None:
        """The register's most recently closed shift, any business day."""

        return self.db.scalar(
            select(CashShift)
            .where(
                CashShift.cash_register_id == register_id,
                CashShift.status == "CLOSED",
            )
            .order_by(CashShift.closed_at.desc(), CashShift.id.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )

    @staticmethod
    def _open_shift_query(restaurant_id: uuid.UUID):
        return (
            select(CashShift)
            .join(
                CashRegister,
                CashRegister.id == CashShift.cash_register_id,
            )
            .where(
                CashRegister.restaurant_id == restaurant_id,
                CashRegister.is_active.is_(True),
                CashShift.status == "OPEN",
            )
        )

