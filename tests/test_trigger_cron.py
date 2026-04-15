"""Tests for cron expression matching."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

import anyio
import pytest

from untether.triggers.cron import (
    _parse_field,
    _resolve_now,
    cron_matches,
    run_cron_scheduler,
)
from untether.triggers.manager import TriggerManager
from untether.triggers.settings import parse_trigger_config


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


# ── run_once cron flag (#288) ─────────────────────────────────────────


@dataclass
class FakeDispatcher:
    fired: list[str] = field(default_factory=list)

    async def dispatch_cron(self, cron: Any) -> None:
        self.fired.append(cron.id)


pytestmark_runonce = pytest.mark.anyio


@pytest.mark.anyio
async def test_run_once_removes_after_fire(monkeypatch):
    """A run_once cron removes itself from TriggerManager after firing."""
    settings = parse_trigger_config(
        {
            "enabled": True,
            "crons": [
                {
                    "id": "once",
                    "schedule": "* * * * *",
                    "prompt": "hi",
                    "run_once": True,
                },
            ],
        }
    )
    manager = TriggerManager(settings)
    dispatcher = FakeDispatcher()

    # Patch scheduler's sleep to yield immediately so the tick fires fast.
    _real_sleep = anyio.sleep

    async def fast_sleep(s: float) -> None:
        await _real_sleep(0)

    monkeypatch.setattr("untether.triggers.cron.anyio.sleep", fast_sleep)

    async with anyio.create_task_group() as tg:
        tg.start_soon(run_cron_scheduler, manager, dispatcher)
        # Give scheduler one tick to fire, then cancel.
        await _real_sleep(0)
        for _ in range(3):
            await _real_sleep(0)
        # Cancel the scheduler.
        tg.cancel_scope.cancel()

    assert dispatcher.fired == ["once"]
    assert manager.cron_ids() == []


@pytest.mark.anyio
async def test_run_once_false_keeps_cron_active(monkeypatch):
    """A normal cron (run_once=False) stays in the manager after firing."""
    settings = parse_trigger_config(
        {
            "enabled": True,
            "crons": [
                {
                    "id": "repeating",
                    "schedule": "* * * * *",
                    "prompt": "hi",
                },
            ],
        }
    )
    manager = TriggerManager(settings)
    dispatcher = FakeDispatcher()

    _real_sleep = anyio.sleep

    async def fast_sleep(s: float) -> None:
        await _real_sleep(0)

    monkeypatch.setattr("untether.triggers.cron.anyio.sleep", fast_sleep)

    async with anyio.create_task_group() as tg:
        tg.start_soon(run_cron_scheduler, manager, dispatcher)
        for _ in range(3):
            await _real_sleep(0)
        tg.cancel_scope.cancel()

    # Fired at least once, cron still active.
    assert "repeating" in dispatcher.fired
    assert manager.cron_ids() == ["repeating"]


@pytest.mark.anyio
async def test_daily_cron_fires_on_consecutive_days(monkeypatch):
    """Regression: #309 — cron last_fired key must include date.

    A bug in v0.35.1rc1-rc6 keyed last_fired by (hour, minute) only, so a daily
    cron at 09:00 would fire today and then be suppressed forever (tomorrow's
    09:00 looks identical). Verify the scheduler fires on each calendar day.
    """
    settings = parse_trigger_config(
        {
            "enabled": True,
            "crons": [
                {
                    "id": "daily",
                    "schedule": "0 9 * * *",
                    "prompt": "hi",
                    "timezone": "UTC",
                },
            ],
        }
    )
    manager = TriggerManager(settings)
    dispatcher = FakeDispatcher()

    # Fake clock — advance one day per scheduler tick.
    base_utc = datetime.datetime(2026, 4, 15, 9, 0, tzinfo=datetime.UTC)
    clock = [base_utc, base_utc + datetime.timedelta(days=1)]
    tick = [0]

    def fake_now(tz: Any = None) -> datetime.datetime:
        if tick[0] >= len(clock):
            return clock[-1]
        return clock[tick[0]]

    monkeypatch.setattr("untether.triggers.cron.datetime.datetime", _NowStub(fake_now))

    _real_sleep = anyio.sleep

    async def fast_sleep(s: float) -> None:
        tick[0] += 1
        await _real_sleep(0)

    monkeypatch.setattr("untether.triggers.cron.anyio.sleep", fast_sleep)

    async with anyio.create_task_group() as tg:
        tg.start_soon(run_cron_scheduler, manager, dispatcher)
        for _ in range(6):
            await _real_sleep(0)
        tg.cancel_scope.cancel()

    # Must have fired on day 1 AND day 2 (both at 09:00).
    assert dispatcher.fired.count("daily") >= 2, (
        f"Expected ≥2 fires across 2 days, got {dispatcher.fired}"
    )


class _NowStub:
    """Minimal datetime replacement that overrides .now() and .UTC."""

    UTC = datetime.UTC

    def __init__(self, now_fn):
        self._now = now_fn

    def now(self, tz: Any = None) -> datetime.datetime:
        n = self._now(tz)
        if tz is not None and n.tzinfo is None:
            return n.replace(tzinfo=tz)
        if tz is not None:
            return n.astimezone(tz)
        return n


def test_run_once_survives_reload_via_config():
    """A reload with the same TOML re-adds a run_once cron that was removed."""
    settings = parse_trigger_config(
        {
            "enabled": True,
            "crons": [
                {
                    "id": "once",
                    "schedule": "0 9 * * *",
                    "prompt": "hi",
                    "run_once": True,
                },
            ],
        }
    )
    mgr = TriggerManager(settings)
    assert mgr.cron_ids() == ["once"]
    # Simulate firing: remove it.
    assert mgr.remove_cron("once") is True
    assert mgr.cron_ids() == []
    # Config reload (TOML unchanged) re-adds the cron.
    mgr.update(settings)
    assert mgr.cron_ids() == ["once"]
