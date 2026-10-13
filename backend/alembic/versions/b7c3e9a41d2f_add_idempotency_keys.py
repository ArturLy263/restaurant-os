"""add idempotency keys

Revision ID: b7c3e9a41d2f
Revises: 67f2c099d4d4
Create Date: 2026-10-13 21:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c3e9a41d2f'
down_revision: Union[str, Sequence[str], None] = '67f2c099d4d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # A new table only: no existing rows are read or changed.
    op.create_table('idempotency_keys',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('restaurant_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('idempotency_key', sa.String(length=255), nullable=False),
    sa.Column('operation', sa.String(length=200), nullable=False),
    sa.Column('request_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('response_status_code', sa.Integer(), nullable=True),
    sa.Column('response_body', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['restaurant_id'], ['restaurants.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('restaurant_id', 'user_id', 'idempotency_key', name='uq_idempotency_keys_scope_key')
    )
    op.create_index(
        'ix_idempotency_keys_expires_at',
        'idempotency_keys',
        ['expires_at'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        'ix_idempotency_keys_expires_at',
        table_name='idempotency_keys',
    )
    op.drop_table('idempotency_keys')
