"""Daily token quota with reservations."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from chart_analyzer.domain.quota import (
    QuotaDecision,
    QuotaPolicy,
    QuotaUsage,
    next_reset,
    utc_day_start,
)

POLICY = QuotaPolicy(daily_limit=500_000, reservation_tokens=20_000, max_active_analyses=3)


class TestDecide:
    def test_fresh_user_is_allowed(self) -> None:
        assert POLICY.decide(QuotaUsage(0, 0)) is QuotaDecision.ALLOWED

    def test_allowed_when_the_reservation_exactly_fits(self) -> None:
        assert POLICY.decide(QuotaUsage(480_000, 0)) is QuotaDecision.ALLOWED

    def test_rejected_when_the_reservation_does_not_fit(self) -> None:
        assert POLICY.decide(QuotaUsage(480_001, 0)) is QuotaDecision.DAILY_LIMIT_REACHED

    def test_rejected_at_and_over_the_limit(self) -> None:
        assert POLICY.decide(QuotaUsage(500_000, 0)) is QuotaDecision.DAILY_LIMIT_REACHED
        assert POLICY.decide(QuotaUsage(650_000, 0)) is QuotaDecision.DAILY_LIMIT_REACHED

    def test_active_analyses_hold_reservations(self) -> None:
        # 460k spent + one running (20k reserved) leaves room for exactly one more.
        assert POLICY.decide(QuotaUsage(460_000, 0)) is QuotaDecision.ALLOWED
        assert POLICY.decide(QuotaUsage(460_000, 1)) is QuotaDecision.ALLOWED
        assert POLICY.decide(QuotaUsage(460_000, 2)) is QuotaDecision.DAILY_LIMIT_REACHED

    def test_concurrency_cap(self) -> None:
        assert POLICY.decide(QuotaUsage(0, 2)) is QuotaDecision.ALLOWED
        assert POLICY.decide(QuotaUsage(0, 3)) is QuotaDecision.TOO_MANY_ACTIVE

    def test_admitted_analyses_can_never_exceed_the_limit(self) -> None:
        """Admit greedily; even if every analysis spends its full reservation we stay within."""
        used = 0
        while POLICY.decide(QuotaUsage(used, 0)) is QuotaDecision.ALLOWED:
            used += POLICY.reservation_tokens
        assert used <= POLICY.daily_limit


class TestAccounting:
    def test_reserved_and_remaining(self) -> None:
        usage = QuotaUsage(used_tokens=100_000, active_analyses=2)
        assert POLICY.reserved_tokens(usage) == 40_000
        assert POLICY.remaining_tokens(usage) == 360_000

    def test_remaining_never_goes_negative(self) -> None:
        assert POLICY.remaining_tokens(QuotaUsage(600_000, 1)) == 0


class TestPolicyValidation:
    @pytest.mark.parametrize(
        ("limit", "reservation", "active"),
        [(0, 1, 1), (10, 0, 1), (10, 5, 0), (10, 11, 1)],
    )
    def test_rejects_nonsense_settings(self, limit: int, reservation: int, active: int) -> None:
        with pytest.raises(ValueError):
            QuotaPolicy(limit, reservation, active)


class TestQuotaDay:
    def test_day_starts_at_utc_midnight(self) -> None:
        now = datetime(2026, 10, 13, 23, 59, 59, tzinfo=UTC)
        assert utc_day_start(now) == datetime(2026, 10, 13, tzinfo=UTC)
        assert next_reset(now) == datetime(2026, 10, 14, tzinfo=UTC)

    def test_local_time_is_converted_to_utc_first(self) -> None:
        # 02:00 on the 14th in UTC+5 is still the 13th in UTC.
        almaty = timezone(timedelta(hours=5))
        now = datetime(2026, 10, 14, 2, 0, tzinfo=almaty)
        assert utc_day_start(now) == datetime(2026, 10, 13, tzinfo=UTC)
