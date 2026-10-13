"""PostgreSQL adapters of the persistence ports.

Every state change of an analysis is one ``UPDATE`` whose ``WHERE`` clause carries the
precondition, so two workers can never both win and nothing is read-modify-written.
``updated_at`` always comes from the database clock (``now()``); the business timestamps
(``created_at``, ``locked_at``, ``completed_at``, ``sent_at``) come from the caller's clock.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Row, and_, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from chart_analyzer.application.ports.repositories import ClaimResult, ClaimStatus, OutboxMessage
from chart_analyzer.domain.analysis import Analysis, NewAnalysis, StoredImage
from chart_analyzer.domain.enums import AnalysisStatus, ErrorCode, Language, Stage
from chart_analyzer.domain.models import AnalysisResult, ChartRecognition, MarketSnapshot
from chart_analyzer.domain.quota import QuotaUsage
from chart_analyzer.infrastructure.db.models import AnalysisRow, OutboxRow

_ANALYSES = AnalysisRow.__table__
_ACTIVE = (AnalysisStatus.QUEUED.value, AnalysisStatus.PROCESSING.value)
_TERMINAL = (AnalysisStatus.COMPLETED.value, AnalysisStatus.FAILED.value)


def _owned_by(attempt: int) -> tuple[Any, Any]:
    """Fencing: still in progress, and still under the claim numbered ``attempt``.

    Every claim increments ``attempts``, so a worker whose lease expired and was taken
    over carries a stale number and its late writes match no row.
    """
    return (
        AnalysisRow.status == AnalysisStatus.PROCESSING.value,
        AnalysisRow.attempts == attempt,
    )


def _to_analysis(row: Row[Any]) -> Analysis:
    return Analysis(
        id=row.id,
        user_id=row.user_id,
        language=Language(row.language),
        status=AnalysisStatus(row.status),
        stage=Stage(row.stage),
        image=StoredImage(key=row.image_key, content_type=row.image_content_type),
        idempotency_key=row.idempotency_key,
        recognition=(
            None if row.recognition is None else ChartRecognition.model_validate(row.recognition)
        ),
        market_snapshot=(
            None
            if row.market_snapshot is None
            else MarketSnapshot.model_validate(row.market_snapshot)
        ),
        result=None if row.result is None else AnalysisResult.model_validate(row.result),
        error_code=None if row.error_code is None else ErrorCode(row.error_code),
        tokens_used=row.tokens_used,
        attempts=row.attempts,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )


class SqlAlchemyAnalysisRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, new: NewAnalysis) -> Analysis:
        """Raises ``IntegrityError`` on a duplicate ``(user_id, idempotency_key)``."""
        statement = (
            insert(AnalysisRow)
            .values(
                id=new.id,
                user_id=new.user_id,
                idempotency_key=new.idempotency_key,
                language=new.language.value,
                status=AnalysisStatus.QUEUED.value,
                stage=Stage.QUEUED.value,
                image_key=new.image.key,
                image_content_type=new.image.content_type,
                tokens_used=0,
                attempts=0,
                created_at=new.created_at,
                updated_at=new.created_at,
            )
            .returning(_ANALYSES)
        )
        return _to_analysis((await self._session.execute(statement)).one())

    async def get(self, analysis_id: UUID) -> Analysis | None:
        statement = select(_ANALYSES).where(AnalysisRow.id == analysis_id)
        return await self._one_or_none(statement)

    async def get_for_user(self, analysis_id: UUID, user_id: str) -> Analysis | None:
        statement = select(_ANALYSES).where(
            AnalysisRow.id == analysis_id, AnalysisRow.user_id == user_id
        )
        return await self._one_or_none(statement)

    async def find_by_idempotency_key(self, user_id: str, key: str) -> Analysis | None:
        statement = select(_ANALYSES).where(
            AnalysisRow.user_id == user_id, AnalysisRow.idempotency_key == key
        )
        return await self._one_or_none(statement)

    async def list_for_user(
        self, user_id: str, *, limit: int, offset: int
    ) -> tuple[Sequence[Analysis], int]:
        page = (
            select(_ANALYSES)
            .where(AnalysisRow.user_id == user_id)
            # id breaks ties, so pages stay stable when two analyses share a timestamp
            .order_by(AnalysisRow.created_at.desc(), AnalysisRow.id.desc())
            .limit(limit)
            .offset(offset)
        )
        count = select(func.count()).select_from(AnalysisRow).where(AnalysisRow.user_id == user_id)
        rows = (await self._session.execute(page)).all()
        total = (await self._session.execute(count)).scalar_one()
        return [_to_analysis(row) for row in rows], total

    async def get_quota_usage(self, user_id: str, day_start: datetime) -> QuotaUsage:
        completed_today = and_(
            AnalysisRow.status == AnalysisStatus.COMPLETED.value,
            AnalysisRow.completed_at >= day_start,
        )
        is_active = AnalysisRow.status.in_(_ACTIVE)
        statement = select(
            func.coalesce(func.sum(AnalysisRow.tokens_used).filter(completed_today), 0),
            func.count().filter(is_active),
        ).where(AnalysisRow.user_id == user_id, or_(completed_today, is_active))
        used, active = (await self._session.execute(statement)).one()
        return QuotaUsage(used_tokens=int(used), active_analyses=int(active))

    async def claim(self, analysis_id: UUID, *, now: datetime, lease: timedelta) -> ClaimResult:
        lease_expired = and_(
            AnalysisRow.status == AnalysisStatus.PROCESSING.value,
            AnalysisRow.locked_at < now - lease,
        )
        statement = (
            update(AnalysisRow)
            .where(
                AnalysisRow.id == analysis_id,
                or_(AnalysisRow.status == AnalysisStatus.QUEUED.value, lease_expired),
            )
            .values(
                status=AnalysisStatus.PROCESSING.value,
                locked_at=now,
                attempts=AnalysisRow.attempts + 1,
                updated_at=func.now(),
            )
            .returning(_ANALYSES)
        )
        claimed = (await self._session.execute(statement)).one_or_none()
        if claimed is not None:
            return ClaimResult(ClaimStatus.CLAIMED, _to_analysis(claimed))

        status = (
            await self._session.execute(
                select(AnalysisRow.status).where(AnalysisRow.id == analysis_id)
            )
        ).scalar_one_or_none()
        if status is None:
            return ClaimResult(ClaimStatus.MISSING)
        if status in _TERMINAL:
            return ClaimResult(ClaimStatus.FINISHED)
        return ClaimResult(ClaimStatus.BUSY)

    async def set_stage(self, analysis_id: UUID, stage: Stage) -> None:
        await self._update(analysis_id, stage=stage.value)

    async def save_recognition(
        self, analysis_id: UUID, recognition: ChartRecognition, *, tokens: int
    ) -> None:
        await self._update(
            analysis_id,
            recognition=recognition.model_dump(mode="json"),
            tokens_used=AnalysisRow.tokens_used + tokens,
        )

    async def save_market_snapshot(self, analysis_id: UUID, snapshot: MarketSnapshot) -> None:
        await self._update(analysis_id, market_snapshot=snapshot.model_dump(mode="json"))

    async def complete(
        self,
        analysis_id: UUID,
        result: AnalysisResult,
        *,
        tokens: int,
        now: datetime,
        attempt: int,
    ) -> bool:
        return await self._finish(
            analysis_id,
            and_(*_owned_by(attempt)),
            status=AnalysisStatus.COMPLETED.value,
            result=result.model_dump(mode="json"),
            tokens_used=AnalysisRow.tokens_used + tokens,
            completed_at=now,
        )

    async def fail(
        self, analysis_id: UUID, code: ErrorCode, *, now: datetime, attempt: int | None = None
    ) -> bool:
        precondition = (
            AnalysisRow.status.in_(_ACTIVE) if attempt is None else and_(*_owned_by(attempt))
        )
        return await self._finish(
            analysis_id,
            precondition,
            status=AnalysisStatus.FAILED.value,
            error_code=code.value,
            completed_at=now,
        )

    async def release(self, analysis_id: UUID, *, attempt: int) -> None:
        await self._session.execute(
            update(AnalysisRow)
            .where(AnalysisRow.id == analysis_id, *_owned_by(attempt))
            .values(status=AnalysisStatus.QUEUED.value, locked_at=None, updated_at=func.now())
        )

    async def fail_stale(
        self, *, inactive_since: datetime, code: ErrorCode, now: datetime
    ) -> Sequence[UUID]:
        result = await self._session.execute(
            update(AnalysisRow)
            .where(AnalysisRow.status.in_(_ACTIVE), AnalysisRow.updated_at < inactive_since)
            .values(
                status=AnalysisStatus.FAILED.value,
                stage=Stage.DONE.value,
                error_code=code.value,
                completed_at=now,
                locked_at=None,
                updated_at=func.now(),
            )
            .returning(AnalysisRow.id)
        )
        return list(result.scalars())

    async def _one_or_none(self, statement: Any) -> Analysis | None:
        row = (await self._session.execute(statement)).one_or_none()
        return None if row is None else _to_analysis(row)

    async def _update(self, analysis_id: UUID, **values: Any) -> None:
        """Progress and checkpoint writes; they never touch a finished analysis."""
        await self._session.execute(
            update(AnalysisRow)
            .where(
                AnalysisRow.id == analysis_id,
                AnalysisRow.status == AnalysisStatus.PROCESSING.value,
            )
            .values(**values, updated_at=func.now())
        )

    async def _finish(self, analysis_id: UUID, precondition: Any, **values: Any) -> bool:
        """Move to a terminal status if ``precondition`` still holds; tells whether it did."""
        statement = (
            update(AnalysisRow)
            .where(AnalysisRow.id == analysis_id, precondition)
            .values(**values, stage=Stage.DONE.value, locked_at=None, updated_at=func.now())
            .returning(AnalysisRow.id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None


class SqlAlchemyOutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, analysis_id: UUID, *, now: datetime) -> None:
        await self._session.execute(
            insert(OutboxRow).values(analysis_id=analysis_id, created_at=now)
        )

    async def fetch_unsent(self, limit: int) -> Sequence[OutboxMessage]:
        """The row locks last until the transaction ends, so publish and mark inside it."""
        statement = (
            select(OutboxRow.id, OutboxRow.analysis_id)
            .where(OutboxRow.sent_at.is_(None))
            .order_by(OutboxRow.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = (await self._session.execute(statement)).all()
        return [OutboxMessage(id=row.id, analysis_id=row.analysis_id) for row in rows]

    async def mark_sent(self, message_ids: Sequence[int], *, now: datetime) -> None:
        if not message_ids:
            return
        await self._session.execute(
            update(OutboxRow).where(OutboxRow.id.in_(message_ids)).values(sent_at=now)
        )
