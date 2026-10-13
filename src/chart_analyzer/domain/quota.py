"""Daily token quota with reservations.

The cost of an analysis is known only after the AI answers, so admission cannot compare
"cost of this request" with "tokens left". Instead every analysis that is still running
holds a reservation of ``reservation_tokens`` - an upper bound on what one analysis can
spend. A new analysis is admitted only if

    used_today + (active + 1) * reservation_tokens <= daily_limit

The reservation is not stored anywhere: it is the number of active analyses, so it is
released by the same status change that finishes the analysis and can never leak.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class QuotaUsage:
    """Snapshot read under the per-user lock."""

    used_tokens: int
    active_analyses: int


class QuotaDecision(StrEnum):
    ALLOWED = "allowed"
    DAILY_LIMIT_REACHED = "daily_limit_reached"
    TOO_MANY_ACTIVE = "too_many_active"


@dataclass(frozen=True, slots=True)
class QuotaPolicy:
    daily_limit: int
    reservation_tokens: int
    max_active_analyses: int

    def __post_init__(self) -> None:
        if self.daily_limit <= 0 or self.reservation_tokens <= 0 or self.max_active_analyses <= 0:
            raise ValueError("quota settings must be positive")
        if self.reservation_tokens > self.daily_limit:
            raise ValueError("a single reservation cannot exceed the daily limit")

    def reserved_tokens(self, usage: QuotaUsage) -> int:
        return usage.active_analyses * self.reservation_tokens

    def remaining_tokens(self, usage: QuotaUsage) -> int:
        return max(self.daily_limit - usage.used_tokens - self.reserved_tokens(usage), 0)

    def decide(self, usage: QuotaUsage) -> QuotaDecision:
        """May the user start one more analysis?"""
        committed = usage.used_tokens + self.reserved_tokens(usage)
        if committed + self.reservation_tokens > self.daily_limit:
            return QuotaDecision.DAILY_LIMIT_REACHED
        if usage.active_analyses >= self.max_active_analyses:
            return QuotaDecision.TOO_MANY_ACTIVE
        return QuotaDecision.ALLOWED


def utc_day_start(now: datetime) -> datetime:
    """Start of the current quota day. The quota resets at 00:00 UTC."""
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def next_reset(now: datetime) -> datetime:
    return utc_day_start(now) + timedelta(days=1)
