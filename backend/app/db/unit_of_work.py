"""Who commits a service's changes.

Services end their unit of work with `commit(db)`. Normally that commits.
Inside `owned_transaction(db)` the caller owns the transaction instead:
`commit(db)` only flushes, and the caller commits through the handle it
was given, or rolls back, once it has finished its own writes. Idempotent
requests use this to commit the business change together with the stored
response (app.api.idempotency).

While a transaction is owned, any other `db.commit()` (e.g. a service that
bypasses `commit(db)`) is refused with OwnedTransactionCommitError instead
of committing half of the owner's work. Rollbacks stay allowed: they discard
everything, which is always safe.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event
from sqlalchemy.orm import Session


_OWNED = "restaurantos.transaction_owned_by_caller"
_OWNER_COMMITTING = "restaurantos.transaction_owner_committing"


class OwnedTransactionCommitError(RuntimeError):
    """A commit was attempted on a transaction owned by someone else."""


def commit(db: Session) -> None:
    if db.info.get(_OWNED):
        db.flush()
    else:
        db.commit()


class OwnedTransaction:
    """The owner's handle: the only way to commit an owned transaction."""

    def __init__(self, db: Session):
        self._db = db

    def commit(self) -> None:
        self._db.info[_OWNER_COMMITTING] = True

        try:
            self._db.commit()
        finally:
            self._db.info.pop(_OWNER_COMMITTING, None)


@contextmanager
def owned_transaction(db: Session) -> Iterator[OwnedTransaction]:
    """Make `commit(db)` flush only; the caller must commit or roll back."""

    if db.info.get(_OWNED):
        raise RuntimeError("The session's transaction is already owned.")

    db.info[_OWNED] = True

    try:
        yield OwnedTransaction(db)
    finally:
        db.info.pop(_OWNED, None)


@event.listens_for(Session, "before_commit")
def _refuse_commits_not_made_by_the_owner(session: Session) -> None:
    if session.info.get(_OWNED) and not session.info.get(_OWNER_COMMITTING):
        raise OwnedTransactionCommitError(
            "This transaction is owned by an enclosing operation; end the "
            "unit of work with app.db.unit_of_work.commit(db) instead of "
            "db.commit()."
        )
