"""unique preparation per order item

Revision ID: 67f2c099d4d4
Revises: c9400b023175
Create Date: 2026-10-11 23:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '67f2c099d4d4'
down_revision: Union[str, Sequence[str], None] = 'c9400b023175'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Refuse to guess which duplicate to keep; they must be resolved by hand.
    duplicates = op.get_bind().execute(
        sa.text(
            "SELECT order_item_id, count(*) FROM preparations "
            "GROUP BY order_item_id HAVING count(*) > 1"
        )
    ).fetchall()

    if duplicates:
        listed = ", ".join(
            f"{order_item_id} ({count})"
            for order_item_id, count in duplicates
        )
        raise RuntimeError(
            f"Cannot add uq_preparations_order_item_id: order items with "
            f"more than one preparation: {listed}"
        )

    # An order item is sent at most once, so it has at most one preparation.
    op.create_unique_constraint(
        'uq_preparations_order_item_id',
        'preparations',
        ['order_item_id'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        'uq_preparations_order_item_id',
        'preparations',
        type_='unique',
    )
