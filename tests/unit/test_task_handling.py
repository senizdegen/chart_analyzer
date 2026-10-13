"""Retry policy of the worker and the outbox relay."""

from datetime import timedelta
from uuid import uuid4

import pytest

from chart_analyzer.application.handle_task import Action, RetryPolicy, TaskHandler
from chart_analyzer.application.process_analysis import ProcessAnalysis
from chart_analyzer.application.relay_outbox import RelayOutbox
from chart_analyzer.domain.enums import AnalysisStatus, ErrorCode
from chart_analyzer.domain.errors import TransientFailure
from tests.fakes import (
    NOW,
    FakeChartAI,
    FakeMarketData,
    FixedClock,
    InMemoryDatabase,
    InMemoryImageStorage,
    InMemoryUnitOfWork,
    RecordingProgress,
    RecordingTaskPublisher,
    make_analysis,
)

POLICY = RetryPolicy(delays=(5, 30, 120), busy_delay=30)
LEASE = timedelta(seconds=180)


class Harness:
    def __init__(self) -> None:
        self.db = InMemoryDatabase()
        self.ai = FakeChartAI()
        self.market = FakeMarketData()
        self.progress = RecordingProgress()
        process = ProcessAnalysis(
            lambda: InMemoryUnitOfWork(self.db),
            InMemoryImageStorage(),
            self.ai,
            self.market,
            self.progress,
            FixedClock(),
            lease=LEASE,
            max_news=3,
        )
        self.handler = TaskHandler(process, POLICY)
        self.analysis_id = self.db.seed(make_analysis(uuid4())).id


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
