"""Use case: run one analysis through the pipeline (the worker side of the flow).

    recognize (AI, cheap)  ->  market data  ->  full analysis (AI)  ->  validate  ->  save

Each step saves its result as a checkpoint before the next one starts. Tasks are delivered
at least once, so the same analysis may be handed to this use case several times; it is
safe because

* ``claim`` lets only one worker own an analysis at a time, and
* a step whose checkpoint already exists is skipped instead of being paid for again.
"""

import asyncio
from datetime import timedelta
from enum import StrEnum
from uuid import UUID

import structlog

from chart_analyzer.application.ports.repositories import ClaimStatus
from chart_analyzer.application.ports.services import (
    AssetRef,
    ChartAI,
    ChartImage,
    Clock,
    ImageStorage,
    MarketDataProvider,
    ProgressEvent,
    ProgressPublisher,
)
from chart_analyzer.application.ports.unit_of_work import UnitOfWorkFactory
from chart_analyzer.application.progress_events import final_event, stage_event
from chart_analyzer.domain.analysis import Analysis
from chart_analyzer.domain.analysis_rules import assemble_result, resolve_recognized_asset
from chart_analyzer.domain.enums import ErrorCode, Stage
from chart_analyzer.domain.errors import AnalysisFailure, TransientFailure
from chart_analyzer.domain.models import ChartRecognition, MarketSnapshot

log = structlog.get_logger(__name__)


class ProcessOutcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"  # permanent failure, recorded
    DUPLICATE = "duplicate"  # already finished or unknown: nothing to do
    BUSY = "busy"  # another worker holds it: look again later


