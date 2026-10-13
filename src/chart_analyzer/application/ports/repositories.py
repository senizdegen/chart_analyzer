"""Persistence ports. The application layer depends on these, never on SQLAlchemy."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from chart_analyzer.domain.analysis import Analysis, NewAnalysis
from chart_analyzer.domain.enums import ErrorCode, Stage
from chart_analyzer.domain.models import AnalysisResult, ChartRecognition, MarketSnapshot
from chart_analyzer.domain.quota import QuotaUsage


class ClaimStatus(StrEnum):
    CLAIMED = "claimed"  # this worker now owns the analysis
    BUSY = "busy"  # another worker holds a fresh lease
    FINISHED = "finished"  # already completed or failed: the message is a duplicate
    MISSING = "missing"  # no such analysis


@dataclass(frozen=True, slots=True)
class ClaimResult:
    status: ClaimStatus
    analysis: Analysis | None = None


class AnalysisRepository(Protocol):
    async def add(self, new: NewAnalysis) -> Analysis: ...

    async def get(self, analysis_id: UUID) -> Analysis | None: ...

    async def get_for_user(self, analysis_id: UUID, user_id: str) -> Analysis | None: ...

    async def find_by_idempotency_key(self, user_id: str, key: str) -> Analysis | None: ...

    async def list_for_user(
        self, user_id: str, *, limit: int, offset: int
    ) -> tuple[Sequence[Analysis], int]:
        """Newest first, plus the total number of the user's analyses."""
        ...

    async def get_quota_usage(self, user_id: str, day_start: datetime) -> QuotaUsage:
        """Tokens of analyses completed since ``day_start`` and the number of active ones."""
        ...

    async def claim(self, analysis_id: UUID, *, now: datetime, lease: timedelta) -> ClaimResult:
        """Atomically take a queued analysis, or one whose lease expired (its worker died)."""
        ...

    # Progress and checkpoints are written only while the analysis is in progress, so a
    # late write can never alter a finished analysis.

    async def set_stage(self, analysis_id: UUID, stage: Stage) -> None: ...

    async def save_recognition(
        self, analysis_id: UUID, recognition: ChartRecognition, *, tokens: int
    ) -> None:
        """Checkpoint of step 1; adds ``tokens`` to the analysis' spend."""
        ...

    async def save_market_snapshot(self, analysis_id: UUID, snapshot: MarketSnapshot) -> None:
        """Checkpoint of step 2."""
        ...

    async def complete(
        self,
        analysis_id: UUID,
        result: AnalysisResult,
        *,
        tokens: int,
        now: datetime,
        attempt: int,
    ) -> bool:
        """Finish successfully.

        ``attempt`` is the claim that did the work (``Analysis.attempts`` right after
        ``claim``). ``False`` when that claim no longer owns the analysis: it was finished
        meanwhile, or another worker took it over after the lease expired.
        """
        ...

    async def fail(
        self, analysis_id: UUID, code: ErrorCode, *, now: datetime, attempt: int | None = None
    ) -> bool:
        """Finish unsuccessfully. ``False`` if the analysis was already finished, or if
        ``attempt`` is given and that claim no longer owns it."""
        ...

    async def release(self, analysis_id: UUID, *, attempt: int) -> None:
        """Give a claimed analysis back to the queue before a retry; a no-op when the
        claim ``attempt`` no longer owns it."""
        ...

    async def fail_stale(
        self, *, inactive_since: datetime, code: ErrorCode, now: datetime
    ) -> Sequence[UUID]:
        """Fail every active analysis nothing has touched since ``inactive_since``.

        Safety net for analyses whose task was lost or dead-lettered without the failure
        being recorded: they must not hold a quota reservation forever.
        """
        ...


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    id: int
    analysis_id: UUID


class OutboxRepository(Protocol):
    async def add(self, analysis_id: UUID, *, now: datetime) -> None: ...

    async def fetch_unsent(self, limit: int) -> Sequence[OutboxMessage]:
        """Lock a batch of unsent messages, skipping rows another relay already holds."""
        ...

    async def mark_sent(self, message_ids: Sequence[int], *, now: datetime) -> None: ...
