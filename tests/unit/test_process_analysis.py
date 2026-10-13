"""The worker pipeline: steps, checkpoints, failures, idempotency."""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal
from uuid import UUID, uuid4

import pytest

from chart_analyzer.application.ports.services import AIAnswer, ChartImage, ProgressEventType
from chart_analyzer.application.process_analysis import ProcessAnalysis, ProcessOutcome
from chart_analyzer.domain.enums import AnalysisStatus, AssetType, ErrorCode, Language, Stage
from chart_analyzer.domain.errors import AnalysisFailure, TransientFailure
from chart_analyzer.domain.models import ChartInsights, ChartRecognition, MarketSnapshot
from tests.fakes import (
    NOW,
    FakeChartAI,
    FakeMarketData,
    FixedClock,
    InMemoryDatabase,
    InMemoryImageStorage,
    InMemoryUnitOfWork,
    RecordingProgress,
    make_analysis,
    make_insights,
    make_recognition,
    make_signals,
    make_snapshot,
)

LEASE = timedelta(seconds=180)
DEADLINE = timedelta(seconds=150)


class Harness:
    """One worker. ``second_worker`` gives another one on the same database and clock."""

    def __init__(
        self,
        *,
        progress: RecordingProgress | None = None,
        ai: FakeChartAI | None = None,
        deadline: timedelta = DEADLINE,
        shared_with: "Harness | None" = None,
    ) -> None:
        self.db = shared_with.db if shared_with else InMemoryDatabase()
        self.storage = shared_with.storage if shared_with else InMemoryImageStorage()
        self.clock = shared_with.clock if shared_with else FixedClock()
        self.ai = ai or FakeChartAI()
        self.market = FakeMarketData()
        self.progress = progress or RecordingProgress()
        self.process = ProcessAnalysis(
            lambda: InMemoryUnitOfWork(self.db),
            self.storage,
            self.ai,
            self.market,
            self.progress,
            self.clock,
            lease=LEASE,
            deadline=deadline,
            max_news=3,
        )

    def queued(self, **overrides: object) -> UUID:
        analysis = self.db.seed(make_analysis(uuid4(), **overrides))
        self.storage.put(analysis.image)
        return analysis.id

    def second_worker(self, ai: FakeChartAI | None = None) -> "Harness":
        return Harness(ai=ai, shared_with=self)


@dataclass
class GatedChartAI(FakeChartAI):
    """Stops inside one call until the test opens the gate: a worker caught mid-pipeline."""

    hold: Literal["recognize", "analyze"] = "analyze"
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    gate: asyncio.Event = field(default_factory=asyncio.Event)

    async def recognize(self, image: ChartImage) -> AIAnswer[ChartRecognition]:
        if self.hold == "recognize":
            self.entered.set()
            await self.gate.wait()
        return await super().recognize(image)

    async def analyze(
        self, image: ChartImage, language: Language, snapshot: MarketSnapshot
    ) -> AIAnswer[ChartInsights]:
        if self.hold == "analyze":
            self.entered.set()
            await self.gate.wait()
        return await super().analyze(image, language, snapshot)


@dataclass
class SlowChartAI(FakeChartAI):
    """An analysis call that hangs far longer than any deadline used in these tests."""

    analyze_seconds: float = 30.0

    async def analyze(
        self, image: ChartImage, language: Language, snapshot: MarketSnapshot
    ) -> AIAnswer[ChartInsights]:
        await asyncio.sleep(self.analyze_seconds)
        return await super().analyze(image, language, snapshot)


@pytest.fixture
def h() -> Harness:
    return Harness()


