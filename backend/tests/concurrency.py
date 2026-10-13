"""Deterministic concurrent API requests against real PostgreSQL.

Requests run in threads through the real application (its own sessions and
commits, no dependency overrides), so they are separate transactions on
separate connections. Ordering is controlled with row locks, not sleeps:

1. A "gate" connection holds a lock on a row every request needs.
2. Requests are started one by one; after each, we wait until PostgreSQL
   reports that request's backend as blocked (pg_blocking_pids) on the
   expected backend.
3. Releasing the gate lets them run; the results are compared.

The waits poll PostgreSQL's lock graph and fail after a generous timeout,
so they cannot pass by chance; they only decide when to continue.
"""

import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import app


WAIT_TIMEOUT_SECONDS = 15

_BLOCKED_BACKENDS = text(
    "SELECT pid, pg_blocking_pids(pid) AS blockers "
    "FROM pg_stat_activity "
    "WHERE datname = current_database() "
    "AND cardinality(pg_blocking_pids(pid)) > 0"
)


class Gate:
    """A connection holding a row lock until `release()`."""

    def __init__(self, engine, table: str, row_id):
        self.connection = engine.connect()
        self.pid = self.connection.scalar(text("SELECT pg_backend_pid()"))
        # FOR KEY SHARE conflicts with the services' FOR UPDATE, not with
        # foreign-key checks, so only the explicit row lock waits on it.
        self.connection.execute(
            text(f"SELECT id FROM {table} WHERE id = :id FOR KEY SHARE"),
            {"id": row_id},
        )

    def release(self):
        if self.connection is not None:
            self.connection.rollback()
            self.connection.close()
            self.connection = None


class Concurrency:
    def __init__(self, engine):
        self.engine = engine
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.gates: list[Gate] = []

    def gate(self, table: str, row_id) -> Gate:
        gate = Gate(self.engine, table, row_id)
        self.gates.append(gate)
        return gate

    def blocked(self) -> dict[int, set[int]]:
        with self.engine.connect() as connection:
            return {
                row.pid: set(row.blockers)
                for row in connection.execute(_BLOCKED_BACKENDS)
            }

    def wait_for_new_backend_blocked_by(
        self,
        blocker_pids: int | set[int],
        known: set[int],
    ) -> int:
        """Wait until a backend not in `known` is blocked by a blocker.

        A request queued behind another waiter for the same row lock is
        reported as blocked by that waiter, so callers pass the whole chain.
        """

        if isinstance(blocker_pids, int):
            blocker_pids = {blocker_pids}

        deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS

        while time.monotonic() < deadline:
            for pid, blockers in self.blocked().items():
                if pid not in known and blocker_pids & blockers:
                    return pid

            time.sleep(0.01)

        raise AssertionError(
            f"No new backend became blocked by {blocker_pids} within "
            f"{WAIT_TIMEOUT_SECONDS}s; blocked now: {self.blocked()}"
        )

    def start(self, method: str, path: str, **kwargs) -> Future:
        def call():
            client = TestClient(app, raise_server_exceptions=False)
            return client.request(method, path, **kwargs)

        return self.executor.submit(call)

    def close(self):
        for gate in self.gates:
            gate.release()

        self.executor.shutdown(wait=True, cancel_futures=True)


@contextmanager
def concurrency(engine):
    helper = Concurrency(engine)

    try:
        yield helper
    finally:
        helper.close()


def results(*futures: Future):
    return [future.result(timeout=WAIT_TIMEOUT_SECONDS) for future in futures]
