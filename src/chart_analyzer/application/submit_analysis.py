"""Use case: admit a new chart analysis (the API side of the flow)."""

import asyncio
from dataclasses import dataclass
from uuid import uuid4

import structlog

from chart_analyzer.application.ports.repositories import AnalysisRepository
from chart_analyzer.application.ports.services import Clock, ImageInspector, ImageStorage
from chart_analyzer.application.ports.unit_of_work import UnitOfWorkFactory
from chart_analyzer.domain.analysis import Analysis, NewAnalysis
from chart_analyzer.domain.enums import ErrorCode, Language
from chart_analyzer.domain.errors import QuotaExceeded
from chart_analyzer.domain.quota import QuotaDecision, QuotaPolicy, QuotaUsage, utc_day_start

log = structlog.get_logger(__name__)

_DECISION_ERRORS = {
    QuotaDecision.DAILY_LIMIT_REACHED: ErrorCode.QUOTA_EXCEEDED,
    QuotaDecision.TOO_MANY_ACTIVE: ErrorCode.TOO_MANY_ACTIVE_ANALYSES,
}


@dataclass(frozen=True, slots=True)
class SubmitAnalysisCommand:
    user_id: str
    image_bytes: bytes
    language: Language
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class SubmitAnalysisResult:
    analysis: Analysis
    created: bool  # False when an earlier request with the same idempotency key is replayed


class SubmitAnalysis:
    """Validates the upload, enforces the quota and records the analysis with its task.

    The analysis row and the outbox row are written in one transaction, so a stored
    analysis always has a task that will reach the queue (transactional outbox).
    """

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        storage: ImageStorage,
        inspector: ImageInspector,
        policy: QuotaPolicy,
        clock: Clock,
    ) -> None:
        self._uow_factory = uow_factory
        self._storage = storage
        self._inspector = inspector
        self._policy = policy
        self._clock = clock

    async def execute(self, command: SubmitAnalysisCommand) -> SubmitAnalysisResult:
        user_id, key = command.user_id, command.idempotency_key

        # Cheap checks before any file is written. They take no lock, so they are only an
        # early exit; the authoritative checks are repeated under the lock below.
        async with self._uow_factory() as uow:
            if key and (existing := await uow.analyses.find_by_idempotency_key(user_id, key)):
                return SubmitAnalysisResult(existing, created=False)
            self._ensure_allowed(await self._usage(uow.analyses, user_id), user_id)

        image = await asyncio.to_thread(self._inspector.inspect, command.image_bytes)
        analysis_id = uuid4()
        stored = await self._storage.save(analysis_id, image)

        commit_started = False
        try:
            async with self._uow_factory() as uow:
                await uow.lock_user(user_id)

                if key and (existing := await uow.analyses.find_by_idempotency_key(user_id, key)):
                    await self._storage.delete(stored)
                    return SubmitAnalysisResult(existing, created=False)

                self._ensure_allowed(await self._usage(uow.analyses, user_id), user_id)

                now = self._clock.now()
                analysis = await uow.analyses.add(
                    NewAnalysis(
                        id=analysis_id,
                        user_id=user_id,
                        language=command.language,
                        image=stored,
                        idempotency_key=key,
                        created_at=now,
                    )
                )
                await uow.outbox.add(analysis_id, now=now)
                commit_started = True
                await uow.commit()
        except BaseException:
            # If the commit itself failed its outcome is unknown: the analysis may exist,
            # and then it needs its image. An orphan file is the cheaper mistake.
            if not commit_started:
                await self._storage.delete(stored)
            raise

        log.info("analysis.submitted", analysis_id=str(analysis_id), user_id=user_id)
        return SubmitAnalysisResult(analysis, created=True)

    async def _usage(self, analyses: AnalysisRepository, user_id: str) -> QuotaUsage:
        return await analyses.get_quota_usage(user_id, utc_day_start(self._clock.now()))

    def _ensure_allowed(self, usage: QuotaUsage, user_id: str) -> None:
        decision = self._policy.decide(usage)
        if decision is QuotaDecision.ALLOWED:
            return
        log.info(
            "analysis.rejected",
            user_id=user_id,
            reason=decision.value,
            used_tokens=usage.used_tokens,
            active_analyses=usage.active_analyses,
        )
        raise QuotaExceeded(_DECISION_ERRORS[decision])
