"""In-memory implementations of the application ports, shared by unit tests.

They follow the same contracts as the real adapters (see ``application/ports``), so the
use cases are tested without Postgres, RabbitMQ, Redis, Finnhub or OpenAI.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID

from chart_analyzer.application.ports.repositories import (
    ClaimResult,
    ClaimStatus,
    OutboxMessage,
)
from chart_analyzer.application.ports.services import (
    AIAnswer,
    AssetRef,
    ChartImage,
    ProgressEvent,
    ProgressEventType,
    StoredProgressEvent,
)
from chart_analyzer.domain.analysis import Analysis, NewAnalysis, StoredImage
from chart_analyzer.domain.analysis_rules import build_analyst_review
from chart_analyzer.domain.enums import (
    AnalysisStatus,
    AssetType,
    ErrorCode,
    Language,
    MarketSentiment,
    Position,
    Stage,
    Trend,
    Volatility,
)
from chart_analyzer.domain.models import (
    AnalysisResult,
    AnalystReview,
    Asset,
    ChartInsights,
    ChartRecognition,
    CurrentPrice,
    KeyInsights,
    MarketSentimentInsight,
    MarketSnapshot,
    NewsArticle,
    NewsAssessment,
    TradingSignals,
    TrendInsight,
    VolatilityInsight,
)
from chart_analyzer.domain.quota import QuotaUsage

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


# --- sample data ------------------------------------------------------------------------------


def make_recognition(**overrides: object) -> ChartRecognition:
    data: dict[str, object] = {
        "is_chart": True,
        "symbol": "AAPL",
        "asset_name": "Apple",
        "asset_type": AssetType.STOCK,
    }
    return ChartRecognition.model_validate(data | overrides)


def make_signals(**overrides: object) -> TradingSignals:
    data: dict[str, object] = {
        "position": Position.LONG,
        "start_level": 340.0,
        "safety_stop": 323.0,
        "projected_exit_1": 365.0,
        "projected_exit_2": 380.0,
        "support_level": 325.0,
        "resistance_level": 366.0,
        "rationale": "Price holds above the rising trend line.",
    }
    return TradingSignals.model_validate(data | overrides)


def make_insights(**overrides: object) -> ChartInsights:
    data: dict[str, object] = {
        "summary": "The chart shows an uptrend with resistance near 366.",
        "key_insights": KeyInsights(
            trend=TrendInsight(value=Trend.BULLISH, explanation="Higher highs and higher lows."),
            volatility=VolatilityInsight(value=Volatility.HIGH, explanation="Wide candles."),
            market_sentiment=MarketSentimentInsight(
                value=MarketSentiment.BULLISH, explanation="Buyers defend every dip."
            ),
        ),
        "trading_signals": make_signals(),
        "news": [
            NewsAssessment(id="n1", headline="Apple posts record results", sentiment="positive"),
            NewsAssessment(id="n2", headline="Regulators probe Apple", sentiment="negative"),
        ],
    }
    return ChartInsights.model_validate(data | overrides)


def make_review() -> AnalystReview:
    review = build_analyst_review(
        date(2026, 9, 1), strong_buy=12, buy=22, hold=15, sell=3, strong_sell=1
    )
    assert review is not None
    return review


def make_news(count: int = 3) -> list[NewsArticle]:
    return [
        NewsArticle(
            id=f"n{index}",
            headline=f"Original headline {index}",
            summary="",
            source="Yahoo",
            url=f"https://example.com/{index}",
            published_at=NOW - timedelta(hours=index),
        )
        for index in range(1, count + 1)
    ]


def make_snapshot(**overrides: object) -> MarketSnapshot:
    data: dict[str, object] = {
        "asset": Asset(symbol="AAPL", name="Apple Inc", type=AssetType.STOCK, logo_url=None),
        "price": CurrentPrice(
            price=340.97, currency="USD", change=4.33, change_percent=1.29, as_of=NOW
        ),
        "analyst_review": make_review(),
        "news": make_news(),
    }
    return MarketSnapshot.model_validate(data | overrides)


# --- persistence ------------------------------------------------------------------------------


@dataclass
class _Row:
    analysis: Analysis
    locked_at: datetime | None = None


class InMemoryDatabase:
    """Shared state behind every ``InMemoryUnitOfWork`` of one test."""

    def __init__(self) -> None:
        self.rows: dict[UUID, _Row] = {}
        self.outbox: list[tuple[int, UUID, datetime | None]] = []
        self.locked_users: list[str] = []
        self.commits = 0

    def analysis(self, analysis_id: UUID) -> Analysis:
        return self.rows[analysis_id].analysis

    def seed(self, analysis: Analysis) -> Analysis:
        self.rows[analysis.id] = _Row(analysis)
        return analysis


def make_analysis(analysis_id: UUID, **overrides: object) -> Analysis:
    base = Analysis(
        id=analysis_id,
        user_id="user-1",
        language=Language.EN,
        status=AnalysisStatus.QUEUED,
        stage=Stage.QUEUED,
        image=StoredImage(key=f"{analysis_id}.png", content_type="image/png"),
        idempotency_key=None,
        recognition=None,
        market_snapshot=None,
        result=None,
        error_code=None,
        tokens_used=0,
        attempts=0,
        created_at=NOW,
        updated_at=NOW,
        completed_at=None,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


class InMemoryAnalysisRepository:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def _update(self, analysis_id: UUID, **changes: object) -> Analysis:
        row = self._db.rows[analysis_id]
        row.analysis = replace(row.analysis, **changes)  # type: ignore[arg-type]
        return row.analysis

    async def add(self, new: NewAnalysis) -> Analysis:
        analysis = make_analysis(
            new.id,
            user_id=new.user_id,
            language=new.language,
            image=new.image,
            idempotency_key=new.idempotency_key,
            created_at=new.created_at,
            updated_at=new.created_at,
        )
        return self._db.seed(analysis)

    async def get(self, analysis_id: UUID) -> Analysis | None:
        row = self._db.rows.get(analysis_id)
        return row.analysis if row else None

    async def get_for_user(self, analysis_id: UUID, user_id: str) -> Analysis | None:
        analysis = await self.get(analysis_id)
        return analysis if analysis and analysis.user_id == user_id else None

    async def find_by_idempotency_key(self, user_id: str, key: str) -> Analysis | None:
        for row in self._db.rows.values():
            if row.analysis.user_id == user_id and row.analysis.idempotency_key == key:
                return row.analysis
        return None

    async def list_for_user(
        self, user_id: str, *, limit: int, offset: int
    ) -> tuple[Sequence[Analysis], int]:
        mine = [row.analysis for row in self._db.rows.values() if row.analysis.user_id == user_id]
        mine.sort(key=lambda analysis: analysis.created_at, reverse=True)
        return mine[offset : offset + limit], len(mine)

    async def get_quota_usage(self, user_id: str, day_start: datetime) -> QuotaUsage:
        used = active = 0
        for row in self._db.rows.values():
            analysis = row.analysis
            if analysis.user_id != user_id:
                continue
            if analysis.status.is_active:
                active += 1
            elif (
                analysis.status is AnalysisStatus.COMPLETED
                and analysis.completed_at is not None
                and analysis.completed_at >= day_start
            ):
                used += analysis.tokens_used
        return QuotaUsage(used_tokens=used, active_analyses=active)

    async def claim(self, analysis_id: UUID, *, now: datetime, lease: timedelta) -> ClaimResult:
        row = self._db.rows.get(analysis_id)
        if row is None:
            return ClaimResult(ClaimStatus.MISSING)
        analysis = row.analysis
        if analysis.status.is_terminal:
            return ClaimResult(ClaimStatus.FINISHED)
        lease_expired = row.locked_at is None or row.locked_at < now - lease
        if analysis.status is AnalysisStatus.PROCESSING and not lease_expired:
            return ClaimResult(ClaimStatus.BUSY)
        row.locked_at = now
        claimed = self._update(
            analysis_id, status=AnalysisStatus.PROCESSING, attempts=analysis.attempts + 1
        )
        return ClaimResult(ClaimStatus.CLAIMED, claimed)

    async def set_stage(self, analysis_id: UUID, stage: Stage) -> None:
        self._update(analysis_id, stage=stage)

    async def save_recognition(
        self, analysis_id: UUID, recognition: ChartRecognition, *, tokens: int
    ) -> None:
        current = self._db.analysis(analysis_id)
        self._update(analysis_id, recognition=recognition, tokens_used=current.tokens_used + tokens)

    async def save_market_snapshot(self, analysis_id: UUID, snapshot: MarketSnapshot) -> None:
        self._update(analysis_id, market_snapshot=snapshot)

    async def add_tokens(self, analysis_id: UUID, tokens: int) -> None:
        current = self._db.analysis(analysis_id)
        self._update(analysis_id, tokens_used=current.tokens_used + tokens)

    async def complete(
        self, analysis_id: UUID, result: AnalysisResult, *, tokens: int, now: datetime
    ) -> bool:
        current = self._db.analysis(analysis_id)
        if current.status is not AnalysisStatus.PROCESSING:
            return False
        self._update(
            analysis_id,
            status=AnalysisStatus.COMPLETED,
            stage=Stage.DONE,
            result=result,
            tokens_used=current.tokens_used + tokens,
            completed_at=now,
        )
        return True

    async def fail(self, analysis_id: UUID, code: ErrorCode, *, now: datetime) -> bool:
        if self._db.analysis(analysis_id).status.is_terminal:
            return False
        self._update(
            analysis_id,
            status=AnalysisStatus.FAILED,
            stage=Stage.DONE,
            error_code=code,
            completed_at=now,
        )
        return True

    async def release(self, analysis_id: UUID) -> None:
        if self._db.analysis(analysis_id).status is AnalysisStatus.PROCESSING:
            self._update(analysis_id, status=AnalysisStatus.QUEUED)
            self._db.rows[analysis_id].locked_at = None


class InMemoryOutboxRepository:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    async def add(self, analysis_id: UUID, *, now: datetime) -> None:
        self._db.outbox.append((len(self._db.outbox) + 1, analysis_id, None))

    async def fetch_unsent(self, limit: int) -> Sequence[OutboxMessage]:
        unsent = [
            OutboxMessage(id=message_id, analysis_id=analysis_id)
            for message_id, analysis_id, sent_at in self._db.outbox
            if sent_at is None
        ]
        return unsent[:limit]

    async def mark_sent(self, message_ids: Sequence[int], *, now: datetime) -> None:
        self._db.outbox = [
            (message_id, analysis_id, now if message_id in message_ids else sent_at)
            for message_id, analysis_id, sent_at in self._db.outbox
        ]


class InMemoryUnitOfWork:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db
        self.analyses = InMemoryAnalysisRepository(db)
        self.outbox = InMemoryOutboxRepository(db)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self._db.commits += 1

    async def lock_user(self, user_id: str) -> None:
        self._db.locked_users.append(user_id)


# --- outside world ----------------------------------------------------------------------------


class FixedClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


class FakeInspector:
    def inspect(self, data: bytes) -> ChartImage:
        return ChartImage(data=data, content_type="image/png")


class InMemoryImageStorage:
    def __init__(self) -> None:
        self.files: dict[str, ChartImage] = {}

    async def save(self, analysis_id: UUID, image: ChartImage) -> StoredImage:
        key = f"{analysis_id}.png"
        self.files[key] = image
        return StoredImage(key=key, content_type=image.content_type)

    async def load(self, image: StoredImage) -> ChartImage:
        return self.files.get(image.key, ChartImage(data=b"chart", content_type=image.content_type))

    async def delete(self, image: StoredImage) -> None:
        self.files.pop(image.key, None)


@dataclass
class FakeChartAI:
    recognition: ChartRecognition = field(default_factory=make_recognition)
    insights: ChartInsights = field(default_factory=make_insights)
    recognition_tokens: int = 1_200
    analysis_tokens: int = 6_800
    recognize_error: Exception | None = None
    analyze_error: Exception | None = None
    recognize_calls: int = 0
    analyze_calls: int = 0
    analyzed_languages: list[Language] = field(default_factory=list)

    async def recognize(self, image: ChartImage) -> AIAnswer[ChartRecognition]:
        self.recognize_calls += 1
        if self.recognize_error:
            raise self.recognize_error
        return AIAnswer(self.recognition, self.recognition_tokens)

    async def analyze(
        self, image: ChartImage, language: Language, snapshot: MarketSnapshot
    ) -> AIAnswer[ChartInsights]:
        self.analyze_calls += 1
        self.analyzed_languages.append(language)
        if self.analyze_error:
            raise self.analyze_error
        return AIAnswer(self.insights, self.analysis_tokens)


@dataclass
class FakeMarketData:
    snapshot: MarketSnapshot = field(default_factory=make_snapshot)
    error: Exception | None = None
    calls: int = 0
    requested: list[AssetRef] = field(default_factory=list)

    def _answer(self, ref: AssetRef) -> MarketSnapshot:
        self.calls += 1
        self.requested.append(ref)
        if self.error:
            raise self.error
        return self.snapshot

    async def get_asset(self, ref: AssetRef) -> Asset:
        return self._answer(ref).asset

    async def get_price(self, ref: AssetRef) -> CurrentPrice:
        return self._answer(ref).price

    async def get_analyst_review(self, ref: AssetRef) -> AnalystReview | None:
        return self._answer(ref).analyst_review

    async def get_news(self, ref: AssetRef) -> list[NewsArticle]:
        return self._answer(ref).news


class RecordingProgress:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[tuple[UUID, ProgressEvent]] = []
        self._fail = fail

    async def publish(self, analysis_id: UUID, event: ProgressEvent) -> None:
        if self._fail:
            raise ConnectionError("redis is down")
        self.events.append((analysis_id, event))

    def types(self) -> list[str]:
        """Stage names for stage events, the event type for the final one."""
        return [
            event.data["stage"] if event.type is ProgressEventType.STAGE else event.type.value
            for _, event in self.events
        ]


class RecordingTaskPublisher:
    def __init__(self, *, fail_after: int | None = None) -> None:
        self.published: list[UUID] = []
        self._fail_after = fail_after

    async def publish(self, analysis_id: UUID) -> None:
        if self._fail_after is not None and len(self.published) >= self._fail_after:
            raise ConnectionError("broker is down")
        self.published.append(analysis_id)


class InMemoryProgressStream:
    """Publisher and reader in one, like the Redis Stream adapter."""

    def __init__(self) -> None:
        self.events: dict[UUID, list[ProgressEvent]] = {}

    async def publish(self, analysis_id: UUID, event: ProgressEvent) -> None:
        self.events.setdefault(analysis_id, []).append(event)

    async def read(
        self, analysis_id: UUID, *, after: str | None = None
    ) -> AsyncIterator[StoredProgressEvent | None]:
        position = int(after) if after and after.isdigit() else 0
        while True:
            stored = self.events.get(analysis_id, [])
            if position < len(stored):
                position += 1
                yield StoredProgressEvent(id=str(position), event=stored[position - 1])
            else:
                await asyncio.sleep(0)
                yield None
