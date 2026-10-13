"""Use case: fail analyses that stayed active with nobody working on them.

Normally every analysis ends through the worker. This sweep is the safety net for the
rest: a task that was dead-lettered while the database was down, or lost with a broker.
Without it such an analysis would hold a quota reservation and a concurrency slot of its
user forever, and its event stream would never end.
"""

from datetime import timedelta

import structlog

from chart_analyzer.application.ports.services import Clock, ProgressPublisher
from chart_analyzer.application.ports.unit_of_work import UnitOfWorkFactory
from chart_analyzer.application.progress_events import final_event
from chart_analyzer.domain.enums import ErrorCode

log = structlog.get_logger(__name__)


class ReapStaleAnalyses:
    """``stale_after`` must exceed the longest legitimate gap between two writes of a
    live analysis: the whole retry schedule plus a lease takeover."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        progress: ProgressPublisher,
        clock: Clock,
        *,
        stale_after: timedelta,
    ) -> None:
        self._uow_factory = uow_factory
        self._progress = progress
        self._clock = clock
        self._stale_after = stale_after

    async def run_once(self) -> int:
        """Fail stale analyses and tell their listeners; returns how many there were."""
        now = self._clock.now()
        async with self._uow_factory() as uow:
            stale = await uow.analyses.fail_stale(
                inactive_since=now - self._stale_after, code=ErrorCode.INTERNAL_ERROR, now=now
            )
            failed = [await uow.analyses.get(analysis_id) for analysis_id in stale]
            await uow.commit()

        for analysis in failed:
            if analysis is None:
                continue
            log.error("analysis.reaped", analysis_id=str(analysis.id), user_id=analysis.user_id)
            try:
                await self._progress.publish(analysis.id, final_event(analysis))
            except Exception:
                log.warning("progress.publish_failed", analysis_id=str(analysis.id), exc_info=True)
        return len(stale)
