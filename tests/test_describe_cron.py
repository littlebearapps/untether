"""Tests for describe_cron — human-friendly cron schedule rendering (#271)."""

from __future__ import annotations

import pytest

from untether.triggers.describe import describe_cron


class TestDailyTimes:
    @pytest.mark.parametrize(
        "schedule,timezone,expected",
        [
            ("0 9 * * *", "Australia/Melbourne", "9:00 AM daily (Melbourne)"),
            ("0 0 * * *", None, "12:00 AM daily"),
            ("30 0 * * *", None, "12:30 AM daily"),
            ("0 12 * * *", None, "12:00 PM daily"),
            ("30 14 * * *", "America/New_York", "2:30 PM daily (New York)"),
            ("0 23 * * *", None, "11:00 PM daily"),
            ("59 23 * * *", None, "11:59 PM daily"),
        ],
    )
    def test_daily(self, schedule, timezone, expected):
        assert describe_cron(schedule, timezone) == expected


class TestWeekdayRanges:
    def test_mon_fri_range(self):
        assert (
            describe_cron("0 8 * * 1-5", "Australia/Melbourne")
            == "8:00 AM Mon\u2013Fri (Melbourne)"
        )

    def test_tue_thu_range(self):
        assert describe_cron("30 14 * * 2-4", None) == "2:30 PM Tue\u2013Thu"


class TestWeekdayLists:
    def test_weekends(self):
        assert describe_cron("0 10 * * 0,6", None) == "10:00 AM Sun,Sat"

    def test_three_days(self):
        assert describe_cron("0 10 * * 1,3,5", None) == "10:00 AM Mon,Wed,Fri"


class TestSingleDay:
    def test_sunday_as_zero(self):
        assert describe_cron("0 9 * * 0", None) == "9:00 AM Sun"

    def test_sunday_as_seven(self):
        assert describe_cron("0 9 * * 7", None) == "9:00 AM Sun"

    def test_monday(self):
        assert describe_cron("0 9 * * 1", None) == "9:00 AM Mon"


class TestTimezoneSuffix:
    def test_underscore_replaced_with_space(self):
        # Some IANA names have underscores in the leaf component.
        out = describe_cron("0 9 * * *", "America/Los_Angeles")
        assert "(Los Angeles)" in out

    def test_no_timezone_no_suffix(self):
        assert "(" not in describe_cron("0 9 * * *", None)

    def test_unqualified_timezone_used_as_is(self):
        # Non-namespaced tz name — take it verbatim.
        out = describe_cron("0 9 * * *", "UTC")
        assert out.endswith("(UTC)")


class TestFallback:
    @pytest.mark.parametrize(
        "schedule",
        [
            "*/15 * * * *",  # stepped minutes
            "0 */4 * * *",  # stepped hours
            "0 9 1 * *",  # day-of-month
            "0 9 * 6 *",  # specific month
            "invalid",  # totally wrong
            "0 9 * *",  # too few fields
            "0 9 * * * *",  # too many fields
            "0 25 * * *",  # hour out of range
            "60 0 * * *",  # minute out of range
        ],
    )
    def test_fallback_returns_raw(self, schedule):
        assert describe_cron(schedule, None) == schedule


class TestBoundary:
    def test_midnight(self):
        assert describe_cron("0 0 * * *", None) == "12:00 AM daily"

    def test_noon(self):
        assert describe_cron("0 12 * * *", None) == "12:00 PM daily"

    def test_one_am(self):
        assert describe_cron("0 1 * * *", None) == "1:00 AM daily"

    def test_eleven_pm(self):
        assert describe_cron("0 23 * * *", None) == "11:00 PM daily"


class TestDefaults:
    def test_timezone_none_explicit(self):
        """Explicit None ≡ default."""
        assert describe_cron("0 9 * * *") == describe_cron("0 9 * * *", None)


class TestRegression309:
    """Regression tests for #309 CodeRabbit findings on describe.py."""

    def test_stepped_dom_falls_back_to_raw(self):
        """`*/2` in day-of-month — must NOT render as 'daily' (#309)."""
        assert describe_cron("0 9 */2 * *", None) == "0 9 */2 * *"

    def test_stepped_month_falls_back_to_raw(self):
        """`*/2` in month — must NOT render as 'daily' (#309)."""
        assert describe_cron("0 9 * */2 *", None) == "0 9 * */2 *"

    def test_dow_out_of_range_falls_back(self):
        """`8` is invalid for day-of-week — must NOT silently % 7 → 'Mon' (#309)."""
        assert describe_cron("0 9 * * 8", None) == "0 9 * * 8"

    def test_dow_range_with_invalid_end_falls_back(self):
        assert describe_cron("0 9 * * 1-9", None) == "0 9 * * 1-9"

    def test_dow_list_with_invalid_value_falls_back(self):
        assert describe_cron("0 9 * * 1,8", None) == "0 9 * * 1,8"

    def test_dow_seven_normalises_to_sunday(self):
        """`7` is the canonical-form Sunday in cron; should still render."""
        assert describe_cron("0 9 * * 7", None) == "9:00 AM Sun"
