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
        assert POLICY.decide(QuotaUsage(460_000, 2)) is not QuotaDecision.ALLOWED

    def test_concurrency_cap(self) -> None:
        assert POLICY.decide(QuotaUsage(0, 2)) is QuotaDecision.ALLOWED
        assert POLICY.decide(QuotaUsage(0, 3)) is QuotaDecision.TOO_MANY_ACTIVE

    @pytest.mark.parametrize(
        ("used", "active"),
        [
            (460_000, 2),  # 460k + 2 x 20k reserved: the third reservation does not fit
            (480_000, 1),  # the last reservation of the day is taken by a running analysis
            (470_000, 1),
        ],
    )
    def test_blocked_only_by_reservations_of_running_analyses_is_not_the_daily_limit(
        self, used: int, active: int
    ) -> None:
        # The spend alone would still admit one more analysis ...
        assert POLICY.decide(QuotaUsage(used, 0)) is QuotaDecision.ALLOWED
        # ... so the user is told to wait for the running ones, not for tomorrow.
        assert POLICY.decide(QuotaUsage(used, active)) is QuotaDecision.TOO_MANY_ACTIVE

    @pytest.mark.parametrize("active", [0, 1, 2, 3, 10])
    def test_spent_quota_is_the_daily_limit_whatever_is_running(self, active: int) -> None:
        # used + one reservation > limit: waiting for running analyses cannot help today
        assert POLICY.decide(QuotaUsage(480_001, active)) is QuotaDecision.DAILY_LIMIT_REACHED
        assert POLICY.decide(QuotaUsage(500_000, active)) is QuotaDecision.DAILY_LIMIT_REACHED

    def test_daily_limit_starts_exactly_where_one_reservation_stops_fitting(self) -> None:
        assert POLICY.decide(QuotaUsage(480_000, 3)) is QuotaDecision.TOO_MANY_ACTIVE
        assert POLICY.decide(QuotaUsage(480_001, 3)) is QuotaDecision.DAILY_LIMIT_REACHED

    def test_an_allowed_analysis_always_has_a_full_reservation_and_a_free_slot(self) -> None:
        """Whatever the usage, admission never promises tokens that are already reserved."""
        policy = QuotaPolicy(daily_limit=100, reservation_tokens=30, max_active_analyses=3)
        allowed = [
            (used, active)
            for used in range(0, 131)
            for active in range(0, 5)
            if policy.decide(QuotaUsage(used, active)) is QuotaDecision.ALLOWED
        ]

        assert allowed  # the sweep does admit something
        for used, active in allowed:
            assert used + (active + 1) * 30 <= 100, (used, active)
            assert active < 3, (used, active)
        # and it admits everything that fits: nothing is refused without a reason
        assert len(allowed) == sum(
            1 for used in range(0, 131) for active in range(0, 3) if used + (active + 1) * 30 <= 100
        )


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
