"""Admission of a new analysis: quota, idempotency, outbox."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from chart_analyzer.application.submit_analysis import SubmitAnalysis, SubmitAnalysisCommand
from chart_analyzer.domain.enums import AnalysisStatus, ErrorCode, Language
from chart_analyzer.domain.errors import InvalidImage, QuotaExceeded
from chart_analyzer.domain.quota import QuotaPolicy
from tests.fakes import (
    NOW,
    FakeInspector,
    FixedClock,
    InMemoryDatabase,
    InMemoryImageStorage,
    InMemoryUnitOfWork,
    make_analysis,
)

POLICY = QuotaPolicy(daily_limit=500_000, reservation_tokens=20_000, max_active_analyses=3)


class RejectingInspector:
    def inspect(self, data: bytes) -> object:
        raise InvalidImage(ErrorCode.INVALID_IMAGE)


class Harness:
    def __init__(self, inspector: object | None = None) -> None:
        self.db = InMemoryDatabase()
        self.storage = InMemoryImageStorage()
        self.submit = SubmitAnalysis(
            lambda: InMemoryUnitOfWork(self.db),
            self.storage,
            inspector or FakeInspector(),  # type: ignore[arg-type]
            POLICY,
            FixedClock(),
        )

    def command(self, **overrides: object) -> SubmitAnalysisCommand:
        data: dict[str, object] = {
            "user_id": "user-1",
            "image_bytes": b"png-bytes",
            "language": Language.EN,
            "idempotency_key": None,
        }
        return SubmitAnalysisCommand(**(data | overrides))  # type: ignore[arg-type]

    def completed(self, tokens: int, *, at: datetime = NOW, user_id: str = "user-1") -> None:
        self.db.seed(
            make_analysis(
                uuid4(),
                user_id=user_id,
                status=AnalysisStatus.COMPLETED,
                tokens_used=tokens,
                completed_at=at,
            )
        )


@pytest.fixture
def h() -> Harness:
    return Harness()


class TestAdmission:
    async def test_stores_analysis_image_and_outbox_task_together(self, h: Harness) -> None:
        result = await h.submit.execute(h.command(language=Language.FR))

        analysis = result.analysis
        assert result.created is True
        assert analysis.status is AnalysisStatus.QUEUED
        assert analysis.language is Language.FR
        assert analysis.image.key in h.storage.files
        assert [(analysis_id, sent) for _, analysis_id, sent in h.db.outbox] == [
            (analysis.id, None)
        ]

    async def test_takes_the_user_lock_before_the_authoritative_check(self, h: Harness) -> None:
        await h.submit.execute(h.command())

        assert h.db.locked_users == ["user-1"]

    async def test_invalid_image_is_rejected_without_side_effects(self) -> None:
        h = Harness(inspector=RejectingInspector())

        with pytest.raises(InvalidImage):
            await h.submit.execute(h.command())

        assert not h.db.rows and not h.db.outbox and not h.storage.files


class TestQuota:
    async def test_rejects_when_the_daily_limit_is_spent(self, h: Harness) -> None:
        h.completed(500_000)

        with pytest.raises(QuotaExceeded) as error:
            await h.submit.execute(h.command())

        assert error.value.code is ErrorCode.QUOTA_EXCEEDED
        assert not h.db.outbox and not h.storage.files

    async def test_yesterdays_spend_does_not_count(self, h: Harness) -> None:
        h.completed(500_000, at=datetime(2026, 10, 12, 23, 59, tzinfo=UTC))

        result = await h.submit.execute(h.command())

        assert result.created is True

    async def test_other_users_spend_does_not_count(self, h: Harness) -> None:
        h.completed(500_000, user_id="someone-else")

        assert (await h.submit.execute(h.command())).created is True

    async def test_failed_analyses_do_not_count(self, h: Harness) -> None:
        h.db.seed(
            make_analysis(
                uuid4(), status=AnalysisStatus.FAILED, tokens_used=500_000, completed_at=NOW
            )
        )

        assert (await h.submit.execute(h.command())).created is True

    async def test_running_analyses_reserve_tokens(self, h: Harness) -> None:
        h.completed(460_000)
        await h.submit.execute(h.command())  # reserves 20k -> 480k committed
        await h.submit.execute(h.command())  # reserves 20k -> 500k committed

        with pytest.raises(QuotaExceeded) as error:
            await h.submit.execute(h.command())

        assert error.value.code is ErrorCode.QUOTA_EXCEEDED
        assert len(h.db.outbox) == 2

    async def test_caps_concurrent_analyses_per_user(self, h: Harness) -> None:
        for _ in range(3):
            await h.submit.execute(h.command())

        with pytest.raises(QuotaExceeded) as error:
            await h.submit.execute(h.command())

        assert error.value.code is ErrorCode.TOO_MANY_ACTIVE_ANALYSES


class TestIdempotency:
    async def test_same_key_returns_the_same_analysis(self, h: Harness) -> None:
        first = await h.submit.execute(h.command(idempotency_key="tap-1"))
        second = await h.submit.execute(h.command(idempotency_key="tap-1"))

        assert second.created is False
        assert second.analysis.id == first.analysis.id
        assert len(h.db.rows) == 1 and len(h.db.outbox) == 1 and len(h.storage.files) == 1

    async def test_replay_is_served_even_when_the_quota_is_now_spent(self, h: Harness) -> None:
        first = await h.submit.execute(h.command(idempotency_key="tap-1"))
        h.completed(500_000, at=NOW + timedelta(minutes=1))

        replay = await h.submit.execute(h.command(idempotency_key="tap-1"))

        assert replay.analysis.id == first.analysis.id

    async def test_keys_are_scoped_to_the_user(self, h: Harness) -> None:
        mine = await h.submit.execute(h.command(idempotency_key="tap-1"))
        theirs = await h.submit.execute(h.command(user_id="user-2", idempotency_key="tap-1"))

        assert theirs.created is True
        assert theirs.analysis.id != mine.analysis.id

    async def test_requests_without_a_key_are_always_new(self, h: Harness) -> None:
        first = await h.submit.execute(h.command())
        second = await h.submit.execute(h.command())

        assert first.analysis.id != second.analysis.id
