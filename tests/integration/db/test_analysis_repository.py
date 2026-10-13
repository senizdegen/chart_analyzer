"""SqlAlchemyAnalysisRepository against a real Postgres: storage, quota query, state changes."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chart_analyzer.application.ports.repositories import ClaimStatus
from chart_analyzer.application.reap_stale import ReapStaleAnalyses
from chart_analyzer.domain.analysis import Analysis
from chart_analyzer.domain.enums import AnalysisStatus, ErrorCode, Language, Stage
from chart_analyzer.domain.quota import QuotaUsage, utc_day_start
from tests.fakes import NOW, FixedClock, RecordingProgress, make_recognition, make_snapshot
from tests.integration.db.conftest import (
    LEASE,
    UowFactory,
    add_analysis,
    add_completed,
    add_processing,
    load,
    make_result,
    take_over,
)

pytestmark = pytest.mark.integration

DAY_START = utc_day_start(NOW)
EXPIRED = NOW + LEASE + timedelta(seconds=1)  # the lease of a claim made at NOW is over


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
        assert await uow.analyses.complete(
            analysis.id, result, tokens=6_800, now=NOW, attempt=analysis.attempts
        )
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
        await uow.analyses.save_recognition(failed.id, make_recognition(), tokens=5_000)
        assert await uow.analyses.fail(failed.id, ErrorCode.UNCLEAR_CHART, now=NOW)
        await uow.commit()

    await add_analysis(uow_factory)  # queued
    running = await add_processing(uow_factory)
    async with uow_factory() as uow:
        # not counted until it completes
        await uow.analyses.save_recognition(running.id, make_recognition(), tokens=1_200)
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
        completed = await uow.analyses.complete(
            analysis.id, make_result(), tokens=6_800, now=finished_at, attempt=analysis.attempts
        )
        await uow.commit()

    stored = await load(uow_factory, analysis.id)
    assert completed is True
    assert stored.tokens_used == 8_000
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
        for analysis in (queued, done):
            completed = await uow.analyses.complete(
                analysis.id, make_result(), tokens=9, now=NOW, attempt=analysis.attempts
            )
            assert completed is False
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
        await uow.analyses.release(analysis.id, attempt=analysis.attempts)
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
        await uow.analyses.release(done.id, attempt=done.attempts)
        await uow.commit()

    assert await load(uow_factory, done.id) == done


# --- fencing: a claim that was taken over can no longer write ---------------------------------


async def test_complete_with_a_stale_attempt_changes_nothing(uow_factory: UowFactory) -> None:
    stale = await add_processing(uow_factory)
    current = await take_over(uow_factory, stale, at=EXPIRED)

    async with uow_factory() as uow:
        completed = await uow.analyses.complete(
            stale.id, make_result(), tokens=6_800, now=EXPIRED, attempt=stale.attempts
        )
        await uow.commit()

    assert completed is False
    assert await load(uow_factory, stale.id) == current

    # the worker that holds the claim now is not affected
    async with uow_factory() as uow:
        completed = await uow.analyses.complete(
            stale.id, make_result(), tokens=6_800, now=EXPIRED, attempt=current.attempts
        )
        await uow.commit()
    stored = await load(uow_factory, stale.id)
    assert completed is True
    assert (stored.status, stored.tokens_used) == (AnalysisStatus.COMPLETED, 6_800)


async def test_complete_with_an_attempt_from_the_future_changes_nothing(
    uow_factory: UowFactory,
) -> None:
    analysis = await add_processing(uow_factory)

    async with uow_factory() as uow:
        completed = await uow.analyses.complete(
            analysis.id, make_result(), tokens=1, now=NOW, attempt=analysis.attempts + 1
        )
        await uow.commit()

    assert completed is False
    assert await load(uow_factory, analysis.id) == analysis


async def test_release_with_a_stale_attempt_keeps_the_new_claim(uow_factory: UowFactory) -> None:
    stale = await add_processing(uow_factory)
    current = await take_over(uow_factory, stale, at=EXPIRED)

    async with uow_factory() as uow:
        await uow.analyses.release(stale.id, attempt=stale.attempts)
        await uow.commit()

    assert await load(uow_factory, stale.id) == current
    # the lease of the new claim is intact: nobody else can take the analysis
    async with uow_factory() as uow:
        again = await uow.analyses.claim(stale.id, now=EXPIRED, lease=LEASE)
    assert again.status is ClaimStatus.BUSY


async def test_fenced_fail_with_a_stale_attempt_changes_nothing(uow_factory: UowFactory) -> None:
    stale = await add_processing(uow_factory)
    current = await take_over(uow_factory, stale, at=EXPIRED)

    async with uow_factory() as uow:
        failed = await uow.analyses.fail(
            stale.id, ErrorCode.UNCLEAR_CHART, now=EXPIRED, attempt=stale.attempts
        )
        await uow.commit()

    assert failed is False
    assert await load(uow_factory, stale.id) == current


async def test_fenced_fail_with_the_current_attempt_finishes_the_analysis(
    uow_factory: UowFactory,
) -> None:
    stale = await add_processing(uow_factory)
    current = await take_over(uow_factory, stale, at=EXPIRED)

    async with uow_factory() as uow:
        failed = await uow.analyses.fail(
            stale.id, ErrorCode.UNCLEAR_CHART, now=EXPIRED, attempt=current.attempts
        )
        await uow.commit()

    stored = await load(uow_factory, stale.id)
    assert failed is True
    assert (stored.status, stored.error_code) == (AnalysisStatus.FAILED, ErrorCode.UNCLEAR_CHART)
    assert stored.stage is Stage.DONE


async def test_fenced_fail_needs_the_analysis_to_be_in_progress(uow_factory: UowFactory) -> None:
    analysis = await add_processing(uow_factory)
    async with uow_factory() as uow:
        await uow.analyses.release(analysis.id, attempt=analysis.attempts)
        await uow.commit()
    released = await load(uow_factory, analysis.id)

    async with uow_factory() as uow:
        # the attempt number still matches, but the claim was given back
        fenced = await uow.analyses.fail(
            analysis.id, ErrorCode.UNCLEAR_CHART, now=NOW, attempt=analysis.attempts
        )
        await uow.commit()
    assert fenced is False
    assert await load(uow_factory, analysis.id) == released

    async with uow_factory() as uow:
        # without an attempt (retries exhausted) any active analysis is failed
        unfenced = await uow.analyses.fail(analysis.id, ErrorCode.INTERNAL_ERROR, now=NOW)
        await uow.commit()
    assert unfenced is True
    assert (await load(uow_factory, analysis.id)).status is AnalysisStatus.FAILED


# --- checkpoints are written only while the analysis is in progress ---------------------------


async def _write_every_checkpoint(uow_factory: UowFactory, analysis_id: UUID) -> None:
    async with uow_factory() as uow:
        await uow.analyses.set_stage(analysis_id, Stage.MARKET_DATA)
        await uow.analyses.save_recognition(
            analysis_id, make_recognition(symbol="LATE"), tokens=999
        )
        await uow.analyses.save_market_snapshot(analysis_id, make_snapshot(news=[]))
        await uow.commit()


async def test_checkpoints_are_stored_while_the_analysis_is_in_progress(
    uow_factory: UowFactory,
) -> None:
    analysis = await add_processing(uow_factory)

    await _write_every_checkpoint(uow_factory, analysis.id)

    stored = await load(uow_factory, analysis.id)
    assert stored.stage is Stage.MARKET_DATA
    assert stored.tokens_used == 999
    assert stored.recognition == make_recognition(symbol="LATE")
    assert stored.market_snapshot == make_snapshot(news=[])


async def test_late_checkpoints_do_not_alter_a_completed_analysis(
    uow_factory: UowFactory,
) -> None:
    done = await add_completed(uow_factory, tokens=500)

    await _write_every_checkpoint(uow_factory, done.id)

    stored = await load(uow_factory, done.id)
    assert stored == done
    assert (stored.stage, stored.tokens_used) == (Stage.DONE, 500)
    assert (stored.recognition, stored.market_snapshot) == (None, None)


async def test_late_checkpoints_do_not_alter_a_failed_analysis(uow_factory: UowFactory) -> None:
    analysis = await add_processing(uow_factory)
    async with uow_factory() as uow:
        assert await uow.analyses.fail(analysis.id, ErrorCode.ASSET_NOT_FOUND, now=NOW)
        await uow.commit()
    failed = await load(uow_factory, analysis.id)

    await _write_every_checkpoint(uow_factory, analysis.id)

    assert await load(uow_factory, analysis.id) == failed


async def test_late_checkpoints_do_not_alter_a_requeued_analysis(uow_factory: UowFactory) -> None:
    analysis = await add_processing(uow_factory)
    async with uow_factory() as uow:
        await uow.analyses.release(analysis.id, attempt=analysis.attempts)
        await uow.commit()
    queued = await load(uow_factory, analysis.id)

    await _write_every_checkpoint(uow_factory, analysis.id)

    assert await load(uow_factory, analysis.id) == queued


# --- stale analyses ---------------------------------------------------------------------------

STALE_AFTER = timedelta(minutes=30)


async def _touched(
    session_factory: async_sessionmaker[AsyncSession], analysis: Analysis, *, ago: timedelta
) -> Analysis:
    """``updated_at`` is set by the database clock; move it back as if time had passed."""
    async with session_factory() as session:
        await session.execute(
            text("UPDATE analyses SET updated_at = now() - CAST(:ago AS interval) WHERE id = :id"),
            {"ago": ago, "id": analysis.id},
        )
        await session.commit()
    async with session_factory() as session:
        row = (
            await session.execute(
                text("SELECT updated_at FROM analyses WHERE id = :id"), {"id": analysis.id}
            )
        ).one()
    updated: datetime = row.updated_at
    assert datetime.now(UTC) - updated >= ago - timedelta(minutes=1)
    return analysis


async def _locked_at(
    session_factory: async_sessionmaker[AsyncSession], analysis_id: UUID
) -> datetime | None:
    async with session_factory() as session:
        row = (
            await session.execute(
                text("SELECT locked_at FROM analyses WHERE id = :id"), {"id": analysis_id}
            )
        ).one()
    locked: datetime | None = row.locked_at
    return locked


async def test_fail_stale_fails_only_active_analyses_untouched_since_the_cutoff(
    uow_factory: UowFactory, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    hour = timedelta(hours=1)
    stale_queued = await _touched(session_factory, await add_analysis(uow_factory), ago=hour)
    stale_processing = await _touched(session_factory, await add_processing(uow_factory), ago=hour)
    other_users = await _touched(
        session_factory, await add_processing(uow_factory, user_id="user-2"), ago=hour
    )
    fresh_queued = await add_analysis(uow_factory)
    fresh_processing = await add_processing(uow_factory)
    barely_fresh = await _touched(
        session_factory, await add_processing(uow_factory), ago=STALE_AFTER - timedelta(minutes=5)
    )
    old_completed = await _touched(
        session_factory, await add_completed(uow_factory, tokens=500), ago=hour
    )
    old_failed = await add_processing(uow_factory)
    async with uow_factory() as uow:
        assert await uow.analyses.fail(old_failed.id, ErrorCode.UNCLEAR_CHART, now=NOW)
        await uow.commit()
    await _touched(session_factory, old_failed, ago=hour)
    untouched = {
        analysis.id: await load(uow_factory, analysis.id)
        for analysis in (fresh_queued, fresh_processing, barely_fresh, old_completed, old_failed)
    }
    assert await _locked_at(session_factory, stale_processing.id) is not None
    reaped_at = NOW + timedelta(hours=3)
    before = datetime.now(UTC)

    async with uow_factory() as uow:
        failed_ids = await uow.analyses.fail_stale(
            inactive_since=before - STALE_AFTER, code=ErrorCode.INTERNAL_ERROR, now=reaped_at
        )
        await uow.commit()

    assert set(failed_ids) == {stale_queued.id, stale_processing.id, other_users.id}
    assert len(failed_ids) == 3
    for analysis_id in failed_ids:
        stored = await load(uow_factory, analysis_id)
        assert stored.status is AnalysisStatus.FAILED
        assert stored.stage is Stage.DONE
        assert stored.error_code is ErrorCode.INTERNAL_ERROR
        assert stored.completed_at == reaped_at
        assert stored.result is None
        assert stored.updated_at >= before - timedelta(minutes=1)  # the write is recorded
        assert await _locked_at(session_factory, analysis_id) is None
    for analysis_id, analysis in untouched.items():
        assert await load(uow_factory, analysis_id) == analysis
    assert await _locked_at(session_factory, fresh_processing.id) is not None

    # reaped analyses are finished: no reservation is held and no late task can take them
    async with uow_factory() as uow:
        usage = await uow.analyses.get_quota_usage("user-2", DAY_START)
        claim = await uow.analyses.claim(stale_processing.id, now=NOW + 10 * LEASE, lease=LEASE)
        again = await uow.analyses.fail_stale(
            inactive_since=datetime.now(UTC) + timedelta(days=1),
            code=ErrorCode.AI_UNAVAILABLE,
            now=reaped_at,
        )
        # not committed: leaving the block rolls this second sweep back
    assert usage == QuotaUsage(used_tokens=0, active_analyses=0)
    assert claim.status is ClaimStatus.FINISHED
    # a second sweep, with every row older than its cutoff, finds only the still-active ones
    assert set(again) == {fresh_queued.id, fresh_processing.id, barely_fresh.id}


async def test_fail_stale_with_nothing_stale_returns_nothing(uow_factory: UowFactory) -> None:
    fresh = await add_processing(uow_factory)

    async with uow_factory() as uow:
        failed_ids = await uow.analyses.fail_stale(
            inactive_since=datetime.now(UTC) - STALE_AFTER, code=ErrorCode.INTERNAL_ERROR, now=NOW
        )
        await uow.commit()

    assert list(failed_ids) == []
    assert await load(uow_factory, fresh.id) == fresh


async def test_reaper_fails_stale_analyses_in_postgres_and_announces_them(
    uow_factory: UowFactory, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    stale = await _touched(
        session_factory, await add_processing(uow_factory), ago=timedelta(hours=1)
    )
    fresh = await add_processing(uow_factory)
    progress = RecordingProgress()
    clock = FixedClock(datetime.now(UTC))
    reaper = ReapStaleAnalyses(uow_factory, progress, clock, stale_after=STALE_AFTER)

    assert await reaper.run_once() == 1
    assert await reaper.run_once() == 0

    stored = await load(uow_factory, stale.id)
    assert (stored.status, stored.error_code) == (AnalysisStatus.FAILED, ErrorCode.INTERNAL_ERROR)
    assert stored.completed_at == clock.current
    assert await load(uow_factory, fresh.id) == fresh
    assert [(analysis_id, event.type.value) for analysis_id, event in progress.events] == [
        (stale.id, "failed")
    ]
    assert progress.events[0][1].data["error"]["code"] == "internal_error"
