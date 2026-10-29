"""add order item product name snapshot

Revision ID: a3d9e7c1f5b2
Revises: f4c2a8d6b1e3
Create Date: 2026-10-27 22:30:00.000000

Order items keep the product's name from when they were added
(product_name_snapshot), as unit_price keeps its price, so renaming a
product does not change orders, bills or station queues already made.

Only adds a nullable column: existing items keep NULL and are not
backfilled (the current product name is not necessarily the name they
were ordered under); displays fall back to the current name for them.
The downgrade drops the column and its snapshots.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3d9e7c1f5b2'
down_revision: Union[str, Sequence[str], None] = 'f4c2a8d6b1e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'order_items',
        sa.Column('product_name_snapshot', sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('order_items', 'product_name_snapshot')
