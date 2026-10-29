"""add outbox delivery tracking

Revision ID: a7d3e5f1c9b4
Revises: f3a9c1e7b5d2
Create Date: 2026-10-18 23:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7d3e5f1c9b4'
down_revision: Union[str, Sequence[str], None] = 'f3a9c1e7b5d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # New columns only. Existing events get attempts = 0 and are due at
    # once (next_attempt_at = the time of the upgrade); their type,
    # version, scope, aggregate, payload and published_at are not touched.
    op.add_column(
        'outbox_events',
        sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    )
    op.add_column(
        'outbox_events',
        sa.Column('last_attempt_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'outbox_events',
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False),
    )
    op.add_column(
        'outbox_events',
        sa.Column('last_error', sa.String(length=1000), nullable=True),
    )
    op.create_check_constraint(
        'ck_outbox_events_attempts_non_negative',
        'outbox_events',
        'attempts >= 0',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        'ck_outbox_events_attempts_non_negative',
        'outbox_events',
        type_='check',
    )
    op.drop_column('outbox_events', 'last_error')
    op.drop_column('outbox_events', 'next_attempt_at')
    op.drop_column('outbox_events', 'last_attempt_at')
    op.drop_column('outbox_events', 'attempts')