class TestHappyPath:
    async def test_completes_and_stores_the_result(self, h: Harness) -> None:
        analysis_id = h.queued(language=Language.DE)

        outcome = await h.process.run(analysis_id)

        analysis = h.db.analysis(analysis_id)
        assert outcome is ProcessOutcome.COMPLETED
        assert analysis.status is AnalysisStatus.COMPLETED
        assert analysis.stage is Stage.DONE
        assert analysis.completed_at == NOW
        assert analysis.result is not None
        assert analysis.result.asset.symbol == "AAPL"
        assert analysis.result.current_price.price == 340.97
        assert analysis.result.language is Language.DE

    async def test_counts_tokens_of_both_ai_calls(self, h: Harness) -> None:
        analysis_id = h.queued()

        await h.process.run(analysis_id)

        assert h.db.analysis(analysis_id).tokens_used == 1_200 + 6_800

    async def test_asks_the_ai_to_write_in_the_users_language(self, h: Harness) -> None:
        await h.process.run(h.queued(language=Language.TR))

        assert h.ai.analyzed_languages == [Language.TR]

    async def test_reports_real_stages_then_the_result(self, h: Harness) -> None:
        analysis_id = h.queued()

        await h.process.run(analysis_id)

        assert h.progress.types() == ["recognizing", "market_data", "analyzing", "completed"]
        final = h.progress.events[-1][1]
        assert final.type is ProgressEventType.COMPLETED
        assert final.data["id"] == str(analysis_id)
        assert final.data["result"]["asset"]["symbol"] == "AAPL"

    async def test_looks_up_the_normalized_ticker(self, h: Harness) -> None:
        h.ai.recognition = make_recognition(
            symbol="BINANCE:BTCUSDT", asset_name="Bitcoin", asset_type=AssetType.CRYPTO
        )

        await h.process.run(h.queued())

        ref = h.market.requested[0]
        assert (ref.symbol, ref.type, ref.name_hint) == ("BTC", AssetType.CRYPTO, "Bitcoin")

    async def test_progress_outage_does_not_fail_the_analysis(self) -> None:
        h = Harness(progress=RecordingProgress(fail=True))
        analysis_id = h.queued()

        outcome = await h.process.run(analysis_id)

        assert outcome is ProcessOutcome.COMPLETED
        assert h.db.analysis(analysis_id).status is AnalysisStatus.COMPLETED


class TestPermanentFailures:
    async def test_image_that_is_not_a_chart(self, h: Harness) -> None:
        h.ai.recognition = make_recognition(is_chart=False, symbol=None, asset_type=None)
        analysis_id = h.queued(language=Language.ES)

        outcome = await h.process.run(analysis_id)

        analysis = h.db.analysis(analysis_id)
        assert outcome is ProcessOutcome.FAILED
        assert analysis.status is AnalysisStatus.FAILED
        assert analysis.error_code is ErrorCode.UNCLEAR_CHART
        assert h.ai.analyze_calls == 0 and h.market.calls == 0
        final = h.progress.events[-1][1]
        assert final.type is ProgressEventType.FAILED
        assert final.data["id"] == str(analysis_id)
        assert final.data["status"] == "failed"
        assert final.data["result"] is None
        assert final.data["error"] == {
            "code": "unclear_chart",
            "message": "Sube un gráfico de trading claro.",
        }

    async def test_chart_without_a_visible_ticker(self, h: Harness) -> None:
        h.ai.recognition = make_recognition(symbol=None)

        await h.process.run(analysis_id := h.queued())

        assert h.db.analysis(analysis_id).error_code is ErrorCode.UNCLEAR_CHART

    async def test_unknown_asset_stops_before_the_expensive_call(self, h: Harness) -> None:
        h.market.error = AnalysisFailure(ErrorCode.ASSET_NOT_FOUND)

        outcome = await h.process.run(analysis_id := h.queued())

        assert outcome is ProcessOutcome.FAILED
        assert h.db.analysis(analysis_id).error_code is ErrorCode.ASSET_NOT_FOUND
        assert h.ai.analyze_calls == 0

    async def test_failed_analysis_keeps_its_spend_visible(self, h: Harness) -> None:
        h.ai.recognition = make_recognition(is_chart=False, symbol=None, asset_type=None)

        await h.process.run(analysis_id := h.queued())

        assert h.db.analysis(analysis_id).tokens_used == 1_200


