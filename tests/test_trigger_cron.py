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


def test_run_once_does_not_resurrect_on_reload():
    """#317: a reload must NOT re-add a run_once cron that has already fired.

    Prior to #317 the TOML entry re-entered the active list on every reload,
    causing unexpected re-fires after the user edited any unrelated setting.
    Now the fired-once set (in-memory, plus optionally persisted to
    ``run_once_fired.json``) is consulted on reload and filters the cron out.
    """
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
    # Config reload (TOML unchanged) should NOT re-activate the fired one-shot.
    mgr.update(settings)
    assert mgr.cron_ids() == []
    assert mgr.fired_run_once_ids() == ["once"]


# ── #271 Tier 3: cron firing records last_fired_at ─────────────────────────


@pytest.mark.anyio
async def test_cron_firing_records_last_fired(monkeypatch, tmp_path):
    """A successful cron dispatch records the trigger id in the history store."""
    from untether.triggers import history

    history.reset_history()
    history.init_history(tmp_path / "untether.toml")

    settings = parse_trigger_config(
        {
            "enabled": True,
            "crons": [
                {
                    "id": "daily-job",
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

    assert "daily-job" in dispatcher.fired
    assert history.get_last_fired("daily-job") is not None
    history.reset_history()


@pytest.mark.anyio
async def test_cron_history_failure_does_not_break_scheduler(monkeypatch, tmp_path):
    """A history-store write failure must not propagate out of the scheduler."""
    from untether.triggers import history

    history.reset_history()
    history.init_history(tmp_path / "untether.toml")

    # Make the underlying store raise on every record_fired.
    def boom(self, trigger_id: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(
        "untether.triggers.history.TriggerHistoryStore.record_fired", boom
    )

    settings = parse_trigger_config(
        {
            "enabled": True,
            "crons": [
                {"id": "robust", "schedule": "* * * * *", "prompt": "hi"},
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

    # Cron still fired even though history write failed.
    assert "robust" in dispatcher.fired
    history.reset_history()


# ── #893: a run_once cron whose dispatch send fails stays pending ─────────


@dataclass
class ScriptedDispatcher:
    """Dispatcher whose ``dispatch_cron`` result follows a script.

    ``results[i]`` is returned for the i-th call (the last value repeats).
    ``False`` models ``triggers.dispatch.send_failed`` — nothing ran.
    """

    results: list[bool]
    calls: list[tuple[str, Any]] = field(default_factory=list)

    async def dispatch_cron(self, cron: Any, **kwargs: Any) -> bool:
        idx = min(len(self.calls), len(self.results) - 1)
        self.calls.append((cron.id, kwargs.get("retry_delays", "default")))
        return self.results[idx]


def _one_shot_settings(schedule: str = "0 9 15 4 *") -> Any:
    return parse_trigger_config(
        {
            "enabled": True,
            "crons": [
                {
                    "id": "once",
                    "schedule": schedule,
                    "prompt": "hi",
                    "timezone": "UTC",
                    "run_once": True,
                },
            ],
        }
    )


async def _drive_scheduler(
    monkeypatch: pytest.MonkeyPatch,
    manager: TriggerManager,
    dispatcher: Any,
    clock: list[datetime.datetime],
) -> None:
    """Run the scheduler for exactly one tick per entry in *clock*."""
    tick = [0]
    done = anyio.Event()

    def fake_now(tz: Any = None) -> datetime.datetime:
        return clock[min(tick[0], len(clock) - 1)]

    monkeypatch.setattr("untether.triggers.cron.datetime.datetime", _NowStub(fake_now))
    real_sleep = anyio.sleep

    async def fast_sleep(s: float) -> None:
        tick[0] += 1
        if tick[0] >= len(clock):
            done.set()
            await anyio.Event().wait()  # park until cancelled
        await real_sleep(0)

    monkeypatch.setattr("untether.triggers.cron.anyio.sleep", fast_sleep)
    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(run_cron_scheduler, manager, dispatcher)
            await done.wait()
            tg.cancel_scope.cancel()


_T0 = datetime.datetime(2026, 4, 15, 9, 0, tzinfo=datetime.UTC)


def _minutes(*offsets: int) -> list[datetime.datetime]:
    return [_T0 + datetime.timedelta(minutes=m) for m in offsets]


@pytest.mark.anyio
async def test_893_run_once_not_consumed_when_send_fails(monkeypatch, tmp_path):
    """A failed announce send must not record the one-shot as fired."""
    from structlog.testing import capture_logs

    from untether.triggers.run_once_state import STATE_FILENAME

    manager = TriggerManager(
        _one_shot_settings(), config_path=tmp_path / "untether.toml"
    )
    dispatcher = ScriptedDispatcher(results=[False])

    with capture_logs() as logs:
        await _drive_scheduler(monkeypatch, manager, dispatcher, _minutes(0))

    assert dispatcher.calls == [("once", "default")]
    assert manager.cron_ids() == ["once"]  # still active
    assert manager.fired_run_once_ids() == []
    assert not (tmp_path / STATE_FILENAME).exists()
    events = [e["event"] for e in logs]
    assert "triggers.cron.run_once_completed" not in events
    assert "triggers.cron.run_once_pending" in events


@pytest.mark.anyio
async def test_893_run_once_retried_on_next_tick_then_consumed(monkeypatch, tmp_path):
    """The pending one-shot is retried on the next minute tick (outside its
    schedule) with a single send attempt, and consumed once dispatched."""
    manager = TriggerManager(
        _one_shot_settings(), config_path=tmp_path / "untether.toml"
    )
    dispatcher = ScriptedDispatcher(results=[False, True])

    await _drive_scheduler(monkeypatch, manager, dispatcher, _minutes(0, 1, 2, 3))

    # First fire uses the dispatcher's default retry schedule; the retry tick
    # is a single attempt so a dead chat can't stall the scheduler loop.
    assert dispatcher.calls == [("once", "default"), ("once", ())]
    assert manager.cron_ids() == []
    assert manager.fired_run_once_ids() == ["once"]


@pytest.mark.anyio
async def test_893_run_once_lost_after_retry_window(monkeypatch, tmp_path):
    """Past the retry window the one-shot is given up loudly (error-level
    ``triggers.cron.run_once_lost``) and consumed so it can't surprise-fire
    on a later schedule match."""
    from structlog.testing import capture_logs

    from untether.triggers import cron as cron_mod

    window_min = int(cron_mod.RUN_ONCE_RETRY_WINDOW_S // 60)
    manager = TriggerManager(
        _one_shot_settings(), config_path=tmp_path / "untether.toml"
    )
    dispatcher = ScriptedDispatcher(results=[False])

    with capture_logs() as logs:
        await _drive_scheduler(
            monkeypatch,
            manager,
            dispatcher,
            _minutes(0, 1, window_min + 1, window_min + 2),
        )

    # Fire + one retry inside the window; nothing after giving up.
    assert dispatcher.calls == [("once", "default"), ("once", ())]
    assert manager.cron_ids() == []
    assert manager.fired_run_once_ids() == ["once"]
    lost = [e for e in logs if e["event"] == "triggers.cron.run_once_lost"]
    assert len(lost) == 1
    assert lost[0]["log_level"] == "error"
    assert lost[0]["cron_id"] == "once"
    events = [e["event"] for e in logs]
    assert "triggers.cron.run_once_completed" not in events


@pytest.mark.anyio
async def test_893_recurring_cron_send_failure_is_not_retried(monkeypatch):
    """Only run_once crons are retried — a recurring cron just waits for its
    next scheduled match (no double-dispatch)."""
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
    dispatcher = ScriptedDispatcher(results=[False])

    await _drive_scheduler(monkeypatch, manager, dispatcher, _minutes(0, 1, 2))

    assert dispatcher.calls == [("daily", "default")]
    assert manager.cron_ids() == ["daily"]


@pytest.mark.anyio
async def test_893_failed_send_does_not_record_last_fired(monkeypatch, tmp_path):
    """The /config history must not show a fire for a cron that never ran."""
    from untether.triggers import history

    history.reset_history()
    history.init_history(tmp_path / "untether.toml")
    try:
        manager = TriggerManager(_one_shot_settings())
        dispatcher = ScriptedDispatcher(results=[False])
        await _drive_scheduler(monkeypatch, manager, dispatcher, _minutes(0))
        assert history.get_last_fired("once") is None
    finally:
        history.reset_history()


@pytest.mark.anyio
async def test_893_pending_retry_dropped_when_cron_removed_on_reload(
    monkeypatch, tmp_path
):
    """A pending one-shot removed from the TOML by a reload is not retried."""
    manager = TriggerManager(
        _one_shot_settings(), config_path=tmp_path / "untether.toml"
    )

    class ReloadingDispatcher(ScriptedDispatcher):
        async def dispatch_cron(self, cron: Any, **kwargs: Any) -> bool:
            result = await super().dispatch_cron(cron, **kwargs)
            manager.update(parse_trigger_config({"enabled": True, "crons": []}))
            return result

    dispatcher = ReloadingDispatcher(results=[False])
    await _drive_scheduler(monkeypatch, manager, dispatcher, _minutes(0, 1, 2))

    assert dispatcher.calls == [("once", "default")]
    assert manager.fired_run_once_ids() == []


# ── #893 follow-up: pending state survives a restart; pause still retries ──


def _pending_on_disk(tmp_path) -> dict[str, str]:
    from untether.triggers.run_once_state import (
        load_pending_state,
        resolve_pending_path,
    )

    return load_pending_state(resolve_pending_path(tmp_path / "untether.toml"))


@pytest.mark.anyio
async def test_893_pending_run_once_survives_restart_and_retries(monkeypatch, tmp_path):
    """A restart (e.g. the daily 03:00 reboot) inside the retry window keeps
    the one-shot pending: the new scheduler retries it on its first tick,
    outside its schedule — it must not wait for the next match (a year away
    for a date-pinned one-shot)."""
    config_path = tmp_path / "untether.toml"
    before = TriggerManager(_one_shot_settings(), config_path=config_path)
    await _drive_scheduler(
        monkeypatch, before, ScriptedDispatcher(results=[False]), _minutes(0)
    )
    assert list(_pending_on_disk(tmp_path)) == ["once"]

    # Restart: a fresh manager + scheduler, 5 minutes later (the real
    # datetime back for the manager's load, as in a new process).
    monkeypatch.undo()
    after = TriggerManager(_one_shot_settings(), config_path=config_path)
    dispatcher = ScriptedDispatcher(results=[True])
    await _drive_scheduler(monkeypatch, after, dispatcher, _minutes(5))

    assert dispatcher.calls == [("once", ())]
    assert after.cron_ids() == []
    assert after.fired_run_once_ids() == ["once"]
    assert _pending_on_disk(tmp_path) == {}


@pytest.mark.anyio
async def test_893_pending_run_once_past_window_after_restart_is_lost(
    monkeypatch, tmp_path
):
    """Restarting after the window closed gives the one-shot up loudly at
    once (consumed, ``run_once_lost``) instead of leaving it active."""
    from structlog.testing import capture_logs

    from untether.triggers import cron as cron_mod

    window_min = int(cron_mod.RUN_ONCE_RETRY_WINDOW_S // 60)
    config_path = tmp_path / "untether.toml"
    before = TriggerManager(_one_shot_settings(), config_path=config_path)
    await _drive_scheduler(
        monkeypatch, before, ScriptedDispatcher(results=[False]), _minutes(0)
    )

    monkeypatch.undo()  # restart: a new process with the real datetime
    after = TriggerManager(_one_shot_settings(), config_path=config_path)
    dispatcher = ScriptedDispatcher(results=[True])
    with capture_logs() as logs:
        await _drive_scheduler(monkeypatch, after, dispatcher, _minutes(window_min + 5))

    assert dispatcher.calls == []
    assert after.cron_ids() == []
    assert after.fired_run_once_ids() == ["once"]
    lost = [e for e in logs if e["event"] == "triggers.cron.run_once_lost"]
    assert [e["cron_id"] for e in lost] == ["once"]
    assert _pending_on_disk(tmp_path) == {}


@pytest.mark.anyio
async def test_893_pending_entry_cleared_when_cron_removed(monkeypatch, tmp_path):
    """A pending one-shot removed from the TOML loses its persisted entry, so
    re-adding the id later starts fresh."""
    config_path = tmp_path / "untether.toml"
    manager = TriggerManager(_one_shot_settings(), config_path=config_path)
    await _drive_scheduler(
        monkeypatch, manager, ScriptedDispatcher(results=[False]), _minutes(0)
    )
    assert list(_pending_on_disk(tmp_path)) == ["once"]

    manager.update(parse_trigger_config({"enabled": True, "crons": []}))
    assert _pending_on_disk(tmp_path) == {}


def test_893_old_state_files_still_load(tmp_path):
    """Upgrading from a build without the pending file: the existing
    ``run_once_fired.json`` loads unchanged and nothing is pending."""
    import json

    from untether.triggers.run_once_state import STATE_FILENAME

    (tmp_path / STATE_FILENAME).write_text(
        json.dumps({"fired": {"done": "2026-04-01T09:00:00+00:00"}}),
        encoding="utf-8",
    )
    settings = parse_trigger_config(
        {
            "enabled": True,
            "crons": [
                {
                    "id": "done",
                    "schedule": "0 9 1 4 *",
                    "prompt": "x",
                    "run_once": True,
                },
                {
                    "id": "once",
                    "schedule": "0 9 15 4 *",
                    "prompt": "y",
                    "run_once": True,
                },
            ],
        }
    )
    manager = TriggerManager(settings, config_path=tmp_path / "untether.toml")
    assert manager.fired_run_once_ids() == ["done"]
    assert manager.cron_ids() == ["once"]
    assert manager.run_once_pending_since("once") is None


class _PauseAtTicks(TriggerManager):
    """A manager whose pause flag is scripted per scheduler tick (the
    scheduler reads ``is_paused`` exactly once per tick)."""

    def __init__(self, *args: Any, paused_ticks: set[int], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.paused_ticks = paused_ticks
        self.reads = 0

    @property
    def is_paused(self) -> bool:
        tick = self.reads
        self.reads += 1
        return tick in self.paused_ticks


@pytest.mark.anyio
async def test_893_pause_longer_than_window_still_retries_once(monkeypatch):
    """A /pause spanning the whole retry window must not abandon the pending
    one-shot without trying: the first tick after resume retries it, and
    only a failure there gives it up."""
    from structlog.testing import capture_logs

    from untether.triggers import cron as cron_mod

    window_min = int(cron_mod.RUN_ONCE_RETRY_WINDOW_S // 60)
    manager = _PauseAtTicks(_one_shot_settings(), paused_ticks={1, 2})
    dispatcher = ScriptedDispatcher(results=[False])

    with capture_logs() as logs:
        await _drive_scheduler(
            monkeypatch,
            manager,
            dispatcher,
            _minutes(0, 1, window_min, window_min + 1, window_min + 2),
        )

    # Fire, (paused twice), one retry after resume, then given up.
    assert dispatcher.calls == [("once", "default"), ("once", ())]
    lost = [e for e in logs if e["event"] == "triggers.cron.run_once_lost"]
    assert len(lost) == 1
    assert manager.fired_run_once_ids() == ["once"]


@pytest.mark.anyio
async def test_893_retry_after_pause_can_still_dispatch(monkeypatch):
    """The post-resume retry dispatches normally when the send now works."""
    from untether.triggers import cron as cron_mod

    window_min = int(cron_mod.RUN_ONCE_RETRY_WINDOW_S // 60)
    manager = _PauseAtTicks(_one_shot_settings(), paused_ticks={1, 2, 3})
    dispatcher = ScriptedDispatcher(results=[False, True])
    await _drive_scheduler(
        monkeypatch,
        manager,
        dispatcher,
        _minutes(0, 1, 5, window_min + 3, window_min + 4),
    )
    assert dispatcher.calls == [("once", "default"), ("once", ())]
    assert manager.cron_ids() == []
    assert manager.fired_run_once_ids() == ["once"]


# ── #896: a run_once cron refused by the daily budget is not consumed ─────


@pytest.mark.anyio
async def test_896_refused_run_once_stays_scheduled(monkeypatch, tmp_path):
    """Refused by "Stop at limit" nothing ran: the one-shot is neither
    consumed nor recorded as fired, and it isn't retried every tick (it
    fires at its next schedule match, like a one-shot skipped by /pause)."""
    from structlog.testing import capture_logs

    from untether.triggers import history
    from untether.triggers.dispatcher import DISPATCH_REFUSED

    history.reset_history()
    manager = TriggerManager(
        _one_shot_settings(), config_path=tmp_path / "untether.toml"
    )
    dispatcher = ScriptedDispatcher(results=[DISPATCH_REFUSED])  # type: ignore[list-item]

    with capture_logs() as logs:
        await _drive_scheduler(monkeypatch, manager, dispatcher, _minutes(0, 1, 2))

    assert dispatcher.calls == [("once", "default")]  # no per-tick retries
    assert manager.cron_ids() == ["once"]
    assert manager.fired_run_once_ids() == []
    assert manager.run_once_pending_since("once") is None
    assert history.get_last_fired("once") is None
    events = [e["event"] for e in logs]
    assert "triggers.cron.run_once_refused" in events
    assert "triggers.cron.run_once_completed" not in events
    history.reset_history()


@pytest.mark.anyio
async def test_896_refused_pending_retry_is_not_lost(monkeypatch, tmp_path):
    """A one-shot pending a #893 retry that the budget then refuses drops the
    retry (no run_once_lost later) and stays scheduled."""
    from untether.triggers.dispatcher import DISPATCH_REFUSED

    manager = TriggerManager(
        _one_shot_settings(), config_path=tmp_path / "untether.toml"
    )
    dispatcher = ScriptedDispatcher(results=[False, DISPATCH_REFUSED])  # type: ignore[list-item]

    await _drive_scheduler(monkeypatch, manager, dispatcher, _minutes(0, 1, 2, 3))

    assert dispatcher.calls == [("once", "default"), ("once", ())]
    assert manager.cron_ids() == ["once"]
    assert manager.fired_run_once_ids() == []
    assert manager.run_once_pending_since("once") is None
