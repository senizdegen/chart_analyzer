"""Market data from Finnhub (https://finnhub.io/docs/api).

Things the API does that shaped this adapter:

* An unknown symbol is not an error: ``/quote`` answers HTTP 200 with price 0.
* Crypto is quoted per exchange pair, so ``BTC`` is looked up as ``BINANCE:BTCUSDT``.
  A bare ``BTC`` would silently return an unrelated stock.
* There is no per-coin news feed; the crypto feed is filtered by ticker and name here.
* The free tier is rate limited (HTTP 429), hence one quick retry before giving up and
  leaving the rest to the worker's retry queue.
"""

import asyncio
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, Field, ValidationError

from chart_analyzer.application.ports.services import AssetRef, Clock
from chart_analyzer.domain.analysis_rules import build_analyst_review
from chart_analyzer.domain.enums import AssetType, ErrorCode
from chart_analyzer.domain.errors import AnalysisFailure, TransientFailure
from chart_analyzer.domain.models import AnalystReview, Asset, CurrentPrice, NewsArticle

log = structlog.get_logger(__name__)

_ATTEMPTS = 2
_PRICE_CURRENCY = "USD"  # the free tier only quotes US listings and USDT pairs
_MAX_UNIX_SECONDS = 2**32  # anything later is not a timestamp in seconds


class _Quote(BaseModel):
    c: float | None = None
    d: float | None = None
    dp: float | None = None
    t: int = Field(0, ge=0, lt=_MAX_UNIX_SECONDS)


class _Profile(BaseModel):
    name: str | None = None
    logo: str | None = None


class _Recommendation(BaseModel):
    period: date
    strong_buy: int = Field(0, alias="strongBuy", ge=0)
    buy: int = Field(0, ge=0)
    hold: int = Field(0, ge=0)
    sell: int = Field(0, ge=0)
    strong_sell: int = Field(0, alias="strongSell", ge=0)


class _NewsItem(BaseModel):
    id: int | str
    published: int = Field(alias="datetime", gt=0, lt=_MAX_UNIX_SECONDS)
    headline: str = ""
    summary: str = ""
    source: str = ""
    url: str = ""