class TestTransientFailures:
    async def test_market_outage_is_retryable_and_releases_the_analysis(self, h: Harness) -> None:
        h.market.error = TransientFailure(ErrorCode.MARKET_DATA_UNAVAILABLE)
        analysis_id = h.queued()

        with pytest.raises(TransientFailure) as error:
            await h.process.run(analysis_id)

        assert error.value.code is ErrorCode.MARKET_DATA_UNAVAILABLE
        assert h.db.analysis(analysis_id).status is AnalysisStatus.QUEUED
        assert h.ai.analyze_calls == 0

    async def test_ai_outage_is_retryable(self, h: Harness) -> None:
        h.ai.analyze_error = TransientFailure(ErrorCode.AI_UNAVAILABLE)

        with pytest.raises(TransientFailure):
            await h.process.run(analysis_id := h.queued())

        assert h.db.analysis(analysis_id).status is AnalysisStatus.QUEUED

    async def test_inconsistent_ai_answer_is_retryable_and_its_tokens_are_not_charged(
        self, h: Harness
    ) -> None:
        h.ai.insights = make_insights(trading_signals=make_signals(safety_stop=999.0))
        analysis_id = h.queued()

        with pytest.raises(TransientFailure) as error:
            await h.process.run(analysis_id)

        analysis = h.db.analysis(analysis_id)
        assert error.value.code is ErrorCode.INVALID_AI_RESPONSE
        assert analysis.status is AnalysisStatus.QUEUED
        assert analysis.result is None
        assert analysis.tokens_used == 1_200  # recognition only: the rejected answer is free

    async def test_only_the_accepted_answer_is_charged_after_a_rejected_one(
        self, h: Harness
    ) -> None:
        h.ai.insights = make_insights(trading_signals=make_signals(safety_stop=999.0))
        analysis_id = h.queued()
        with pytest.raises(TransientFailure):
            await h.process.run(analysis_id)

        h.ai.insights = make_insights()
        outcome = await h.process.run(analysis_id)

        assert outcome is ProcessOutcome.COMPLETED
        assert h.ai.analyze_calls == 2
        assert h.db.analysis(analysis_id).tokens_used == 1_200 + 6_800

    async def test_answer_on_the_wrong_price_scale_is_retryable(self, h: Harness) -> None:
        h.ai.insights = make_insights(
            trading_signals=make_signals(
                start_level=3.40,
                safety_stop=3.23,
                projected_exit_1=3.65,
                projected_exit_2=3.80,
                support_level=3.25,
                resistance_level=3.66,
            )
        )

        with pytest.raises(TransientFailure) as error:
            await h.process.run(analysis_id := h.queued())

        assert error.value.code is ErrorCode.INVALID_AI_RESPONSE
        assert h.db.analysis(analysis_id).status is AnalysisStatus.QUEUED

    async def test_unexpected_error_becomes_a_retryable_internal_error(self, h: Harness) -> None:
        h.ai.recognize_error = RuntimeError("boom")

        with pytest.raises(TransientFailure) as error:
            await h.process.run(analysis_id := h.queued())

        assert error.value.code is ErrorCode.INTERNAL_ERROR
        assert h.db.analysis(analysis_id).status is AnalysisStatus.QUEUED


