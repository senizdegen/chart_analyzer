"""Publishes analysis tasks, to the work queue and (for the consumer) to a retry queue.

The channel must come from ``connection.open_channel``: with publisher confirms and
``on_return_raises`` a publish returns only after the broker confirmed the message and
raises when the broker refused it or could not route it, so a task is never lost silently.
"""

import json
from uuid import UUID

from aio_pika import DeliveryMode, Message
from aio_pika.abc import AbstractChannel

from chart_analyzer.infrastructure.messaging.topology import TASKS_KEY, Topology

ATTEMPT_HEADER = "x-attempt"


class RabbitTaskPublisher:
    def __init__(self, channel: AbstractChannel, topology: Topology) -> None:
        self._channel = channel
        self._topology = topology

    async def publish(self, analysis_id: UUID) -> None:
        await self._publish(analysis_id, attempt=1, routing_key=TASKS_KEY)

    async def publish_retry(self, analysis_id: UUID, *, attempt: int, delay_seconds: int) -> None:
        """Deliver the task again as ``attempt`` after roughly ``delay_seconds``."""
        await self._publish(
            analysis_id,
            attempt=attempt,
            routing_key=self._topology.retry_routing_key(delay_seconds),
        )

    async def _publish(self, analysis_id: UUID, *, attempt: int, routing_key: str) -> None:
        message = Message(
            json.dumps({"analysis_id": str(analysis_id)}).encode(),
            headers={ATTEMPT_HEADER: attempt},
            content_type="application/json",
            delivery_mode=DeliveryMode.PERSISTENT,
        )
        # ensure=False: no round trip per publish; the topology is declared at startup.
        exchange = await self._channel.get_exchange(self._topology.exchange, ensure=False)
        await exchange.publish(message, routing_key, mandatory=True)