class FinnhubMarketData:
    """``MarketDataProvider`` over a client that already carries the base URL and the token."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        clock: Clock,
        *,
        news_lookback_days: int,
        news_candidates: int,
        retry_pause_seconds: float = 0.5,
    ) -> None:
        self._client = client
        self._clock = clock
        self._news_lookback_days = news_lookback_days
        self._news_candidates = news_candidates
        self._retry_pause_seconds = retry_pause_seconds

    async def get_asset(self, ref: AssetRef) -> Asset:
        fallback_name = ref.name_hint or ref.symbol
        if ref.type is AssetType.CRYPTO:
            return Asset(symbol=ref.symbol, name=fallback_name, type=ref.type)
        path = "/stock/profile2"
        profile = _parse(_Profile, await self._get(path, {"symbol": ref.symbol}), path)
        return Asset(
            symbol=ref.symbol,
            name=profile.name or fallback_name,
            type=ref.type,
            logo_url=profile.logo or None,
        )

    async def get_price(self, ref: AssetRef) -> CurrentPrice:
        path = "/quote"
        quote = _parse(_Quote, await self._get(path, {"symbol": _quote_symbol(ref)}), path)
        if not quote.c:
            raise AnalysisFailure(ErrorCode.ASSET_NOT_FOUND, f"no quote for {ref.symbol}")
        return CurrentPrice(
            price=quote.c,
            currency=_PRICE_CURRENCY,
            change=quote.d,
            change_percent=quote.dp,
            as_of=datetime.fromtimestamp(quote.t, UTC) if quote.t else self._clock.now(),
        )

    async def get_analyst_review(self, ref: AssetRef) -> AnalystReview | None:
        if ref.type is AssetType.CRYPTO:
            return None
        path = "/stock/recommendation"
        periods = [
            _parse(_Recommendation, item, path)
            for item in _as_list(await self._get(path, {"symbol": ref.symbol}), path)
        ]
        if not periods:
            return None
        newest = max(periods, key=lambda item: item.period)
        return build_analyst_review(
            newest.period,
            strong_buy=newest.strong_buy,
            buy=newest.buy,
            hold=newest.hold,
            sell=newest.sell,
            strong_sell=newest.strong_sell,
        )

    async def get_news(self, ref: AssetRef) -> list[NewsArticle]:
        if ref.type is AssetType.CRYPTO:
            path = "/news"
            items = _news_items(await self._get(path, {"category": "crypto"}), path)
            mentions = _mention_matcher(ref)
            items = [item for item in items if mentions(f"{item.headline}\n{item.summary}")]
        else:
            path = "/company-news"
            today = self._clock.now().date()
            since = today - timedelta(days=self._news_lookback_days)
            params = {"symbol": ref.symbol, "from": since.isoformat(), "to": today.isoformat()}
            items = _news_items(await self._get(path, params), path)

        articles: list[NewsArticle] = []
        seen: set[str] = set()
        for item in sorted(items, key=lambda item: item.published, reverse=True):
            headline = item.headline.strip()
            if not headline or not item.url or headline in seen:
                continue
            seen.add(headline)
            articles.append(
                NewsArticle(
                    id=str(item.id),
                    headline=headline,
                    summary=item.summary.strip(),
                    source=item.source,
                    url=item.url,
                    published_at=datetime.fromtimestamp(item.published, UTC),
                )
            )
            if len(articles) == self._news_candidates:
                break
        return articles

    async def _get(self, path: str, params: dict[str, str]) -> Any:
        for attempt in range(1, _ATTEMPTS + 1):
            try:
                response = await self._client.get(path, params=params)
            except httpx.TransportError as exc:  # timeouts and connection errors
                log.warning("finnhub.request_failed", endpoint=path, error=type(exc).__name__)
            except httpx.HTTPError as exc:
                log.warning("finnhub.request_failed", endpoint=path, error=type(exc).__name__)
                raise _unavailable(f"{path}: {type(exc).__name__}") from exc
            else:
                status = response.status_code
                if response.is_success:
                    try:
                        return response.json()
                    except ValueError as exc:
                        log.warning("finnhub.unparseable_body", endpoint=path, status=status)
                        raise _unavailable(f"{path}: body is not JSON") from exc
                log.warning("finnhub.request_failed", endpoint=path, status=status)
                if status != httpx.codes.TOO_MANY_REQUESTS and status < 500:
                    raise _unavailable(f"{path}: HTTP {status}")
            if attempt < _ATTEMPTS:
                await asyncio.sleep(self._retry_pause_seconds)
        raise _unavailable(f"{path}: no usable answer after {_ATTEMPTS} attempts")


def _quote_symbol(ref: AssetRef) -> str:
    return f"BINANCE:{ref.symbol}USDT" if ref.type is AssetType.CRYPTO else ref.symbol


def _mention_matcher(ref: AssetRef) -> Callable[[str], bool]:
    """Does a text talk about this coin: its ticker in capitals or its name, as whole words."""
    patterns = [re.compile(rf"(?<!\w){re.escape(ref.symbol)}(?!\w)")]
    if ref.name_hint and ref.name_hint.strip():
        name = re.escape(ref.name_hint.strip())
        patterns.append(re.compile(rf"(?<!\w){name}(?!\w)", re.IGNORECASE))
    return lambda text: any(pattern.search(text) for pattern in patterns)


def _unavailable(detail: str) -> TransientFailure:
    return TransientFailure(ErrorCode.MARKET_DATA_UNAVAILABLE, detail)


def _parse[T: BaseModel](model: type[T], payload: Any, path: str) -> T:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        log.warning("finnhub.unexpected_body", endpoint=path, model=model.__name__)
        raise _unavailable(f"{path}: unexpected body") from exc


def _as_list(payload: Any, path: str) -> list[Any]:
    if not isinstance(payload, list):
        log.warning("finnhub.unexpected_body", endpoint=path, model="list")
        raise _unavailable(f"{path}: unexpected body")
    return payload


def _news_items(payload: Any, path: str) -> list[_NewsItem]:
    """One malformed article is skipped; it must not cost the user the whole analysis."""
    items: list[_NewsItem] = []
    for raw in _as_list(payload, path):
        try:
            items.append(_NewsItem.model_validate(raw))
        except ValidationError:
            continue
    return items