class TestDeadline:
    async def test_pipeline_slower_than_the_deadline_is_stopped_and_retryable(self) -> None:
        h = Harness(ai=SlowChartAI(), deadline=timedelta(milliseconds=30))
        analysis_id = h.queued()
        started = time.monotonic()

        with pytest.raises(TransientFailure) as error:
            await h.process.run(analysis_id)

        analysis = h.db.analysis(analysis_id)
        assert time.monotonic() - started < 5  # the hanging call was cancelled, not awaited
        assert error.value.code is ErrorCode.INTERNAL_ERROR
        assert error.value.detail == "deadline exceeded"
        assert analysis.status is AnalysisStatus.QUEUED
        assert analysis.result is None
        assert analysis.tokens_used == 1_200  # checkpoints made before the deadline are kept
        assert h.ai.analyze_calls == 0
        assert "completed" not in h.progress.types()

    async def test_analysis_stopped_by_the_deadline_can_be_claimed_again_at_once(self) -> None:
        slow = Harness(ai=SlowChartAI(), deadline=timedelta(milliseconds=30))
        analysis_id = slow.queued()
        with pytest.raises(TransientFailure):
            await slow.process.run(analysis_id)

        # no clock movement: the lease was given back, not left to expire
        retry = slow.second_worker()
        outcome = await retry.process.run(analysis_id)

        assert outcome is ProcessOutcome.COMPLETED
        assert retry.ai.recognize_calls == 0
        assert slow.db.analysis(analysis_id).tokens_used == 1_200 + 6_800

    async def test_pipeline_within_the_deadline_is_not_disturbed(self) -> None:
        h = Harness(ai=SlowChartAI(analyze_seconds=0.02), deadline=timedelta(seconds=5))

        assert await h.process.run(h.queued()) is ProcessOutcome.COMPLETED

    @pytest.mark.parametrize("deadline", [LEASE, LEASE + timedelta(seconds=1)])
    def test_deadline_must_be_shorter_than_the_lease(self, deadline: timedelta) -> None:
        with pytest.raises(ValueError, match="shorter than the lease"):
            Harness(deadline=deadline)


