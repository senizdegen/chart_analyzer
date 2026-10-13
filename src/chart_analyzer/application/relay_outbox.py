"""Use case: move tasks from the outbox table to the queue (transactional outbox relay)."""

import structlog

from chart_analyzer.application.ports.services import Clock, TaskPublisher
from chart_analyzer.application.ports.unit_of_work import UnitOfWorkFactory

log = structlog.get_logger(__name__)


class RelayOutbox:
    """Publishes unsent outbox rows and marks them sent, in one transaction per batch.

    If the process dies after publishing but before the commit, the rows stay unsent and
    are published again. That makes delivery at-least-once; the worker's ``claim`` turns
    the duplicates into no-ops.
    """

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        publisher: TaskPublisher,
        clock: Clock,
        *,
        batch_size: int,
    ) -> None:
        self._uow_factory = uow_factory
        self._publisher = publisher
        self._clock = clock
        self._batch_size = batch_size

    async def run_once(self) -> int:
        """Relay one batch; returns how many tasks were published."""
        async with self._uow_factory() as uow:
            messages = await uow.outbox.fetch_unsent(self._batch_size)
            if not messages:
                return 0
            for message in messages:
                await self._publisher.publish(message.analysis_id)
            await uow.outbox.mark_sent([message.id for message in messages], now=self._clock.now())
            await uow.commit()
        log.info("outbox.relayed", count=len(messages))
        return len(messages)
