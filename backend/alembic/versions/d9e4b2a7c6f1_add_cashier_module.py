"""add cashier module

Revision ID: d9e4b2a7c6f1
Revises: c7e1a5b9d3f8
Create Date: 2026-11-01 10:00:00.000000

The cashier module (Stage 3, docs/stage-3-cashier-plan.md, Revision 2):

- business_days: the restaurant's trading days, one OPEN at a time,
  independent of cash shifts;
- cash_registers, cash_shifts (one OPEN shift per register, one active
  register per restaurant, each shift in one business day),
  cash_shift_handovers (the drawer passing between consecutive shifts,
  also across days), cash_opening_corrections, cash_movements and
  cash_shift_closings: an append-only cash ledger;
- payments get their restaurant, currency, tip, the employee who took
  them, their shift, how they were confirmed and an optional card
  terminal reference, and are limited to CASH and CARD;
- payment_allocations (an order item paid by a payment, at most once)
  and refunds, each in the shift and business day it was made in.

Closed business days and shifts, closings, handovers and the ledgers
can't be updated or deleted: triggers refuse it (closed periods are
immutable; corrections are new rows in an open period).

Existing payments are kept. restaurant_id and currency are filled in
from their bill's order and restaurant; user_id from their
payment.completed audit event where one exists. They have no shift or
confirmation: ck_payments_recorded_in_shift requires both (and a user)
only for rows written from now on (NOT VALID). The upgrade refuses to run
if a payment has a method other than CASH or CARD, before changing
anything.

The downgrade drops the new tables and columns with their data.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd9e4b2a7c6f1'
down_revision: Union[str, Sequence[str], None] = 'c7e1a5b9d3f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The status vocabularies this migration adds (see app.core.states).
STATUSES = {
    'business_days': ('CLOSED', 'OPEN'),
    'cash_shifts': ('CLOSED', 'OPEN'),
}

# Rows never changed or deleted once written.
APPEND_ONLY_TABLES = (
    'payment_allocations',
    'refunds',
    'cash_movements',
    'cash_opening_corrections',
    'cash_shift_handovers',
    'cash_shift_closings',
)
# Never deleted, and never changed once CLOSED.
CLOSABLE_TABLES = ('business_days', 'cash_shifts')

MONEY = sa.Numeric(10, 2)


def _id():
    return sa.Column('id', sa.Uuid(), nullable=False)


def _restaurant():
    return sa.Column('restaurant_id', sa.Uuid(), sa.ForeignKey('restaurants.id'), nullable=False)


def _shift_fk(name):
    return sa.ForeignKeyConstraint(
        ['cash_shift_id', 'restaurant_id'],
        ['cash_shifts.id', 'cash_shifts.restaurant_id'],
        name=name,
    )


def upgrade() -> None:
    """Upgrade schema."""

    connection = op.get_bind()
    methods = connection.execute(
        sa.text("SELECT DISTINCT method FROM payments WHERE method NOT IN ('CASH', 'CARD')")
    ).scalars().all()
    if methods:
        raise RuntimeError(
            'Existing payments have methods other than CASH and CARD; review '
            f'them before upgrading: {sorted(methods)}'
        )

    op.create_table(
        'business_days',
        _id(),
        _restaurant(),
        sa.Column('business_date', sa.Date(), nullable=False),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('opened_by_user_id', sa.Uuid(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('opened_at', sa.DateTime(), nullable=False),
        sa.Column('closed_by_user_id', sa.Uuid(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.Column('closing_summary', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("status IN ('CLOSED', 'OPEN')", name='ck_business_days_status'),
        sa.CheckConstraint(
            "(status = 'CLOSED') = (closed_at IS NOT NULL "
            "AND closed_by_user_id IS NOT NULL AND closing_summary IS NOT NULL)",
            name='ck_business_days_closed_fields',
        ),
        sa.UniqueConstraint('restaurant_id', 'business_date', name='uq_business_days_restaurant_date'),
        sa.UniqueConstraint('id', 'restaurant_id', name='uq_business_days_id_restaurant_id'),
    )
    op.create_index(
        'uq_business_days_one_open', 'business_days', ['restaurant_id'],
        unique=True, postgresql_where=sa.text("status = 'OPEN'"),
    )

    op.create_table(
        'cash_registers',
        _id(),
        _restaurant(),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('id', 'restaurant_id', name='uq_cash_registers_id_restaurant_id'),
    )
    op.create_index(
        'uq_cash_registers_one_active', 'cash_registers', ['restaurant_id'],
        unique=True, postgresql_where=sa.text('is_active'),
    )

    op.create_table(
        'cash_shifts',
        _id(),
        _restaurant(),
        sa.Column('cash_register_id', sa.Uuid(), nullable=False),
        sa.Column('business_day_id', sa.Uuid(), nullable=False),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('opening_amount', MONEY, nullable=False),
        sa.Column('opened_by_user_id', sa.Uuid(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('opened_at', sa.DateTime(), nullable=False),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("status IN ('CLOSED', 'OPEN')", name='ck_cash_shifts_status'),
        sa.CheckConstraint('opening_amount >= 0', name='ck_cash_shifts_opening_amount_nonnegative'),
        sa.CheckConstraint("(status = 'CLOSED') = (closed_at IS NOT NULL)", name='ck_cash_shifts_closed_at'),
        sa.UniqueConstraint('id', 'restaurant_id', name='uq_cash_shifts_id_restaurant_id'),
        sa.UniqueConstraint('id', 'business_day_id', name='uq_cash_shifts_id_business_day_id'),
        sa.UniqueConstraint('id', 'cash_register_id', name='uq_cash_shifts_id_cash_register_id'),
        sa.ForeignKeyConstraint(
            ['cash_register_id', 'restaurant_id'],
            ['cash_registers.id', 'cash_registers.restaurant_id'],
            name='fk_cash_shifts_register_restaurant',
        ),
        sa.ForeignKeyConstraint(
            ['business_day_id', 'restaurant_id'],
            ['business_days.id', 'business_days.restaurant_id'],
            name='fk_cash_shifts_business_day_restaurant',
        ),
    )
    op.create_index('ix_cash_shifts_business_day_id', 'cash_shifts', ['business_day_id'])
    op.create_index(
        'uq_cash_shifts_one_open', 'cash_shifts', ['cash_register_id'],
        unique=True, postgresql_where=sa.text("status = 'OPEN'"),
    )
    op.create_index('ix_cash_shifts_register_opened_at', 'cash_shifts', ['cash_register_id', 'opened_at'])

    # Payments: new columns, then fill in what the old rows imply.
    op.add_column('payments', sa.Column('restaurant_id', sa.Uuid(), nullable=True))
    op.add_column('payments', sa.Column('currency', sa.String(length=3), nullable=True))
    op.add_column('payments', sa.Column('tip_amount', MONEY, server_default=sa.text('0'), nullable=False))
    op.add_column('payments', sa.Column('user_id', sa.Uuid(), nullable=True))
    op.add_column('payments', sa.Column('cash_shift_id', sa.Uuid(), nullable=True))
    op.add_column('payments', sa.Column('business_day_id', sa.Uuid(), nullable=True))
    op.add_column('payments', sa.Column('confirmation_method', sa.String(length=30), nullable=True))
    op.add_column('payments', sa.Column('confirmed_at', sa.DateTime(), nullable=True))
    op.add_column('payments', sa.Column('external_reference', sa.String(length=100), nullable=True))
    op.execute(
        """
        UPDATE payments p
        SET restaurant_id = o.restaurant_id, currency = r.currency
        FROM bills b
        JOIN orders o ON o.id = b.order_id
        JOIN restaurants r ON r.id = o.restaurant_id
        WHERE b.id = p.bill_id
        """
    )
    op.execute(
        """
        UPDATE payments p
        SET user_id = e.user_id
        FROM operational_events e
        WHERE e.event_type = 'payment.completed'
          AND e.entity_type = 'payment'
          AND e.entity_id = p.id
          AND e.restaurant_id = p.restaurant_id
          AND e.user_id IS NOT NULL
        """
    )
    op.alter_column('payments', 'restaurant_id', nullable=False)
    op.alter_column('payments', 'currency', nullable=False)
    op.create_foreign_key('payments_restaurant_id_fkey', 'payments', 'restaurants', ['restaurant_id'], ['id'])
    op.create_foreign_key('payments_user_id_fkey', 'payments', 'users', ['user_id'], ['id'])
    op.create_unique_constraint('uq_payments_id_restaurant_id', 'payments', ['id', 'restaurant_id'])
    op.create_foreign_key(
        'fk_payments_cash_shift_restaurant', 'payments', 'cash_shifts',
        ['cash_shift_id', 'restaurant_id'], ['id', 'restaurant_id'],
    )
    op.create_foreign_key(
        'fk_payments_cash_shift_business_day', 'payments', 'cash_shifts',
        ['cash_shift_id', 'business_day_id'], ['id', 'business_day_id'],
    )
    op.create_check_constraint('ck_payments_method', 'payments', "method IN ('CASH', 'CARD')")
    op.create_check_constraint('ck_payments_amount_positive', 'payments', 'amount > 0')
    op.create_check_constraint('ck_payments_tip_amount_nonnegative', 'payments', 'tip_amount >= 0')
    op.create_check_constraint(
        'ck_payments_external_reference_card_only', 'payments',
        "external_reference IS NULL OR method = 'CARD'",
    )
    # Every payment from now on: rows before this migration are exempt.
    op.execute(
        """
        ALTER TABLE payments ADD CONSTRAINT ck_payments_recorded_in_shift
        CHECK (
            cash_shift_id IS NOT NULL AND business_day_id IS NOT NULL
            AND user_id IS NOT NULL
            AND confirmation_method IN ('CASH_RECEIVED', 'MANUAL_CARD_TERMINAL')
            AND confirmed_at IS NOT NULL
            AND (confirmation_method = 'CASH_RECEIVED') = (method = 'CASH')
        ) NOT VALID
        """
    )
    op.create_index('ix_payments_bill_id', 'payments', ['bill_id'])
    op.create_index('ix_payments_cash_shift_id', 'payments', ['cash_shift_id'])
    op.create_index('ix_payments_business_day_id', 'payments', ['business_day_id'])

    op.create_table(
        'payment_allocations',
        _id(),
        _restaurant(),
        sa.Column('payment_id', sa.Uuid(), nullable=False),
        sa.Column('order_item_id', sa.Uuid(), sa.ForeignKey('order_items.id'), nullable=False),
        sa.Column('amount', MONEY, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('order_item_id'),
        sa.CheckConstraint('amount > 0', name='ck_payment_allocations_amount_positive'),
        sa.ForeignKeyConstraint(
            ['payment_id', 'restaurant_id'],
            ['payments.id', 'payments.restaurant_id'],
            name='fk_payment_allocations_payment_restaurant',
        ),
    )
    op.create_index('ix_payment_allocations_payment_id', 'payment_allocations', ['payment_id'])

    op.create_table(
        'refunds',
        _id(),
        _restaurant(),
        sa.Column('payment_id', sa.Uuid(), nullable=False),
        sa.Column('cash_shift_id', sa.Uuid(), nullable=False),
        sa.Column('business_day_id', sa.Uuid(), nullable=False),
        sa.Column('amount', MONEY, nullable=False),
        sa.Column('tip_amount', MONEY, nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('method', sa.String(length=30), nullable=False),
        sa.Column('confirmation_method', sa.String(length=30), nullable=False),
        sa.Column('external_reference', sa.String(length=100), nullable=True),
        sa.Column('reason', sa.String(length=500), nullable=False),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            'amount >= 0 AND tip_amount >= 0 AND amount + tip_amount > 0',
            name='ck_refunds_amounts',
        ),
        sa.CheckConstraint("method IN ('CASH', 'CARD')", name='ck_refunds_method'),
        sa.CheckConstraint(
            "external_reference IS NULL OR method = 'CARD'",
            name='ck_refunds_external_reference_card_only',
        ),
        sa.CheckConstraint(
            "confirmation_method IN ('CASH_RECEIVED', 'MANUAL_CARD_TERMINAL') "
            "AND (confirmation_method = 'CASH_RECEIVED') = (method = 'CASH')",
            name='ck_refunds_confirmation_method',
        ),
        sa.UniqueConstraint('id', 'restaurant_id', name='uq_refunds_id_restaurant_id'),
        sa.ForeignKeyConstraint(
            ['payment_id', 'restaurant_id'],
            ['payments.id', 'payments.restaurant_id'],
            name='fk_refunds_payment_restaurant',
        ),
        _shift_fk('fk_refunds_cash_shift_restaurant'),
        sa.ForeignKeyConstraint(
            ['cash_shift_id', 'business_day_id'],
            ['cash_shifts.id', 'cash_shifts.business_day_id'],
            name='fk_refunds_cash_shift_business_day',
        ),
    )
    op.create_index('ix_refunds_payment_id', 'refunds', ['payment_id'])
    op.create_index('ix_refunds_cash_shift_id', 'refunds', ['cash_shift_id'])
    op.create_index('ix_refunds_business_day_id', 'refunds', ['business_day_id'])

    op.create_table(
        'cash_movements',
        _id(),
        _restaurant(),
        sa.Column('cash_shift_id', sa.Uuid(), nullable=False),
        sa.Column('type', sa.String(length=30), nullable=False),
        sa.Column('amount', MONEY, nullable=False),
        sa.Column('reason', sa.String(length=500), nullable=True),
        sa.Column('payment_id', sa.Uuid(), nullable=True),
        sa.Column('refund_id', sa.Uuid(), nullable=True),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            "type IN ('CASH_IN', 'CASH_OUT', 'CASH_REFUND', 'CASH_TIP')",
            name='ck_cash_movements_type',
        ),
        sa.CheckConstraint('amount > 0', name='ck_cash_movements_amount_positive'),
        sa.CheckConstraint(
            "(type IN ('CASH_IN', 'CASH_OUT') AND reason IS NOT NULL "
            "AND payment_id IS NULL AND refund_id IS NULL) "
            "OR (type = 'CASH_TIP' AND payment_id IS NOT NULL "
            "AND refund_id IS NULL) "
            "OR (type = 'CASH_REFUND' AND refund_id IS NOT NULL "
            "AND payment_id IS NULL)",
            name='ck_cash_movements_links',
        ),
        _shift_fk('fk_cash_movements_shift_restaurant'),
        sa.ForeignKeyConstraint(
            ['payment_id', 'restaurant_id'],
            ['payments.id', 'payments.restaurant_id'],
            name='fk_cash_movements_payment_restaurant',
        ),
        sa.ForeignKeyConstraint(
            ['refund_id', 'restaurant_id'],
            ['refunds.id', 'refunds.restaurant_id'],
            name='fk_cash_movements_refund_restaurant',
        ),
    )
    op.create_index(
        'uq_cash_movements_payment_tip', 'cash_movements', ['payment_id'],
        unique=True, postgresql_where=sa.text('payment_id IS NOT NULL'),
    )
    op.create_index(
        'uq_cash_movements_refund', 'cash_movements', ['refund_id'],
        unique=True, postgresql_where=sa.text('refund_id IS NOT NULL'),
    )
    op.create_index('ix_cash_movements_shift', 'cash_movements', ['cash_shift_id'])

    op.create_table(
        'cash_shift_handovers',
        _id(),
        _restaurant(),
        sa.Column('cash_register_id', sa.Uuid(), nullable=False),
        sa.Column('business_day_id', sa.Uuid(), nullable=False),
        sa.Column('from_shift_id', sa.Uuid(), nullable=False),
        sa.Column('to_shift_id', sa.Uuid(), nullable=False),
        sa.Column('previous_expected_amount', MONEY, nullable=False),
        sa.Column('handed_over_amount', MONEY, nullable=False),
        sa.Column('received_amount', MONEY, nullable=False),
        sa.Column('difference', MONEY, nullable=False),
        sa.Column('reason', sa.String(length=500), nullable=True),
        sa.Column('outgoing_user_id', sa.Uuid(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('incoming_user_id', sa.Uuid(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('from_shift_id'),
        sa.UniqueConstraint('to_shift_id'),
        sa.CheckConstraint(
            'previous_expected_amount >= 0 AND handed_over_amount >= 0 AND received_amount >= 0',
            name='ck_cash_shift_handovers_amounts_nonnegative',
        ),
        sa.CheckConstraint(
            'difference = received_amount - handed_over_amount',
            name='ck_cash_shift_handovers_difference',
        ),
        sa.CheckConstraint(
            'difference = 0 OR reason IS NOT NULL',
            name='ck_cash_shift_handovers_reason',
        ),
        sa.CheckConstraint('from_shift_id <> to_shift_id', name='ck_cash_shift_handovers_distinct_shifts'),
        sa.ForeignKeyConstraint(
            ['from_shift_id', 'cash_register_id'],
            ['cash_shifts.id', 'cash_shifts.cash_register_id'],
            name='fk_cash_shift_handovers_from_shift_register',
        ),
        sa.ForeignKeyConstraint(
            ['to_shift_id', 'cash_register_id'],
            ['cash_shifts.id', 'cash_shifts.cash_register_id'],
            name='fk_cash_shift_handovers_to_shift_register',
        ),
        sa.ForeignKeyConstraint(
            ['to_shift_id', 'business_day_id'],
            ['cash_shifts.id', 'cash_shifts.business_day_id'],
            name='fk_cash_shift_handovers_to_shift_business_day',
        ),
        sa.ForeignKeyConstraint(
            ['business_day_id', 'restaurant_id'],
            ['business_days.id', 'business_days.restaurant_id'],
            name='fk_cash_shift_handovers_business_day_restaurant',
        ),
        sa.ForeignKeyConstraint(
            ['cash_register_id', 'restaurant_id'],
            ['cash_registers.id', 'cash_registers.restaurant_id'],
            name='fk_cash_shift_handovers_register_restaurant',
        ),
    )
    op.create_index('ix_cash_shift_handovers_business_day_id', 'cash_shift_handovers', ['business_day_id'])

    op.create_table(
        'cash_opening_corrections',
        _id(),
        _restaurant(),
        sa.Column('cash_shift_id', sa.Uuid(), nullable=False),
        sa.Column('previous_amount', MONEY, nullable=False),
        sa.Column('new_amount', MONEY, nullable=False),
        sa.Column('reason', sa.String(length=500), nullable=False),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            'previous_amount >= 0 AND new_amount >= 0 AND new_amount <> previous_amount',
            name='ck_cash_opening_corrections_amounts',
        ),
        _shift_fk('fk_cash_opening_corrections_shift_restaurant'),
    )
    op.create_index('ix_cash_opening_corrections_shift', 'cash_opening_corrections', ['cash_shift_id'])

    op.create_table(
        'cash_shift_closings',
        _id(),
        _restaurant(),
        sa.Column('cash_shift_id', sa.Uuid(), nullable=False),
        sa.Column('expected_amount', MONEY, nullable=False),
        sa.Column('counted_amount', MONEY, nullable=False),
        sa.Column('difference', MONEY, nullable=False),
        sa.Column('explanation', sa.String(length=500), nullable=True),
        sa.Column('summary', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('cash_shift_id'),
        sa.CheckConstraint('counted_amount >= 0', name='ck_cash_shift_closings_counted_nonnegative'),
        sa.CheckConstraint(
            'difference = counted_amount - expected_amount',
            name='ck_cash_shift_closings_difference',
        ),
        sa.CheckConstraint(
            'difference = 0 OR explanation IS NOT NULL',
            name='ck_cash_shift_closings_explained',
        ),
        _shift_fk('fk_cash_shift_closings_shift_restaurant'),
    )

    # Closed periods and ledgers are immutable.
    op.execute(
        """
        CREATE FUNCTION cashier_refuse_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% rows are append-only (% refused)', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'restrict_violation';
        END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION cashier_refuse_change_when_closed() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' OR OLD.status = 'CLOSED' THEN
                RAISE EXCEPTION '% rows are kept and immutable once closed (% refused)', TG_TABLE_NAME, TG_OP
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    for table in APPEND_ONLY_TABLES:
        op.execute(
            f'CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} '
            'FOR EACH ROW EXECUTE FUNCTION cashier_refuse_change()'
        )
    for table in CLOSABLE_TABLES:
        op.execute(
            f'CREATE TRIGGER {table}_immutable_once_closed BEFORE UPDATE OR DELETE ON {table} '
            'FOR EACH ROW EXECUTE FUNCTION cashier_refuse_change_when_closed()'
        )


