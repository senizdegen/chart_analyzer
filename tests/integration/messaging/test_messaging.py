"""Publisher, consumer and topology against a real RabbitMQ."""

import asyncio
import json
import time
from uuid import UUID, uuid4

import pytest
from aio_pika import DeliveryMode
from aio_pika.abc import AbstractRobustChannel
from aio_pika.exceptions import PublishError

from chart_analyzer.application.handle_task import Action, Disposition
from chart_analyzer.infrastructure.messaging.publisher import RabbitTaskPublisher
from chart_analyzer.infrastructure.messaging.topology import Topology
from tests.integration.messaging.conftest import TIMEOUT, Broker, StartConsumer, StubHandler

pytestmark = pytest.mark.integration


async def test_declaring_the_topology_twice_is_fine(
    channel: AbstractRobustChannel, topology: Topology, broker: Broker
) -> None:
    await topology.declare(channel)  # the fixture declared it once already

    for queue in (topology.tasks_queue, topology.retry_queue(1), topology.dead_letter_queue):
        assert await broker.message_count(queue) == 0


async def test_published_task_is_persistent_json_with_attempt_one(
    publisher: RabbitTaskPublisher, topology: Topology, broker: Broker
) -> None:
    analysis_id = uuid4()

    await publisher.publish(analysis_id)

    message = await broker.pop(topology.tasks_queue)
    assert json.loads(message.body) == {"analysis_id": str(analysis_id)}
    assert message.headers == {"x-attempt": 1}
    assert message.content_type == "application/json"
    assert message.delivery_mode is DeliveryMode.PERSISTENT


async def test_publish_fails_when_the_broker_cannot_route_the_task(
    publisher: RabbitTaskPublisher, channel: AbstractRobustChannel, topology: Topology
) -> None:
    await channel.queue_delete(topology.tasks_queue)

    with pytest.raises(PublishError):
        await publisher.publish(uuid4())


async def test_acked_task_is_delivered_once_and_leaves_the_queue_empty(
    publisher: RabbitTaskPublisher,
    start_consumer: StartConsumer,
    handler: StubHandler,
    topology: Topology,
    broker: Broker,
) -> None:
    analysis_id = uuid4()
    consumer = await start_consumer()

    await publisher.publish(analysis_id)

    assert await handler.next_call() == (analysis_id, 1)
    await consumer.stop()  # an unacked delivery would return to the queue here
    assert await broker.message_count(topology.tasks_queue) == 0
    assert await broker.message_count(topology.dead_letter_queue) == 0


async def test_retry_delivers_the_task_again_after_the_delay_with_the_next_attempt(
    publisher: RabbitTaskPublisher, start_consumer: StartConsumer, handler: StubHandler
) -> None:
    async def retry_once(_: UUID, attempt: int) -> Disposition:
        if attempt == 1:
            return Disposition(Action.RETRY, delay_seconds=1, next_attempt=2)
        return Disposition(Action.ACK)

    handler.respond = retry_once
    analysis_id = uuid4()
    await start_consumer()

    await publisher.publish(analysis_id)

    assert await handler.next_call() == (analysis_id, 1)
    first = time.monotonic()
    assert await handler.next_call() == (analysis_id, 2)
    assert 0.8 <= time.monotonic() - first < 3.0
    await handler.assert_no_call(within=1.5)


async def test_retry_keeps_the_attempt_when_the_handler_asks_for_the_same_one(
    publisher: RabbitTaskPublisher, start_consumer: StartConsumer, handler: StubHandler
) -> None:
    deliveries = 0

    async def busy_once(_: UUID, attempt: int) -> Disposition:
        nonlocal deliveries
        deliveries += 1
        if deliveries == 1:
            return Disposition(Action.RETRY, delay_seconds=1, next_attempt=attempt)
        return Disposition(Action.ACK)

    handler.respond = busy_once
    analysis_id = uuid4()
    await start_consumer()

    await publisher.publish(analysis_id)

    assert await handler.next_call() == (analysis_id, 1)
    assert await handler.next_call() == (analysis_id, 1)


