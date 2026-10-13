"""The real clock. Time is a port so tests can pin it; this is the production side."""

from datetime import UTC, datetime


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)
