"""Quota status, so a client can show the remaining budget before a request is rejected."""

from fastapi import APIRouter

from chart_analyzer.api.dependencies import Services, UserId
from chart_analyzer.api.schemas import QuotaResponse

router = APIRouter(prefix="/quota", tags=["quota"])


@router.get("", response_model=QuotaResponse, summary="Today's token quota")
async def get_quota(services: Services, user_id: UserId) -> QuotaResponse:
    """Tokens spent by successful analyses since 00:00 UTC and what is left."""
    return QuotaResponse.of(await services.queries.get_quota(user_id))
