"""SqlAlchemyAnalysisRepository against a real Postgres: storage, quota query, state changes."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from chart_analyzer.application.ports.repositories import ClaimStatus
from chart_analyzer.domain.enums import AnalysisStatus, ErrorCode, Language, Stage
from chart_analyzer.domain.quota import QuotaUsage, utc_day_start
from tests.fakes import NOW, make_recognition, make_snapshot
from tests.integration.db.conftest import (
    LEASE,
    UowFactory,
    add_analysis,
    add_completed,
    add_processing,
    load,
    make_result,
)

pytestmark = pytest.mark.integration

DAY_START = utc_day_start(NOW)


# --- add / read -------------------------------------------------------------------------------


async def test_add_stores_a_queued_analysis(uow_factory: UowFactory) -> None:
    added = await add_analysis(uow_factory, idempotency_key="key-1")

    stored = await load(uow_factory, added.id)

    assert stored == added
    assert stored.user_id == "user-1"
    assert stored.language is Language.EN
    assert stored.status is AnalysisStatus.QUEUED
    assert stored.stage is Stage.QUEUED
    assert stored.image.key == f"{added.id}.png"
    assert stored.image.content_type == "image/png"
    assert stored.idempotency_key == "key-1"
    assert (stored.recognition, stored.market_snapshot, stored.result) == (None, None, None)
    assert stored.error_code is None
    assert (stored.tokens_used, stored.attempts) == (0, 0)
    assert stored.completed_at is None


async def test_timestamps_are_timezone_aware_utc(uow_factory: UowFactory) -> None:
    stored = await add_completed(uow_factory, tokens=10)

    assert stored.created_at == NOW
    assert stored.completed_at == NOW
    for value in (stored.created_at, stored.updated_at, stored.completed_at):
        assert value is not None
        assert value.utcoffset() == timedelta(0)


async def test_get_returns_none_for_an_unknown_id(uow_factory: UowFactory) -> None:
    async with uow_factory() as uow:
        assert await uow.analyses.get(uuid4()) is None


async def test_get_for_user_hides_other_users_analyses(uow_factory: UowFactory) -> None:
    mine = await add_analysis(uow_factory, user_id="user-1")

    async with uow_factory() as uow:
        assert await uow.analyses.get_for_user(mine.id, "user-1") == mine
        assert await uow.analyses.get_for_user(mine.id, "user-2") is None


async def test_list_for_user_is_newest_first_with_pagination_and_total(
    uow_factory: UowFactory,
) -> None:
    mine = [
        await add_analysis(uow_factory, created_at=NOW + timedelta(minutes=minute))
        for minute in range(5)
    ]
    await add_analysis(uow_factory, user_id="user-2")
    newest_first = [analysis.id for analysis in reversed(mine)]

    async with uow_factory() as uow:
        first_page, total = await uow.analyses.list_for_user("user-1", limit=2, offset=0)
        second_page, _ = await uow.analyses.list_for_user("user-1", limit=2, offset=2)
        last_page, _ = await uow.analyses.list_for_user("user-1", limit=2, offset=4)
        beyond, total_beyond = await uow.analyses.list_for_user("user-1", limit=2, offset=10)
        nobody, total_nobody = await uow.analyses.list_for_user("user-3", limit=2, offset=0)

    assert total == 5
    assert [analysis.id for analysis in first_page] == newest_first[:2]
    assert [analysis.id for analysis in second_page] == newest_first[2:4]
    assert [analysis.id for analysis in last_page] == newest_first[4:]
    assert (list(beyond), total_beyond) == ([], 5)
    assert (list(nobody), total_nobody) == ([], 0)


# --- JSONB checkpoints ------------------------------------------------------------------------


async def test_jsonb_round_trip_of_recognition_snapshot_and_result(
    uow_factory: UowFactory,
) -> None:
    analysis = await add_processing(uow_factory)
    recognition, snapshot, result = make_recognition(), make_snapshot(), make_result()

    async with uow_factory() as uow:
        await uow.analyses.save_recognition(analysis.id, recognition, tokens=1_200)
        await uow.analyses.save_market_snapshot(analysis.id, snapshot)
        assert await uow.analyses.complete(analysis.id, result, tokens=6_800, now=NOW)
        await uow.commit()

    stored = await load(uow_factory, analysis.id)
    assert stored.recognition == recognition
    assert stored.market_snapshot == snapshot
    assert stored.market_snapshot is not None
    assert stored.market_snapshot.price.as_of == NOW
    assert stored.result == result
    assert stored.result is not None
    assert stored.result.latest_news  # nested lists with datetimes survive the round trip


# --- idempotency key --------------------------------------------------------------------------


async def test_find_by_idempotency_key(uow_factory: UowFactory) -> None:
    mine = await add_analysis(uow_factory, user_id="user-1", idempotency_key="key-1")

    async with uow_factory() as uow:
        assert await uow.analyses.find_by_idempotency_key("user-1", "key-1") == mine
        assert await uow.analyses.find_by_idempotency_key("user-1", "key-2") is None
        assert await uow.analyses.find_by_idempotency_key("user-2", "key-1") is None


async def test_duplicate_idempotency_key_of_one_user_is_rejected(uow_factory: UowFactory) -> None:
    await add_analysis(uow_factory, idempotency_key="key-1")

    with pytest.raises(IntegrityError):
        await add_analysis(uow_factory, idempotency_key="key-1")

    async with uow_factory() as uow:
        _, total = await uow.analyses.list_for_user("user-1", limit=10, offset=0)
    assert total == 1


async def test_same_idempotency_key_for_two_users_is_fine(uow_factory: UowFactory) -> None:
    first = await add_analysis(uow_factory, user_id="user-1", idempotency_key="key-1")
    second = await add_analysis(uow_factory, user_id="user-2", idempotency_key="key-1")

    assert first.id != second.id


async def test_null_idempotency_keys_never_collide(uow_factory: UowFactory) -> None:
    await add_analysis(uow_factory)
    await add_analysis(uow_factory)

    async with uow_factory() as uow:
        _, total = await uow.analyses.list_for_user("user-1", limit=10, offset=0)
    assert total == 2


# --- quota usage ------------------------------------------------------------------------------


async def test_quota_usage_of_a_new_user_is_zero(uow_factory: UowFactory) -> None:
    async with uow_factory() as uow:
        usage = await uow.analyses.get_quota_usage("user-1", DAY_START)

    assert usage == QuotaUsage(used_tokens=0, active_analyses=0)


async def test_quota_usage_counts_todays_completed_tokens_and_active_analyses(
    uow_factory: UowFactory,
) -> None:
    await add_completed(uow_factory, tokens=8_000)
    await add_completed(uow_factory, tokens=7_000, at=DAY_START)  # the boundary is inclusive
    await add_completed(uow_factory, tokens=100_000, at=DAY_START - timedelta(seconds=1))

    failed = await add_processing(uow_factory)
    async with uow_factory() as uow:
        await uow.analyses.add_tokens(failed.id, 5_000)
        assert await uow.analyses.fail(failed.id, ErrorCode.UNCLEAR_CHART, now=NOW)
        await uow.commit()

    await add_analysis(uow_factory)  # queued
    running = await add_processing(uow_factory)
    async with uow_factory() as uow:
        await uow.analyses.add_tokens(running.id, 1_200)  # not counted until it completes
        await uow.commit()

    await add_completed(uow_factory, user_id="user-2", tokens=50_000)
    await add_analysis(uow_factory, user_id="user-2")

    async with uow_factory() as uow:
        usage = await uow.analyses.get_quota_usage("user-1", DAY_START)

    assert usage == QuotaUsage(used_tokens=15_000, active_analyses=2)


# --- claim ------------------------------------------------------------------------------------


async def test_claim_takes_a_queued_analysis(uow_factory: UowFactory) -> None:
    analysis = await add_analysis(uow_factory)

    async with uow_factory() as uow:
        result = await uow.analyses.claim(analysis.id, now=NOW, lease=LEASE)
        await uow.commit()

    assert result.status is ClaimStatus.CLAIMED
    assert result.analysis is not None
    assert result.analysis.status is AnalysisStatus.PROCESSING
    assert result.analysis.attempts == 1
    assert result.analysis.updated_at != analysis.updated_at
    assert await load(uow_factory, analysis.id) == result.analysis


async def test_claim_is_busy_while_the_lease_is_fresh(uow_factory: UowFactory) -> None:
    analysis = await add_processing(uow_factory)

    async with uow_factory() as uow:
        result = await uow.analyses.claim(analysis.id, now=NOW + LEASE, lease=LEASE)
        await uow.commit()

    assert result.status is ClaimStatus.BUSY
    assert result.analysis is None
    assert (await load(uow_factory, analysis.id)).attempts == 1


async def test_claim_takes_over_an_expired_lease(uow_factory: UowFactory) -> None:
    analysis = await add_processing(uow_factory)
    later = NOW + LEASE + timedelta(seconds=1)

    async with uow_factory() as uow:
        result = await uow.analyses.claim(analysis.id, now=later, lease=LEASE)
        await uow.commit()

    assert result.status is ClaimStatus.CLAIMED
    assert result.analysis is not None
    assert result.analysis.attempts == 2

    # the takeover started a new lease
    async with uow_factory() as uow:
        again = await uow.analyses.claim(analysis.id, now=later + LEASE, lease=LEASE)
    assert again.status is ClaimStatus.BUSY


async def test_claim_of_a_finished_analysis_reports_finished(uow_factory: UowFactory) -> None:
    completed = await add_completed(uow_factory, tokens=10)
    failed = await add_analysis(uow_factory)
    async with uow_factory() as uow:
        assert await uow.analyses.fail(failed.id, ErrorCode.INTERNAL_ERROR, now=NOW)
        await uow.commit()

    async with uow_factory() as uow:
        for analysis_id in (completed.id, failed.id):
            # even long after the lease: a finished analysis is never taken again
            result = await uow.analyses.claim(analysis_id, now=NOW + 10 * LEASE, lease=LEASE)
            assert result.status is ClaimStatus.FINISHED
            assert result.analysis is None


async def test_claim_of_an_unknown_id_reports_missing(uow_factory: UowFactory) -> None:
    async with uow_factory() as uow:
        result = await uow.analyses.claim(uuid4(), now=NOW, lease=LEASE)

    assert result.status is ClaimStatus.MISSING


async def test_concurrent_claims_have_exactly_one_winner(uow_factory: UowFactory) -> None:
    analysis = await add_analysis(uow_factory)

    async def claim() -> ClaimStatus:
        async with uow_factory() as uow:
            result = await uow.analyses.claim(analysis.id, now=NOW, lease=LEASE)
            await uow.commit()
        return result.status

    statuses = await asyncio.gather(*(claim() for _ in range(20)))

    assert statuses.count(ClaimStatus.CLAIMED) == 1
    assert statuses.count(ClaimStatus.BUSY) == 19
    assert (await load(uow_factory, analysis.id)).attempts == 1


# --- progress and completion ------------------------------------------------------------------


async def test_set_stage(uow_factory: UowFactory) -> None:
    analysis = await add_processing(uow_factory)

    async with uow_factory() as uow:
        await uow.analyses.set_stage(analysis.id, Stage.MARKET_DATA)
        await uow.commit()

    assert (await load(uow_factory, analysis.id)).stage is Stage.MARKET_DATA


async def test_tokens_accumulate_over_checkpoints_and_completion(uow_factory: UowFactory) -> None:
    analysis = await add_processing(uow_factory)
    finished_at = NOW + timedelta(seconds=12)

    async with uow_factory() as uow:
        await uow.analyses.save_recognition(analysis.id, make_recognition(), tokens=1_200)
        await uow.analyses.add_tokens(analysis.id, 300)
        completed = await uow.analyses.complete(
            analysis.id, make_result(), tokens=6_800, now=finished_at
        )
        await uow.commit()

    stored = await load(uow_factory, analysis.id)
    assert completed is True
    assert stored.tokens_used == 8_300
    assert stored.status is AnalysisStatus.COMPLETED
    assert stored.stage is Stage.DONE
    assert stored.completed_at == finished_at
    assert stored.error_code is None


async def test_complete_of_a_non_processing_analysis_changes_nothing(
    uow_factory: UowFactory,
) -> None:
    queued = await add_analysis(uow_factory)
    done = await add_completed(uow_factory, tokens=500)

    async with uow_factory() as uow:
        assert await uow.analyses.complete(queued.id, make_result(), tokens=9, now=NOW) is False
        assert await uow.analyses.complete(done.id, make_result(), tokens=9, now=NOW) is False
        await uow.commit()

    assert await load(uow_factory, queued.id) == queued
    assert await load(uow_factory, done.id) == done


async def test_fail_finishes_an_active_analysis(uow_factory: UowFactory) -> None:
    analysis = await add_processing(uow_factory)
    failed_at = NOW + timedelta(seconds=5)

    async with uow_factory() as uow:
        failed = await uow.analyses.fail(analysis.id, ErrorCode.ASSET_NOT_FOUND, now=failed_at)
        await uow.commit()

    stored = await load(uow_factory, analysis.id)
    assert failed is True
    assert stored.status is AnalysisStatus.FAILED
    assert stored.stage is Stage.DONE
    assert stored.error_code is ErrorCode.ASSET_NOT_FOUND
    assert stored.completed_at == failed_at
    assert stored.result is None


async def test_fail_of_a_finished_analysis_changes_nothing(uow_factory: UowFactory) -> None:
    done = await add_completed(uow_factory, tokens=500)

    async with uow_factory() as uow:
        assert await uow.analyses.fail(done.id, ErrorCode.INTERNAL_ERROR, now=NOW) is False
        await uow.commit()

    assert await load(uow_factory, done.id) == done


async def test_release_returns_a_claimed_analysis_to_the_queue(uow_factory: UowFactory) -> None:
    analysis = await add_processing(uow_factory)

    async with uow_factory() as uow:
        await uow.analyses.release(analysis.id)
        await uow.commit()

    assert (await load(uow_factory, analysis.id)).status is AnalysisStatus.QUEUED

    # the lease is gone as well: the next claim succeeds at once and counts as a new attempt
    async with uow_factory() as uow:
        result = await uow.analyses.claim(analysis.id, now=NOW, lease=LEASE)
        await uow.commit()
    assert result.status is ClaimStatus.CLAIMED
    assert result.analysis is not None
    assert result.analysis.attempts == 2


async def test_release_leaves_a_finished_analysis_alone(uow_factory: UowFactory) -> None:
    done = await add_completed(uow_factory, tokens=500)

    async with uow_factory() as uow:
        await uow.analyses.release(done.id)
        await uow.commit()

    assert await load(uow_factory, done.id) == done
