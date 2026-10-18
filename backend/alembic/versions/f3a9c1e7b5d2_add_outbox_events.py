"""add outbox events

Revision ID: f3a9c1e7b5d2
Revises: d4f1a8c2b6e3
Create Date: 2026-10-18 22:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'f3a9c1e7b5d2'
down_revision: Union[str, Sequence[str], None] = 'd4f1a8c2b6e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # A new table only: no existing rows are read or changed, and no
    # events are backfilled for changes made before it existed.
    op.create_table('outbox_events',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('restaurant_id', sa.Uuid(), nullable=False),
    sa.Column('event_type', sa.String(length=100), nullable=False),
    sa.Column('schema_version', sa.Integer(), nullable=False),
    sa.Column('aggregate_type', sa.String(length=50), nullable=False),
    sa.Column('aggregate_id', sa.Uuid(), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('schema_version >= 1', name='ck_outbox_events_schema_version_positive'),
    sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name='ck_outbox_events_payload_is_object'),
    sa.ForeignKeyConstraint(['restaurant_id'], ['restaurants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(
        'ix_outbox_events_unpublished',
        'outbox_events',
        ['created_at'],
        postgresql_where=sa.text('published_at IS NULL'),
    )
    op.create_index(
        'ix_outbox_events_aggregate',
        'outbox_events',
        ['aggregate_type', 'aggregate_id', 'created_at'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        'ix_outbox_events_aggregate',
        table_name='outbox_events',
    )
    op.drop_index(
        'ix_outbox_events_unpublished',
        table_name='outbox_events',
    )
    op.drop_table('outbox_events')
