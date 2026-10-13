"""Pure analysis logic: no I/O, no framework. This is the unit-tested core.

The AI model is treated as an untrusted source. Its answer is accepted only after the
checks in this module pass; a rejected answer is a retryable failure.
"""

import math
import re
from datetime import date

from chart_analyzer.domain.enums import (
    AnalystConsensus,
    AssetType,
    ErrorCode,
    Language,
    Position,
)
from chart_analyzer.domain.errors import AnalysisFailure, TransientFailure
from chart_analyzer.domain.models import (
    AnalysisResult,
    AnalystReview,
    ChartInsights,
    ChartRecognition,
    MarketSnapshot,
    NewsItem,
    RatingShare,
    TradingSignals,
)

_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9.\-]{1,12}$")
_CRYPTO_QUOTE_SUFFIXES = ("USDT", "USDC", "BUSD", "USD")
_PERPETUAL_MARKERS = (".P", "PERP")  # how exchanges label perpetual futures: BTCUSDT.P
_PRICE_BAND = 5.0  # levels further than 5x from the real price mean a wrong price scale


def normalize_symbol(raw: str, asset_type: AssetType) -> str:
    """Reduce what is printed on a chart to a base ticker.

    Charts label the same asset in many ways: ``NASDAQ:AAPL``, ``BTC/USDT``, ``BTCUSD``.
    Market data is requested by the base ticker (``AAPL``, ``BTC``).
    """
    symbol = raw.strip().upper()
    if ":" in symbol:
        symbol = symbol.rsplit(":", 1)[1]
    if asset_type is AssetType.CRYPTO:
        for marker in _PERPETUAL_MARKERS:
            if symbol.endswith(marker) and len(symbol) > len(marker):
                symbol = symbol[: -len(marker)]
                break
        symbol = re.split(r"[/\-_ ]", symbol, maxsplit=1)[0]
        for suffix in _CRYPTO_QUOTE_SUFFIXES:
            if symbol.endswith(suffix) and len(symbol) > len(suffix):
                symbol = symbol[: -len(suffix)]
                break
    return symbol


def resolve_recognized_asset(recognition: ChartRecognition) -> tuple[str, AssetType]:
    """Turn the recognition answer into a ticker we can look up, or fail permanently."""
    if not recognition.is_chart:
        raise AnalysisFailure(ErrorCode.UNCLEAR_CHART, "the image is not a trading chart")
    if not recognition.symbol or recognition.asset_type is None:
        raise AnalysisFailure(ErrorCode.UNCLEAR_CHART, "no ticker is visible on the chart")
    symbol = normalize_symbol(recognition.symbol, recognition.asset_type)
    if not _SYMBOL_PATTERN.match(symbol):
        raise AnalysisFailure(ErrorCode.UNCLEAR_CHART, f"unusable ticker {recognition.symbol!r}")
    return symbol, recognition.asset_type


def build_analyst_review(
    period: date, *, strong_buy: int, buy: int, hold: int, sell: int, strong_sell: int
) -> AnalystReview | None:
    """Convert raw recommendation counts into shares and a Buy/Hold/Sell consensus.

    Returns ``None`` when nobody covers the asset, so the client hides the block instead
    of showing invented numbers.
    """
    counts = (strong_buy, buy, hold, sell, strong_sell)
    if any(count < 0 for count in counts):
        raise ValueError("recommendation counts cannot be negative")
    total = sum(counts)
    if total == 0:
        return None

    def share(count: int) -> RatingShare:
        return RatingShare(count=count, percent=round(count * 100 / total, 1))

    bullish, bearish = strong_buy + buy, sell + strong_sell
    if bullish > hold and bullish > bearish:
        consensus = AnalystConsensus.BUY
    elif bearish > hold and bearish > bullish:
        consensus = AnalystConsensus.SELL
    else:
        consensus = AnalystConsensus.HOLD

    return AnalystReview(
        period=period,
        total_analysts=total,
        consensus=consensus,
        strong_buy=share(strong_buy),
        buy=share(buy),
        hold=share(hold),
        sell=share(sell),
        strong_sell=share(strong_sell),
    )


