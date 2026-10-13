"""The safety net for analyses that stayed active with nobody working on them."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from chart_analyzer.application.localization import message_for
from chart_analyzer.application.ports.services import ProgressEventType
from chart_analyzer.application.reap_stale import ReapStaleAnalyses
from chart_analyzer.domain.analysis import Analysis
from chart_analyzer.domain.enums import AnalysisStatus, ErrorCode, Language, Stage
from chart_analyzer.domain.quota import QuotaUsage, utc_day_start
from tests.fakes import (
    NOW,
    FixedClock,
    InMemoryDatabase,
    InMemoryUnitOfWork,
    RecordingProgress,
    make_analysis,
)

STALE_AFTER = timedelta(minutes=30)
LEASE = timedelta(seconds=180)
LONG_AGO = NOW - STALE_AFTER - timedelta(seconds=1)
RECENTLY = NOW - STALE_AFTER + timedelta(seconds=1)


class Harness:
    def __init__(self, *, progress: RecordingProgress | None = None) -> None:
        self.db = InMemoryDatabase()
        self.progress = progress or RecordingProgress()
        self.clock = FixedClock()
        self.reaper = ReapStaleAnalyses(
            lambda: InMemoryUnitOfWork(self.db),
            self.progress,
            self.clock,
            stale_after=STALE_AFTER,
        )

    def seed(self, **overrides: object) -> Analysis:
        return self.db.seed(make_analysis(uuid4(), **overrides))

    def queued(self, *, touched: object, **overrides: object) -> UUID:
        return self.seed(updated_at=touched, **overrides).id

    def processing(self, *, touched: object, **overrides: object) -> UUID:
        analysis = self.seed(
            status=AnalysisStatus.PROCESSING,
            stage=Stage.ANALYZING,
            attempts=1,
            tokens_used=1_200,
            updated_at=touched,
            **overrides,
        )
        self.db.rows[analysis.id].locked_at = analysis.updated_at
        return analysis.id

    def failed_events(self) -> dict[UUID, dict[str, object]]:
        return {
            analysis_id: event.data
            for analysis_id, event in self.progress.events
            if event.type is ProgressEventType.FAILED
        }


@pytest.fixture
def h() -> Harness:
    return Harness()


async def test_stale_queued_and_processing_analyses_are_failed(h: Harness) -> None:
    queued = h.queued(touched=LONG_AGO)
    processing = h.processing(touched=LONG_AGO)

    await h.reaper.run_once()

    for analysis_id in (queued, processing):
        analysis = h.db.analysis(analysis_id)
        assert analysis.status is AnalysisStatus.FAILED
        assert analysis.stage is Stage.DONE
        assert analysis.error_code is ErrorCode.INTERNAL_ERROR
        assert analysis.completed_at == NOW
        assert analysis.result is None
    assert h.db.analysis(processing).tokens_used == 1_200  # the spend stays visible


async def test_listeners_of_a_reaped_analysis_get_a_final_failed_event(h: Harness) -> None:
    queued = h.queued(touched=LONG_AGO)
    processing = h.processing(touched=LONG_AGO)

    await h.reaper.run_once()

    events = h.failed_events()
    assert set(events) == {queued, processing}
    assert len(h.progress.events) == 2
    for analysis_id, data in events.items():
        assert data["id"] == str(analysis_id)
        assert data["status"] == "failed"
        assert data["result"] is None
        assert data["error"] == {
            "code": "internal_error",
            "message": message_for(ErrorCode.INTERNAL_ERROR, Language.EN),
        }


async def test_returns_how_many_analyses_were_reaped(h: Harness) -> None:
    for _ in range(3):
        h.queued(touched=LONG_AGO)
    h.queued(touched=RECENTLY)

    assert await h.reaper.run_once() == 3
    assert await h.reaper.run_once() == 0  # nothing is reaped, or announced, twice
    assert len(h.progress.events) == 3


async def test_nothing_to_reap_is_zero_and_silent(h: Harness) -> None:
    assert await h.reaper.run_once() == 0
    assert h.progress.events == []


async def test_fresh_active_analyses_are_left_alone(h: Harness) -> None:
    before = [
        h.db.analysis(h.queued(touched=RECENTLY)),
        h.db.analysis(h.processing(touched=RECENTLY)),
        h.db.analysis(h.queued(touched=NOW - STALE_AFTER)),  # exactly at the cutoff: not older
    ]

    assert await h.reaper.run_once() == 0

    assert [h.db.analysis(analysis.id) for analysis in before] == before
    assert h.progress.events == []


async def test_an_old_submission_that_is_still_being_worked_on_is_left_alone(h: Harness) -> None:
    # what counts is the last write, not the age of the analysis
    analysis_id = h.processing(touched=RECENTLY, created_at=NOW - timedelta(days=2))

    assert await h.reaper.run_once() == 0
    assert h.db.analysis(analysis_id).status is AnalysisStatus.PROCESSING


async def test_finished_analyses_are_left_alone_however_old(h: Harness) -> None:
    completed = h.seed(
        status=AnalysisStatus.COMPLETED,
        stage=Stage.DONE,
        tokens_used=8_000,
        updated_at=LONG_AGO,
        completed_at=LONG_AGO,
    )
    failed = h.seed(
        status=AnalysisStatus.FAILED,
        stage=Stage.DONE,
        error_code=ErrorCode.UNCLEAR_CHART,
        updated_at=LONG_AGO,
        completed_at=LONG_AGO,
    )

    assert await h.reaper.run_once() == 0

    assert h.db.analysis(completed.id) == completed
    assert h.db.analysis(failed.id) == failed
    assert h.progress.events == []


async def test_an_analysis_becomes_stale_as_time_passes(h: Harness) -> None:
    analysis_id = h.queued(touched=NOW)
    assert await h.reaper.run_once() == 0

    h.clock.current = NOW + STALE_AFTER + timedelta(seconds=1)

    assert await h.reaper.run_once() == 1
    analysis = h.db.analysis(analysis_id)
    assert analysis.status is AnalysisStatus.FAILED
    assert analysis.completed_at == h.clock.current


async def test_reaping_frees_the_users_quota_reservations(h: Harness) -> None:
    h.queued(touched=LONG_AGO)
    h.processing(touched=LONG_AGO)
    h.queued(touched=LONG_AGO, user_id="user-2")
    repository = InMemoryUnitOfWork(h.db).analyses
    day_start = utc_day_start(NOW)
    assert await repository.get_quota_usage("user-1", day_start) == QuotaUsage(0, 2)

    await h.reaper.run_once()

    # no active analyses, and what the dead ones spent is not charged either
    assert await repository.get_quota_usage("user-1", day_start) == QuotaUsage(0, 0)
    assert await repository.get_quota_usage("user-2", day_start) == QuotaUsage(0, 0)


async def test_a_reaped_analysis_is_never_processed_again(h: Harness) -> None:
    analysis_id = h.processing(touched=LONG_AGO)

    await h.reaper.run_once()

    # even with its lease long expired, a late task finds it finished
    claim = await InMemoryUnitOfWork(h.db).analyses.claim(
        analysis_id, now=NOW + 10 * LEASE, lease=LEASE
    )
    assert claim.analysis is None
    assert h.db.analysis(analysis_id).status is AnalysisStatus.FAILED


async def test_progress_outage_does_not_stop_the_reaping() -> None:
    h = Harness(progress=RecordingProgress(fail=True))
    stale = [h.queued(touched=LONG_AGO), h.processing(touched=LONG_AGO)]

    reaped = await h.reaper.run_once()

    assert reaped == 2
    assert all(h.db.analysis(analysis_id).status is AnalysisStatus.FAILED for analysis_id in stale)
