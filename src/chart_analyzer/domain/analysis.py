"""The Analysis aggregate: one uploaded chart and everything we learned about it."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from chart_analyzer.domain.enums import AnalysisStatus, ErrorCode, Language, Stage
from chart_analyzer.domain.models import AnalysisResult, ChartRecognition, MarketSnapshot


@dataclass(frozen=True, slots=True)
class StoredImage:
    """Where the uploaded chart lives in the image storage."""

    key: str
    content_type: str


@dataclass(frozen=True, slots=True)
class NewAnalysis:
    """Data needed to admit an analysis."""

    id: UUID
    user_id: str
    language: Language
    image: StoredImage
    idempotency_key: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Analysis:
    """Read model of an analysis row.

    ``recognition`` and ``market_snapshot`` are checkpoints: once a step has produced its
    result it is stored here, and a retried task skips the step instead of paying again.
    State changes go through ``AnalysisRepository`` as single atomic statements, so this
    object is immutable.
    """

    id: UUID
    user_id: str
    language: Language
    status: AnalysisStatus
    stage: Stage
    image: StoredImage
    idempotency_key: str | None
    recognition: ChartRecognition | None
    market_snapshot: MarketSnapshot | None
    result: AnalysisResult | None
    error_code: ErrorCode | None
    tokens_used: int
    attempts: int
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
