"""enforce tenant consistency

Revision ID: e1b7d3a9c4f2
Revises: c5e8a2d4f6b1
Create Date: 2026-10-23 12:00:00.000000

Rows that link records of two restaurants are rejected by triggers:

- a preparation's station must belong to its order's restaurant,
- an order item's product (through its category) must belong to its
  order's restaurant,
- a product's station, if set, must belong to its category's restaurant.

The triggers fire on INSERT and on UPDATE OF the linking columns only, so
existing rows are not validated and other updates of them (e.g. a status
change) keep working. Existing violations are reported as warnings and
left unchanged. Changing restaurant_id of an order, station or category
afterwards is not checked.
"""
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1b7d3a9c4f2'
down_revision: Union[str, Sequence[str], None] = 'c5e8a2d4f6b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


logger = logging.getLogger("alembic.tenant_consistency")

# (constraint name reported by the trigger, table, trigger, function,
#  watched columns, body). The body sets `linked` and `own` to the two
# restaurant ids that must be equal; a NULL (missing row, product without
# station) is left to the foreign keys and NOT NULL constraints.
CHECKS = (
    (
        'ck_preparations_station_restaurant',
        'preparations',
        'trg_preparations_station_restaurant',
        'check_preparation_station_restaurant',
        ('order_item_id', 'station_id'),
        """
        SELECT o.restaurant_id INTO own
        FROM order_items oi JOIN orders o ON o.id = oi.order_id
        WHERE oi.id = NEW.order_item_id;
        SELECT s.restaurant_id INTO linked
        FROM stations s WHERE s.id = NEW.station_id;
        """,
    ),
    (
        'ck_order_items_product_restaurant',
        'order_items',
        'trg_order_items_product_restaurant',
        'check_order_item_product_restaurant',
        ('order_id', 'product_id'),
        """
        SELECT o.restaurant_id INTO own
        FROM orders o WHERE o.id = NEW.order_id;
        SELECT c.restaurant_id INTO linked
        FROM products p JOIN categories c ON c.id = p.category_id
        WHERE p.id = NEW.product_id;
        """,
    ),
    (
        'ck_products_station_restaurant',
        'products',
        'trg_products_station_restaurant',
        'check_product_station_restaurant',
        ('category_id', 'station_id'),
        """
        SELECT c.restaurant_id INTO own
        FROM categories c WHERE c.id = NEW.category_id;
        SELECT s.restaurant_id INTO linked
        FROM stations s WHERE s.id = NEW.station_id;
        """,
    ),
)

# Existing rows that the triggers would reject, per constraint.
VIOLATIONS = {
    'ck_preparations_station_restaurant': """
        SELECT p.id FROM preparations p
        JOIN order_items oi ON oi.id = p.order_item_id
        JOIN orders o ON o.id = oi.order_id
        JOIN stations s ON s.id = p.station_id
        WHERE s.restaurant_id <> o.restaurant_id
        ORDER BY p.id
    """,
    'ck_order_items_product_restaurant': """
        SELECT oi.id FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        JOIN products p ON p.id = oi.product_id
        JOIN categories c ON c.id = p.category_id
        WHERE c.restaurant_id <> o.restaurant_id
        ORDER BY oi.id
    """,
    'ck_products_station_restaurant': """
        SELECT p.id FROM products p
        JOIN categories c ON c.id = p.category_id
        JOIN stations s ON s.id = p.station_id
        WHERE s.restaurant_id <> c.restaurant_id
        ORDER BY p.id
    """,
}


def historical_violations(connection) -> dict[str, list]:
    """Ids of existing rows that link two restaurants, per constraint."""

    return {
        constraint: list(connection.scalars(sa.text(query)))
        for constraint, query in VIOLATIONS.items()
    }


def upgrade() -> None:
    """Upgrade schema."""
    for constraint, table, trigger, function, columns, body in CHECKS:
        op.execute(f"""
            CREATE FUNCTION {function}() RETURNS trigger
            LANGUAGE plpgsql AS $$
            DECLARE
                own uuid;
                linked uuid;
            BEGIN
                {body}
                IF own IS NOT NULL AND linked IS NOT NULL
                        AND own <> linked THEN
                    RAISE EXCEPTION
                        '% row % links records of two restaurants',
                        TG_TABLE_NAME, NEW.id
                        USING ERRCODE = 'check_violation',
                              CONSTRAINT = '{constraint}';
                END IF;
                RETURN NULL;
            END
            $$
        """)
        op.execute(f"""
            CREATE TRIGGER {trigger}
            AFTER INSERT OR UPDATE OF {', '.join(columns)} ON {table}
            FOR EACH ROW EXECUTE FUNCTION {function}()
        """)

    for constraint, ids in historical_violations(op.get_bind()).items():
        if ids:
            logger.warning(
                "%d existing row(s) violate %s and were left unchanged: %s",
                len(ids),
                constraint,
                ", ".join(str(row_id) for row_id in ids),
            )


def downgrade() -> None:
    """Downgrade schema."""
    # Removes only the triggers and their functions; no data changes.
    for _, table, trigger, function, _, _ in reversed(CHECKS):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")
