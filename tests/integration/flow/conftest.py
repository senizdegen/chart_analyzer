"""Fixtures for the end-to-end flow: real Postgres, RabbitMQ and Redis together."""

import os
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

# Reuse the fixtures of the per-adapter suites: importing them registers them here.
from tests.integration.db.conftest import (  # noqa: F401
    alembic_config,
    clean_tables,
    engine,
    session_factory,
    uow_factory,
)
from tests.integration.messaging.conftest import channel, connection, topology  # noqa: F401

REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:56379/1")


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=10)
    try:
        yield client
    finally:
        await client.aclose()
