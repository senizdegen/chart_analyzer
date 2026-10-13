"""The worker process: consumes analysis tasks and relays the outbox to the queue."""

import asyncio
import contextlib
import signal

import structlog

from chart_analyzer.application.relay_outbox import RelayOutbox
from chart_analyzer.bootstrap import build_worker
from chart_analyzer.config import Settings, get_settings
from chart_analyzer.logs import configure_logging

log = structlog.get_logger(__name__)

RELAY_ERROR_PAUSE_SECONDS = 2.0


async def relay_loop(relay: RelayOutbox, stop: asyncio.Event, *, poll_interval: float) -> None:
    """Relay batches until asked to stop; sleep only when the outbox is empty."""
    while not stop.is_set():
        try:
            relayed = await relay.run_once()
        except Exception:
            # Database or broker hiccup: the rows are still unsent, try again shortly.
            log.exception("outbox.relay_failed")
            relayed, pause = 0, RELAY_ERROR_PAUSE_SECONDS
        else:
            pause = poll_interval
        if relayed == 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=pause)


async def run(settings: Settings | None = None, stop: asyncio.Event | None = None) -> None:
    settings = settings or get_settings()
    stop = stop or asyncio.Event()
    configure_logging(settings.log_level, json=settings.log_json)
    _install_signal_handlers(stop)

    async with build_worker(settings) as worker:
        await worker.consumer.start()
        relay = asyncio.create_task(
            relay_loop(worker.relay, stop, poll_interval=settings.outbox_poll_interval_seconds)
        )
        log.info("worker.started", prefetch=settings.worker_prefetch)

        await stop.wait()

        log.info("worker.stopping")
        await relay
        await worker.consumer.stop()  # waits for analyses that are in flight
    log.info("worker.stopped")


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:  # Windows event loops
            signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop.set))
