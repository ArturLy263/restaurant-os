"""add bill adjustments and void timestamps

Revision ID: c9400b023175
Revises: 207d224e7bd3
Create Date: 2026-09-29 23:02:26.792070

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9400b023175'
down_revision: Union[str, Sequence[str], None] = '207d224e7bd3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('bill_adjustments',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('restaurant_id', sa.Uuid(), nullable=False),
    sa.Column('bill_id', sa.Uuid(), nullable=False),
    sa.Column('order_item_id', sa.Uuid(), nullable=True),
    sa.Column('type', sa.String(length=30), nullable=False),
    sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('reason', sa.String(length=500), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint('amount <> 0', name='ck_bill_adjustments_amount_nonzero'),
    sa.ForeignKeyConstraint(['bill_id'], ['bills.id'], ),
    sa.ForeignKeyConstraint(['order_item_id'], ['order_items.id'], ),
    sa.ForeignKeyConstraint(['restaurant_id'], ['restaurants.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(
        'ix_bill_adjustments_bill_id',
        'bill_adjustments',
        ['bill_id'],
    )
    op.create_index(
        'ix_bill_adjustments_restaurant_id_created_at',
        'bill_adjustments',
        ['restaurant_id', 'created_at'],
    )
    # At most one APPLIED adjustment of a given type per order item.
    op.create_index(
        'uq_bill_adjustments_applied_order_item_type',
        'bill_adjustments',
        ['order_item_id', 'type'],
        unique=True,
        postgresql_where=sa.text("status = 'APPLIED'"),
    )

    op.add_column('bills', sa.Column('voided_at', sa.DateTime(), nullable=True))
    op.add_column('orders', sa.Column('cancelled_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('orders', 'cancelled_at')
    op.drop_column('bills', 'voided_at')

    op.drop_index(
        'uq_bill_adjustments_applied_order_item_type',
        table_name='bill_adjustments',
        postgresql_where=sa.text("status = 'APPLIED'"),
    )
    op.drop_index(
        'ix_bill_adjustments_restaurant_id_created_at',
        table_name='bill_adjustments',
    )
    op.drop_index(
        'ix_bill_adjustments_bill_id',
        table_name='bill_adjustments',
    )
    op.drop_table('bill_adjustments')
