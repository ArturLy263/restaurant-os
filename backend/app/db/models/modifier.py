import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, Table, Column
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base

if TYPE_CHECKING:
    from app.db.models.restaurant import Restaurant
    from app.db.models.product import Product
    from app.db.models.order import OrderItem


product_modifier_groups = Table(
    "product_modifier_groups",
    Base.metadata,
    Column(
        "product_id",
        ForeignKey("products.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "modifier_group_id",
        ForeignKey("modifier_groups.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class ModifierGroup(Base):
    __tablename__ = "modifier_groups"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("restaurants.id"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    min_selections: Mapped[int] = mapped_column(
        default=0,
        nullable=False,
    )

    max_selections: Mapped[int | None] = mapped_column()

    is_required: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    modifiers: Mapped[list["Modifier"]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
    )

    products: Mapped[list["Product"]] = relationship(
        secondary=product_modifier_groups,
        back_populates="modifier_groups",
    )


class Modifier(Base):
    __tablename__ = "modifiers"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("modifier_groups.id", ondelete="CASCADE"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    price_adjustment: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        default=Decimal("0.00"),
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    group: Mapped["ModifierGroup"] = relationship(
        back_populates="modifiers",
    )


class OrderItemModifier(Base):
    __tablename__ = "order_item_modifiers"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    order_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("order_items.id", ondelete="CASCADE"),
        nullable=False,
    )

    modifier_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("modifiers.id"),
        nullable=False,
    )

    name_snapshot: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    price_snapshot: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    order_item: Mapped["OrderItem"] = relationship(
        back_populates="modifiers",
    )

    modifier: Mapped["Modifier"] = relationship()