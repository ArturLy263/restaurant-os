"""add outbox dead letter and retention

Revision ID: c5e8a2d4f6b1
Revises: a7d3e5f1c9b4
Create Date: 2026-10-19 00:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5e8a2d4f6b1'
down_revision: Union[str, Sequence[str], None] = 'a7d3e5f1c9b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # A nullable column without a default (no table rewrite), a CHECK that
    # every existing row satisfies (none is dead-lettered yet) and two
    # partial indexes. No existing row is changed.
    op.add_column(
        'outbox_events',
        sa.Column('dead_lettered_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        'ck_outbox_events_delivered_or_dead_lettered',
        'outbox_events',
        'published_at IS NULL OR dead_lettered_at IS NULL',
    )
    op.create_index(
        'ix_outbox_events_published',
        'outbox_events',
        ['published_at'],
        postgresql_where=sa.text('published_at IS NOT NULL'),
    )
    op.create_index(
        'ix_outbox_events_dead_lettered',
        'outbox_events',
        ['dead_lettered_at'],
        postgresql_where=sa.text('dead_lettered_at IS NOT NULL'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Dropping the column forgets which events were dead-lettered; they
    # become ordinary undelivered events that the D2.1 worker retries.
    op.drop_index(
        'ix_outbox_events_dead_lettered',
        table_name='outbox_events',
    )
    op.drop_index(
        'ix_outbox_events_published',
        table_name='outbox_events',
    )
    op.drop_constraint(
        'ck_outbox_events_delivered_or_dead_lettered',
        'outbox_events',
        type_='check',
    )
    op.drop_column('outbox_events', 'dead_lettered_at')
