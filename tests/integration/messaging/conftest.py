"""Fixtures for tests against a real RabbitMQ.

Every test gets its own topology (random prefix, 1-second delay), deleted afterwards, so
tests neither see each other's messages nor leave anything behind on the broker.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from aio_pika import Message
from aio_pika.abc import (
    AbstractIncomingMessage,
    AbstractRobustChannel,
    AbstractRobustConnection,
)

from chart_analyzer.application.handle_task import Action, Disposition
from chart_analyzer.infrastructure.messaging.connection import connect, open_channel
from chart_analyzer.infrastructure.messaging.consumer import TaskConsumer
from chart_analyzer.infrastructure.messaging.publisher import RabbitTaskPublisher
from chart_analyzer.infrastructure.messaging.topology import TASKS_KEY, Topology

RABBITMQ_URL = os.environ.get("TEST_RABBITMQ_URL", "amqp://chart:chart@localhost:55672/")
TIMEOUT = 5.0

Respond = Callable[[UUID, int], Awaitable[Disposition]]


@dataclass
class StubHandler:
    """Records every call and answers with ``respond`` (ACK unless a test replaces it)."""

    respond: Respond | None = None
    calls: asyncio.Queue[tuple[UUID, int]] = field(default_factory=asyncio.Queue)

    async def handle(self, analysis_id: UUID, attempt: int) -> Disposition:
        self.calls.put_nowait((analysis_id, attempt))
        if self.respond is None:
            return Disposition(Action.ACK)
        return await self.respond(analysis_id, attempt)

    async def next_call(self) -> tuple[UUID, int]:
        return await asyncio.wait_for(self.calls.get(), TIMEOUT)

    async def assert_no_call(self, within: float) -> None:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(self.calls.get(), within)


@dataclass
class Broker:
    """What a test needs to look at the broker from the outside."""

    channel: AbstractRobustChannel
    topology: Topology

    async def message_count(self, queue_name: str) -> int:
        # A robust channel caches declared queues with their first answer, so ask anew.
        queue = await self.channel.get_queue(queue_name, ensure=False)
        return (await queue.declare()).message_count or 0

    async def pop(self, queue_name: str) -> AbstractIncomingMessage:
        """Take the next message of a queue, waiting for it to arrive."""
        queue = await self.channel.get_queue(queue_name, ensure=False)
        async with asyncio.timeout(TIMEOUT):
            while True:
                message = await queue.get(no_ack=True, fail=False)
                if message is not None:
                    return message
                await asyncio.sleep(0.02)

    async def publish_raw(self, body: bytes) -> None:
        exchange = await self.channel.get_exchange(self.topology.exchange, ensure=False)
        await exchange.publish(Message(body), TASKS_KEY)


@pytest.fixture
async def connection() -> AsyncIterator[AbstractRobustConnection]:
    connection = await connect(RABBITMQ_URL)
    yield connection
    await connection.close()


@pytest.fixture
async def topology(connection: AbstractRobustConnection) -> AsyncIterator[Topology]:
    topology = Topology(prefix=f"test-{uuid4().hex[:12]}", delays=(1,))
    yield topology
    channel = await connection.channel()
    queues = [topology.tasks_queue, topology.dead_letter_queue]
    queues += [topology.retry_queue(delay) for delay in topology.distinct_delays]
    for queue in queues:
        await channel.queue_delete(queue)
    for exchange in (topology.exchange, topology.dead_letter_exchange):
        await channel.exchange_delete(exchange)
    await channel.close()


@pytest.fixture
async def channel(
    connection: AbstractRobustConnection, topology: Topology
) -> AsyncIterator[AbstractRobustChannel]:
    channel = await open_channel(connection)
    await topology.declare(channel)
    yield channel
    await channel.close()


@pytest.fixture
def broker(channel: AbstractRobustChannel, topology: Topology) -> Broker:
    return Broker(channel, topology)


@pytest.fixture
def publisher(channel: AbstractRobustChannel, topology: Topology) -> RabbitTaskPublisher:
    return RabbitTaskPublisher(channel, topology)


@pytest.fixture
def handler() -> StubHandler:
    return StubHandler()


StartConsumer = Callable[..., Awaitable[TaskConsumer]]


@pytest.fixture
async def start_consumer(
    connection: AbstractRobustConnection, topology: Topology, handler: StubHandler
) -> AsyncIterator[StartConsumer]:
    started: list[TaskConsumer] = []

    async def start(prefetch: int = 4) -> TaskConsumer:
        consumer = TaskConsumer(connection, topology, handler, prefetch=prefetch)
        await consumer.start()
        started.append(consumer)
        return consumer

    yield start
    for consumer in started:
        await consumer.stop()
