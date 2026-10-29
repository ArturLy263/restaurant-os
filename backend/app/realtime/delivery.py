"""Outbox delivery to station streams, inside the API process.

The WebSocket connections live in the API process, so this is where their
outbox worker runs: an OutboxWorker with a StationEventPublisher, in one
thread, started by the application's lifespan only when
OUTBOX_WEBSOCKET_DELIVERY is true. Importing app.main starts nothing.

Run one such API process. Outbox events are claimed by whichever worker
gets them first; a second API process (or the standalone
`python -m app.workers.outbox_worker run`, which refuses to start while
this is enabled) would take events that its own connections, not this
process's, receive. Sharing connections between processes needs
infrastructure this design deliberately does not have.
"""

import logging
import threading

from app.realtime.hub import StationEventHub
from app.realtime.publisher import StationEventPublisher
from app.workers.outbox_worker import OutboxWorker, create_worker_engine


logger = logging.getLogger("app.realtime")

THREAD_NAME = "outbox-station-delivery"


class StationDeliveryRunner:

    def __init__(self, hub: StationEventHub, config):
        self.engine = create_worker_engine(config.database_url)
        self.worker = OutboxWorker(
            self.engine,
            StationEventPublisher(hub),
            batch_size=config.outbox_worker_batch_size,
            restaurant_id=config.outbox_worker_restaurant_id,
            max_attempts=config.outbox_max_attempts,
            publish_timeout=config.outbox_publish_timeout_seconds,
        )
        self.poll_interval = config.outbox_worker_poll_interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self.worker.run,
            args=(self._stop,),
            kwargs={"poll_interval": self.poll_interval},
            name=THREAD_NAME,
            daemon=True,
        )

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> None:
        self._thread.start()
        logger.info(
            "Station event delivery started (restaurant %s)",
            self.worker.restaurant_id or "all",
        )

    def stop(self, timeout: float | None = None) -> None:
        """Finish the event being delivered, then stop; waits `timeout`
        seconds at most (default: the publish timeout plus a margin)."""

        self._stop.set()

        if timeout is None:
            timeout = self.worker.publish_timeout + self.poll_interval + 5

        self._thread.join(timeout)

        if self._thread.is_alive():
            logger.error("Station event delivery did not stop in %gs", timeout)
        else:
            logger.info("Station event delivery stopped")

        self.engine.dispose()
