"""Caching decorator for market data.

The provider's free tier allows 60 requests a minute for all users together, while a
popular ticker is asked for again and again; each kind of data gets its own lifetime.

The cache is an optimisation and nothing more: when it is down or holds garbage the
provider is asked directly, so the cache can never fail an analysis. Provider errors are
never stored, so one bad minute of the provider is not remembered for a day.
"""

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Protocol

import structlog
from pydantic import TypeAdapter, ValidationError

from chart_analyzer.application.ports.services import AssetRef, MarketDataProvider
from chart_analyzer.domain.models import AnalystReview, Asset, CurrentPrice, NewsArticle

log = structlog.get_logger(__name__)

_ASSET: TypeAdapter[Asset] = TypeAdapter(Asset)
_PRICE: TypeAdapter[CurrentPrice] = TypeAdapter(CurrentPrice)
_ANALYST_REVIEW: TypeAdapter[AnalystReview | None] = TypeAdapter(AnalystReview | None)
_NEWS: TypeAdapter[list[NewsArticle]] = TypeAdapter(list[NewsArticle])


class Cache(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl_seconds: int) -> None: ...


class CachedMarketData:
    def __init__(
        self,
        inner: MarketDataProvider,
        cache: Cache,
        *,
        price_ttl: int,
        news_ttl: int,
        asset_ttl: int,
        analyst_review_ttl: int,
    ) -> None:
        self._inner = inner
        self._cache = cache
        self._price_ttl = price_ttl
        self._news_ttl = news_ttl
        self._asset_ttl = asset_ttl
        self._analyst_review_ttl = analyst_review_ttl

    async def get_asset(self, ref: AssetRef) -> Asset:
        return await self._cached(
            "asset",
            ref,
            self._asset_ttl,
            _ASSET,
            lambda: self._inner.get_asset(ref),
            per_hint=True,
        )

    async def get_price(self, ref: AssetRef) -> CurrentPrice:
        return await self._cached(
            "price", ref, self._price_ttl, _PRICE, lambda: self._inner.get_price(ref)
        )

    async def get_analyst_review(self, ref: AssetRef) -> AnalystReview | None:
        return await self._cached(
            "analyst_review",
            ref,
            self._analyst_review_ttl,
            _ANALYST_REVIEW,
            lambda: self._inner.get_analyst_review(ref),
        )

    async def get_news(self, ref: AssetRef) -> list[NewsArticle]:
        return await self._cached(
            "news",
            ref,
            self._news_ttl,
            _NEWS,
            lambda: self._inner.get_news(ref),
            per_hint=True,
        )

    async def _cached[T](
        self,
        kind: str,
        ref: AssetRef,
        ttl_seconds: int,
        adapter: TypeAdapter[T],
        fetch: Callable[[], Awaitable[T]],
        *,
        per_hint: bool = False,
    ) -> T:
        key = f"market:{kind}:{ref.type.value}:{ref.symbol}"
        if per_hint and ref.name_hint:
            # The asset name and the news filter may be derived from the name the AI read
            # on one user's image. Keying on it keeps that value from being served to
            # users whose image said something else.
            digest = hashlib.sha256(ref.name_hint.strip().lower().encode()).hexdigest()[:16]
            key = f"{key}:{digest}"
        hit = await self._read(key, adapter)
        if hit is not None:
            return hit[0]
        value = await fetch()
        await self._write(key, adapter, value, ttl_seconds)
        return value

    async def _read[T](self, key: str, adapter: TypeAdapter[T]) -> tuple[T] | None:
        """The cached value in a 1-tuple, so that a cached ``None`` is still a hit."""
        try:
            raw = await self._cache.get(key)
        except Exception as exc:
            log.warning("market_cache.read_failed", key=key, error=type(exc).__name__)
            return None
        if raw is None:
            return None
        try:
            envelope = json.loads(raw)
            return (adapter.validate_python(envelope["value"]),)
        except (ValueError, KeyError, TypeError, ValidationError) as exc:
            log.warning("market_cache.corrupt_entry", key=key, error=type(exc).__name__)
            return None

    async def _write[T](
        self, key: str, adapter: TypeAdapter[T], value: T, ttl_seconds: int
    ) -> None:
        envelope = json.dumps({"value": adapter.dump_python(value, mode="json")})
        try:
            await self._cache.set(key, envelope, ttl_seconds)
        except Exception as exc:
            log.warning("market_cache.write_failed", key=key, error=type(exc).__name__)
