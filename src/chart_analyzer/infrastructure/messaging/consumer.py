"""Consumes analysis tasks and carries out what the handler decides.

The retry policy lives in the application layer; this module only translates a
``Disposition`` into broker operations. Each delivery runs in a task owned by the
consumer, so up to ``prefetch`` tasks are handled at once and ``stop`` can wait for them.
"""

import asyncio
import json
from typing import Protocol
from uuid import UUID

import structlog
from aio_pika.abc import AbstractIncomingMessage, AbstractQueue, AbstractRobustConnection

from chart_analyzer.application.handle_task import Action, Disposition
from chart_analyzer.infrastructure.messaging.connection import open_channel
from chart_analyzer.infrastructure.messaging.publisher import ATTEMPT_HEADER, RabbitTaskPublisher
from chart_analyzer.infrastructure.messaging.topology import Topology
from chart_analyzer.logs import bind_context, clear_context

log = structlog.get_logger(__name__)


class Handler(Protocol):
    async def handle(self, analysis_id: UUID, attempt: int) -> Disposition: ...


class MalformedTask(Exception):
    pass


def parse_task(message: AbstractIncomingMessage) -> tuple[UUID, int]:
    try:
        analysis_id = UUID(json.loads(message.body)["analysis_id"])
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise MalformedTask(f"body: {error!r}") from error
    attempt = message.headers.get(ATTEMPT_HEADER, 1)
    if not isinstance(attempt, int) or attempt < 1:
        raise MalformedTask(f"{ATTEMPT_HEADER} header: {attempt!r}")
    return analysis_id, attempt


class TaskConsumer:
    def __init__(
        self,
        connection: AbstractRobustConnection,
        topology: Topology,
        handler: Handler,
        *,
        prefetch: int,
    ) -> None:
        self._connection = connection
        self._topology = topology
        self._handler = handler
        self._prefetch = prefetch
        self._queue: AbstractQueue | None = None
        self._publisher: RabbitTaskPublisher | None = None
        self._consumer_tag: str | None = None
        self._in_flight: set[asyncio.Task[None]] = set()
        self._stopping = False

    async def start(self) -> None:
        """Declare the topology and start consuming on a channel of our own."""
        channel = await open_channel(self._connection)
        await channel.set_qos(prefetch_count=self._prefetch)
        await self._topology.declare(channel)
        self._publisher = RabbitTaskPublisher(channel, self._topology)
        self._queue = await channel.get_queue(self._topology.tasks_queue, ensure=False)
        self._stopping = False
        self._consumer_tag = await self._queue.consume(self._on_message, no_ack=False)
        log.info("consumer.started", queue=self._topology.tasks_queue, prefetch=self._prefetch)

    async def stop(self) -> None:
        """Take no new deliveries, let the tasks in flight finish, close the channel."""
        if self._queue is None or self._consumer_tag is None:
            return
        self._stopping = True
        await self._queue.cancel(self._consumer_tag)
        if self._in_flight:
            await asyncio.wait(self._in_flight)
        # Closing returns deliveries that were received but never started to the queue.
        await self._queue.channel.close()
        self._queue = None
        self._consumer_tag = None
        self._publisher = None
        log.info("consumer.stopped", queue=self._topology.tasks_queue)

    async def _on_message(self, message: AbstractIncomingMessage) -> None:
        if self._stopping:
            return  # left unacked: the broker requeues it when the channel closes
        # aio-pika runs this callback in a task that dies with the channel; our own task
        # survives until the handler is done, which is what a graceful stop needs.
        task = asyncio.create_task(self._process(message))
        self._in_flight.add(task)
        task.add_done_callback(self._in_flight.discard)

    async def _process(self, message: AbstractIncomingMessage) -> None:
        try:
            await self._handle(message)
        except Exception:
            # Settling failed (typically the connection dropped). The delivery stays
            # unacknowledged, so the broker delivers it again.
            log.exception("task.settle_failed")
        finally:
            clear_context()

    async def _handle(self, message: AbstractIncomingMessage) -> None:
        try:
            analysis_id, attempt = parse_task(message)
        except MalformedTask as error:
            log.error("task.malformed", reason=str(error))
            await message.reject(requeue=False)
            return

        bind_context(analysis_id=str(analysis_id), attempt=attempt)
        log.info("task.received")
        try:
            disposition = await self._handler.handle(analysis_id, attempt)
        except Exception:
            log.exception("task.handler_crashed")
            await message.reject(requeue=False)
            return

        if disposition.action is Action.RETRY:
            await self._retry(message, analysis_id, disposition)
        elif disposition.action is Action.ACK:
            await message.ack()
            log.info("task.acked")
        else:
            await message.reject(requeue=False)
            log.warning("task.dead_lettered")

    async def _retry(
        self, message: AbstractIncomingMessage, analysis_id: UUID, disposition: Disposition
    ) -> None:
        assert self._publisher is not None  # set by start(), before any delivery
        try:
            await self._publisher.publish_retry(
                analysis_id,
                attempt=disposition.next_attempt,
                delay_seconds=disposition.delay_seconds,
            )
        except Exception:
            # Not scheduled, so the original must not be dropped: back to the queue.
            log.exception("task.retry_publish_failed")
            await message.nack(requeue=True)
            return
        # Publish first, ack second: a crash in between duplicates the task, never loses it.
        await message.ack()
        log.info(
            "task.retry_published",
            next_attempt=disposition.next_attempt,
            delay_seconds=disposition.delay_seconds,
        )
