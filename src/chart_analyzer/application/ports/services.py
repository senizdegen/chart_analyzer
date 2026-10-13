"""Ports to everything outside the process: AI, market data, files, queue, events, time."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from chart_analyzer.domain.analysis import StoredImage
from chart_analyzer.domain.enums import AssetType, Language
from chart_analyzer.domain.models import (
    AnalystReview,
    Asset,
    ChartInsights,
    ChartRecognition,
    CurrentPrice,
    MarketSnapshot,
    NewsArticle,
)

# --- images -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChartImage:
    data: bytes
    content_type: str


class ImageInspector(Protocol):
    def inspect(self, data: bytes) -> ChartImage:
        """Verify the bytes are a real image and normalize it (format, size).

        Raises ``InvalidImage``. CPU-bound: callers run it in a thread.
        """
        ...


class ImageStorage(Protocol):
    async def save(self, analysis_id: UUID, image: ChartImage) -> StoredImage: ...

    async def load(self, image: StoredImage) -> ChartImage: ...

    async def delete(self, image: StoredImage) -> None: ...


# --- AI ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AIAnswer[T]:
    value: T
    tokens: int


class ChartAI(Protocol):
    """Raises ``TransientFailure`` (provider down, unusable answer) or ``AnalysisFailure``."""

    async def recognize(self, image: ChartImage) -> AIAnswer[ChartRecognition]:
        """Cheap first call: is it a chart, and which asset?"""
        ...

    async def analyze(
        self, image: ChartImage, language: Language, snapshot: MarketSnapshot
    ) -> AIAnswer[ChartInsights]:
        """Full analysis, with free text written in ``language``."""
        ...


# --- market data ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AssetRef:
    """What recognition found: enough to look the asset up."""

    symbol: str
    type: AssetType
    name_hint: str | None = None


class MarketDataProvider(Protocol):
    """Raises ``AnalysisFailure(ASSET_NOT_FOUND)`` or ``TransientFailure(MARKET_DATA_UNAVAILABLE)``.

    The methods are granular so each can be cached with its own lifetime.
    """

    async def get_asset(self, ref: AssetRef) -> Asset: ...

    async def get_price(self, ref: AssetRef) -> CurrentPrice: ...

    async def get_analyst_review(self, ref: AssetRef) -> AnalystReview | None: ...

    async def get_news(self, ref: AssetRef) -> list[NewsArticle]: ...


# --- task queue -------------------------------------------------------------------------------


class TaskPublisher(Protocol):
    async def publish(self, analysis_id: UUID) -> None:
        """Hand an analysis to the workers. Returns only after the broker confirmed it."""
        ...


# --- progress events --------------------------------------------------------------------------


class ProgressEventType(StrEnum):
    STAGE = "stage"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def is_final(self) -> bool:
        return self is not ProgressEventType.STAGE


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    type: ProgressEventType
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StoredProgressEvent:
    id: str  # position in the stream; clients resume from it
    event: ProgressEvent


class ProgressPublisher(Protocol):
    async def publish(self, analysis_id: UUID, event: ProgressEvent) -> None: ...


class ProgressReader(Protocol):
    def read(
        self, analysis_id: UUID, *, after: str | None = None
    ) -> AsyncIterator[StoredProgressEvent | None]:
        """Yield stored events after position ``after`` (from the start when ``None``).

        Yields ``None`` whenever nothing arrived for a while, so the caller can send a
        keep-alive and check whether the client is still there. Never ends by itself.
        """
        ...


# --- time -------------------------------------------------------------------------------------


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, timezone-aware UTC."""
        ...
