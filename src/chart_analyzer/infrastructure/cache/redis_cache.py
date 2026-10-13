"""A string cache on Redis: values are JSON texts that expire by themselves.

It stays this small on purpose. What to cache and for how long is decided by the caller
(see ``infrastructure/market/cached.py``); errors are raised as they are and the caller
decides that a broken cache is only a miss.
"""

from redis.asyncio import Redis


class RedisCache:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def get(self, key: str) -> str | None:
        value = await self._redis.get(key)
        if value is None:
            return None
        return value.decode() if isinstance(value, bytes) else str(value)

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        await self._redis.set(key, value, ex=ttl_seconds)
