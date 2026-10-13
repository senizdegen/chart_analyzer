"""Builders of progress events.

The worker publishes these while it works; the API rebuilds the final one from the
database when a client connects after the analysis has finished. Both go through the
same functions, so the client sees one shape whichever way the event reached it.
"""

from chart_analyzer.application.ports.services import ProgressEvent, ProgressEventType
from chart_analyzer.application.views import to_view
from chart_analyzer.domain.analysis import Analysis
from chart_analyzer.domain.enums import AnalysisStatus, Stage


def stage_event(stage: Stage) -> ProgressEvent:
    return ProgressEvent(ProgressEventType.STAGE, {"stage": stage.value})


def final_event(analysis: Analysis) -> ProgressEvent:
    """The closing event of a finished analysis: its full view, result or error included."""
    if not analysis.status.is_terminal:
        raise ValueError(f"analysis {analysis.id} is still {analysis.status.value}")
    event_type = (
        ProgressEventType.COMPLETED
        if analysis.status is AnalysisStatus.COMPLETED
        else ProgressEventType.FAILED
    )
    return ProgressEvent(event_type, to_view(analysis).model_dump(mode="json"))
