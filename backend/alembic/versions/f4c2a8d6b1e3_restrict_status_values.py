"""restrict status values

Revision ID: f4c2a8d6b1e3
Revises: e1b7d3a9c4f2
Create Date: 2026-10-27 22:00:00.000000

Adds a CHECK constraint per status column, limiting it to the statuses of
app.core.states (copied here: a migration must not change when the code
does). Rows are not changed. If a table already holds another status, the
upgrade stops before adding anything and names the table and the values,
so they can be reviewed; nothing is rewritten automatically.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4c2a8d6b1e3'
down_revision: Union[str, Sequence[str], None] = 'e1b7d3a9c4f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


STATUSES = {
    'orders': ('CANCELLED', 'CLOSED', 'OPEN', 'SENT'),
    'order_items': ('ADDED', 'CANCELLED', 'SENT'),
    'preparations': ('CANCELLED', 'IN_PROGRESS', 'PENDING', 'READY', 'SERVED'),
    'bills': ('OPEN', 'PAID', 'VOID'),
    'tables': ('AVAILABLE', 'OCCUPIED'),
    'payments': ('COMPLETED',),
    'bill_adjustments': ('APPLIED',),
}


def _condition(values):
    return 'status IN (' + ', '.join(f"'{value}'" for value in values) + ')'


def upgrade() -> None:
    """Upgrade schema."""

    connection = op.get_bind()
    unexpected = {}
    for table, values in STATUSES.items():
        found = connection.execute(
            sa.text(f'SELECT DISTINCT status FROM {table} WHERE NOT ({_condition(values)})')
        ).scalars().all()
        if found:
            unexpected[table] = sorted(found)
    if unexpected:
        raise RuntimeError(
            'Existing rows have statuses outside the allowed values; review '
            f'them before upgrading: {unexpected}'
        )

    for table, values in STATUSES.items():
        op.create_check_constraint(f'ck_{table}_status', table, _condition(values))


def downgrade() -> None:
    """Downgrade schema."""

    for table in reversed(list(STATUSES)):
        op.drop_constraint(f'ck_{table}_status', table, type_='check')
