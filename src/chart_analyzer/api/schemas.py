"""Response bodies of the HTTP API."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from chart_analyzer.application.queries import QuotaStatus
from chart_analyzer.application.views import AnalysisView, AssetSummary, ErrorView, to_view
from chart_analyzer.domain.analysis import Analysis
from chart_analyzer.domain.enums import AnalysisStatus, ErrorCode, Language, Stage

API_PREFIX = "/api/v1"


def image_url(analysis_id: UUID) -> str:
    return f"{API_PREFIX}/analyses/{analysis_id}/image"


def events_url(analysis_id: UUID) -> str:
    return f"{API_PREFIX}/analyses/{analysis_id}/events"


class AnalysisResponse(AnalysisView):
    """A single analysis. ``result`` is set once ``status`` is ``completed``."""

    image_url: str
    events_url: str

    @classmethod
    def of(cls, analysis: Analysis) -> "AnalysisResponse":
        return cls(
            **to_view(analysis).model_dump(),
            image_url=image_url(analysis.id),
            events_url=events_url(analysis.id),
        )


class AnalysisListItem(BaseModel):
    """History entry: enough for a list row, without the full result."""

    id: UUID
    status: AnalysisStatus
    stage: Stage
    language: Language
    asset: AssetSummary | None
    created_at: datetime
    completed_at: datetime | None
    error: ErrorView | None
    image_url: str

    @classmethod
    def of(cls, analysis: Analysis) -> "AnalysisListItem":
        view = to_view(analysis)
        return cls(
            id=view.id,
            status=view.status,
            stage=view.stage,
            language=view.language,
            asset=view.asset,
            created_at=view.created_at,
            completed_at=view.completed_at,
            error=view.error,
            image_url=image_url(view.id),
        )


class AnalysisListResponse(BaseModel):
    items: list[AnalysisListItem]
    total: int
    limit: int
    offset: int


class QuotaResponse(BaseModel):
    """Today's token budget. ``reserved_tokens`` belongs to analyses still in progress."""

    daily_limit: int
    used_tokens: int
    reserved_tokens: int
    remaining_tokens: int
    resets_at: datetime

    @classmethod
    def of(cls, quota: QuotaStatus) -> "QuotaResponse":
        return cls(
            daily_limit=quota.daily_limit,
            used_tokens=quota.used_tokens,
            reserved_tokens=quota.reserved_tokens,
            remaining_tokens=quota.remaining_tokens,
            resets_at=quota.resets_at,
        )


class ErrorBody(BaseModel):
    code: ErrorCode
    message: str
    details: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
