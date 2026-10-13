"""Progress events on a real Redis Stream."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any
from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis

from chart_analyzer.application.ports.services import (
    ProgressEvent,
    ProgressEventType,
    StoredProgressEvent,
)
from chart_analyzer.infrastructure.events.redis_progress import RedisProgressStream

pytestmark = pytest.mark.integration

KEEPALIVE = 0.2
TIMEOUT = 5.0
STAGES = ("recognizing", "market_data", "analyzing")

Reader = AsyncIterator[StoredProgressEvent | None]


def stage(name: str) -> ProgressEvent:
    return ProgressEvent(ProgressEventType.STAGE, {"stage": name})


@pytest.fixture
def stream(redis: Redis) -> RedisProgressStream:
    return RedisProgressStream(redis, ttl_seconds=60, keepalive_seconds=KEEPALIVE)


@pytest.fixture
def analysis_id(cleanup: list[str]) -> UUID:
    new_id = uuid4()
    cleanup.append(f"analysis:{new_id}:events")
    return new_id


def text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


async def entries(redis: Redis, analysis_id: UUID) -> list[tuple[str, dict[str, str]]]:
    """What is really in the stream, whatever reply types the client is configured for."""
    raw: Any = await redis.xrange(f"analysis:{analysis_id}:events")
    return [
        (text(entry_id), {text(name): text(value) for name, value in fields.items()})
        for entry_id, fields in raw
    ]


async def publish_stages(stream: RedisProgressStream, analysis_id: UUID) -> None:
    for name in STAGES:
        await stream.publish(analysis_id, stage(name))


async def take(reader: Reader, count: int) -> list[StoredProgressEvent | None]:
    async def collect() -> list[StoredProgressEvent | None]:
        return [await anext(reader) for _ in range(count)]

    return await asyncio.wait_for(collect(), TIMEOUT)


async def take_events(reader: Reader, count: int) -> list[StoredProgressEvent]:
    items = await take(reader, count)
    assert all(item is not None for item in items)
    return [item for item in items if item is not None]


async def test_published_events_are_read_in_order_with_their_ids(
    stream: RedisProgressStream, analysis_id: UUID, redis: Redis
) -> None:
    await publish_stages(stream, analysis_id)
    completed = ProgressEvent(
        ProgressEventType.COMPLETED, {"result": {"summary": "Тренд ↑", "levels": [1.5, None]}}
    )
    await stream.publish(analysis_id, completed)

    async with aclosing(stream.read(analysis_id)) as reader:
        stored = await take_events(reader, 4)

    assert [item.event for item in stored] == [*(stage(name) for name in STAGES), completed]
    assert all(isinstance(item.id, str) for item in stored)
    in_stream = await entries(redis, analysis_id)
    assert [item.id for item in stored] == [entry_id for entry_id, _ in in_stream]


async def test_the_entry_has_the_documented_fields(
    stream: RedisProgressStream, analysis_id: UUID, redis: Redis
) -> None:
    await stream.publish(analysis_id, stage("recognizing"))

    [(_, fields)] = await entries(redis, analysis_id)

    assert fields == {"type": "stage", "data": '{"stage": "recognizing"}'}


async def test_reading_after_an_id_skips_earlier_events(
    stream: RedisProgressStream, analysis_id: UUID
) -> None:
    await publish_stages(stream, analysis_id)
    async with aclosing(stream.read(analysis_id)) as reader:
        first, second, third = await take_events(reader, 3)

    async with aclosing(stream.read(analysis_id, after=first.id)) as reader:
        resumed = await take(reader, 3)
    async with aclosing(stream.read(analysis_id, after=third.id)) as reader:
        nothing_new = await take(reader, 1)

    assert resumed == [second, third, None]
    assert nothing_new == [None]


@pytest.mark.parametrize(
    "after",
    ["", "garbage", "$", "+", "-", "123", "12-", "-12", "1-2-3", "1-2\n", "99999999999999999999-0"],
)
async def test_an_invalid_position_starts_from_the_beginning(
    stream: RedisProgressStream, analysis_id: UUID, after: str
) -> None:
    await publish_stages(stream, analysis_id)

    async with aclosing(stream.read(analysis_id, after=after)) as reader:
        stored = await take_events(reader, 3)

    assert [item.event.data["stage"] for item in stored] == list(STAGES)


async def test_a_waiting_reader_gets_keepalives_and_then_a_late_event(
    stream: RedisProgressStream, analysis_id: UUID
) -> None:
    loop = asyncio.get_running_loop()

    async with aclosing(stream.read(analysis_id)) as reader:
        started = loop.time()
        assert await take(reader, 2) == [None, None]
        waited = loop.time() - started
        assert 2 * KEEPALIVE * 0.9 <= waited < TIMEOUT

        waiting = asyncio.ensure_future(take(reader, 1))
        await asyncio.sleep(KEEPALIVE / 4)
        assert not waiting.done()
        await stream.publish(analysis_id, stage("recognizing"))
        [late] = await waiting

        assert late is not None and late.event == stage("recognizing")
        assert await take(reader, 1) == [None]


async def test_the_stream_expires(redis: Redis, analysis_id: UUID) -> None:
    key = f"analysis:{analysis_id}:events"
    stream = RedisProgressStream(redis, ttl_seconds=120, keepalive_seconds=KEEPALIVE)

    await stream.publish(analysis_id, stage("recognizing"))
    assert 100 < await redis.ttl(key) <= 120

    await redis.expire(key, 10)
    await stream.publish(analysis_id, stage("analyzing"))
    assert 100 < await redis.ttl(key) <= 120  # every event pushes the expiry forward


async def test_streams_of_different_analyses_are_separate(
    stream: RedisProgressStream, analysis_id: UUID, cleanup: list[str]
) -> None:
    other_id = uuid4()
    cleanup.append(f"analysis:{other_id}:events")
    await stream.publish(analysis_id, stage("recognizing"))
    await stream.publish(other_id, stage("analyzing"))

    async with aclosing(stream.read(other_id)) as reader:
        items = await take(reader, 2)

    assert items[0] is not None and items[0].event == stage("analyzing")
    assert items[1] is None


async def test_an_entry_written_by_someone_else_is_skipped(
    stream: RedisProgressStream, analysis_id: UUID, redis: Redis
) -> None:
    key = f"analysis:{analysis_id}:events"
    await redis.xadd(key, {"type": "stage", "data": "not json"})
    await redis.xadd(key, {"unexpected": "fields"})
    await stream.publish(analysis_id, stage("recognizing"))

    async with aclosing(stream.read(analysis_id)) as reader:
        items = await take(reader, 2)

    assert items[0] is not None and items[0].event == stage("recognizing")
    assert items[1] is None