def downgrade() -> None:
    """Downgrade schema."""

    # Dropping the tables drops their triggers.
    op.drop_table('cash_shift_closings')
    op.drop_table('cash_opening_corrections')
    op.drop_index('ix_cash_shift_handovers_business_day_id', table_name='cash_shift_handovers')
    op.drop_table('cash_shift_handovers')
    op.drop_table('cash_movements')
    op.drop_table('refunds')
    op.drop_table('payment_allocations')

    op.drop_index('ix_payments_business_day_id', table_name='payments')
    op.drop_index('ix_payments_cash_shift_id', table_name='payments')
    op.drop_index('ix_payments_bill_id', table_name='payments')
    for name in (
        'ck_payments_recorded_in_shift',
        'ck_payments_external_reference_card_only',
        'ck_payments_tip_amount_nonnegative',
        'ck_payments_amount_positive',
        'ck_payments_method',
    ):
        op.drop_constraint(name, 'payments', type_='check')
    op.drop_constraint('fk_payments_cash_shift_business_day', 'payments', type_='foreignkey')
    op.drop_constraint('fk_payments_cash_shift_restaurant', 'payments', type_='foreignkey')
    op.drop_constraint('uq_payments_id_restaurant_id', 'payments', type_='unique')
    op.drop_constraint('payments_user_id_fkey', 'payments', type_='foreignkey')
    op.drop_constraint('payments_restaurant_id_fkey', 'payments', type_='foreignkey')
    for column in (
        'external_reference',
        'confirmed_at',
        'confirmation_method',
        'business_day_id',
        'cash_shift_id',
        'user_id',
        'tip_amount',
        'currency',
        'restaurant_id',
    ):
        op.drop_column('payments', column)

    op.drop_index('ix_cash_shifts_business_day_id', table_name='cash_shifts')
    op.drop_index('ix_cash_shifts_register_opened_at', table_name='cash_shifts')
    op.drop_index('uq_cash_shifts_one_open', table_name='cash_shifts')
    op.drop_table('cash_shifts')
    op.drop_index('uq_cash_registers_one_active', table_name='cash_registers')
    op.drop_table('cash_registers')
    op.drop_index('uq_business_days_one_open', table_name='business_days')
    op.drop_table('business_days')
    op.execute('DROP FUNCTION cashier_refuse_change_when_closed()')
    op.execute('DROP FUNCTION cashier_refuse_change()')
