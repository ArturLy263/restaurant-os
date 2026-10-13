"""add station assignments

Revision ID: d4f1a8c2b6e3
Revises: b7c3e9a41d2f
Create Date: 2026-10-14 00:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4f1a8c2b6e3'
down_revision: Union[str, Sequence[str], None] = 'b7c3e9a41d2f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # A new table only: no existing rows are read or changed. Kitchen and
    # Bar users start without stations (fail closed) until assigned.
    op.create_table('station_assignments',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('station_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['station_id'], ['stations.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('user_id', 'station_id')
    )
    op.create_index(
        'ix_station_assignments_station_id',
        'station_assignments',
        ['station_id'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        'ix_station_assignments_station_id',
        table_name='station_assignments',
    )
    op.drop_table('station_assignments')
