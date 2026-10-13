"""Read-side use cases: history, a single analysis, its image, the quota status."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from chart_analyzer.application.ports.services import ChartImage, Clock, ImageStorage
from chart_analyzer.application.ports.unit_of_work import UnitOfWorkFactory
from chart_analyzer.domain.analysis import Analysis
from chart_analyzer.domain.quota import QuotaPolicy, next_reset, utc_day_start


@dataclass(frozen=True, slots=True)
class QuotaStatus:
    daily_limit: int
    used_tokens: int
    reserved_tokens: int
    remaining_tokens: int
    resets_at: datetime


class AnalysisQueries:
    """Every lookup is scoped to the caller: one user never sees another user's analyses."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        storage: ImageStorage,
        policy: QuotaPolicy,
        clock: Clock,
    ) -> None:
        self._uow_factory = uow_factory
        self._storage = storage
        self._policy = policy
        self._clock = clock

    async def get(self, analysis_id: UUID, user_id: str) -> Analysis | None:
        async with self._uow_factory() as uow:
            return await uow.analyses.get_for_user(analysis_id, user_id)

    async def list(
        self, user_id: str, *, limit: int, offset: int
    ) -> tuple[Sequence[Analysis], int]:
        async with self._uow_factory() as uow:
            return await uow.analyses.list_for_user(user_id, limit=limit, offset=offset)

    async def get_image(self, analysis_id: UUID, user_id: str) -> ChartImage | None:
        analysis = await self.get(analysis_id, user_id)
        if analysis is None:
            return None
        return await self._storage.load(analysis.image)

    async def get_quota(self, user_id: str) -> QuotaStatus:
        now = self._clock.now()
        async with self._uow_factory() as uow:
            usage = await uow.analyses.get_quota_usage(user_id, utc_day_start(now))
        return QuotaStatus(
            daily_limit=self._policy.daily_limit,
            used_tokens=usage.used_tokens,
            reserved_tokens=self._policy.reserved_tokens(usage),
            remaining_tokens=self._policy.remaining_tokens(usage),
            resets_at=next_reset(now),
        )
