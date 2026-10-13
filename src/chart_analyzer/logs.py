"""Structured logging.

Every record is one JSON object. Identifiers bound with ``bind_context`` (request id,
user id, analysis id, attempt) are attached to all records emitted while the request or
task is being handled, so one analysis can be followed across the API and the worker by
filtering on ``analysis_id``.
"""

import logging
import sys
from typing import Any

import structlog


def configure_logging(level: str = "INFO", *, json: bool = True) -> None:
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]
    renderer: Any = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                renderer,
            ],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # Our own access log line replaces uvicorn's; library chatter stays at WARNING.
    for noisy in ("uvicorn.access", "httpx", "httpcore", "aio_pika", "aiormq", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def bind_context(**values: Any) -> None:
    structlog.contextvars.bind_contextvars(**values)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()
