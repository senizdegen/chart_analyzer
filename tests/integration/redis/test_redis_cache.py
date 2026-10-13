"""The Redis-backed cache, alone and under the market data decorator."""

import asyncio
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from chart_analyzer.application.ports.services import AssetRef
from chart_analyzer.domain.enums import AssetType
from chart_analyzer.infrastructure.cache.redis_cache import RedisCache
from chart_analyzer.infrastructure.market.cached import CachedMarketData
from tests.fakes import FakeMarketData, make_review, make_snapshot

pytestmark = pytest.mark.integration


@pytest.fixture
def key(cleanup: list[str]) -> str:
    name = f"test:cache:{uuid4()}"
    cleanup.append(name)
    return name


async def test_a_missing_key_is_none(redis: Redis, key: str) -> None:
    assert await RedisCache(redis).get(key) is None


async def test_set_then_get_returns_the_text(redis: Redis, key: str) -> None:
    cache = RedisCache(redis)
    value = '{"value": {"name": "Тест ✓", "price": 1.5}}'

    await cache.set(key, value, 60)

    assert await cache.get(key) == value
    assert 0 < await redis.ttl(key) <= 60


async def test_set_replaces_the_value_and_its_lifetime(redis: Redis, key: str) -> None:
    cache = RedisCache(redis)

    await cache.set(key, "first", 600)
    await cache.set(key, "second", 30)

    assert await cache.get(key) == "second"
    assert 0 < await redis.ttl(key) <= 30


async def test_an_entry_expires(redis: Redis, key: str) -> None:
    cache = RedisCache(redis)

    await cache.set(key, "short-lived", 1)
    assert await cache.get(key) == "short-lived"

    await asyncio.sleep(1.2)
    assert await cache.get(key) is None


async def test_market_data_round_trips_through_redis(redis: Redis, cleanup: list[str]) -> None:
    symbol = f"T{uuid4().hex[:8].upper()}"
    ref = AssetRef(symbol=symbol, type=AssetType.STOCK)
    kinds = ("asset", "price", "analyst_review", "news")
    cleanup.extend(f"market:{kind}:stock:{symbol}" for kind in kinds)
    snapshot = make_snapshot(analyst_review=make_review())

    inners = [FakeMarketData(snapshot), FakeMarketData(snapshot)]
    for inner in inners:
        cached = CachedMarketData(
            inner,
            RedisCache(redis),
            price_ttl=60,
            news_ttl=60,
            asset_ttl=60,
            analyst_review_ttl=60,
        )
        assert await cached.get_asset(ref) == snapshot.asset
        assert await cached.get_price(ref) == snapshot.price
        assert await cached.get_analyst_review(ref) == snapshot.analyst_review
        assert await cached.get_news(ref) == snapshot.news

    assert [inner.calls for inner in inners] == [len(kinds), 0]
    assert await redis.exists(*cleanup) == len(kinds)
