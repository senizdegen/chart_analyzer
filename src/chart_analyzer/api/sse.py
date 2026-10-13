"""Server-Sent Events: progress of one analysis.

The stream replays stored events first, so a client may connect late or reconnect with
``Last-Event-ID`` and still see every stage. It ends with a ``completed`` or ``failed``
event that carries the full analysis.
"""

import json
from collections.abc import AsyncIterator
from uuid import UUID

import structlog
from fastapi import Request

from chart_analyzer.application.ports.services import ProgressEvent, ProgressReader
from chart_analyzer.application.progress_events import final_event
from chart_analyzer.application.queries import AnalysisQueries
from chart_analyzer.domain.analysis import Analysis

log = structlog.get_logger(__name__)

KEEP_ALIVE = ": keep-alive\n\n"


def format_event(event: ProgressEvent, event_id: str | None = None) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event.type.value}")
    lines.append(f"data: {json.dumps(event.data, ensure_ascii=False, separators=(',', ':'))}")
    return "\n".join(lines) + "\n\n"


async def analysis_events(
    request: Request,
    analysis: Analysis,
    user_id: str,
    queries: AnalysisQueries,
    progress: ProgressReader,
    last_event_id: str | None,
) -> AsyncIterator[str]:
    """Yield SSE frames until the analysis finishes or the client goes away."""
    if analysis.status.is_terminal:
        # Finished before the client connected (or the stored events have expired):
        # the database is the source of truth, answer from it right away.
        yield format_event(final_event(analysis))
        return

    try:
        async for stored in progress.read(analysis.id, after=last_event_id):
            if await request.is_disconnected():
                log.info("sse.client_disconnected")
                return
            if stored is not None:
                yield format_event(stored.event, stored.id)
                if stored.event.type.is_final:
                    return
                continue

            # Nothing new for a while. The final event may have been lost (progress is
            # best effort), so check the database before going back to waiting.
            finished = await _finished(queries, analysis.id, user_id)
            if finished is not None:
                yield format_event(final_event(finished))
                return
            yield KEEP_ALIVE
    except Exception:
        # The event store failed mid-stream. Close the response: the client reconnects
        # with Last-Event-ID, and a finished analysis is then answered from the database.
        log.warning("sse.stream_failed", exc_info=True)


async def _finished(queries: AnalysisQueries, analysis_id: UUID, user_id: str) -> Analysis | None:
    current = await queries.get(analysis_id, user_id)
    return current if current is not None and current.status.is_terminal else None
