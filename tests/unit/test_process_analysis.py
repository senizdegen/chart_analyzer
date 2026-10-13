"""The worker pipeline: steps, checkpoints, failures, idempotency."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from chart_analyzer.application.ports.services import ProgressEventType
from chart_analyzer.application.process_analysis import ProcessAnalysis, ProcessOutcome
from chart_analyzer.domain.enums import AnalysisStatus, AssetType, ErrorCode, Language, Stage
from chart_analyzer.domain.errors import AnalysisFailure, TransientFailure
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


class Harness:
    def __init__(self, *, progress: RecordingProgress | None = None) -> None:
        self.db = InMemoryDatabase()
        self.ai = FakeChartAI()
        self.market = FakeMarketData()
        self.progress = progress or RecordingProgress()
        self.clock = FixedClock()
        self.process = ProcessAnalysis(
            lambda: InMemoryUnitOfWork(self.db),
            InMemoryImageStorage(),
            self.ai,
            self.market,
            self.progress,
            self.clock,
            lease=LEASE,
            max_news=3,
        )

    def queued(self, **overrides: object) -> UUID:
        return self.db.seed(make_analysis(uuid4(), **overrides)).id


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

    async def test_inconsistent_ai_answer_is_retryable_and_its_tokens_recorded(
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
        assert analysis.tokens_used == 1_200 + 6_800

    async def test_unexpected_error_becomes_a_retryable_internal_error(self, h: Harness) -> None:
        h.ai.recognize_error = RuntimeError("boom")

        with pytest.raises(TransientFailure) as error:
            await h.process.run(analysis_id := h.queued())

        assert error.value.code is ErrorCode.INTERNAL_ERROR
        assert h.db.analysis(analysis_id).status is AnalysisStatus.QUEUED


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

    async def test_does_not_overwrite_a_finished_analysis(self, h: Harness) -> None:
        analysis_id = h.queued()
        await h.process.run(analysis_id)
        events_before = len(h.progress.events)

        await h.process.give_up(analysis_id, ErrorCode.INTERNAL_ERROR)

        assert h.db.analysis(analysis_id).status is AnalysisStatus.COMPLETED
        assert len(h.progress.events) == events_before
