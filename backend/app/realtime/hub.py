"""In-process registry of open station event streams.

One `StationEventHub` lives on the API application (app.state); every
authorized WebSocket connection is one `StationSubscription` for one
(restaurant, station). The hub is in memory and per process: connections
to another API process are not seen here. It does not distribute anything
between processes.

Thread safety: subscriptions are added and removed on the connections'
event loop, but messages are offered from other threads (the outbox
delivery worker), so the registry and each buffer are guarded by locks
and the waiting connection is woken with `call_soon_threadsafe`.

Memory is bounded: at most `max_connections` subscriptions, each with at
most `queue_size` undelivered messages. A subscription whose buffer is
full is marked overflowed instead of growing; its connection is then
closed, and the client reconnects and reloads its queue over HTTP.
"""

import asyncio
import threading
import uuid
from collections import deque
from typing import Any


class StationSubscription:
    """One connection's view of one station; created by the hub."""

    def __init__(
        self,
        *,
        restaurant_id: uuid.UUID,
        station_id: uuid.UUID,
        user_id: uuid.UUID,
        queue_size: int,
        loop: asyncio.AbstractEventLoop,
    ):
        self.restaurant_id = restaurant_id
        self.station_id = station_id
        self.user_id = user_id
        self.queue_size = queue_size

        self._loop = loop
        self._lock = threading.Lock()
        self._buffer: deque[dict[str, Any]] = deque()
        self._wake = asyncio.Event()
        self._overflowed = False
        self._closed = False

    @property
    def overflowed(self) -> bool:
        with self._lock:
            return self._overflowed

    def offer(self, message: dict[str, Any]) -> bool:
        """Buffer `message` for sending; from any thread.

        False if it was not buffered: the subscription is closed, or its
        buffer is full, which marks it overflowed (and drops what it held,
        since the connection is closed without sending it).
        """

        with self._lock:
            if self._closed or self._overflowed:
                return False

            if len(self._buffer) >= self.queue_size:
                self._overflowed = True
                self._buffer.clear()
                accepted = False
            else:
                self._buffer.append(message)
                accepted = True

        self._notify()
        return accepted

    async def wait(self) -> None:
        """Until something was offered (or the subscription overflowed)."""

        await self._wake.wait()

    def drain(self) -> list[dict[str, Any]]:
        """Take every buffered message, oldest first; on the loop only."""

        # Cleared before taking: an offer after this wakes the next wait.
        self._wake.clear()

        with self._lock:
            messages = list(self._buffer)
            self._buffer.clear()

        return messages

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._buffer.clear()

    def _notify(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._wake.set)
        except RuntimeError:
            # The connection's loop has already stopped: nobody waits.
            pass


class StationEventHub:

    def __init__(self, *, max_connections: int, queue_size: int):
        if max_connections < 1:
            raise ValueError("max_connections must be at least 1")

        if queue_size < 1:
            raise ValueError("queue_size must be at least 1")

        self.max_connections = max_connections
        self.queue_size = queue_size

        self._lock = threading.Lock()
        self._stations: dict[
            tuple[uuid.UUID, uuid.UUID], set[StationSubscription]
        ] = {}
        self._count = 0

    @property
    def connection_count(self) -> int:
        with self._lock:
            return self._count

    def subscribe(
        self,
        *,
        restaurant_id: uuid.UUID,
        station_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> StationSubscription | None:
        """Register a connection; on its event loop. None when full."""

        subscription = StationSubscription(
            restaurant_id=restaurant_id,
            station_id=station_id,
            user_id=user_id,
            queue_size=self.queue_size,
            loop=asyncio.get_running_loop(),
        )

        with self._lock:
            if self._count >= self.max_connections:
                return None

            key = (restaurant_id, station_id)
            self._stations.setdefault(key, set()).add(subscription)
            self._count += 1

        return subscription

    def unsubscribe(self, subscription: StationSubscription) -> None:
        """Remove a connection; safe to call more than once."""

        subscription.close()
        key = (subscription.restaurant_id, subscription.station_id)

        with self._lock:
            subscriptions = self._stations.get(key)

            if subscriptions is None or subscription not in subscriptions:
                return

            subscriptions.remove(subscription)
            self._count -= 1

            if not subscriptions:
                del self._stations[key]

    def station_subscribers(
        self,
        restaurant_id: uuid.UUID,
        station_id: uuid.UUID,
    ) -> list[StationSubscription]:
        """The connections of one restaurant's station, right now."""

        with self._lock:
            return list(self._stations.get((restaurant_id, station_id), ()))

    def restaurant_subscribers(
        self,
        restaurant_id: uuid.UUID,
    ) -> list[StationSubscription]:
        """The connections to any of one restaurant's stations."""

        with self._lock:
            return [
                subscription
                for (restaurant, _), subscriptions in self._stations.items()
                if restaurant == restaurant_id
                for subscription in subscriptions
            ]