class TestFencing:
    """A worker whose lease expired keeps running; its late writes must hit nothing."""

    async def _overtaken(
        self, stale_ai: GatedChartAI, successor_ai: FakeChartAI | None = None
    ) -> tuple[Harness, Harness, UUID, "asyncio.Task[ProcessOutcome]"]:
        """Worker A is stuck inside an AI call and its lease has just expired."""
        a = Harness(ai=stale_ai)
        b = a.second_worker(successor_ai)
        analysis_id = a.queued()
        stale_run = asyncio.create_task(a.process.run(analysis_id))
        await asyncio.wait_for(stale_ai.entered.wait(), timeout=5)
        a.clock.current = NOW + LEASE + timedelta(seconds=1)
        return a, b, analysis_id, stale_run

    async def test_late_writes_do_not_touch_an_analysis_another_worker_completed(self) -> None:
        stale_ai = GatedChartAI(hold="recognize")
        a, b, analysis_id, stale_run = await self._overtaken(stale_ai)
        assert await b.process.run(analysis_id) is ProcessOutcome.COMPLETED
        completed = a.db.analysis(analysis_id)

        stale_ai.gate.set()
        await asyncio.wait_for(stale_run, timeout=5)  # must not raise

        analysis = a.db.analysis(analysis_id)
        assert analysis == completed
        assert analysis.status is AnalysisStatus.COMPLETED
        assert analysis.stage is Stage.DONE
        assert analysis.tokens_used == 1_200 + 6_800  # A's recognition is not added on top
        assert analysis.attempts == 2
        assert b.progress.types()[-1] == "completed"
        assert "completed" not in a.progress.types()

    async def test_late_completion_does_not_finish_an_analysis_another_worker_holds(
        self,
    ) -> None:
        stale_ai, successor_ai = GatedChartAI(), GatedChartAI()
        a, b, analysis_id, stale_run = await self._overtaken(stale_ai, successor_ai)
        successor_run = asyncio.create_task(b.process.run(analysis_id))
        await asyncio.wait_for(successor_ai.entered.wait(), timeout=5)

        stale_ai.gate.set()
        await asyncio.wait_for(stale_run, timeout=5)  # must not raise

        held = a.db.analysis(analysis_id)
        assert held.status is AnalysisStatus.PROCESSING
        assert held.result is None
        assert held.tokens_used == 1_200
        assert "completed" not in a.progress.types()

        successor_ai.gate.set()
        assert await asyncio.wait_for(successor_run, timeout=5) is ProcessOutcome.COMPLETED
        finished = a.db.analysis(analysis_id)
        assert finished.status is AnalysisStatus.COMPLETED
        assert finished.tokens_used == 1_200 + 6_800  # one recognition, one analysis call

    async def test_transient_failure_of_a_stale_worker_does_not_requeue_the_analysis(
        self,
    ) -> None:
        stale_ai = GatedChartAI(analyze_error=TransientFailure(ErrorCode.AI_UNAVAILABLE))
        successor_ai = GatedChartAI()
        a, b, analysis_id, stale_run = await self._overtaken(stale_ai, successor_ai)
        successor_run = asyncio.create_task(b.process.run(analysis_id))
        await asyncio.wait_for(successor_ai.entered.wait(), timeout=5)

        stale_ai.gate.set()
        with pytest.raises(TransientFailure):
            await asyncio.wait_for(stale_run, timeout=5)

        # still B's: not back in the queue, and B's lease is intact
        assert a.db.analysis(analysis_id).status is AnalysisStatus.PROCESSING
        assert await a.second_worker().process.run(analysis_id) is ProcessOutcome.BUSY

        successor_ai.gate.set()
        assert await asyncio.wait_for(successor_run, timeout=5) is ProcessOutcome.COMPLETED
        assert a.db.analysis(analysis_id).status is AnalysisStatus.COMPLETED

    async def test_permanent_failure_of_a_stale_worker_does_not_fail_the_analysis(self) -> None:
        stale_ai = GatedChartAI(analyze_error=AnalysisFailure(ErrorCode.UNCLEAR_CHART))
        successor_ai = GatedChartAI()
        a, b, analysis_id, stale_run = await self._overtaken(stale_ai, successor_ai)
        successor_run = asyncio.create_task(b.process.run(analysis_id))
        await asyncio.wait_for(successor_ai.entered.wait(), timeout=5)

        stale_ai.gate.set()
        await asyncio.wait_for(stale_run, timeout=5)

        held = a.db.analysis(analysis_id)
        assert held.status is AnalysisStatus.PROCESSING
        assert held.error_code is None
        assert "failed" not in a.progress.types()

        successor_ai.gate.set()
        assert await asyncio.wait_for(successor_run, timeout=5) is ProcessOutcome.COMPLETED

    async def test_deadline_of_a_stale_worker_does_not_requeue_the_analysis(self) -> None:
        a = Harness(ai=SlowChartAI(), deadline=timedelta(milliseconds=200))
        successor_ai = GatedChartAI()
        b = a.second_worker(successor_ai)
        analysis_id = a.queued()
        stale_run = asyncio.create_task(a.process.run(analysis_id))
        async with asyncio.timeout(5):
            while a.db.analysis(analysis_id).stage is not Stage.ANALYZING:
                await asyncio.sleep(0)
        a.clock.current = NOW + LEASE + timedelta(seconds=1)
        successor_run = asyncio.create_task(b.process.run(analysis_id))
        await asyncio.wait_for(successor_ai.entered.wait(), timeout=5)

        with pytest.raises(TransientFailure):
            await asyncio.wait_for(stale_run, timeout=5)

        assert a.db.analysis(analysis_id).status is AnalysisStatus.PROCESSING
        successor_ai.gate.set()
        assert await asyncio.wait_for(successor_run, timeout=5) is ProcessOutcome.COMPLETED


