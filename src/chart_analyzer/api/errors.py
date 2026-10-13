"""One error shape for the whole API: ``{"error": {"code": ..., "message": ...}}``.

``code`` is a stable ``ErrorCode``; ``message`` is ready to show, in the user's language.
"""

from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from chart_analyzer.application.localization import message_for
from chart_analyzer.domain.enums import ErrorCode, Language
from chart_analyzer.domain.errors import DomainError, InvalidImage, QuotaExceeded
from chart_analyzer.domain.quota import next_reset

log = structlog.get_logger(__name__)

_STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.MISSING_USER_ID: status.HTTP_401_UNAUTHORIZED,
    ErrorCode.INVALID_IMAGE: status.HTTP_422_UNPROCESSABLE_CONTENT,
    ErrorCode.IMAGE_TOO_LARGE: status.HTTP_413_CONTENT_TOO_LARGE,
    ErrorCode.UNSUPPORTED_LANGUAGE: status.HTTP_422_UNPROCESSABLE_CONTENT,
    ErrorCode.VALIDATION_ERROR: status.HTTP_422_UNPROCESSABLE_CONTENT,
    ErrorCode.QUOTA_EXCEEDED: status.HTTP_429_TOO_MANY_REQUESTS,
    ErrorCode.TOO_MANY_ACTIVE_ANALYSES: status.HTTP_429_TOO_MANY_REQUESTS,
    ErrorCode.ANALYSIS_NOT_FOUND: status.HTTP_404_NOT_FOUND,
}


class ApiError(Exception):
    """An error the API raises on purpose."""

    def __init__(self, code: ErrorCode, *, details: Any = None) -> None:
        super().__init__(code.value)
        self.code = code
        self.details = details


def request_language(request: Request) -> Language:
    """Language for error messages: the request's own choice, else ``Accept-Language``."""
    chosen = getattr(request.state, "language", None)
    if isinstance(chosen, Language):
        return chosen
    return parse_accept_language(request.headers.get("accept-language"))


def parse_accept_language(header: str | None) -> Language:
    """First supported language of an ``Accept-Language`` header; English otherwise."""
    for part in (header or "").split(","):
        tag = part.split(";", 1)[0].strip().lower()
        if not tag:
            continue
        for candidate in (tag, tag.split("-", 1)[0]):
            if candidate == "tl":  # ISO 639-1 code many clients send for Filipino
                candidate = "fil"
            if candidate in Language:
                return Language(candidate)
    return Language.EN


def error_response(
    request: Request,
    code: ErrorCode,
    *,
    status_code: int | None = None,
    details: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "error": {"code": code.value, "message": message_for(code, request_language(request))}
    }
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(
        body,
        status_code=status_code or _STATUS_BY_CODE.get(code, status.HTTP_400_BAD_REQUEST),
        headers=headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, error: ApiError) -> JSONResponse:
        return error_response(request, error.code, details=error.details)

    @app.exception_handler(InvalidImage)
    async def _invalid_image(request: Request, error: InvalidImage) -> JSONResponse:
        return error_response(request, error.code)

    @app.exception_handler(QuotaExceeded)
    async def _quota_exceeded(request: Request, error: QuotaExceeded) -> JSONResponse:
        headers = None
        if error.code is ErrorCode.QUOTA_EXCEEDED:
            now = datetime.now(UTC)
            headers = {"Retry-After": str(int((next_reset(now) - now).total_seconds()) + 1)}
        return error_response(request, error.code, headers=headers)

    @app.exception_handler(DomainError)
    async def _domain_error(request: Request, error: DomainError) -> JSONResponse:
        return error_response(request, error.code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        details = [
            {"field": ".".join(str(part) for part in item["loc"]), "problem": item["msg"]}
            for item in error.errors()
        ]
        return error_response(request, ErrorCode.VALIDATION_ERROR, details=details)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
        # Routing errors raised by the framework itself (unknown path, wrong method).
        code = (
            ErrorCode.ANALYSIS_NOT_FOUND
            if error.status_code == status.HTTP_404_NOT_FOUND
            else ErrorCode.VALIDATION_ERROR
        )
        return error_response(request, code, status_code=error.status_code)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, error: Exception) -> JSONResponse:
        log.exception("api.unhandled_error")
        return error_response(
            request, ErrorCode.INTERNAL_ERROR, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
