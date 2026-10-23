"""Report (and, only with --apply, fix) historical development test data.

Dry run by default: it only reads, inside a transaction that is rolled
back, and prints what it found and what --apply would do.

    .venv/bin/python scripts/cleanup_historical_test_data.py
    .venv/bin/python scripts/cleanup_historical_test_data.py \
        --apply --actor-email manager@example.com

What it looks for:

1. Unfinished preparations (PENDING/IN_PROGRESS/READY) of fully paid,
   CLOSED orders. --apply discards each one through
   OrderService.discard_preparation, exactly like the API: audit event
   with the reason, outbox event, no bill change.
2. Order items whose product belongs to another restaurant than the order
   (written before products were looked up per restaurant), and their
   preparations, which then point at the other restaurant's station and
   appear in no queue. --apply cancels such an item through
   OrderService.cancel_order_item (audit and outbox events, preparation
   cancelled) when its order has no bill and its preparation is not
   READY/SERVED; anything else is reported for a manual decision.
3. Products whose station belongs to another restaurant: reported only.

Every change goes through the services, as the --actor-email user, who
must be an active MANAGER or ADMIN of the order's restaurant. Nothing is
deleted. Run without --apply first and review the output.
"""

import argparse
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.errors import ApiError  # noqa: E402
from app.core.permissions import Permission, permissions_for_role  # noqa: E402
from app.db.database import SessionLocal, engine  # noqa: E402
from app.db.models.role import Role  # noqa: E402
from app.db.models.user import User  # noqa: E402
from app.services.order_service import OrderService  # noqa: E402


REASON = "Historical test data cleanup"


@dataclass(frozen=True)
class Finding:
    kind: str
    restaurant_id: uuid.UUID
    order_id: uuid.UUID | None
    row_id: uuid.UUID
    detail: str
    action: str | None  # what --apply does; None: manual decision


def paid_orders_unfinished_preparations(db: Session) -> list[Finding]:
    rows = db.execute(
        text("""
            SELECT p.id, p.status, o.id AS order_id, o.restaurant_id,
                   t.number AS table_number, pr.name AS product
            FROM preparations p
            JOIN order_items oi ON oi.id = p.order_item_id
            JOIN orders o ON o.id = oi.order_id
            JOIN tables t ON t.id = o.table_id
            JOIN products pr ON pr.id = oi.product_id
            WHERE o.status = 'CLOSED'
              AND p.status IN ('PENDING', 'IN_PROGRESS', 'READY')
            ORDER BY p.created_at, p.id
        """)
    ).mappings()

    return [
        Finding(
            kind="paid order, unfinished preparation",
            restaurant_id=row["restaurant_id"],
            order_id=row["order_id"],
            row_id=row["id"],
            detail=f"{row['product']} at table {row['table_number']}, {row['status']}",
            action="discard preparation",
        )
        for row in rows
    ]


def cross_restaurant_order_items(db: Session) -> list[Finding]:
    rows = db.execute(
        text("""
            SELECT oi.id, oi.status, o.id AS order_id, o.restaurant_id,
                   o.status AS order_status, pr.name AS product,
                   p.status AS preparation_status,
                   EXISTS (SELECT 1 FROM bills b WHERE b.order_id = o.id)
                       AS billed
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
            JOIN products pr ON pr.id = oi.product_id
            JOIN categories c ON c.id = pr.category_id
            LEFT JOIN preparations p ON p.order_item_id = oi.id
            WHERE c.restaurant_id <> o.restaurant_id
            ORDER BY o.created_at, oi.id
        """)
    ).mappings()

    findings = []
    for row in rows:
        cancellable = (
            row["status"] != "CANCELLED"
            and row["order_status"] in ("OPEN", "SENT")
            and not row["billed"]
            and row["preparation_status"] in (None, "PENDING", "IN_PROGRESS")
        )
        findings.append(
            Finding(
                kind="order item of another restaurant's product",
                restaurant_id=row["restaurant_id"],
                order_id=row["order_id"],
                row_id=row["id"],
                detail=(
                    f"{row['product']}, item {row['status']}, order "
                    f"{row['order_status']}, preparation "
                    f"{row['preparation_status'] or 'none'}"
                    f"{', billed' if row['billed'] else ''}"
                ),
                action="cancel order item" if cancellable else None,
            )
        )

    return findings


