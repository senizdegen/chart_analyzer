"""Value objects of an analysis.

They are Pydantic models because each of them crosses a boundary where validation matters:
``ChartRecognition`` and ``ChartInsights`` come from the AI model, ``MarketSnapshot`` from an
external API, and all of them are stored as JSONB checkpoints and read back later.
"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from chart_analyzer.domain.enums import (
    AnalystConsensus,
    AssetType,
    Language,
    MarketSentiment,
    NewsSentiment,
    Position,
    Trend,
    Volatility,
)


class ValueObject(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- step 1: what the AI sees on the picture -------------------------------------------------


class ChartRecognition(ValueObject):
    """Answer of the cheap first AI call."""

    is_chart: bool
    symbol: str | None = None
    asset_name: str | None = None
    asset_type: AssetType | None = None


# --- step 2: real market data -----------------------------------------------------------------


class Asset(ValueObject):
    symbol: str
    name: str
    type: AssetType
    logo_url: str | None = None


class CurrentPrice(ValueObject):
    price: float
    currency: str
    change: float | None = None
    change_percent: float | None = None
    as_of: datetime


class RatingShare(ValueObject):
    count: int
    percent: float


class AnalystReview(ValueObject):
    """Distribution of analyst recommendations; absent for assets nobody covers (crypto)."""

    period: date
    total_analysts: int
    consensus: AnalystConsensus
    strong_buy: RatingShare
    buy: RatingShare
    hold: RatingShare
    sell: RatingShare
    strong_sell: RatingShare


class NewsArticle(ValueObject):
    """A news item exactly as the provider returned it (English)."""

    id: str
    headline: str
    summary: str = ""
    source: str
    url: str
    published_at: datetime


class MarketSnapshot(ValueObject):
    asset: Asset
    price: CurrentPrice
    analyst_review: AnalystReview | None = None
    news: list[NewsArticle] = Field(default_factory=list)


# --- step 3: the AI's reading of the chart ---------------------------------------------------


class TrendInsight(ValueObject):
    value: Trend
    explanation: str


class VolatilityInsight(ValueObject):
    value: Volatility
    explanation: str


class MarketSentimentInsight(ValueObject):
    value: MarketSentiment
    explanation: str


class KeyInsights(ValueObject):
    trend: TrendInsight
    volatility: VolatilityInsight
    market_sentiment: MarketSentimentInsight


class TradingSignals(ValueObject):
    position: Position
    start_level: float
    safety_stop: float
    projected_exit_1: float
    projected_exit_2: float
    support_level: float
    resistance_level: float
    rationale: str


class NewsAssessment(ValueObject):
    """The AI's pick of a relevant article: translated headline plus sentiment."""

    id: str
    headline: str
    sentiment: NewsSentiment


class ChartInsights(ValueObject):
    """Answer of the second, full AI call. Free text is in the user's language."""

    summary: str
    key_insights: KeyInsights
    trading_signals: TradingSignals
    news: list[NewsAssessment] = Field(default_factory=list)


# --- final result -----------------------------------------------------------------------------


class NewsItem(ValueObject):
    headline: str
    original_headline: str
    sentiment: NewsSentiment
    source: str
    url: str
    published_at: datetime


class AnalysisResult(ValueObject):
    """What the client renders: AI insights merged with real market data."""

    language: Language
    asset: Asset
    summary: str
    current_price: CurrentPrice
    analyst_review: AnalystReview | None
    key_insights: KeyInsights
    trading_signals: TradingSignals
    latest_news: list[NewsItem]
