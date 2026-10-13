"""Domain exceptions.

The split that matters to the worker is permanent vs transient:

* ``AnalysisFailure``  - retrying cannot help (not a chart, unknown asset). The analysis
  is marked failed right away and never counts against the quota.
* ``TransientFailure`` - the cause may go away (provider timeout, rate limit, a malformed
  AI answer). The task goes to the retry queue and, when attempts run out, to the DLQ.
"""

from chart_analyzer.domain.enums import ErrorCode


class DomainError(Exception):
    """Base class; always carries a stable ``ErrorCode``."""

    def __init__(self, code: ErrorCode, detail: str = "") -> None:
        super().__init__(detail or code.value)
        self.code = code
        self.detail = detail


class AnalysisFailure(DomainError):
    """Permanent failure of one analysis."""


class TransientFailure(DomainError):
    """Retryable failure of one analysis."""


class InvalidImage(DomainError):
    """The uploaded file is not an acceptable image."""


class QuotaExceeded(DomainError):
    """The user may not start another analysis right now."""
