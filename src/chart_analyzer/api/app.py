"""FastAPI application factory."""

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request, Response

from chart_analyzer.api.dependencies import ApiServices
from chart_analyzer.api.errors import error_response, install_error_handlers
from chart_analyzer.api.routers import analyses, demo, quota
from chart_analyzer.api.schemas import API_PREFIX
from chart_analyzer.domain.enums import ErrorCode
from chart_analyzer.logs import bind_context, clear_context

log = structlog.get_logger(__name__)

DESCRIPTION = """
Upload a trading chart image and get an AI analysis enriched with real market data.

**Flow:** `POST /analyses` returns `202` with an id → follow `GET /analyses/{id}/events`
(Server-Sent Events) for progress → the final event carries the full analysis, which is
also available from `GET /analyses/{id}`.

Every request needs the `x-user-id` header. Errors look like
`{"error": {"code": "...", "message": "..."}}` with the message in the user's language.
"""


def create_app(services: ApiServices | None = None) -> FastAPI:
    """Build the app.

    Called without arguments (production) the real adapters are wired on startup and
    closed on shutdown. Tests pass ready ``services`` built from fakes or test adapters.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if services is not None:
            yield
            return

        # Imported here so tests that inject services never load the real adapters.
        from chart_analyzer.bootstrap import build_api_services
        from chart_analyzer.config import get_settings
        from chart_analyzer.logs import configure_logging

        settings = get_settings()
        configure_logging(settings.log_level, json=settings.log_json)
        async with build_api_services(settings) as built:
            app.state.services = built
            log.info("api.started", environment=settings.environment)
            yield
        log.info("api.stopped")

    app = FastAPI(
        title="Chart Analyzer",
        version="1.0.0",
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    if services is not None:
        app.state.services = services
    install_error_handlers(app)
    app.middleware("http")(_upload_size_guard)
    app.middleware("http")(_request_context)  # added last, so it runs first
    app.include_router(analyses.router, prefix=API_PREFIX)
    app.include_router(quota.router, prefix=API_PREFIX)
    app.include_router(demo.router)

    @app.get("/health", tags=["service"], summary="Liveness probe")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


# Room for the multipart envelope and the small form fields around the image itself.
MULTIPART_OVERHEAD_BYTES = 64 * 1024


async def _upload_size_guard(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Refuse an oversized upload from its Content-Length, before the body is received.

    FastAPI parses the whole multipart body to a temporary file before the endpoint runs,
    so a check inside the endpoint would come only after gigabytes had been accepted.
    """
    if request.method == "POST":
        declared = request.headers.get("content-length", "")
        limit = request.app.state.services.max_image_bytes + MULTIPART_OVERHEAD_BYTES
        if declared.isdigit() and int(declared) > limit:
            return error_response(request, ErrorCode.IMAGE_TOO_LARGE)
    return await call_next(request)


async def _request_context(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Give every request an id, attach it to all log records and write one access line."""
    request_id = request.headers.get("x-request-id") or uuid4().hex
    clear_context()
    bind_context(request_id=request_id)
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    if request.url.path != "/health":
        log.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            # Bound by the endpoint in its own task, so read it from the header here.
            user_id=request.headers.get("x-user-id"),
        )
    return response
