"""Read-side representation of an analysis.

One shape serves both the HTTP API and the final progress event, so a client renders the
same object whether it got it from the stream or from ``GET /analyses/{id}``.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from chart_analyzer.application.localization import message_for
from chart_analyzer.domain.analysis import Analysis
from chart_analyzer.domain.enums import AnalysisStatus, AssetType, ErrorCode, Language, Stage
from chart_analyzer.domain.models import AnalysisResult


class AssetSummary(BaseModel):
    symbol: str
    name: str | None
    type: AssetType | None


class ErrorView(BaseModel):
    code: ErrorCode
    message: str


class AnalysisView(BaseModel):
    id: UUID
    status: AnalysisStatus
    stage: Stage
    language: Language
    asset: AssetSummary | None
    tokens_used: int
    created_at: datetime
    completed_at: datetime | None
    error: ErrorView | None
    result: AnalysisResult | None


def to_view(analysis: Analysis) -> AnalysisView:
    return AnalysisView(
        id=analysis.id,
        status=analysis.status,
        stage=analysis.stage,
        language=analysis.language,
        asset=_asset_summary(analysis),
        tokens_used=analysis.tokens_used,
        created_at=analysis.created_at,
        completed_at=analysis.completed_at,
        error=error_view(analysis.error_code, analysis.language),
        result=analysis.result,
    )


def error_view(code: ErrorCode | None, language: Language) -> ErrorView | None:
    if code is None:
        return None
    return ErrorView(code=code, message=message_for(code, language))


def _asset_summary(analysis: Analysis) -> AssetSummary | None:
    """Best known identity of the asset: market data if we have it, else what the AI read."""
    if analysis.market_snapshot is not None:
        asset = analysis.market_snapshot.asset
        return AssetSummary(symbol=asset.symbol, name=asset.name, type=asset.type)
    recognition = analysis.recognition
    if recognition is not None and recognition.symbol:
        return AssetSummary(
            symbol=recognition.symbol, name=recognition.asset_name, type=recognition.asset_type
        )
    return None
