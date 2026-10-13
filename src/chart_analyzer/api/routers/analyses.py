"""Endpoints of the chart analysis flow."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Header, Query, Request, Response, UploadFile, status
from fastapi.responses import StreamingResponse

from chart_analyzer.api.dependencies import MAX_HEADER_VALUE_LENGTH, Services, UserId
from chart_analyzer.api.errors import ApiError
from chart_analyzer.api.schemas import (
    AnalysisListItem,
    AnalysisListResponse,
    AnalysisResponse,
    ErrorResponse,
)
from chart_analyzer.api.sse import analysis_events
from chart_analyzer.application.submit_analysis import SubmitAnalysisCommand
from chart_analyzer.domain.enums import ErrorCode, Language
from chart_analyzer.logs import bind_context

router = APIRouter(prefix="/analyses", tags=["analyses"])

_ERRORS: dict[int | str, dict[str, object]] = {
    status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "Missing x-user-id"},
    status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "No such analysis"},
}


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AnalysisResponse,
    summary="Upload a chart for analysis",
    responses={
        status.HTTP_200_OK: {
            "model": AnalysisResponse,
            "description": "Replay of an earlier request with the same Idempotency-Key",
        },
        status.HTTP_401_UNAUTHORIZED: _ERRORS[status.HTTP_401_UNAUTHORIZED],
        status.HTTP_413_CONTENT_TOO_LARGE: {"model": ErrorResponse},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "model": ErrorResponse,
            "description": "Daily token quota spent, or too many analyses in progress",
        },
    },
)
async def submit_analysis(
    request: Request,
    response: Response,
    services: Services,
    user_id: UserId,
    image: Annotated[UploadFile, File(description="Chart image: PNG, JPEG or WebP")],
    language: Annotated[
        str, Form(description="Language of the analysis: en, es, pt, de, tr, fr, it, id, fil")
    ] = Language.EN.value,
    idempotency_key: Annotated[
        str | None,
        Header(
            description="Client-generated key; repeating a request with the same key "
            "returns the original analysis instead of creating another one",
        ),
    ] = None,
) -> AnalysisResponse:
    """Accepts the chart and returns immediately.

    The analysis itself runs in a worker. Follow `events_url` for live progress, or poll
    `GET /analyses/{id}` until `status` is `completed` or `failed`.
    """
    if language not in Language:
        raise ApiError(ErrorCode.UNSUPPORTED_LANGUAGE)
    chosen_language = Language(language)
    request.state.language = chosen_language  # errors from here on use this language

    key = (idempotency_key or "").strip() or None
    if key is not None and len(key) > MAX_HEADER_VALUE_LENGTH:
        raise ApiError(ErrorCode.VALIDATION_ERROR, details="Idempotency-Key is too long")

    # Oversized requests are normally refused before the body is read (see the size guard
    # in api/app.py). This covers what that guard cannot see: a body sent without a
    # Content-Length. Reading one byte past the limit is enough to tell.
    data = await image.read(services.max_image_bytes + 1)
    if len(data) > services.max_image_bytes:
        raise ApiError(ErrorCode.IMAGE_TOO_LARGE)

    result = await services.submit_analysis.execute(
        SubmitAnalysisCommand(
            user_id=user_id, image_bytes=data, language=chosen_language, idempotency_key=key
        )
    )
    bind_context(analysis_id=str(result.analysis.id))
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return AnalysisResponse.of(result.analysis)


@router.get("", response_model=AnalysisListResponse, summary="Analysis history")
async def list_analyses(
    services: Services,
    user_id: UserId,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AnalysisListResponse:
    """The caller's analyses, newest first."""
    analyses, total = await services.queries.list(user_id, limit=limit, offset=offset)
    return AnalysisListResponse(
        items=[AnalysisListItem.of(analysis) for analysis in analyses],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{analysis_id}",
    response_model=AnalysisResponse,
    summary="One analysis",
    responses=_ERRORS,
)
async def get_analysis(analysis_id: UUID, services: Services, user_id: UserId) -> AnalysisResponse:
    analysis = await services.queries.get(analysis_id, user_id)
    if analysis is None:
        raise ApiError(ErrorCode.ANALYSIS_NOT_FOUND)
    return AnalysisResponse.of(analysis)


@router.get(
    "/{analysis_id}/events",
    summary="Live progress (Server-Sent Events)",
    response_class=StreamingResponse,
    responses={
        status.HTTP_200_OK: {
            "content": {"text/event-stream": {}},
            "description": "`stage` events (`recognizing`, `market_data`, `analyzing`), then "
            "one `completed` or `failed` event carrying the analysis.",
        },
        **_ERRORS,
    },
)
async def stream_analysis_events(
    analysis_id: UUID,
    request: Request,
    services: Services,
    user_id: UserId,
    last_event_id: Annotated[
        str | None, Header(description="Resume after this event id (sent by browsers on reconnect)")
    ] = None,
) -> StreamingResponse:
    analysis = await services.queries.get(analysis_id, user_id)
    if analysis is None:
        raise ApiError(ErrorCode.ANALYSIS_NOT_FOUND)
    bind_context(analysis_id=str(analysis_id))
    return StreamingResponse(
        analysis_events(
            request, analysis, user_id, services.queries, services.progress, last_event_id
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/{analysis_id}/image",
    summary="The uploaded chart image",
    response_class=Response,
    responses={status.HTTP_200_OK: {"content": {"image/*": {}}}, **_ERRORS},
)
async def get_analysis_image(analysis_id: UUID, services: Services, user_id: UserId) -> Response:
    image = await services.queries.get_image(analysis_id, user_id)
    if image is None:
        raise ApiError(ErrorCode.ANALYSIS_NOT_FOUND)
    return Response(
        image.data,
        media_type=image.content_type,
        headers={"Cache-Control": "private, max-age=86400, immutable"},
    )
