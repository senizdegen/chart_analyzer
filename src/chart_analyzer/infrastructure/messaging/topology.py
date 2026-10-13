"""Names and declaration of the RabbitMQ objects, all derived from one prefix.

Deriving every name from the prefix lets tests run against a real broker with a random
prefix without touching the queues of a running system. Delays are implemented as one
queue per delay (``x-message-ttl`` + dead-lettering back to the work queue) rather than
per-message TTL, which would block short delays behind long ones at the head of a queue.
"""

from dataclasses import dataclass

from aio_pika import ExchangeType
from aio_pika.abc import AbstractChannel

TASKS_KEY = "tasks"
DEAD_KEY = "dead"


@dataclass(frozen=True, slots=True)
class Topology:
    prefix: str = "analysis"
    delays: tuple[int, ...] = (5, 30, 120)  # seconds; one retry queue per distinct value

    def __post_init__(self) -> None:
        if not self.delays or min(self.delays) <= 0:
            raise ValueError("delays must be a non-empty tuple of positive seconds")

    @property
    def exchange(self) -> str:
        return self.prefix

    @property
    def tasks_queue(self) -> str:
        return f"{self.prefix}.tasks"

    @property
    def dead_letter_exchange(self) -> str:
        return f"{self.prefix}.dlx"

    @property
    def dead_letter_queue(self) -> str:
        return f"{self.prefix}.dlq"

    @property
    def distinct_delays(self) -> tuple[int, ...]:
        return tuple(sorted(set(self.delays)))

    def retry_queue(self, delay: int) -> str:
        return f"{self.prefix}.retry.{delay}s"

    def retry_routing_key(self, delay_seconds: int) -> str:
        """Key of the retry queue to use: the shortest delay that is long enough.

        A request longer than every declared delay gets the longest one, so a task is
        retried early rather than published to a queue that does not exist.
        """
        declared = self.distinct_delays
        chosen = next((delay for delay in declared if delay >= delay_seconds), declared[-1])
        return f"retry.{chosen}"

    async def declare(self, channel: AbstractChannel) -> None:
        """Declare everything. Safe to repeat: the arguments are always the same."""
        exchange = await channel.declare_exchange(self.exchange, ExchangeType.DIRECT, durable=True)
        dead_letters = await channel.declare_exchange(
            self.dead_letter_exchange, ExchangeType.DIRECT, durable=True
        )

        tasks = await channel.declare_queue(
            self.tasks_queue,
            durable=True,
            arguments={
                "x-dead-letter-exchange": self.dead_letter_exchange,
                "x-dead-letter-routing-key": DEAD_KEY,
            },
        )
        await tasks.bind(exchange, TASKS_KEY)

        for delay in self.distinct_delays:
            retry = await channel.declare_queue(
                self.retry_queue(delay),
                durable=True,
                arguments={
                    "x-message-ttl": delay * 1000,
                    "x-dead-letter-exchange": self.exchange,
                    "x-dead-letter-routing-key": TASKS_KEY,
                },
            )
            await retry.bind(exchange, f"retry.{delay}")

        dead = await channel.declare_queue(self.dead_letter_queue, durable=True)
        await dead.bind(dead_letters, DEAD_KEY)
