"""add order item notes

Revision ID: c7e1a5b9d3f8
Revises: a3d9e7c1f5b2
Create Date: 2026-10-30 18:00:00.000000

Order items get an optional free-text note from the waiter for the
kitchen or bar (e.g. "no salt", "allergy: nuts"), shown on the station
queue.

Only adds a nullable column: existing items keep NULL. The downgrade drops
the column and its notes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7e1a5b9d3f8'
down_revision: Union[str, Sequence[str], None] = 'a3d9e7c1f5b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'order_items',
        sa.Column('notes', sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('order_items', 'notes')