async def test_dead_lettered_task_lands_in_the_dlq_and_is_not_redelivered(
    publisher: RabbitTaskPublisher,
    start_consumer: StartConsumer,
    handler: StubHandler,
    topology: Topology,
    broker: Broker,
) -> None:
    async def give_up(_: UUID, __: int) -> Disposition:
        return Disposition(Action.DEAD_LETTER)

    handler.respond = give_up
    analysis_id = uuid4()
    await start_consumer()

    await publisher.publish(analysis_id)

    assert await handler.next_call() == (analysis_id, 1)
    dead = await broker.pop(topology.dead_letter_queue)
    assert json.loads(dead.body) == {"analysis_id": str(analysis_id)}
    await handler.assert_no_call(within=1.5)
    assert await broker.message_count(topology.tasks_queue) == 0


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b"{}",
        b'{"analysis_id": "not-a-uuid"}',
        b'{"analysis_id": 42}',
    ],
)
async def test_malformed_task_goes_to_the_dlq_and_the_consumer_keeps_working(
    body: bytes,
    publisher: RabbitTaskPublisher,
    start_consumer: StartConsumer,
    handler: StubHandler,
    topology: Topology,
    broker: Broker,
) -> None:
    await start_consumer()

    await broker.publish_raw(body)

    assert (await broker.pop(topology.dead_letter_queue)).body == body
    analysis_id = uuid4()
    await publisher.publish(analysis_id)
    assert await handler.next_call() == (analysis_id, 1)  # the only call: the bad one never ran


async def test_crashing_handler_dead_letters_the_task_and_the_consumer_keeps_working(
    publisher: RabbitTaskPublisher,
    start_consumer: StartConsumer,
    handler: StubHandler,
    topology: Topology,
    broker: Broker,
) -> None:
    crashing, healthy = uuid4(), uuid4()

    async def crash_on_first(analysis_id: UUID, _: int) -> Disposition:
        if analysis_id == crashing:
            raise RuntimeError("boom")
        return Disposition(Action.ACK)

    handler.respond = crash_on_first
    await start_consumer()

    await publisher.publish(crashing)

    assert await handler.next_call() == (crashing, 1)
    dead = await broker.pop(topology.dead_letter_queue)
    assert json.loads(dead.body) == {"analysis_id": str(crashing)}
    await publisher.publish(healthy)
    assert await handler.next_call() == (healthy, 1)


async def test_tasks_are_handled_concurrently_up_to_the_prefetch_limit(
    publisher: RabbitTaskPublisher, start_consumer: StartConsumer, handler: StubHandler
) -> None:
    release = asyncio.Event()

    async def block(_: UUID, __: int) -> Disposition:
        await release.wait()
        return Disposition(Action.ACK)

    handler.respond = block
    await start_consumer(prefetch=4)
    ids = [uuid4() for _ in range(6)]
    for analysis_id in ids:
        await publisher.publish(analysis_id)

    try:
        in_flight = {(await handler.next_call())[0] for _ in range(4)}
        assert in_flight == set(ids[:4])
        await handler.assert_no_call(within=0.5)  # the fifth waits for a free slot
    finally:
        release.set()

    assert {(await handler.next_call())[0] for _ in range(2)} == set(ids[4:])


async def test_stop_waits_for_the_handler_in_flight(
    publisher: RabbitTaskPublisher,
    start_consumer: StartConsumer,
    handler: StubHandler,
    topology: Topology,
    broker: Broker,
) -> None:
    release = asyncio.Event()
    finished = False

    async def slow(_: UUID, __: int) -> Disposition:
        nonlocal finished
        await release.wait()
        finished = True
        return Disposition(Action.ACK)

    handler.respond = slow
    consumer = await start_consumer(prefetch=1)
    await publisher.publish(uuid4())
    await handler.next_call()
    await publisher.publish(uuid4())  # must not be started by a stopping consumer

    stopping = asyncio.create_task(consumer.stop())
    await asyncio.sleep(0.3)
    assert not stopping.done()

    release.set()
    await asyncio.wait_for(stopping, TIMEOUT)
    assert finished
    await handler.assert_no_call(within=0.3)
    assert await broker.message_count(topology.tasks_queue) == 1
