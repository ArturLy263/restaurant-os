"""security hardening: token revocation, login throttling, blind counts

Revision ID: b8d4e6f2a1c3
Revises: f2a8c6e4b1d9
Create Date: 2026-11-25 12:00:00.000000

Forward-only. Adds, without changing any existing row:

- users.token_version (default 0), carried in access tokens, and the
  trigger users_bump_token_version that increments it whenever
  password_hash changes, so a new password ends every older session.
- revoked_tokens: token ids (jti) ended by sign-out, until they expire.
  Raw tokens are never stored.
- login_throttles: failed sign-in counters per hashed key (email or
  client address), shared by every API process.
- Blind counts without an oracle: the checks ck_cash_shift_closings_
  explained and ck_cash_shift_handovers_reason made a count that differed
  from the expected cash fail without an explanation, so retrying told a
  cashier whether a guess matched. They are replaced by:
  * cash_count_explanations (append-only, one per shift and kind, written
    only while the shift's business day is open) for explanations given
    after the count was recorded;
  * the trigger business_days_explained_before_close, which refuses to
    close a business day while a closing or handover difference has no
    explanation. Frozen reports therefore still explain every difference.

Downgrade drops what is added here and restores the two checks NOT VALID
(rows recorded meanwhile are kept as they are).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b8d4e6f2a1c3'
down_revision: Union[str, Sequence[str], None] = 'f2a8c6e4b1d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    # --- Token revocation ---
    op.add_column(
        'users',
        sa.Column('token_version', sa.Integer(), server_default=sa.text('0'), nullable=False),
    )
    op.execute(
        """
        CREATE FUNCTION users_bump_token_version() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.password_hash IS DISTINCT FROM OLD.password_hash THEN
                NEW.token_version := OLD.token_version + 1;
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        'CREATE TRIGGER users_bump_token_version BEFORE UPDATE OF password_hash ON users '
        'FOR EACH ROW EXECUTE FUNCTION users_bump_token_version()'
    )
    op.create_table(
        'revoked_tokens',
        sa.Column('jti', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('jti'),
    )
    op.create_index('ix_revoked_tokens_expires_at', 'revoked_tokens', ['expires_at'])

    # --- Login throttling ---
    op.create_table(
        'login_throttles',
        sa.Column('key_hash', sa.String(length=64), nullable=False),
        sa.Column('failures', sa.Integer(), nullable=False),
        sa.Column('window_started_at', sa.DateTime(), nullable=False),
        sa.Column('blocked_until', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint('failures >= 0', name='ck_login_throttles_failures_nonnegative'),
        sa.PrimaryKeyConstraint('key_hash'),
    )
    op.create_index('ix_login_throttles_updated_at', 'login_throttles', ['updated_at'])

    # --- Blind counts without an oracle ---
    op.drop_constraint('ck_cash_shift_closings_explained', 'cash_shift_closings', type_='check')
    op.drop_constraint('ck_cash_shift_handovers_reason', 'cash_shift_handovers', type_='check')
    op.create_table(
        'cash_count_explanations',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('restaurant_id', sa.Uuid(), nullable=False),
        sa.Column('cash_shift_id', sa.Uuid(), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('explanation', sa.String(length=500), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint("kind IN ('CLOSING', 'HANDOVER')", name='ck_cash_count_explanations_kind'),
        sa.ForeignKeyConstraint(['restaurant_id'], ['restaurants.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(
            ['cash_shift_id', 'restaurant_id'],
            ['cash_shifts.id', 'cash_shifts.restaurant_id'],
            name='fk_cash_count_explanations_shift_restaurant',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('cash_shift_id', 'kind', name='uq_cash_count_explanations_shift_kind'),
    )
    op.execute(
        'CREATE TRIGGER cash_count_explanations_append_only BEFORE UPDATE OR DELETE '
        'ON cash_count_explanations FOR EACH ROW EXECUTE FUNCTION cashier_refuse_change()'
    )
    op.execute(
        """
        CREATE FUNCTION cash_count_explanation_open_day_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            day_status text;
        BEGIN
            SELECT d.status INTO day_status
            FROM cash_shifts s
            JOIN business_days d ON d.id = s.business_day_id
            WHERE s.id = NEW.cash_shift_id
            FOR SHARE OF d;
            IF day_status IS DISTINCT FROM 'OPEN' THEN
                RAISE EXCEPTION 'cash_count_explanations rows can only be written while the shift''s business day is open';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        'CREATE TRIGGER cash_count_explanations_open_day_only BEFORE INSERT '
        'ON cash_count_explanations FOR EACH ROW EXECUTE FUNCTION cash_count_explanation_open_day_only()'
    )
    op.execute(
        """
        CREATE FUNCTION business_days_refuse_unexplained_close() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.status = 'CLOSED' AND OLD.status IS DISTINCT FROM 'CLOSED' THEN
                IF EXISTS (
                    SELECT 1
                    FROM cash_shift_closings c
                    JOIN cash_shifts s ON s.id = c.cash_shift_id
                    WHERE s.business_day_id = NEW.id
                      AND c.difference <> 0
                      AND c.explanation IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM cash_count_explanations e
                          WHERE e.cash_shift_id = c.cash_shift_id AND e.kind = 'CLOSING'
                      )
                ) OR EXISTS (
                    SELECT 1
                    FROM cash_shift_handovers h
                    WHERE h.business_day_id = NEW.id
                      AND h.difference <> 0
                      AND h.reason IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM cash_count_explanations e
                          WHERE e.cash_shift_id = h.to_shift_id AND e.kind = 'HANDOVER'
                      )
                ) THEN
                    RAISE EXCEPTION 'business day % has unexplained cash differences', NEW.id;
                END IF;
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        'CREATE TRIGGER business_days_explained_before_close BEFORE UPDATE OF status '
        'ON business_days FOR EACH ROW EXECUTE FUNCTION business_days_refuse_unexplained_close()'
    )


def downgrade() -> None:
    """Downgrade schema: drops what upgrade added; recorded money rows are kept."""

    op.execute('DROP TRIGGER business_days_explained_before_close ON business_days')
    op.execute('DROP FUNCTION business_days_refuse_unexplained_close()')
    op.execute('DROP TRIGGER cash_count_explanations_open_day_only ON cash_count_explanations')
    op.execute('DROP FUNCTION cash_count_explanation_open_day_only()')
    op.execute('DROP TRIGGER cash_count_explanations_append_only ON cash_count_explanations')
    op.drop_table('cash_count_explanations')
    # NOT VALID: counts recorded without an explanation meanwhile stay.
    op.execute(
        'ALTER TABLE cash_shift_handovers ADD CONSTRAINT ck_cash_shift_handovers_reason '
        'CHECK (difference = 0 OR reason IS NOT NULL) NOT VALID'
    )
    op.execute(
        'ALTER TABLE cash_shift_closings ADD CONSTRAINT ck_cash_shift_closings_explained '
        'CHECK (difference = 0 OR explanation IS NOT NULL) NOT VALID'
    )

    op.drop_index('ix_login_throttles_updated_at', table_name='login_throttles')
    op.drop_table('login_throttles')
    op.drop_index('ix_revoked_tokens_expires_at', table_name='revoked_tokens')
    op.drop_table('revoked_tokens')
    op.execute('DROP TRIGGER users_bump_token_version ON users')
    op.execute('DROP FUNCTION users_bump_token_version()')
    op.drop_column('users', 'token_version')
