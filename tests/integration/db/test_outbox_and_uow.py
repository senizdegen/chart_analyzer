"""SqlAlchemyOutboxRepository and the transaction boundaries of SqlAlchemyUnitOfWork."""

import asyncio
from uuid import UUID, uuid4

import pytest

from chart_analyzer.domain.analysis import NewAnalysis, StoredImage
from chart_analyzer.domain.enums import Language
from tests.fakes import NOW
from tests.integration.db.conftest import LEASE, UowFactory, add_analysis

pytestmark = pytest.mark.integration


async def add_messages(uow_factory: UowFactory, count: int) -> list[UUID]:
    analysis_ids = [uuid4() for _ in range(count)]
    async with uow_factory() as uow:
        for analysis_id in analysis_ids:
            await uow.outbox.add(analysis_id, now=NOW)
        await uow.commit()
    return analysis_ids


# --- outbox -----------------------------------------------------------------------------------


async def test_fetch_unsent_returns_messages_in_insertion_order(uow_factory: UowFactory) -> None:
    analysis_ids = await add_messages(uow_factory, 5)

    async with uow_factory() as uow:
        batch = await uow.outbox.fetch_unsent(3)
    async with uow_factory() as uow:
        everything = await uow.outbox.fetch_unsent(50)

    assert [message.analysis_id for message in batch] == analysis_ids[:3]
    assert [message.analysis_id for message in everything] == analysis_ids
    assert [message.id for message in everything] == sorted(m.id for m in everything)


async def test_mark_sent_hides_messages(uow_factory: UowFactory) -> None:
    analysis_ids = await add_messages(uow_factory, 4)

    async with uow_factory() as uow:
        batch = await uow.outbox.fetch_unsent(2)
        await uow.outbox.mark_sent([message.id for message in batch], now=NOW)
        await uow.outbox.mark_sent([], now=NOW)  # an empty batch is a no-op
        await uow.commit()

    async with uow_factory() as uow:
        remaining = await uow.outbox.fetch_unsent(50)
    assert [message.analysis_id for message in remaining] == analysis_ids[2:]


async def test_two_open_transactions_fetch_disjoint_batches(uow_factory: UowFactory) -> None:
    analysis_ids = await add_messages(uow_factory, 6)

    async with uow_factory() as first, uow_factory() as second:
        first_batch = await first.outbox.fetch_unsent(3)
        second_batch = await second.outbox.fetch_unsent(3)  # must not wait for the first
        async with uow_factory() as third:
            assert await third.outbox.fetch_unsent(3) == []  # everything is held

    assert [message.analysis_id for message in first_batch] == analysis_ids[:3]
    assert [message.analysis_id for message in second_batch] == analysis_ids[3:]

    # nobody committed, so the locks are gone and every message is still unsent
    async with uow_factory() as uow:
        assert len(await uow.outbox.fetch_unsent(50)) == 6


# --- unit of work -----------------------------------------------------------------------------


def new_analysis() -> NewAnalysis:
    analysis_id = uuid4()
    return NewAnalysis(
        id=analysis_id,
        user_id="user-1",
        language=Language.EN,
        image=StoredImage(key=f"{analysis_id}.png", content_type="image/png"),
        idempotency_key=None,
        created_at=NOW,
    )


async def test_commit_persists_the_analysis_and_its_outbox_row_together(
    uow_factory: UowFactory,
) -> None:
    new = new_analysis()

    async with uow_factory() as uow:
        await uow.analyses.add(new)
        await uow.outbox.add(new.id, now=NOW)
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.analyses.get(new.id) is not None
        assert [message.analysis_id for message in await uow.outbox.fetch_unsent(10)] == [new.id]


async def test_leaving_the_block_without_commit_rolls_back(uow_factory: UowFactory) -> None:
    new = new_analysis()

    async with uow_factory() as uow:
        await uow.analyses.add(new)
        await uow.outbox.add(new.id, now=NOW)
        assert await uow.analyses.get(new.id) is not None  # visible inside the transaction

    async with uow_factory() as uow:
        assert await uow.analyses.get(new.id) is None
        assert await uow.outbox.fetch_unsent(10) == []


async def test_an_exception_rolls_back_and_propagates(uow_factory: UowFactory) -> None:
    new = new_analysis()

    with pytest.raises(RuntimeError, match="boom"):
        async with uow_factory() as uow:
            await uow.analyses.add(new)
            raise RuntimeError("boom")

    async with uow_factory() as uow:
        assert await uow.analyses.get(new.id) is None


async def test_work_after_commit_belongs_to_a_new_transaction(uow_factory: UowFactory) -> None:
    kept = await add_analysis(uow_factory)
    dropped = new_analysis()

    async with uow_factory() as uow:
        await uow.analyses.claim(kept.id, now=NOW, lease=LEASE)
        await uow.commit()
        await uow.analyses.add(dropped)  # never committed

    async with uow_factory() as uow:
        stored = await uow.analyses.get(kept.id)
        assert stored is not None
        assert stored.attempts == 1
        assert await uow.analyses.get(dropped.id) is None


async def test_a_unit_of_work_cannot_be_used_outside_its_block(uow_factory: UowFactory) -> None:
    uow = uow_factory()

    with pytest.raises(RuntimeError):
        await uow.commit()


async def test_lock_user_blocks_the_same_user_until_the_transaction_ends(
    uow_factory: UowFactory,
) -> None:
    acquired = asyncio.Event()

    async def contender() -> None:
        async with uow_factory() as uow:
            await uow.lock_user("user-1")
            acquired.set()

    async with uow_factory() as holder:
        await holder.lock_user("user-1")
        async with uow_factory() as other_user:
            await other_user.lock_user("user-2")  # a different user is not blocked
        task = asyncio.create_task(contender())
        await asyncio.sleep(0.3)
        assert not acquired.is_set()

    # the holder never committed: the rollback released the lock as well
    await asyncio.wait_for(task, timeout=5)
    assert acquired.is_set()