class ProcessAnalysis:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        storage: ImageStorage,
        ai: ChartAI,
        market: MarketDataProvider,
        progress: ProgressPublisher,
        clock: Clock,
        *,
        lease: timedelta,
        deadline: timedelta,
        max_news: int,
    ) -> None:
        if deadline >= lease:
            raise ValueError("the pipeline deadline must be shorter than the lease")
        self._uow_factory = uow_factory
        self._storage = storage
        self._ai = ai
        self._market = market
        self._progress = progress
        self._clock = clock
        self._lease = lease
        self._deadline = deadline
        self._max_news = max_news

    async def run(self, analysis_id: UUID) -> ProcessOutcome:
        """Process the analysis. Raises ``TransientFailure`` when a retry may help."""
        async with self._uow_factory() as uow:
            claim = await uow.analyses.claim(analysis_id, now=self._clock.now(), lease=self._lease)
            await uow.commit()

        if claim.status is ClaimStatus.BUSY:
            return ProcessOutcome.BUSY
        if claim.status is not ClaimStatus.CLAIMED or claim.analysis is None:
            log.info("analysis.skipped", reason=claim.status.value)
            return ProcessOutcome.DUPLICATE

        analysis = claim.analysis
        log.info("analysis.started", attempt=analysis.attempts)
        try:
            # The deadline is shorter than the lease, so this worker stops before another
            # one may take the analysis over.
            async with asyncio.timeout(self._deadline.total_seconds()):
                await self._run_pipeline(analysis)
        except AnalysisFailure as failure:
            log.info("analysis.failed", code=failure.code.value, detail=failure.detail)
            await self._fail(analysis, failure.code, attempt=analysis.attempts)
            return ProcessOutcome.FAILED
        except TransientFailure as failure:
            log.warning(
                "analysis.transient_failure", code=failure.code.value, detail=failure.detail
            )
            await self._release(analysis)
            raise
        except TimeoutError as error:
            log.warning("analysis.deadline_exceeded", seconds=self._deadline.total_seconds())
            await self._release(analysis)
            raise TransientFailure(ErrorCode.INTERNAL_ERROR, "deadline exceeded") from error
        except Exception as error:
            log.exception("analysis.unexpected_error")
            await self._release(analysis)
            raise TransientFailure(ErrorCode.INTERNAL_ERROR, repr(error)) from error
        return ProcessOutcome.COMPLETED

    async def give_up(self, analysis_id: UUID, code: ErrorCode) -> None:
        """Retries are exhausted: record the failure so the user is not left waiting."""
        async with self._uow_factory() as uow:
            analysis = await uow.analyses.get(analysis_id)
        if analysis is None:
            return
        log.error("analysis.gave_up", code=code.value)
        await self._fail(analysis, code, attempt=None)

    # --- pipeline -----------------------------------------------------------------------------

    async def _run_pipeline(self, analysis: Analysis) -> None:
        image: ChartImage | None = None

        recognition = analysis.recognition
        if recognition is None:
            await self._enter_stage(analysis.id, Stage.RECOGNIZING)
            image = await self._storage.load(analysis.image)
            recognition = await self._recognize(analysis.id, image)
        symbol, asset_type = resolve_recognized_asset(recognition)
        asset_ref = AssetRef(symbol=symbol, type=asset_type, name_hint=recognition.asset_name)

        snapshot = analysis.market_snapshot
        if snapshot is None:
            await self._enter_stage(analysis.id, Stage.MARKET_DATA)
            snapshot = await self._load_market_snapshot(asset_ref)
            async with self._uow_factory() as uow:
                await uow.analyses.save_market_snapshot(analysis.id, snapshot)
                await uow.commit()

        await self._enter_stage(analysis.id, Stage.ANALYZING)
        if image is None:
            image = await self._storage.load(analysis.image)
        answer = await self._ai.analyze(image, analysis.language, snapshot)
        try:
            result = assemble_result(
                analysis.language, snapshot, answer.value, max_news=self._max_news
            )
        except TransientFailure:
            # A rejected answer is not charged to the analysis: only the attempt that
            # succeeds counts, which keeps the spend within the quota reservation.
            log.warning("analysis.answer_rejected", tokens=answer.tokens)
            raise

        async with self._uow_factory() as uow:
            completed = await uow.analyses.complete(
                analysis.id,
                result,
                tokens=answer.tokens,
                now=self._clock.now(),
                attempt=analysis.attempts,
            )
            finished = await uow.analyses.get(analysis.id)
            await uow.commit()

        if not completed or finished is None:
            log.warning("analysis.completion_skipped", reason="the claim was lost")
            return
        log.info("analysis.completed", symbol=symbol, tokens_used=finished.tokens_used)
        await self._publish(analysis.id, final_event(finished))

    async def _recognize(self, analysis_id: UUID, image: ChartImage) -> ChartRecognition:
        answer = await self._ai.recognize(image)
        async with self._uow_factory() as uow:
            await uow.analyses.save_recognition(analysis_id, answer.value, tokens=answer.tokens)
            await uow.commit()
        return answer.value

    async def _load_market_snapshot(self, ref: AssetRef) -> MarketSnapshot:
        """Four independent lookups, run concurrently."""
        asset, price, review, news = await asyncio.gather(
            self._market.get_asset(ref),
            self._market.get_price(ref),
            self._market.get_analyst_review(ref),
            self._market.get_news(ref),
        )
        return MarketSnapshot(asset=asset, price=price, analyst_review=review, news=news)

    # --- state changes ------------------------------------------------------------------------

    async def _enter_stage(self, analysis_id: UUID, stage: Stage) -> None:
        async with self._uow_factory() as uow:
            await uow.analyses.set_stage(analysis_id, stage)
            await uow.commit()
        await self._publish(analysis_id, stage_event(stage))

    async def _fail(self, analysis: Analysis, code: ErrorCode, *, attempt: int | None) -> None:
        async with self._uow_factory() as uow:
            failed = await uow.analyses.fail(
                analysis.id, code, now=self._clock.now(), attempt=attempt
            )
            current = await uow.analyses.get(analysis.id)
            await uow.commit()
        if failed and current is not None:
            await self._publish(analysis.id, final_event(current))

    async def _release(self, analysis: Analysis) -> None:
        async with self._uow_factory() as uow:
            await uow.analyses.release(analysis.id, attempt=analysis.attempts)
            await uow.commit()

    async def _publish(self, analysis_id: UUID, event: ProgressEvent) -> None:
        """Progress is best effort: the database, not the stream, is the source of truth."""
        try:
            await self._progress.publish(analysis_id, event)
        except Exception:
            log.warning("progress.publish_failed", event_type=event.type.value, exc_info=True)