def cross_restaurant_products(db: Session) -> list[Finding]:
    rows = db.execute(
        text("""
            SELECT p.id, p.name, c.restaurant_id
            FROM products p
            JOIN categories c ON c.id = p.category_id
            JOIN stations s ON s.id = p.station_id
            WHERE s.restaurant_id <> c.restaurant_id
            ORDER BY p.id
        """)
    ).mappings()

    return [
        Finding(
            kind="product with another restaurant's station",
            restaurant_id=row["restaurant_id"],
            order_id=None,
            row_id=row["id"],
            detail=row["name"],
            action=None,
        )
        for row in rows
    ]


def find_all(db: Session) -> list[Finding]:
    return (
        paid_orders_unfinished_preparations(db)
        + cross_restaurant_order_items(db)
        + cross_restaurant_products(db)
    )


def report(findings: list[Finding]) -> None:
    if not findings:
        print("No historical data issues found.")
        return

    for finding in findings:
        order = f" order {finding.order_id}" if finding.order_id else ""
        action = finding.action or "manual decision (not changed)"
        print(
            f"- {finding.kind}: {finding.row_id}{order} "
            f"[{finding.detail}] -> {action}"
        )

    actionable = sum(1 for finding in findings if finding.action)
    print(
        f"\n{len(findings)} finding(s); --apply would change {actionable}, "
        f"{len(findings) - actionable} need a manual decision."
    )


def actor_for(db: Session, email: str, restaurant_id: uuid.UUID) -> User:
    user = db.query(User).filter(User.email == email).one_or_none()
    if user is None or not user.is_active:
        raise SystemExit(f"No active user {email}.")

    role = db.get(Role, user.role_id)
    permissions = permissions_for_role(
        role.name if role is not None and role.restaurant_id == user.restaurant_id else None
    )
    if Permission.PREPARATIONS_DISCARD not in permissions:
        raise SystemExit(f"{email} is not a MANAGER or ADMIN.")
    if user.restaurant_id != restaurant_id:
        raise SystemExit(f"{email} does not belong to restaurant {restaurant_id}.")

    return user


def apply(findings: list[Finding], actor_email: str) -> None:
    for finding in findings:
        if finding.action is None:
            continue

        # One transaction per change, through the services (which commit).
        with SessionLocal() as db:
            actor = actor_for(db, actor_email, finding.restaurant_id)
            try:
                if finding.action == "discard preparation":
                    OrderService.discard_preparation(
                        db=db,
                        restaurant_id=finding.restaurant_id,
                        user_id=actor.id,
                        preparation_id=finding.row_id,
                        reason=REASON,
                        station_scope=None,
                    )
                else:
                    OrderService.cancel_order_item(
                        db=db,
                        restaurant_id=finding.restaurant_id,
                        user_id=actor.id,
                        order_id=finding.order_id,
                        item_id=finding.row_id,
                        reason=REASON,
                        can_modify_any_order=True,
                    )
            except ApiError as error:
                db.rollback()
                print(f"  skipped {finding.row_id}: {error.code} {error.detail}")
            else:
                print(f"  {finding.action}: {finding.row_id} done")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="change the data (default: dry run, read only)",
    )
    parser.add_argument(
        "--actor-email",
        help="active MANAGER or ADMIN recorded as the actor (required with --apply)",
    )
    args = parser.parse_args()

    if args.apply and not args.actor_email:
        parser.error("--apply requires --actor-email")

    url = engine.url
    print(f"Database: {url.database} on {url.host}:{url.port}")

    with SessionLocal() as db:
        findings = find_all(db)
        db.rollback()

    report(findings)

    if not args.apply:
        print("\nDry run: nothing was changed. Review, then rerun with --apply.")
        return

    print("\nApplying...")
    apply(findings, args.actor_email)


if __name__ == "__main__":
    main()
