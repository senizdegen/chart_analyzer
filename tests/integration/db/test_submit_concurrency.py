"""The real SubmitAnalysis use case on Postgres under concurrent requests.

This is what the per-user advisory lock exists for: without it every request would read
the same quota usage before any of them inserted, and all of them would be admitted.
"""

import asyncio

import pytest

from chart_analyzer.application.submit_analysis import (
    SubmitAnalysis,
    SubmitAnalysisCommand,
    SubmitAnalysisResult,
)
from chart_analyzer.domain.enums import ErrorCode, Language
from chart_analyzer.domain.errors import QuotaExceeded
from chart_analyzer.domain.quota import QuotaPolicy, QuotaUsage, utc_day_start
from tests.fakes import NOW, FakeInspector, FixedClock, InMemoryImageStorage
from tests.integration.db.conftest import UowFactory, add_completed

pytestmark = pytest.mark.integration

# max_active_analyses is out of the way, so only the token budget can reject
POLICY = QuotaPolicy(daily_limit=500_000, reservation_tokens=20_000, max_active_analyses=50)


def build_use_case(uow_factory: UowFactory, storage: InMemoryImageStorage) -> SubmitAnalysis:
    return SubmitAnalysis(uow_factory, storage, FakeInspector(), POLICY, FixedClock())


def command(idempotency_key: str | None = None) -> SubmitAnalysisCommand:
    return SubmitAnalysisCommand(
        user_id="user-1",
        image_bytes=b"chart",
        language=Language.EN,
        idempotency_key=idempotency_key,
    )


async def test_concurrent_submissions_cannot_exceed_the_daily_limit(
    uow_factory: UowFactory,
) -> None:
    # 460,000 used: there is room for exactly two more reservations of 20,000
    await add_completed(uow_factory, tokens=460_000)
    storage = InMemoryImageStorage()
    submit = build_use_case(uow_factory, storage)

    outcomes = await asyncio.gather(
        *(submit.execute(command()) for _ in range(30)), return_exceptions=True
    )

    admitted = [outcome for outcome in outcomes if isinstance(outcome, SubmitAnalysisResult)]
    rejected = [outcome for outcome in outcomes if isinstance(outcome, QuotaExceeded)]
    assert len(admitted) == 2
    assert len(rejected) == 28
    # 460,000 spent would still admit one analysis: what blocks the rest is the two
    # reservations, so they are told to wait for those, not for tomorrow.
    assert {error.code for error in rejected} == {ErrorCode.TOO_MANY_ACTIVE_ANALYSES}
    assert all(result.created for result in admitted)

    async with uow_factory() as uow:
        usage = await uow.analyses.get_quota_usage("user-1", utc_day_start(NOW))
        messages = await uow.outbox.fetch_unsent(100)
    assert usage == QuotaUsage(used_tokens=460_000, active_analyses=2)
    assert {message.analysis_id for message in messages} == {r.analysis.id for r in admitted}
    assert len(messages) == 2
    # images of the rejected requests were removed again
    assert set(storage.files) == {result.analysis.image.key for result in admitted}


async def test_concurrent_submissions_after_the_limit_is_spent_are_all_rejected(
    uow_factory: UowFactory,
) -> None:
    await add_completed(uow_factory, tokens=480_001)  # not even one reservation fits
    storage = InMemoryImageStorage()
    submit = build_use_case(uow_factory, storage)

    outcomes = await asyncio.gather(
        *(submit.execute(command()) for _ in range(10)), return_exceptions=True
    )

    assert all(isinstance(outcome, QuotaExceeded) for outcome in outcomes)
    assert {outcome.code for outcome in outcomes if isinstance(outcome, QuotaExceeded)} == {
        ErrorCode.QUOTA_EXCEEDED
    }
    assert not storage.files


async def test_concurrent_submissions_with_one_idempotency_key_create_one_analysis(
    uow_factory: UowFactory,
) -> None:
    storage = InMemoryImageStorage()
    submit = build_use_case(uow_factory, storage)

    results = await asyncio.gather(*(submit.execute(command("key-1")) for _ in range(15)))

    assert len({result.analysis.id for result in results}) == 1
    assert [result.created for result in results].count(True) == 1

    async with uow_factory() as uow:
        analyses, total = await uow.analyses.list_for_user("user-1", limit=100, offset=0)
        messages = await uow.outbox.fetch_unsent(100)
    assert total == 1
    assert [message.analysis_id for message in messages] == [analyses[0].id]
    assert set(storage.files) == {analyses[0].image.key}
