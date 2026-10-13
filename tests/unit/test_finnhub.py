"""The Finnhub adapter against canned answers copied from the live API."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from chart_analyzer.application.ports.services import AssetRef
from chart_analyzer.domain.enums import AnalystConsensus, AssetType, ErrorCode
from chart_analyzer.domain.errors import AnalysisFailure, TransientFailure
from chart_analyzer.infrastructure.market.finnhub import FinnhubMarketData
from tests.fakes import NOW, FixedClock

AAPL = AssetRef(symbol="AAPL", type=AssetType.STOCK, name_hint="Apple")
BTC = AssetRef(symbol="BTC", type=AssetType.CRYPTO, name_hint="Bitcoin")

QUOTE = {"c": 340.97, "d": 4.33, "dp": 1.2862, "h": 342.0, "l": 335.1, "o": 336.0, "pc": 336.64}
QUOTE_TIME = 1791835200
UNKNOWN_QUOTE = {"c": 0, "d": None, "dp": None, "h": 0, "l": 0, "o": 0, "pc": 0, "t": 0}
PROFILE = {
    "name": "Apple Inc",
    "ticker": "AAPL",
    "logo": "https://static.finnhub.io/logo/aapl.png",
    "currency": "USD",
}
RECOMMENDATIONS = [
    {
        "symbol": "AAPL",
        "period": "2026-09-01",
        "strongBuy": 12,
        "buy": 22,
        "hold": 15,
        "sell": 3,
        "strongSell": 1,
    },
    {
        "symbol": "AAPL",
        "period": "2026-08-01",
        "strongBuy": 1,
        "buy": 1,
        "hold": 1,
        "sell": 30,
        "strongSell": 20,
    },
]

Handler = Callable[[httpx.Request], httpx.Response]


class Api:
    """A scripted Finnhub: answers each request with the next response and records it."""

    def __init__(self, *answers: httpx.Response | Exception | Any) -> None:
        self._answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


def provider(api: Handler, *, news_candidates: int = 12) -> FinnhubMarketData:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(api),
        base_url="https://finnhub.test/api/v1",
        headers={"X-Finnhub-Token": "test-token"},
    )
    return FinnhubMarketData(
        client,
        FixedClock(),
        news_lookback_days=7,
        news_candidates=news_candidates,
        retry_pause_seconds=0,
    )


def article(
    item_id: int, headline: str, *, at: int, summary: str = "", **extra: Any
) -> dict[str, Any]:
    return {
        "id": item_id,
        "datetime": at,
        "headline": headline,
        "summary": summary,
        "source": "Yahoo",
        "url": f"https://example.com/{item_id}",
        "related": "AAPL",
        "category": "company",
        "image": "",
    } | extra


# --- price ------------------------------------------------------------------------------------


async def test_stock_price() -> None:
    api = Api(QUOTE | {"t": QUOTE_TIME})

    price = await provider(api).get_price(AAPL)

    assert api.last.url.path == "/api/v1/quote"
    assert api.last.url.params["symbol"] == "AAPL"
    assert price.price == 340.97
    assert price.change == 4.33
    assert price.change_percent == 1.2862
    assert price.currency == "USD"
    assert price.as_of == datetime.fromtimestamp(QUOTE_TIME, UTC)
    assert price.as_of.tzinfo is not None


async def test_crypto_price_is_asked_as_a_binance_usdt_pair() -> None:
    api = Api(QUOTE | {"t": QUOTE_TIME})

    price = await provider(api).get_price(BTC)

    assert api.last.url.path == "/api/v1/quote"
    assert api.last.url.params["symbol"] == "BINANCE:BTCUSDT"
    assert price.currency == "USD"


@pytest.mark.parametrize("ref", [AAPL, BTC])
async def test_unknown_symbol_is_asset_not_found(ref: AssetRef) -> None:
    api = Api(UNKNOWN_QUOTE)

    with pytest.raises(AnalysisFailure) as failure:
        await provider(api).get_price(ref)

    assert failure.value.code is ErrorCode.ASSET_NOT_FOUND
    assert len(api.requests) == 1


@pytest.mark.parametrize("price", [-1.5, -0.0001, 0.0, None])
async def test_quote_without_a_positive_price_is_asset_not_found(price: float | None) -> None:
    api = Api(QUOTE | {"c": price, "t": QUOTE_TIME})

    with pytest.raises(AnalysisFailure) as failure:
        await provider(api).get_price(AAPL)

    assert failure.value.code is ErrorCode.ASSET_NOT_FOUND
    assert len(api.requests) == 1  # a definite answer: not retried


async def test_the_token_header_is_sent() -> None:
    api = Api(QUOTE | {"t": QUOTE_TIME})

    await provider(api).get_price(AAPL)

    assert api.last.headers["X-Finnhub-Token"] == "test-token"


# --- asset ------------------------------------------------------------------------------------


async def test_stock_asset_comes_from_the_profile() -> None:
    api = Api(PROFILE)

    asset = await provider(api).get_asset(AAPL)

    assert api.last.url.path == "/api/v1/stock/profile2"
    assert api.last.url.params["symbol"] == "AAPL"
    assert asset.symbol == "AAPL"
    assert asset.name == "Apple Inc"
    assert asset.type is AssetType.STOCK
    assert asset.logo_url == "https://static.finnhub.io/logo/aapl.png"


async def test_empty_profile_falls_back_to_the_name_hint_then_the_symbol() -> None:
    hinted = await provider(Api({})).get_asset(AAPL)
    bare = await provider(Api({})).get_asset(AssetRef(symbol="AAPL", type=AssetType.STOCK))

    assert (hinted.name, hinted.logo_url) == ("Apple", None)
    assert (bare.name, bare.logo_url) == ("AAPL", None)


async def test_crypto_asset_makes_no_http_call() -> None:
    api = Api({})

    hinted = await provider(api).get_asset(BTC)
    bare = await provider(api).get_asset(AssetRef(symbol="BTC", type=AssetType.CRYPTO))

    assert api.requests == []
    assert (hinted.symbol, hinted.name, hinted.type) == ("BTC", "Bitcoin", AssetType.CRYPTO)
    assert hinted.logo_url is None
    assert bare.name == "BTC"


# --- analyst review ---------------------------------------------------------------------------


async def test_analyst_review_uses_the_newest_period() -> None:
    api = Api(RECOMMENDATIONS)

    review = await provider(api).get_analyst_review(AAPL)

    assert api.last.url.path == "/api/v1/stock/recommendation"
    assert api.last.url.params["symbol"] == "AAPL"
    assert review is not None
    assert review.period == date(2026, 9, 1)
    assert review.total_analysts == 53
    assert review.consensus is AnalystConsensus.BUY
    assert (review.strong_buy.count, review.strong_sell.count) == (12, 1)


async def test_no_recommendations_is_no_review() -> None:
    assert await provider(Api([])).get_analyst_review(AAPL) is None


async def test_crypto_review_makes_no_http_call() -> None:
    api = Api(RECOMMENDATIONS)

    assert await provider(api).get_analyst_review(BTC) is None
    assert api.requests == []


# --- news -------------------------------------------------------------------------------------


async def test_stock_news_asks_for_the_lookback_window() -> None:
    api = Api([])

    assert await provider(api).get_news(AAPL) == []

    assert api.last.url.path == "/api/v1/company-news"
    assert dict(api.last.url.params) == {"symbol": "AAPL", "from": "2026-10-06", "to": "2026-10-13"}
    assert NOW.date() == date(2026, 10, 13)


async def test_stock_news_is_cleaned_ordered_and_capped() -> None:
    api = Api(
        [
            article(1, "Oldest", at=1000),
            article(2, "Newest", at=5000, summary=" Details "),
            article(3, "Repeated", at=3000),
            article(4, "Repeated", at=4000),
            article(5, "", at=6000),
            article(6, "No link", at=7000, url=""),
            article(7, "Middle", at=2000),
            {"headline": "No id and no time", "url": "https://example.com/x"},
        ]
    )

    news = await provider(api, news_candidates=3).get_news(AAPL)

    assert [item.headline for item in news] == ["Newest", "Repeated", "Middle"]
    assert [item.id for item in news] == ["2", "4", "7"]
    newest = news[0]
    assert newest.summary == "Details"
    assert newest.source == "Yahoo"
    assert newest.url == "https://example.com/2"
    assert newest.published_at == datetime.fromtimestamp(5000, UTC)
    assert newest.published_at.tzinfo is not None


async def test_crypto_news_keeps_only_articles_about_the_coin() -> None:
    api = Api(
        [
            article(1, "BTC breaks its record", at=9000),
            article(2, "Markets wrap", at=8000, summary="Traders moved into BTC."),
            article(3, "bitcoin miners expand", at=7000),
            article(4, "Why Bitcoin's rally matters", at=6000),
            article(5, "Ethereum upgrade ships", at=5000),
            article(6, "WBTC supply grows", at=4000),
            article(7, "Subtcase study of a hack", at=3000),
        ]
    )

    news = await provider(api).get_news(BTC)

    assert api.last.url.path == "/api/v1/news"
    assert dict(api.last.url.params) == {"category": "crypto"}
    assert [item.id for item in news] == ["1", "2", "3", "4"]


async def test_crypto_news_without_a_name_hint_matches_the_symbol_only() -> None:
    api = Api([article(1, "bitcoin miners expand", at=2000), article(2, "BTC dips", at=1000)])

    news = await provider(api).get_news(AssetRef(symbol="BTC", type=AssetType.CRYPTO))

    assert [item.id for item in news] == ["2"]


# --- failures ---------------------------------------------------------------------------------


async def test_rate_limit_then_success_after_one_retry() -> None:
    api = Api(httpx.Response(429, json={"error": "API limit reached."}), QUOTE | {"t": QUOTE_TIME})

    price = await provider(api).get_price(AAPL)

    assert price.price == 340.97
    assert len(api.requests) == 2


async def test_server_error_twice_is_transient() -> None:
    api = Api(httpx.Response(500, text="oops"))

    with pytest.raises(TransientFailure) as failure:
        await provider(api).get_price(AAPL)

    assert failure.value.code is ErrorCode.MARKET_DATA_UNAVAILABLE
    assert len(api.requests) == 2


@pytest.mark.parametrize(
    "error", [httpx.ReadTimeout("too slow"), httpx.ConnectError("connection refused")]
)
async def test_timeouts_and_connection_errors_are_transient(error: Exception) -> None:
    api = Api(error)

    with pytest.raises(TransientFailure) as failure:
        await provider(api).get_news(AAPL)

    assert failure.value.code is ErrorCode.MARKET_DATA_UNAVAILABLE
    assert len(api.requests) == 2


async def test_timeout_then_success_after_one_retry() -> None:
    api = Api(httpx.ConnectTimeout("too slow"), PROFILE)

    assert (await provider(api).get_asset(AAPL)).name == "Apple Inc"
    assert len(api.requests) == 2


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_key_is_transient_without_a_retry(status: int) -> None:
    api = Api(httpx.Response(status, json={"error": "You don't have access to this resource."}))

    with pytest.raises(TransientFailure) as failure:
        await provider(api).get_analyst_review(AAPL)

    assert failure.value.code is ErrorCode.MARKET_DATA_UNAVAILABLE
    assert len(api.requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        httpx.Response(200, text="<html>maintenance</html>"),
        httpx.Response(200, json=["not", "a", "quote"]),
        httpx.Response(200, json={"c": "a lot", "t": QUOTE_TIME}),
    ],
)
async def test_unparseable_body_is_transient_without_a_retry(body: httpx.Response) -> None:
    api = Api(body)

    with pytest.raises(TransientFailure) as failure:
        await provider(api).get_price(AAPL)

    assert failure.value.code is ErrorCode.MARKET_DATA_UNAVAILABLE
    assert len(api.requests) == 1


async def test_news_that_is_not_a_list_is_transient() -> None:
    with pytest.raises(TransientFailure):
        await provider(Api({"error": "unexpected"})).get_news(AAPL)
