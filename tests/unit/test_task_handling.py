"""Retry policy of the worker and the outbox relay."""

from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest

from chart_analyzer.application.handle_task import Action, RetryPolicy, TaskHandler
from chart_analyzer.application.ports.repositories import ClaimResult
from chart_analyzer.application.process_analysis import ProcessAnalysis
from chart_analyzer.application.relay_outbox import RelayOutbox
from chart_analyzer.domain.analysis import Analysis
from chart_analyzer.domain.enums import AnalysisStatus, ErrorCode
from chart_analyzer.domain.errors import TransientFailure
from tests.fakes import (
    NOW,
    FakeChartAI,
    FakeMarketData,
    FixedClock,
    InMemoryAnalysisRepository,
    InMemoryDatabase,
    InMemoryImageStorage,
    InMemoryUnitOfWork,
    RecordingProgress,
    RecordingTaskPublisher,
    make_analysis,
)

POLICY = RetryPolicy(delays=(5, 30, 120), busy_delay=30)
LEASE = timedelta(seconds=180)
DEADLINE = timedelta(seconds=150)


class UnreliableAnalysisRepository(InMemoryAnalysisRepository):
    """Raises a connection error from the operations named in ``broken``."""

    def __init__(self, db: InMemoryDatabase, broken: set[str]) -> None:
        super().__init__(db)
        self._broken = broken

    def _check(self, operation: str) -> None:
        if operation in self._broken:
            raise ConnectionError(f"database is down ({operation})")

    async def claim(self, analysis_id: UUID, *, now: datetime, lease: timedelta) -> ClaimResult:
        self._check("claim")
        return await super().claim(analysis_id, now=now, lease=lease)

    async def get(self, analysis_id: UUID) -> Analysis | None:
        self._check("get")
        return await super().get(analysis_id)

    async def fail(
        self, analysis_id: UUID, code: ErrorCode, *, now: datetime, attempt: int | None = None
    ) -> bool:
        self._check("fail")
        return await super().fail(analysis_id, code, now=now, attempt=attempt)


class UnreliableUnitOfWork(InMemoryUnitOfWork):
    def __init__(self, db: InMemoryDatabase, broken: set[str]) -> None:
        super().__init__(db)
        self.analyses = UnreliableAnalysisRepository(db, broken)


class Harness:
    def __init__(self) -> None:
        self.db = InMemoryDatabase()
        self.ai = FakeChartAI()
        self.market = FakeMarketData()
        self.progress = RecordingProgress()
        self.broken: set[str] = set()  # repository operations that currently fail
        storage = InMemoryImageStorage()
        process = ProcessAnalysis(
            lambda: UnreliableUnitOfWork(self.db, self.broken),
            storage,
            self.ai,
            self.market,
            self.progress,
            FixedClock(),
            lease=LEASE,
            deadline=DEADLINE,
            max_news=3,
        )
        self.handler = TaskHandler(process, POLICY)
        analysis = self.db.seed(make_analysis(uuid4()))
        storage.put(analysis.image)
        self.analysis_id = analysis.id


@pytest.fixture
def h() -> Harness:
    return Harness()