def validate_trading_signals(signals: TradingSignals) -> None:
    """Reject price levels that contradict the position the model itself chose.

    Long:  safety_stop < start_level < projected_exit_1 <= projected_exit_2
    Short: safety_stop > start_level > projected_exit_1 >= projected_exit_2
    """
    levels = {
        "start_level": signals.start_level,
        "safety_stop": signals.safety_stop,
        "projected_exit_1": signals.projected_exit_1,
        "projected_exit_2": signals.projected_exit_2,
        "support_level": signals.support_level,
        "resistance_level": signals.resistance_level,
    }
    for name, value in levels.items():
        if not math.isfinite(value) or value <= 0:
            raise _invalid(f"{name} must be a positive number, got {value}")

    if signals.support_level >= signals.resistance_level:
        raise _invalid("support_level must be below resistance_level")

    stop, start = signals.safety_stop, signals.start_level
    exit_1, exit_2 = signals.projected_exit_1, signals.projected_exit_2
    if signals.position is Position.LONG:
        ordered = stop < start < exit_1 <= exit_2
    else:
        ordered = stop > start > exit_1 >= exit_2
    if not ordered:
        raise _invalid(f"price levels are inconsistent with a {signals.position.value} position")


def validate_insights(insights: ChartInsights) -> None:
    """All checks an AI answer must pass before it is shown to a user."""
    texts = {
        "summary": insights.summary,
        "trend explanation": insights.key_insights.trend.explanation,
        "volatility explanation": insights.key_insights.volatility.explanation,
        "market sentiment explanation": insights.key_insights.market_sentiment.explanation,
        "rationale": insights.trading_signals.rationale,
    }
    for name, text in texts.items():
        if not text.strip():
            raise _invalid(f"{name} is empty")
    validate_trading_signals(insights.trading_signals)


def validate_price_scale(signals: TradingSignals, current_price: float) -> None:
    """Reject levels on a different scale than the real price (cents vs dollars, a wrong
    asset, a hallucinated axis). The band is wide on purpose: charts can be old."""
    low, high = current_price / _PRICE_BAND, current_price * _PRICE_BAND
    for name in (
        "start_level",
        "safety_stop",
        "projected_exit_1",
        "projected_exit_2",
        "support_level",
        "resistance_level",
    ):
        value = getattr(signals, name)
        if not low <= value <= high:
            raise _invalid(f"{name} {value} is far from the current price {current_price}")


def assemble_result(
    language: Language,
    snapshot: MarketSnapshot,
    insights: ChartInsights,
    *,
    max_news: int,
) -> AnalysisResult:
    """Merge validated AI insights with real market data into the final result.

    News facts (source, url, date, original headline) always come from the provider.
    The model only contributes the translated headline and the sentiment, and only for
    articles that really exist in the snapshot - ids it made up are dropped.
    """
    validate_insights(insights)
    validate_price_scale(insights.trading_signals, snapshot.price.price)

    articles = {article.id: article for article in snapshot.news}
    latest_news: list[NewsItem] = []
    seen: set[str] = set()
    for assessment in insights.news:
        article = articles.get(assessment.id)
        if article is None or assessment.id in seen:
            continue
        seen.add(assessment.id)
        latest_news.append(
            NewsItem(
                headline=assessment.headline.strip() or article.headline,
                original_headline=article.headline,
                sentiment=assessment.sentiment,
                source=article.source,
                url=article.url,
                published_at=article.published_at,
            )
        )
        if len(latest_news) == max_news:
            break

    return AnalysisResult(
        language=language,
        asset=snapshot.asset,
        summary=insights.summary.strip(),
        current_price=snapshot.price,
        analyst_review=snapshot.analyst_review,
        key_insights=insights.key_insights,
        trading_signals=insights.trading_signals,
        latest_news=latest_news,
    )


def _invalid(detail: str) -> TransientFailure:
    # Retryable on purpose: the model is not deterministic, a second answer is often valid.
    return TransientFailure(ErrorCode.INVALID_AI_RESPONSE, detail)
