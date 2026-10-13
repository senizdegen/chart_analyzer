"""Opening connections and channels the way the publisher and the consumer rely on.

Channels are opened with publisher confirms and ``on_return_raises``: a publish then
either returns after the broker took responsibility for the message or raises, also
when the broker could not route it to any queue.
"""

import aio_pika
from aio_pika.abc import AbstractRobustChannel, AbstractRobustConnection


async def connect(url: str) -> AbstractRobustConnection:
    """Connection that re-establishes itself (and its channels, queues, consumers)."""
    return await aio_pika.connect_robust(url)


async def open_channel(connection: AbstractRobustConnection) -> AbstractRobustChannel:
    return await connection.channel(publisher_confirms=True, on_return_raises=True)
