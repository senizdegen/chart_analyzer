"""Fixtures for tests against a real Redis.

Every test works on keys made from fresh UUIDs and deletes them afterwards, so tests can
run in any order and next to each other on the shared test database.

The client is created in each way the application could configure it (bytes or text
replies, RESP2 or RESP3), because the shape of stream replies differs between them.
"""

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from redis.asyncio import Redis

REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:56379/1")

CLIENT_OPTIONS: dict[str, dict[str, Any]] = {
    "bytes": {},
    "text": {"decode_responses": True},
    "resp3": {"decode_responses": True, "protocol": 3},
}


@pytest.fixture(params=list(CLIENT_OPTIONS))
async def redis(request: pytest.FixtureRequest) -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL, **CLIENT_OPTIONS[request.param])
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def cleanup(redis: Redis) -> AsyncIterator[list[str]]:
    """Keys a test has touched; they are deleted when the test ends."""
    keys: list[str] = []
    try:
        yield keys
    finally:
        if keys:
            await redis.delete(*keys)
