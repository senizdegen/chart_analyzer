"""What the routers need from the outside, and how they get it."""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request

from chart_analyzer.api.errors import ApiError
from chart_analyzer.application.ports.services import ProgressReader
from chart_analyzer.application.queries import AnalysisQueries
from chart_analyzer.application.submit_analysis import SubmitAnalysis
from chart_analyzer.domain.enums import ErrorCode
from chart_analyzer.logs import bind_context

MAX_HEADER_VALUE_LENGTH = 128


@dataclass(frozen=True, slots=True)
class ApiServices:
    """Everything the HTTP layer uses. Built by ``bootstrap``, or by a test with fakes."""

    submit_analysis: SubmitAnalysis
    queries: AnalysisQueries
    progress: ProgressReader
    max_image_bytes: int


def get_services(request: Request) -> ApiServices:
    services: ApiServices = request.app.state.services
    return services


def get_user_id(x_user_id: Annotated[str | None, Header()] = None) -> str:
    """Identity of the caller.

    The assignment states that authorization happens upstream and ``x-user-id`` can be
    trusted, so the header is only checked for presence and a sane length.
    """
    user_id = (x_user_id or "").strip()
    if not user_id or len(user_id) > MAX_HEADER_VALUE_LENGTH:
        raise ApiError(ErrorCode.MISSING_USER_ID)
    bind_context(user_id=user_id)
    return user_id


Services = Annotated[ApiServices, Depends(get_services)]
UserId = Annotated[str, Depends(get_user_id)]
