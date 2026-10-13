"""The background loops of the worker process."""

import asyncio
from datetime import timedelta
from uuid import uuid4

from chart_analyzer.application.reap_stale import ReapStaleAnalyses
from chart_analyzer.domain.enums import AnalysisStatus
from chart_analyzer.worker.runner import sweep_loop
from tests.fakes import (
    NOW,
    FixedClock,
    InMemoryDatabase,
    InMemoryUnitOfWork,
    RecordingProgress,
    make_analysis,
)

STALE_AFTER = timedelta(minutes=30)


class Harness:
    """A real reaper whose database can be made to fail for the first sweeps."""

    def __init__(self, *, failing_sweeps: int = 0) -> None:
        self.db = InMemoryDatabase()
        self.sweeps = 0
        self.swept = asyncio.Event()
        self._failing_sweeps = failing_sweeps
        self.reaper = ReapStaleAnalyses(
            self._uow, RecordingProgress(), FixedClock(), stale_after=STALE_AFTER
        )

    def _uow(self) -> InMemoryUnitOfWork:
        self.sweeps += 1
        self.swept.set()
        if self.sweeps <= self._failing_sweeps:
            raise ConnectionError("database is down")
        return InMemoryUnitOfWork(self.db)


async def test_sweep_loop_reaps_right_away_and_stops_promptly_when_asked() -> None:
    h = Harness()
    stale = h.db.seed(make_analysis(uuid4(), updated_at=NOW - STALE_AFTER - timedelta(seconds=1)))
    stop = asyncio.Event()
    loop = asyncio.create_task(sweep_loop(h.reaper, stop, interval=3600))
    await asyncio.wait_for(h.swept.wait(), timeout=5)

    stop.set()
    await asyncio.wait_for(loop, timeout=5)  # far sooner than the hour-long interval

    assert h.sweeps == 1
    assert h.db.analysis(stale.id).status is AnalysisStatus.FAILED


async def test_sweep_loop_does_nothing_when_already_stopped() -> None:
    h = Harness()
    stop = asyncio.Event()
    stop.set()

    await asyncio.wait_for(sweep_loop(h.reaper, stop, interval=3600), timeout=5)

    assert h.sweeps == 0


async def test_sweep_loop_survives_a_failing_sweep_and_tries_again() -> None:
    h = Harness(failing_sweeps=2)
    stale = h.db.seed(make_analysis(uuid4(), updated_at=NOW - STALE_AFTER - timedelta(seconds=1)))
    stop = asyncio.Event()
    loop = asyncio.create_task(sweep_loop(h.reaper, stop, interval=0.01))

    async with asyncio.timeout(5):
        while h.db.analysis(stale.id).status is not AnalysisStatus.FAILED:
            await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(loop, timeout=5)

    assert h.sweeps >= 3