class TestCheckpoints:
    async def test_retry_does_not_pay_for_recognition_twice(self, h: Harness) -> None:
        h.market.error = TransientFailure(ErrorCode.MARKET_DATA_UNAVAILABLE)
        analysis_id = h.queued()
        with pytest.raises(TransientFailure):
            await h.process.run(analysis_id)

        h.market.error = None
        outcome = await h.process.run(analysis_id)

        assert outcome is ProcessOutcome.COMPLETED
        assert h.ai.recognize_calls == 1
        assert h.db.analysis(analysis_id).tokens_used == 1_200 + 6_800
        assert h.db.analysis(analysis_id).attempts == 2

    async def test_retry_reuses_the_saved_market_snapshot(self, h: Harness) -> None:
        h.ai.analyze_error = TransientFailure(ErrorCode.AI_UNAVAILABLE)
        analysis_id = h.queued()
        with pytest.raises(TransientFailure):
            await h.process.run(analysis_id)
        calls_after_first_attempt = h.market.calls

        h.ai.analyze_error = None
        await h.process.run(analysis_id)

        assert h.market.calls == calls_after_first_attempt
        assert h.ai.recognize_calls == 1

    async def test_resumes_from_checkpoints_left_by_a_dead_worker(self, h: Harness) -> None:
        analysis_id = h.queued(
            recognition=make_recognition(), market_snapshot=make_snapshot(), tokens_used=1_200
        )

        await h.process.run(analysis_id)

        assert h.ai.recognize_calls == 0 and h.market.calls == 0
        assert h.progress.types() == ["analyzing", "completed"]


class TestDuplicateDeliveries:
    async def test_finished_analysis_is_not_processed_again(self, h: Harness) -> None:
        analysis_id = h.queued()
        await h.process.run(analysis_id)

        outcome = await h.process.run(analysis_id)

        assert outcome is ProcessOutcome.DUPLICATE
        assert h.ai.recognize_calls == 1 and h.ai.analyze_calls == 1

    async def test_unknown_analysis_is_ignored(self, h: Harness) -> None:
        assert await h.process.run(uuid4()) is ProcessOutcome.DUPLICATE

    async def test_analysis_held_by_another_worker_is_busy(self, h: Harness) -> None:
        analysis_id = h.queued()
        claimed = await InMemoryUnitOfWork(h.db).analyses.claim(analysis_id, now=NOW, lease=LEASE)
        assert claimed.analysis is not None

        outcome = await h.process.run(analysis_id)

        assert outcome is ProcessOutcome.BUSY
        assert h.ai.recognize_calls == 0

    async def test_expired_lease_is_taken_over(self, h: Harness) -> None:
        analysis_id = h.queued()
        await InMemoryUnitOfWork(h.db).analyses.claim(analysis_id, now=NOW, lease=LEASE)

        h.clock.current = NOW + LEASE + timedelta(seconds=1)
        outcome = await h.process.run(analysis_id)

        assert outcome is ProcessOutcome.COMPLETED


class TestGiveUp:
    async def test_marks_failed_and_notifies(self, h: Harness) -> None:
        analysis_id = h.queued()

        await h.process.give_up(analysis_id, ErrorCode.AI_UNAVAILABLE)

        analysis = h.db.analysis(analysis_id)
        assert analysis.status is AnalysisStatus.FAILED
        assert analysis.error_code is ErrorCode.AI_UNAVAILABLE
        assert h.progress.events[-1][1].type is ProgressEventType.FAILED

    async def test_fails_an_analysis_whatever_claim_holds_it(self, h: Harness) -> None:
        # give_up is not tied to a claim: the retries are over, whoever ran last
        analysis_id = h.queued()
        await InMemoryUnitOfWork(h.db).analyses.claim(analysis_id, now=NOW, lease=LEASE)

        await h.process.give_up(analysis_id, ErrorCode.INTERNAL_ERROR)

        assert h.db.analysis(analysis_id).status is AnalysisStatus.FAILED

    async def test_unknown_analysis_is_ignored(self, h: Harness) -> None:
        await h.process.give_up(uuid4(), ErrorCode.INTERNAL_ERROR)

        assert h.progress.events == []

    async def test_does_not_overwrite_a_finished_analysis(self, h: Harness) -> None:
        analysis_id = h.queued()
        await h.process.run(analysis_id)
        events_before = len(h.progress.events)

        await h.process.give_up(analysis_id, ErrorCode.INTERNAL_ERROR)

        assert h.db.analysis(analysis_id).status is AnalysisStatus.COMPLETED
        assert len(h.progress.events) == events_before
