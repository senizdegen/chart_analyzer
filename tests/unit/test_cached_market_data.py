"""The caching decorator over a counting provider and a dict-backed cache."""

from collections import Counter

import pytest

from chart_analyzer.application.ports.services import AssetRef
from chart_analyzer.domain.enums import AssetType, ErrorCode
from chart_analyzer.domain.errors import AnalysisFailure, DomainError, TransientFailure
from chart_analyzer.domain.models import (
    AnalystReview,
    Asset,
    CurrentPrice,
    MarketSnapshot,
    NewsArticle,
)
from chart_analyzer.infrastructure.market.cached import CachedMarketData
from tests.fakes import NOW, make_news, make_review, make_snapshot

AAPL = AssetRef(symbol="AAPL", type=AssetType.STOCK, name_hint="Apple")
AAPL_NO_HINT = AssetRef(symbol="AAPL", type=AssetType.STOCK)
TTLS = {"price_ttl": 60, "news_ttl": 600, "asset_ttl": 86_400, "analyst_review_ttl": 43_200}


class HintEchoingProvider:
    """Like Finnhub for crypto: the asset name and the news depend on the name hint."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()

    async def get_asset(self, ref: AssetRef) -> Asset:
        self.calls["asset"] += 1
        return Asset(symbol=ref.symbol, name=ref.name_hint or ref.symbol, type=ref.type)

    async def get_price(self, ref: AssetRef) -> CurrentPrice:
        self.calls["price"] += 1
        return CurrentPrice(price=100.0 + self.calls["price"], currency="USD", as_of=NOW)

    async def get_analyst_review(self, ref: AssetRef) -> AnalystReview | None:
        self.calls["analyst_review"] += 1
        return make_review()

    async def get_news(self, ref: AssetRef) -> list[NewsArticle]:
        self.calls["news"] += 1
        article = make_news(1)[0]
        return [article.model_copy(update={"headline": f"News about {ref.name_hint}"})]


class CountingProvider:
    """Answers from snapshots keyed by (type, symbol) and counts every call."""

    def __init__(self, *snapshots: MarketSnapshot) -> None:
        self._snapshots = {(s.asset.type, s.asset.symbol): s for s in snapshots}
        self.calls: Counter[str] = Counter()
        self.error: DomainError | None = None

    def _answer(self, kind: str, ref: AssetRef) -> MarketSnapshot:
        self.calls[kind] += 1
        if self.error is not None:
            raise self.error
        return self._snapshots[(ref.type, ref.symbol)]

    async def get_asset(self, ref: AssetRef) -> Asset:
        return self._answer("asset", ref).asset

    async def get_price(self, ref: AssetRef) -> CurrentPrice:
        return self._answer("price", ref).price

    async def get_analyst_review(self, ref: AssetRef) -> AnalystReview | None:
        return self._answer("analyst_review", ref).analyst_review

    async def get_news(self, ref: AssetRef) -> list[NewsArticle]:
        return self._answer("news", ref).news


class DictCache:
    def __init__(self) -> None:
        self.entries: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self.entries.get(key)

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self.entries[key] = value
        self.ttls[key] = ttl_seconds


class BrokenCache:
    def __init__(self) -> None:
        self.attempts = 0

    async def get(self, key: str) -> str | None:
        self.attempts += 1
        raise ConnectionError("redis is down")

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self.attempts += 1
        raise ConnectionError("redis is down")


def snapshot_for(symbol: str, asset_type: AssetType, price: float) -> MarketSnapshot:
    return make_snapshot(
        asset=Asset(symbol=symbol, name=f"{symbol} {asset_type.value}", type=asset_type),
        price=CurrentPrice(price=price, currency="USD", as_of=NOW),
    )


async def test_second_call_of_each_method_is_served_from_the_cache() -> None:
    snapshot = make_snapshot(analyst_review=make_review(), news=make_news(3))
    inner, cache = CountingProvider(snapshot), DictCache()
    cached = CachedMarketData(inner, cache, **TTLS)

    for _ in range(2):
        assert await cached.get_asset(AAPL_NO_HINT) == snapshot.asset
        assert await cached.get_price(AAPL_NO_HINT) == snapshot.price
        assert await cached.get_analyst_review(AAPL_NO_HINT) == snapshot.analyst_review
        assert await cached.get_news(AAPL_NO_HINT) == snapshot.news

    assert inner.calls == {"asset": 1, "price": 1, "analyst_review": 1, "news": 1}
    assert cache.ttls == {
        "market:asset:stock:AAPL": 86_400,
        "market:price:stock:AAPL": 60,
        "market:analyst_review:stock:AAPL": 43_200,
        "market:news:stock:AAPL": 600,
    }


async def test_second_call_with_a_name_hint_is_served_from_the_cache_too() -> None:
    snapshot = make_snapshot(analyst_review=make_review(), news=make_news(3))
    inner, cache = CountingProvider(snapshot), DictCache()
    cached = CachedMarketData(inner, cache, **TTLS)

    for _ in range(2):
        assert await cached.get_asset(AAPL) == snapshot.asset
        assert await cached.get_price(AAPL) == snapshot.price
        assert await cached.get_analyst_review(AAPL) == snapshot.analyst_review
        assert await cached.get_news(AAPL) == snapshot.news

    assert inner.calls == {"asset": 1, "price": 1, "analyst_review": 1, "news": 1}
    # the hint is part of the asset and news keys only, and never in clear text
    by_kind = {key.split(":")[1]: key for key in cache.ttls}
    assert by_kind["price"] == "market:price:stock:AAPL"
    assert by_kind["analyst_review"] == "market:analyst_review:stock:AAPL"
    for kind in ("asset", "news"):
        prefix, _, digest = by_kind[kind].rpartition(":")
        assert prefix == f"market:{kind}:stock:AAPL"
        assert digest and "apple" not in digest.lower()
    assert {cache.ttls[by_kind[kind]] for kind in ("asset", "news")} == {86_400, 600}


async def test_different_name_hints_for_one_symbol_do_not_share_asset_and_news() -> None:
    inner = HintEchoingProvider()
    cached = CachedMarketData(inner, DictCache(), **TTLS)
    luna = AssetRef(symbol="LUNA", type=AssetType.CRYPTO, name_hint="Terra")
    other = AssetRef(symbol="LUNA", type=AssetType.CRYPTO, name_hint="Luna Classic")
    unnamed = AssetRef(symbol="LUNA", type=AssetType.CRYPTO)

    for _ in range(2):
        assert (await cached.get_asset(luna)).name == "Terra"
        assert (await cached.get_asset(other)).name == "Luna Classic"
        assert (await cached.get_asset(unnamed)).name == "LUNA"
        assert (await cached.get_news(luna))[0].headline == "News about Terra"
        assert (await cached.get_news(other))[0].headline == "News about Luna Classic"
        assert (await cached.get_news(unnamed))[0].headline == "News about None"

    assert inner.calls == {"asset": 3, "news": 3}


async def test_the_same_name_hint_shares_an_entry_ignoring_case_and_spaces() -> None:
    inner = HintEchoingProvider()
    cached = CachedMarketData(inner, DictCache(), **TTLS)
    first = AssetRef(symbol="BTC", type=AssetType.CRYPTO, name_hint="Bitcoin")

    assert (await cached.get_asset(first)).name == "Bitcoin"
    assert (await cached.get_news(first))[0].headline == "News about Bitcoin"
    for hint in ("bitcoin", "  BITCOIN ", "Bitcoin\n"):
        ref = AssetRef(symbol="BTC", type=AssetType.CRYPTO, name_hint=hint)
        assert (await cached.get_asset(ref)).name == "Bitcoin"
        assert (await cached.get_news(ref))[0].headline == "News about Bitcoin"

    assert inner.calls == {"asset": 1, "news": 1}


async def test_price_and_analyst_review_are_shared_whatever_the_name_hint() -> None:
    inner = HintEchoingProvider()
    cached = CachedMarketData(inner, DictCache(), **TTLS)
    refs = [
        AssetRef(symbol="BTC", type=AssetType.CRYPTO, name_hint=hint)
        for hint in ("Bitcoin", "Bitcoin Cash", None, "")
    ]

    prices = [await cached.get_price(ref) for ref in refs]
    for ref in refs:
        await cached.get_analyst_review(ref)

    assert len(set(prices)) == 1
    assert inner.calls == {"price": 1, "analyst_review": 1}


async def test_an_empty_name_hint_is_the_same_as_none() -> None:
    inner, cache = HintEchoingProvider(), DictCache()
    cached = CachedMarketData(inner, cache, **TTLS)

    await cached.get_asset(AssetRef(symbol="BTC", type=AssetType.CRYPTO, name_hint=""))
    await cached.get_asset(AssetRef(symbol="BTC", type=AssetType.CRYPTO))

    assert inner.calls == {"asset": 1}
    assert list(cache.entries) == ["market:asset:crypto:BTC"]


async def test_absent_review_and_empty_news_are_cached_too() -> None:
    inner = CountingProvider(make_snapshot(analyst_review=None, news=[]))
    cached = CachedMarketData(inner, DictCache(), **TTLS)

    for _ in range(2):
        assert await cached.get_analyst_review(AAPL) is None
        assert await cached.get_news(AAPL) == []

    assert inner.calls == {"analyst_review": 1, "news": 1}


async def test_symbols_and_asset_types_do_not_collide() -> None:
    inner = CountingProvider(
        snapshot_for("AAPL", AssetType.STOCK, 340.0),
        snapshot_for("MSFT", AssetType.STOCK, 510.0),
        snapshot_for("SOL", AssetType.STOCK, 2.5),
        snapshot_for("SOL", AssetType.CRYPTO, 180.0),
    )
    cached = CachedMarketData(inner, DictCache(), **TTLS)
    refs = {
        AssetRef(symbol="AAPL", type=AssetType.STOCK): 340.0,
        AssetRef(symbol="MSFT", type=AssetType.STOCK): 510.0,
        AssetRef(symbol="SOL", type=AssetType.STOCK): 2.5,
        AssetRef(symbol="SOL", type=AssetType.CRYPTO): 180.0,
    }

    for _ in range(2):
        for ref, price in refs.items():
            assert (await cached.get_price(ref)).price == price
            assert (await cached.get_asset(ref)).name == f"{ref.symbol} {ref.type.value}"

    assert inner.calls == {"price": 4, "asset": 4}


@pytest.mark.parametrize(
    "error",
    [
        TransientFailure(ErrorCode.MARKET_DATA_UNAVAILABLE),
        AnalysisFailure(ErrorCode.ASSET_NOT_FOUND),
    ],
)
async def test_inner_errors_are_raised_and_not_cached(error: DomainError) -> None:
    snapshot = make_snapshot()
    inner, cache = CountingProvider(snapshot), DictCache()
    cached = CachedMarketData(inner, cache, **TTLS)
    inner.error = error

    with pytest.raises(type(error)):
        await cached.get_price(AAPL)
    assert cache.entries == {}

    inner.error = None
    assert await cached.get_price(AAPL) == snapshot.price
    assert inner.calls["price"] == 2


async def test_a_broken_cache_falls_through_to_the_provider() -> None:
    snapshot = make_snapshot(analyst_review=make_review(), news=make_news(2))
    inner, cache = CountingProvider(snapshot), BrokenCache()
    cached = CachedMarketData(inner, cache, **TTLS)

    for _ in range(2):
        assert await cached.get_asset(AAPL) == snapshot.asset
        assert await cached.get_price(AAPL) == snapshot.price
        assert await cached.get_analyst_review(AAPL) == snapshot.analyst_review
        assert await cached.get_news(AAPL) == snapshot.news

    assert inner.calls == {"asset": 2, "price": 2, "analyst_review": 2, "news": 2}
    assert cache.attempts == 16  # a failed read and a failed write per call


@pytest.mark.parametrize(
    "corrupt",
    [
        "not json at all",
        "[1, 2, 3]",
        "null",
        '{"other": 1}',
        '{"value": {"price": "many"}}',
        '{"value": null}',
    ],
)
async def test_a_corrupt_entry_is_a_miss_and_gets_replaced(corrupt: str) -> None:
    snapshot = make_snapshot()
    inner, cache = CountingProvider(snapshot), DictCache()
    cache.entries["market:price:stock:AAPL"] = corrupt
    cached = CachedMarketData(inner, cache, **TTLS)

    assert await cached.get_price(AAPL) == snapshot.price
    assert await cached.get_price(AAPL) == snapshot.price

    assert inner.calls["price"] == 1
    assert cache.entries["market:price:stock:AAPL"] != corrupt
