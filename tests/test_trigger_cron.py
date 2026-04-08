"""Tests for cron expression matching."""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from untether.triggers.cron import _parse_field, _resolve_now, cron_matches


class TestCronMatches:
    def test_every_minute(self):
        now = datetime.datetime(2026, 2, 24, 10, 30)
        assert cron_matches("* * * * *", now) is True

    def test_specific_minute_match(self):
        now = datetime.datetime(2026, 2, 24, 9, 0)
        assert cron_matches("0 9 * * *", now) is True

    def test_specific_minute_no_match(self):
        now = datetime.datetime(2026, 2, 24, 9, 5)
        assert cron_matches("0 9 * * *", now) is False

    def test_weekday_match(self):
        # 2026-02-24 is a Tuesday (weekday 1 in Python, cron day-of-week 2)
        now = datetime.datetime(2026, 2, 24, 9, 0)
        assert cron_matches("0 9 * * 1-5", now) is True

    def test_weekend_no_match_on_weekday(self):
        # Tuesday
        now = datetime.datetime(2026, 2, 24, 9, 0)
        assert cron_matches("0 9 * * 0,6", now) is False

    def test_sunday_match_with_0(self):
        # 2026-03-01 is a Sunday
        now = datetime.datetime(2026, 3, 1, 10, 0)
        assert cron_matches("0 10 * * 0", now) is True

    def test_sunday_match_with_7(self):
        now = datetime.datetime(2026, 3, 1, 10, 0)
        assert cron_matches("0 10 * * 7", now) is True

    def test_step_expression(self):
        now = datetime.datetime(2026, 2, 24, 10, 0)
        assert cron_matches("*/15 * * * *", now) is True  # 0 is in 0,15,30,45
        now2 = datetime.datetime(2026, 2, 24, 10, 7)
        assert cron_matches("*/15 * * * *", now2) is False

    def test_range_expression(self):
        now = datetime.datetime(2026, 2, 24, 14, 0)
        assert cron_matches("0 9-17 * * *", now) is True
        now2 = datetime.datetime(2026, 2, 24, 20, 0)
        assert cron_matches("0 9-17 * * *", now2) is False

    def test_month_filter(self):
        now = datetime.datetime(2026, 6, 1, 0, 0)
        assert cron_matches("0 0 1 6 *", now) is True
        now2 = datetime.datetime(2026, 7, 1, 0, 0)
        assert cron_matches("0 0 1 6 *", now2) is False

    def test_invalid_expression_returns_false(self):
        now = datetime.datetime(2026, 2, 24, 10, 0)
        assert cron_matches("not a cron", now) is False
        assert cron_matches("* *", now) is False

    def test_comma_separated_values(self):
        now = datetime.datetime(2026, 2, 24, 10, 30)
        assert cron_matches("0,30 * * * *", now) is True
        now2 = datetime.datetime(2026, 2, 24, 10, 15)
        assert cron_matches("0,30 * * * *", now2) is False


class TestResolveNow:
    """Timezone-aware now resolution for cron matching."""

    def test_melbourne_converts_utc(self):
        # 2026-02-24 22:00 UTC = 2026-02-25 09:00 AEDT (+11)
        utc_now = datetime.datetime(2026, 2, 24, 22, 0, tzinfo=datetime.UTC)
        local_now = _resolve_now(utc_now, "Australia/Melbourne", None)
        assert local_now.hour == 9
        assert local_now.day == 25
        assert cron_matches("0 9 * * *", local_now) is True

    def test_no_timezone_returns_naive_local(self):
        utc_now = datetime.datetime(2026, 2, 24, 10, 0, tzinfo=datetime.UTC)
        local_now = _resolve_now(utc_now, None, None)
        assert local_now.tzinfo is None

    def test_per_cron_overrides_default(self):
        utc_now = datetime.datetime(2026, 2, 24, 22, 0, tzinfo=datetime.UTC)
        mel = _resolve_now(utc_now, "Australia/Melbourne", "US/Eastern")
        expected = utc_now.astimezone(ZoneInfo("Australia/Melbourne"))
        assert mel.hour == expected.hour
        assert mel.day == expected.day

    def test_default_used_when_cron_none(self):
        utc_now = datetime.datetime(2026, 2, 24, 22, 0, tzinfo=datetime.UTC)
        local_now = _resolve_now(utc_now, None, "Australia/Melbourne")
        expected = utc_now.astimezone(ZoneInfo("Australia/Melbourne"))
        assert local_now.hour == expected.hour

    def test_dst_transition(self):
        # 2025-10-05 01:30 UTC — Melbourne is AEDT (+11) after spring forward
        utc_now = datetime.datetime(2025, 10, 5, 1, 30, tzinfo=datetime.UTC)
        local_now = _resolve_now(utc_now, "Australia/Melbourne", None)
        expected = utc_now.astimezone(ZoneInfo("Australia/Melbourne"))
        assert local_now.hour == expected.hour
        assert local_now.minute == 30

    def test_different_timezones_different_hours(self):
        utc_now = datetime.datetime(2026, 2, 24, 22, 0, tzinfo=datetime.UTC)
        mel = _resolve_now(utc_now, "Australia/Melbourne", None)
        nyc = _resolve_now(utc_now, "America/New_York", None)
        assert mel.hour != nyc.hour


class TestCronStepValidation:
    """Security fix: step=0 must not crash the scheduler."""

    def test_step_zero_returns_empty_set(self):
        result = _parse_field("*/0", 0, 59)
        assert result == set()

    def test_negative_step_returns_empty_set(self):
        result = _parse_field("*/-1", 0, 59)
        assert result == set()

    def test_step_zero_in_expression_no_match(self):
        now = datetime.datetime(2026, 2, 24, 10, 0)
        # Expression with step=0 should not match (returns empty set)
        assert cron_matches("*/0 * * * *", now) is False
