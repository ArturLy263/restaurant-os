"""harden the cashier ledger

Revision ID: f2a8c6e4b1d9
Revises: d9e4b2a7c6f1
Create Date: 2026-11-05 15:00:00.000000

Stage 3.1 (cashier financial integrity), forward-only hardening of the
schema added by d9e4b2a7c6f1. No table, column or row is added, changed
or removed: only triggers.

- payments become append-only like the rest of the ledger: a payment is
  never updated or deleted (the payment state machine has no
  transitions; a refund is a new row). cashier_refuse_change() from
  d9e4b2a7c6f1 is reused.
- Financial rows can only be written into an OPEN cash shift of an OPEN
  business day: payments, payment_allocations (through their payment),
  refunds, cash_movements, cash_opening_corrections, cash_shift_closings
  and cash_shift_handovers (through the incoming shift); and a cash shift
  only into an OPEN business day. The services already guarantee this
  under row locks; the triggers make the database refuse it too. The
  shift row is read FOR SHARE, so an insert racing a shift close waits
  for it and then sees the shift CLOSED.

Existing rows are not checked or touched (the triggers fire on INSERT,
UPDATE and DELETE only).

Downgrade drops the triggers and the function added here and nothing
else: no financial data is changed or lost.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'f2a8c6e4b1d9'
down_revision: Union[str, Sequence[str], None] = 'd9e4b2a7c6f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Ledger rows written in a cash shift (and so in its business day).
SHIFT_LEDGER_TABLES = (
    'payments',
    'payment_allocations',
    'refunds',
    'cash_movements',
    'cash_opening_corrections',
    'cash_shift_closings',
    'cash_shift_handovers',
)


def upgrade() -> None:
    """Upgrade schema."""

    op.execute(
        'CREATE TRIGGER payments_append_only BEFORE UPDATE OR DELETE ON payments '
        'FOR EACH ROW EXECUTE FUNCTION cashier_refuse_change()'
    )

    # Each branch reads only the columns of its own table: PL/pgSQL plans a
    # statement when it first runs, per trigger table.
    op.execute(
        """
        CREATE FUNCTION cashier_refuse_insert_into_closed_period() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            shift_id uuid;
            day_id uuid;
            shift_status text := 'OPEN';
            day_status text;
        BEGIN
            IF TG_TABLE_NAME = 'cash_shifts' THEN
                day_id := NEW.business_day_id;
            ELSE
                IF TG_TABLE_NAME = 'payment_allocations' THEN
                    SELECT p.cash_shift_id INTO shift_id FROM payments p WHERE p.id = NEW.payment_id;
                ELSIF TG_TABLE_NAME = 'cash_shift_handovers' THEN
                    shift_id := NEW.to_shift_id;
                ELSE
                    shift_id := NEW.cash_shift_id;
                END IF;
                IF shift_id IS NULL THEN
                    RAISE EXCEPTION '% rows can only be written in a cash shift', TG_TABLE_NAME
                        USING ERRCODE = 'restrict_violation';
                END IF;
                SELECT s.status, s.business_day_id INTO shift_status, day_id
                FROM cash_shifts s WHERE s.id = shift_id
                FOR SHARE;
            END IF;
            SELECT d.status INTO day_status FROM business_days d WHERE d.id = day_id;
            IF shift_status IS DISTINCT FROM 'OPEN' OR day_status IS DISTINCT FROM 'OPEN' THEN
                RAISE EXCEPTION '% rows can only be written into an open cash shift of an open business day', TG_TABLE_NAME
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    for table in (*SHIFT_LEDGER_TABLES, 'cash_shifts'):
        op.execute(
            f'CREATE TRIGGER {table}_open_period_only BEFORE INSERT ON {table} '
            'FOR EACH ROW EXECUTE FUNCTION cashier_refuse_insert_into_closed_period()'
        )


def downgrade() -> None:
    """Downgrade schema: triggers only, no data is touched."""

    for table in (*SHIFT_LEDGER_TABLES, 'cash_shifts'):
        op.execute(f'DROP TRIGGER {table}_open_period_only ON {table}')
    op.execute('DROP FUNCTION cashier_refuse_insert_into_closed_period()')
    op.execute('DROP TRIGGER payments_append_only ON payments')
