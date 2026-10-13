"""Progress events of an analysis on a Redis Stream.

A Stream rather than pub/sub: events are kept, so a client that connects late or
reconnects replays what it missed, starting after the last event id it has seen. The key
expires some time after the last event, so finished analyses clean up after themselves.

A reader holds one Redis connection while it waits (a blocking ``XREAD``): the connection
pool has to be sized for the number of clients following progress at the same time, and
the socket timeout of the client must be longer than ``keepalive_seconds``.
"""

import json
import re
from collections.abc import AsyncGenerator, Mapping
from typing import Any
from uuid import UUID

import structlog
from redis.asyncio import Redis

from chart_analyzer.application.ports.services import (
    ProgressEvent,
    ProgressEventType,
    StoredProgressEvent,
)

log = structlog.get_logger(__name__)

_BEGINNING = "0-0"
_STREAM_ID = re.compile(r"^(\d{1,20})-(\d{1,20})$")
_MAX_ID_PART = 2**64 - 1
_READ_BATCH = 100


class RedisProgressStream:
    """Both ``ProgressPublisher`` (worker side) and ``ProgressReader`` (API side)."""

    def __init__(self, redis: Redis, *, ttl_seconds: int, keepalive_seconds: float) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds
        self._block_ms = max(1, round(keepalive_seconds * 1000))  # 0 would block forever

    async def publish(self, analysis_id: UUID, event: ProgressEvent) -> None:
        key = _key(analysis_id)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.xadd(key, {"type": event.type.value, "data": json.dumps(event.data)})
            pipe.expire(key, self._ttl_seconds)
            await pipe.execute()

    async def read(
        self, analysis_id: UUID, *, after: str | None = None
    ) -> AsyncGenerator[StoredProgressEvent | None, None]:
        key = _key(analysis_id)
        position = _start_position(after)
        while True:
            reply = await self._redis.xread(
                {key: position}, count=_READ_BATCH, block=self._block_ms
            )
            entries = _entries(reply)
            if not entries:
                yield None
                continue
            for raw_id, fields in entries:
                position = _text(raw_id)
                event = _decode(fields)
                if event is None:
                    log.warning("progress.malformed_entry", key=key, entry_id=position)
                    continue
                yield StoredProgressEvent(id=position, event=event)


def _key(analysis_id: UUID) -> str:
    return f"analysis:{analysis_id}:events"


def _start_position(after: str | None) -> str:
    """``after`` comes from a client header: anything that is not a stream id means the start."""
    match = _STREAM_ID.match(after) if after else None
    if match is None or any(int(part) > _MAX_ID_PART for part in match.groups()):
        return _BEGINNING
    return match.group(0)


def _entries(reply: Any) -> list[tuple[Any, Mapping[Any, Any]]]:
    """Entries of the single stream that was asked for.

    The reply is ``[[key, entries]]`` on RESP2 and ``{key: [entries]}`` on RESP3, and it is
    empty when the block timed out.
    """
    if not reply:
        return []
    if isinstance(reply, Mapping):
        return [entry for batch in next(iter(reply.values())) for entry in batch]
    return list(reply[0][1])


def _decode(fields: Mapping[Any, Any]) -> ProgressEvent | None:
    values = {_text(name): _text(value) for name, value in fields.items()}
    try:
        data = json.loads(values["data"])
        event_type = ProgressEventType(values["type"])
    except (KeyError, ValueError):
        return None
    return ProgressEvent(type=event_type, data=data) if isinstance(data, dict) else None


def _text(value: Any) -> str:
    """Redis answers with bytes unless the client was created with ``decode_responses``."""
    return value.decode() if isinstance(value, bytes) else str(value)
