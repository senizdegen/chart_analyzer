"""Wire format of the two AI answers.

Strict Structured Outputs only accept schemas in which every object is closed
(``additionalProperties: false``) and lists all its properties as required; an optional
value has to be a required nullable one. The domain models have defaults, so the answers
get their own models here, and each is converted into its domain model right after parsing.

``$ref``s are inlined because the request may travel through a proxy that translates the
schema for another provider, and those translations handle references unevenly.
"""

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from chart_analyzer.domain.enums import (
    AssetType,
    MarketSentiment,
    NewsSentiment,
    Position,
    Trend,
    Volatility,
)
from chart_analyzer.domain.models import ChartInsights, ChartRecognition


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- recognition ------------------------------------------------------------------------------


class RecognitionAnswer(_WireModel):
    is_chart: bool = Field(description="True only if the image is a price chart.")
    symbol: str | None = Field(
        description="Ticker exactly as printed on the chart; null if no ticker is visible."
    )
    asset_name: str | None = Field(
        description="Common name of the asset; null if the asset is not identified."
    )
    asset_type: AssetType | None = Field(
        description="null for forex, commodities, indices, funds or anything uncertain."
    )


# --- analysis ---------------------------------------------------------------------------------


class TrendAnswer(_WireModel):
    value: Trend
    explanation: str = Field(description="1-2 sentences in the target language.")


class VolatilityAnswer(_WireModel):
    value: Volatility
    explanation: str = Field(description="1-2 sentences in the target language.")


class MarketSentimentAnswer(_WireModel):
    value: MarketSentiment
    explanation: str = Field(description="1-2 sentences in the target language.")


class KeyInsightsAnswer(_WireModel):
    trend: TrendAnswer
    volatility: VolatilityAnswer
    market_sentiment: MarketSentimentAnswer


class TradingSignalsAnswer(_WireModel):
    position: Position
    start_level: float = Field(description="Entry price; positive.")
    safety_stop: float = Field(
        description="Stop-loss price; below start_level for long, above it for short."
    )
    projected_exit_1: float = Field(
        description="First target; above start_level for long, below it for short."
    )
    projected_exit_2: float = Field(
        description="Second target; at or beyond projected_exit_1 in the direction of the trade."
    )
    support_level: float = Field(description="Nearest support; below resistance_level.")
    resistance_level: float = Field(description="Nearest resistance; above support_level.")
    rationale: str = Field(description="1-2 sentences in the target language.")


class NewsAnswer(_WireModel):
    id: str = Field(description="Id of a candidate news item, copied unchanged.")
    headline: str = Field(description="The headline translated into the target language.")
    sentiment: NewsSentiment = Field(description="Effect of the news on this asset.")


class InsightsAnswer(_WireModel):
    summary: str = Field(description="2-4 sentences in the target language.")
    key_insights: KeyInsightsAnswer
    trading_signals: TradingSignalsAnswer
    news: list[NewsAnswer] = Field(
        description="Relevant candidate news, most relevant first; empty if none is relevant."
    )


# --- formats ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AnswerFormat[T]:
    """Everything the adapter needs to request one kind of answer and to read it back."""

    name: str
    schema: dict[str, object]
    parse: Callable[[str], T]
    """Raises ``pydantic.ValidationError`` for invalid JSON or JSON that breaks the schema."""


def strict_json_schema(model: type[BaseModel]) -> dict[str, object]:
    """JSON schema of ``model`` with every ``$ref`` replaced by its definition."""
    schema = model.model_json_schema()
    definitions: dict[str, object] = schema.pop("$defs", {})

    def inline(node: object) -> object:
        if isinstance(node, list):
            return [inline(item) for item in node]
        if not isinstance(node, dict):
            return node
        resolved = {key: inline(value) for key, value in node.items() if key != "$ref"}
        if "$ref" in node:
            definition = inline(definitions[node["$ref"].removeprefix("#/$defs/")])
            assert isinstance(definition, dict)
            return definition | resolved
        return resolved

    inlined = inline(schema)
    assert isinstance(inlined, dict)
    return inlined


def _parse_recognition(raw: str) -> ChartRecognition:
    answer = RecognitionAnswer.model_validate_json(raw)
    return ChartRecognition.model_validate(answer.model_dump())


def _parse_insights(raw: str) -> ChartInsights:
    answer = InsightsAnswer.model_validate_json(raw)
    return ChartInsights.model_validate(answer.model_dump())


RECOGNITION_FORMAT = AnswerFormat(
    name="chart_recognition",
    schema=strict_json_schema(RecognitionAnswer),
    parse=_parse_recognition,
)

ANALYSIS_FORMAT = AnswerFormat(
    name="chart_insights",
    schema=strict_json_schema(InsightsAnswer),
    parse=_parse_insights,
)
