"""The statuses of stateful records and the status changes they allow.

This is the single list of what may happen to an order, item, preparation,
bill or table. Services still check each operation's business rules first
and answer with its specific error (ORDER_BILLED, INVALID_PREPARATION_
TRANSITION, ...); every status write then goes through `StateMachine.apply`,
which refuses a change not listed here. A guard that is missing or wrong
therefore fails the request (a 500, rolled back) instead of storing an
impossible state.

The database enforces each vocabulary with a CHECK constraint (migration
f4c2a8d6b1e3), and tests keep these tables, those constraints, the model
defaults and the preparation event contracts in step.
"""

from dataclasses import dataclass
from typing import Any


class IllegalTransition(RuntimeError):
    """A status change these rules do not allow: a bug, never user input."""


@dataclass(frozen=True)
class StateMachine:
    name: str
    initial: str
    transitions: frozenset[tuple[str, str]]

    @property
    def statuses(self) -> frozenset[str]:
        return frozenset({self.initial}).union(*self.transitions)

    def allows(self, current: str, new: str) -> bool:
        return (current, new) in self.transitions

    def apply(self, record: Any, new_status: str) -> str:
        """Set `record.status`; returns the previous status."""

        previous = record.status
        if not self.allows(previous, new_status):
            raise IllegalTransition(f"{self.name}: {previous} -> {new_status}")
        record.status = new_status
        return previous


ORDER = StateMachine(
    name="order",
    initial="OPEN",
    transitions=frozenset({
        ("OPEN", "SENT"),
        # Every later round of items is sent the same way.
        ("SENT", "SENT"),
        # Void.
        ("OPEN", "CANCELLED"),
        ("SENT", "CANCELLED"),
        # The final payment; bills exist only for sent orders.
        ("SENT", "CLOSED"),
    }),
)

ORDER_ITEM = StateMachine(
    name="order item",
    initial="ADDED",
    transitions=frozenset({
        ("ADDED", "SENT"),
        ("ADDED", "CANCELLED"),
        ("SENT", "CANCELLED"),
    }),
)

# The forward steps a station works through (start, ready, serve).
PREPARATION_STEPS = {
    "PENDING": "IN_PROGRESS",
    "IN_PROGRESS": "READY",
    "READY": "SERVED",
}

PREPARATION = StateMachine(
    name="preparation",
    initial="PENDING",
    transitions=frozenset(PREPARATION_STEPS.items()) | {
        # Cancelling the item.
        ("PENDING", "CANCELLED"),
        ("IN_PROGRESS", "CANCELLED"),
        # Discarding a ready dish of a paid order (also PENDING and
        # IN_PROGRESS above).
        ("READY", "CANCELLED"),
    },
)

BILL = StateMachine(
    name="bill",
    initial="OPEN",
    transitions=frozenset({
        ("OPEN", "PAID"),
        # Voiding the order.
        ("OPEN", "VOID"),
    }),
)

TABLE = StateMachine(
    name="table",
    initial="AVAILABLE",
    transitions=frozenset({
        ("AVAILABLE", "OCCUPIED"),
        ("OCCUPIED", "AVAILABLE"),
        # Releasing a table that was already marked available.
        ("AVAILABLE", "AVAILABLE"),
    }),
)

# Records written once in their only status.
PAYMENT = StateMachine(name="payment", initial="COMPLETED", transitions=frozenset())
BILL_ADJUSTMENT = StateMachine(name="bill adjustment", initial="APPLIED", transitions=frozenset())

# Table name -> its machine; the CHECK constraint of each is ck_<table>_status.
BY_TABLE: dict[str, StateMachine] = {
    "orders": ORDER,
    "order_items": ORDER_ITEM,
    "preparations": PREPARATION,
    "bills": BILL,
    "tables": TABLE,
    "payments": PAYMENT,
    "bill_adjustments": BILL_ADJUSTMENT,
}


def status_check(machine: StateMachine) -> str:
    """The SQL of the CHECK constraint for `machine`'s vocabulary."""

    values = ", ".join(f"'{status}'" for status in sorted(machine.statuses))
    return f"status IN ({values})"