class TestTaskHandler:
    def test_attempts_are_one_more_than_the_delays(self) -> None:
        assert POLICY.max_attempts == 4

    async def test_success_is_acknowledged(self, h: Harness) -> None:
        disposition = await h.handler.handle(h.analysis_id, attempt=1)

        assert disposition.action is Action.ACK

    async def test_permanent_failure_is_acknowledged_not_retried(self, h: Harness) -> None:
        h.ai.recognition = h.ai.recognition.model_copy(update={"is_chart": False})

        disposition = await h.handler.handle(h.analysis_id, attempt=1)

        assert disposition.action is Action.ACK
        assert h.db.analysis(h.analysis_id).status is AnalysisStatus.FAILED

    @pytest.mark.parametrize(("attempt", "delay"), [(1, 5), (2, 30), (3, 120)])
    async def test_transient_failure_backs_off_with_growing_delays(
        self, h: Harness, attempt: int, delay: int
    ) -> None:
        h.market.error = TransientFailure(ErrorCode.MARKET_DATA_UNAVAILABLE)

        disposition = await h.handler.handle(h.analysis_id, attempt=attempt)

        assert disposition.action is Action.RETRY
        assert disposition.delay_seconds == delay
        assert disposition.next_attempt == attempt + 1
        assert h.db.analysis(h.analysis_id).status is AnalysisStatus.QUEUED

    async def test_last_attempt_dead_letters_and_fails_the_analysis(self, h: Harness) -> None:
        h.market.error = TransientFailure(ErrorCode.MARKET_DATA_UNAVAILABLE)

        disposition = await h.handler.handle(h.analysis_id, attempt=4)

        analysis = h.db.analysis(h.analysis_id)
        assert disposition.action is Action.DEAD_LETTER
        assert analysis.status is AnalysisStatus.FAILED
        assert analysis.error_code is ErrorCode.MARKET_DATA_UNAVAILABLE
        assert h.progress.types()[-1] == "failed"

    async def test_last_attempt_dead_letters_even_if_the_failure_cannot_be_recorded(
        self, h: Harness
    ) -> None:
        h.market.error = TransientFailure(ErrorCode.MARKET_DATA_UNAVAILABLE)
        h.broken.add("fail")

        disposition = await h.handler.handle(h.analysis_id, attempt=4)

        assert disposition.action is Action.DEAD_LETTER
        # left active for the stale-analysis sweep
        assert h.db.analysis(h.analysis_id).status is AnalysisStatus.QUEUED

    @pytest.mark.parametrize(("attempt", "delay"), [(1, 5), (2, 30), (3, 120)])
    async def test_infrastructure_error_is_retried_like_a_transient_failure(
        self, h: Harness, attempt: int, delay: int
    ) -> None:
        h.broken.add("claim")  # not a domain error: the database broke around the pipeline

        disposition = await h.handler.handle(h.analysis_id, attempt=attempt)

        assert disposition.action is Action.RETRY
        assert disposition.delay_seconds == delay
        assert disposition.next_attempt == attempt + 1
        assert h.db.analysis(h.analysis_id).status is AnalysisStatus.QUEUED
        assert h.ai.recognize_calls == 0

    async def test_retry_after_an_infrastructure_error_completes_the_analysis(
        self, h: Harness
    ) -> None:
        h.broken.add("claim")
        first = await h.handler.handle(h.analysis_id, attempt=1)
        h.broken.clear()

        second = await h.handler.handle(h.analysis_id, attempt=first.next_attempt)

        assert second.action is Action.ACK
        assert h.db.analysis(h.analysis_id).status is AnalysisStatus.COMPLETED

    async def test_infrastructure_error_on_the_last_attempt_fails_the_analysis(
        self, h: Harness
    ) -> None:
        h.broken.add("claim")

        disposition = await h.handler.handle(h.analysis_id, attempt=4)

        analysis = h.db.analysis(h.analysis_id)
        assert disposition.action is Action.DEAD_LETTER
        assert analysis.status is AnalysisStatus.FAILED
        assert analysis.error_code is ErrorCode.INTERNAL_ERROR
        assert h.progress.types() == ["failed"]

    async def test_infrastructure_error_while_giving_up_still_dead_letters(
        self, h: Harness
    ) -> None:
        h.broken.update({"claim", "get"})  # the database is gone for good

        disposition = await h.handler.handle(h.analysis_id, attempt=4)

        assert disposition.action is Action.DEAD_LETTER
        assert h.db.analysis(h.analysis_id).status is AnalysisStatus.QUEUED
        assert h.progress.events == []

    async def test_busy_analysis_is_looked_at_later_without_using_an_attempt(
        self, h: Harness
    ) -> None:
        await InMemoryUnitOfWork(h.db).analyses.claim(h.analysis_id, now=NOW, lease=LEASE)

        disposition = await h.handler.handle(h.analysis_id, attempt=2)

        assert disposition.action is Action.RETRY
        assert disposition.delay_seconds == 30
        assert disposition.next_attempt == 2

    async def test_duplicate_of_a_finished_analysis_is_acknowledged(self, h: Harness) -> None:
        await h.handler.handle(h.analysis_id, attempt=1)

        disposition = await h.handler.handle(h.analysis_id, attempt=1)

        assert disposition.action is Action.ACK
        assert h.ai.analyze_calls == 1


class TestRelayOutbox:
    def _relay(self, db: InMemoryDatabase, publisher: RecordingTaskPublisher) -> RelayOutbox:
        return RelayOutbox(lambda: InMemoryUnitOfWork(db), publisher, FixedClock(), batch_size=2)

    async def _enqueue(self, db: InMemoryDatabase, count: int) -> list[object]:
        ids = [uuid4() for _ in range(count)]
        for analysis_id in ids:
            await InMemoryUnitOfWork(db).outbox.add(analysis_id, now=NOW)
        return list(ids)

    async def test_publishes_unsent_tasks_and_marks_them_sent(self) -> None:
        db, publisher = InMemoryDatabase(), RecordingTaskPublisher()
        ids = await self._enqueue(db, 2)

        relayed = await self._relay(db, publisher).run_once()

        assert relayed == 2
        assert publisher.published == ids
        assert all(sent_at == NOW for _, _, sent_at in db.outbox)

    async def test_works_in_batches(self) -> None:
        db, publisher = InMemoryDatabase(), RecordingTaskPublisher()
        await self._enqueue(db, 3)
        relay = self._relay(db, publisher)

        assert [await relay.run_once() for _ in range(3)] == [2, 1, 0]
        assert len(publisher.published) == 3

    async def test_nothing_is_marked_sent_when_the_broker_fails(self) -> None:
        db = InMemoryDatabase()
        await self._enqueue(db, 2)

        with pytest.raises(ConnectionError):
            await self._relay(db, RecordingTaskPublisher(fail_after=1)).run_once()

        assert all(sent_at is None for _, _, sent_at in db.outbox)

    async def test_tasks_are_published_again_after_a_failed_batch(self) -> None:
        db = InMemoryDatabase()
        ids = await self._enqueue(db, 2)
        with pytest.raises(ConnectionError):
            await self._relay(db, RecordingTaskPublisher(fail_after=1)).run_once()

        publisher = RecordingTaskPublisher()
        await self._relay(db, publisher).run_once()

        assert publisher.published == ids
