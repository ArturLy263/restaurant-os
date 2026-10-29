"""The API lifespan starts and stops station delivery (Epic D4); no database.

The real StationDeliveryRunner is replaced by a stand-in, so nothing here
connects anywhere or starts a worker thread.
"""

import asyncio
import threading

import app.main as main


# How long the stand-in's stop() waits to be released by the event loop.
# Only reached when the test fails (stop() blocked the loop).
RELEASE_TIMEOUT = 5


class BlockingRunner:
    """stop() returns only once the event loop has released it."""

    instances: list["BlockingRunner"] = []

    def __init__(self, hub, config):
        self.started = 0
        self.stop_threads: list[threading.Thread] = []
        self.stop_entered = threading.Event()
        self.release = threading.Event()
        self.released: bool | None = None
        BlockingRunner.instances.append(self)

    def start(self) -> None:
        self.started += 1

    def stop(self, timeout: float | None = None) -> None:
        self.stop_threads.append(threading.current_thread())
        self.stop_entered.set()
        # On the loop's thread this blocks the loop, which then cannot
        # release it: the wait times out and `released` is False.
        self.released = self.release.wait(RELEASE_TIMEOUT)


def test_lifespan_stops_delivery_off_the_event_loop(monkeypatch):
    BlockingRunner.instances.clear()
    monkeypatch.setattr(main, "StationDeliveryRunner", BlockingRunner)
    monkeypatch.setattr(main.settings, "outbox_websocket_delivery", True)

    async def scenario():
        loop_thread = threading.current_thread()
        lifespan = main.lifespan(main.app)
        await lifespan.__aenter__()
        [runner] = BlockingRunner.instances
        assert runner.started == 1

        shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))

        # Runs only while the loop is free, i.e. while stop() waits
        # elsewhere; then lets stop() return.
        entered = await asyncio.to_thread(runner.stop_entered.wait, RELEASE_TIMEOUT)
        runner.release.set()
        await shutdown

        return loop_thread, runner, entered

    loop_thread, runner, entered = asyncio.run(scenario())

    assert entered
    assert runner.released is True, "stop() blocked the event loop"
    assert len(runner.stop_threads) == 1
    assert runner.stop_threads[0] is not loop_thread


def test_lifespan_starts_no_delivery_when_disabled(monkeypatch):
    BlockingRunner.instances.clear()
    monkeypatch.setattr(main, "StationDeliveryRunner", BlockingRunner)
    monkeypatch.setattr(main.settings, "outbox_websocket_delivery", False)

    async def scenario():
        async with main.lifespan(main.app):
            pass

    asyncio.run(scenario())

    assert BlockingRunner.instances == []
