"""#776 phase 02: the Claude runner keeps reading after the first result.

Drives a REAL ``ClaudeRunner`` (control-channel mode: argv, spawn, stdin
payload, msgspec decode, translation) against the protocol-speaking fake at
``tests/fake_clis/fake_claude_live.py``.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any

import anyio
import pytest
from structlog.testing import capture_logs

from untether.model import CompletedEvent, ResumeToken, StartedEvent, TurnEvent
from untether.runners import claude as claude_mod
from untether.runners.claude import ENGINE, ClaudeRunner, write_user_message

pytestmark = pytest.mark.anyio

FAKE_CLI = Path(__file__).parent / "fake_clis" / "fake_claude_live.py"
SID = "fake-live-session"
_ENV = (
    "FAKE_CLAUDE_SCENARIO",
    "FAKE_CLAUDE_WAKE_S",
    "FAKE_CLAUDE_SESSION_ID",
    "FAKE_CLAUDE_TASK_END",
)


class _LiveRunner(ClaudeRunner):
    def env(self, *, state: Any) -> dict[str, str] | None:
        base = super().env(state=state) or {}
        for key in _ENV:
            if key in os.environ:
                base[key] = os.environ[key]
        return base


def _runner() -> ClaudeRunner:
    return _LiveRunner(claude_cmd=str(FAKE_CLI), permission_mode="bypassPermissions")


async def _collect(
    scenario: str,
    *,
    until: int,
    resume: ResumeToken | None = None,
    on_event: Any = None,
    timeout: float = 15.0,
) -> list[Any]:
    """Run the fake scenario and collect events until ``until`` completions
    (the run's CompletedEvent counts as 1, each TurnEvent(completed) as 1)."""
    os.environ["FAKE_CLAUDE_SCENARIO"] = scenario
    runner = _runner()
    events: list[Any] = []
    done = 0
    with anyio.fail_after(timeout):
        # Consume to the natural end, like the bridge does: once enough turns
        # completed, close the live session's stdin (graceful teardown, F3/F4)
        # and let the CLI exit.
        async for evt in runner.run("hello", resume):
            events.append(evt)
            if on_event is not None:
                await on_event(evt, runner)
            if isinstance(evt, CompletedEvent) or (
                isinstance(evt, TurnEvent) and evt.phase == "completed"
            ):
                done += 1
                if done == until:
                    stdin = claude_mod._SESSION_STDIN.get(SID)
                    if stdin is not None:
                        await stdin.aclose()
    return events


@pytest.fixture(autouse=True)
def _clean_env():
    yield
    for key in _ENV:
        os.environ.pop(key, None)


def _turns(events: list[Any]) -> list[TurnEvent]:
    return [e for e in events if isinstance(e, TurnEvent)]


async def test_bg_bash_wake_yields_turn_segment() -> None:
    with capture_logs() as logs:
        events = await _collect("bg_bash_wake", until=2)
    completed = [e for e in events if isinstance(e, CompletedEvent)]
    assert len(completed) == 1 and completed[0].answer == "waiting"
    turns = _turns(events)
    assert [t.phase for t in turns] == ["started", "completed"]
    assert turns[0].turn == 2 and turns[0].reason == "task_finished"
    assert turns[1].answer == "GOT: BG-FINISHED" and turns[1].ok is True
    # The run's CompletedEvent precedes the follow-up segment.
    assert events.index(completed[0]) < events.index(turns[0])
    assert not any(e["event"] == "runner.drop.jsonl_after_completed" for e in logs)


async def test_bg_agent_wake_actions_are_bracketed() -> None:
    events = await _collect("bg_agent_wake", until=2)
    start = next(e for e in events if isinstance(e, TurnEvent) and e.phase == "started")
    end = next(e for e in events if isinstance(e, TurnEvent) and e.phase == "completed")
    between = events[events.index(start) + 1 : events.index(end)]
    assert any(
        getattr(e, "action", None) and e.action.id == "toolu_read" for e in between
    )
    assert not any(isinstance(e, (StartedEvent, CompletedEvent)) for e in between)
    assert end.answer == "REPORT: all good"
    # Regression (dev bot, B-LIVE-2): the subagent's own events while the
    # parent idled opened a premature "unknown" turn.
    assert start.reason == "task_finished"
    assert len([t for t in _turns(events) if t.phase == "started"]) == 1
    assert not any(
        getattr(e, "action", None) and e.action.id == "toolu_sub" for e in between
    )


async def test_monitor_ticks_yield_one_segment_per_tick() -> None:
    events = await _collect("monitor_ticks", until=4)
    finals = [t for t in _turns(events) if t.phase == "completed"]
    assert [t.answer for t in finals] == ["TICK 1", "TICK 2", "TICK 3"]
    starts = [t for t in _turns(events) if t.phase == "started"]
    assert [t.reason for t in starts] == [
        "monitor_event",
        "monitor_event",
        "task_finished",
    ]


async def test_scheduled_wakeup_segment_reason() -> None:
    events = await _collect("scheduled_wakeup", until=2)
    start = next(t for t in _turns(events) if t.phase == "started")
    assert start.reason == "scheduled_wakeup"


async def test_followup_turn_attributed_by_command_uuid() -> None:
    cmd = str(uuid.uuid4())

    async def inject(evt: Any, runner: ClaudeRunner) -> None:
        if isinstance(evt, CompletedEvent):
            assert await write_user_message(SID, "second", command_uuid=cmd)

    events = await _collect("followup", until=2, on_event=inject)
    start, end = _turns(events)
    assert start.reason == "followup" and start.command_uuid == cmd
    assert end.answer == "ECHO: second" and end.command_uuid == cmd


async def test_stdin_not_closed_at_first_result() -> None:
    seen: dict[str, Any] = {}

    async def probe(evt: Any, runner: ClaudeRunner) -> None:
        if isinstance(evt, CompletedEvent):
            seen["stdin_registered"] = SID in claude_mod._SESSION_STDIN

    events = await _collect("bg_bash_wake", until=2, on_event=probe)
    assert seen["stdin_registered"] is True
    assert any(isinstance(e, TurnEvent) for e in events)


async def test_live_sessions_kill_switch_restores_single_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from untether.settings import WatchdogSettings

    # Short idle timeout -> short legacy watchdog poll (timeout/20), so the
    # pre-#776 generator ends promptly after the CLI exits.
    settings = SimpleNamespace(
        watchdog=WatchdogSettings(live_sessions=False, post_result_idle_timeout=30)
    )
    monkeypatch.setattr(
        claude_mod, "load_settings_if_exists", lambda *a, **k: (settings, Path("x"))
    )
    os.environ["FAKE_CLAUDE_SCENARIO"] = "bg_bash_wake"
    runner = _runner()
    with anyio.fail_after(15):
        events = [evt async for evt in runner.run("hello", None)]
    assert not any(isinstance(e, TurnEvent) for e in events)
    assert isinstance(events[-1], CompletedEvent)


async def test_resume_guard_absorbs_stopped_notification_zero_turn_result() -> None:
    with capture_logs() as logs:
        events = await _collect(
            "resume_after_killed_task",
            until=1,
            resume=ResumeToken(engine=ENGINE, value=SID),
        )
    completed = [e for e in events if isinstance(e, CompletedEvent)]
    assert len(completed) == 1
    assert completed[0].answer == "4"  # the real answer, not the 0-turn result
    absorbed = [e for e in logs if e["event"] == "claude.resume_guard.absorbed"]
    assert len(absorbed) == 1 and absorbed[0]["total_cost_usd"] == 0.05


async def test_resume_guard_not_applied_to_fresh_runs() -> None:
    events = await _collect("resume_after_killed_task", until=1)
    completed = [e for e in events if isinstance(e, CompletedEvent)]
    assert completed[0].answer == "4"


async def test_inherited_fd_reader_stops_after_exit_drain() -> None:
    """#505 regression: a grandchild holding stdout must not wedge the
    reader now that it no longer stops at the first result."""
    os.environ["FAKE_CLAUDE_SCENARIO"] = "inherited_fd_after_exit"
    runner = _runner()
    started = time.monotonic()
    with anyio.fail_after(20):
        events = [evt async for evt in runner.run("hello", None)]
    assert isinstance(events[-1], CompletedEvent)
    assert time.monotonic() - started < 15
    assert runner.current_stream.stdout_held_after_exit is True


async def test_write_user_message_returns_false_without_live_stdin() -> None:
    assert await write_user_message("no-such-session", "x", command_uuid="u") is False


# ── #785: wake-turn attribution ─────────────────────────────────────────────


def _labels(turn: TurnEvent) -> list[str]:
    return list((turn.detail or {}).get("tasks", []))


async def test_wake_turn_opened_before_task_end_is_retro_attributed() -> None:
    """#785: the CLI opens the wake turn before any task event names the
    finished agent (it opens as ``unknown``); the task ends inside that turn,
    so the turn completes attributed to it. The notification's own turn that
    follows is flagged as already announced (the bridge doesn't push it)."""
    os.environ["FAKE_CLAUDE_TASK_END"] = "mid"
    events = await _collect("agent_wake_unknown_first", until=3)
    turns = _turns(events)
    assert [(t.phase, t.reason) for t in turns] == [
        ("started", "unknown"),
        ("completed", "task_finished"),
        ("started", "task_finished"),
        ("completed", "task_finished"),
    ]
    assert _labels(turns[1]) == ["bg a1"]
    assert turns[1].answer == "The sweep is back"
    assert not turns[1].detail.get("already_announced")
    # #795/#785: the attributed task and the turn that launched it (the run).
    assert turns[1].detail["task_ids"] == ["a1"]
    assert turns[1].detail["origin_turn"] == 1
    assert turns[2].detail["task_ids"] == ["a1"]
    assert turns[2].detail["origin_turn"] == 1
    assert _labels(turns[2]) == ["bg a1"]
    assert turns[2].detail.get("already_announced") is True
    assert turns[3].detail.get("already_announced") is True


async def test_task_end_just_after_unknown_turn_marks_next_turn_announced() -> None:
    """#785: the task ends a moment AFTER the unknown turn delivered — too
    late to relabel it, but the notification turn for the same task is the
    second buzz for one finish, so it is flagged as already announced."""
    os.environ["FAKE_CLAUDE_TASK_END"] = "after"
    events = await _collect("agent_wake_unknown_first", until=3)
    turns = _turns(events)
    assert [(t.phase, t.reason) for t in turns] == [
        ("started", "unknown"),
        ("completed", "unknown"),
        ("started", "task_finished"),
        ("completed", "task_finished"),
    ]
    assert _labels(turns[2]) == ["bg a1"]
    assert turns[2].detail.get("already_announced") is True


async def test_resumed_agent_second_finish_is_announced() -> None:
    """#801: the wake turn sends a finished agent back to work under the same
    task_id; its second finish opens a normal ``task_finished`` wake turn —
    not one suppressed as the already-announced first finish (#785)."""
    with capture_logs() as logs:
        events = await _collect("agent_resumed", until=3)
    turns = _turns(events)
    assert [(t.phase, t.reason) for t in turns] == [
        ("started", "task_finished"),
        ("completed", "task_finished"),
        ("started", "task_finished"),
        ("completed", "task_finished"),
    ]
    assert turns[1].answer == "I've sent it back to re-check, it's running now"
    assert _labels(turns[2]) == ["bg a1"]
    assert not turns[2].detail.get("already_announced")
    assert turns[3].answer == "RECHECK DONE"
    revived = [e for e in logs if e["event"] == "claude.task.revived"]
    assert len(revived) == 1
    assert revived[0]["task_id"] == "a1"
    assert revived[0]["prior_status"] == "completed"


async def test_subagent_owned_notification_never_labels_a_turn() -> None:
    """#785: a subagent's own (foreground, ``owned_by_subagent``) task
    finishing while the parent idles must not attribute the next wake turn
    — it named the wrong task in the nsd evidence."""
    for mode in ("mid", "after"):
        os.environ["FAKE_CLAUDE_TASK_END"] = mode
        events = await _collect("agent_wake_unknown_first", until=3)
        for turn in _turns(events):
            assert "Inspect nested agents" not in _labels(turn), (mode, turn)


async def test_812_async_rewake_delivers_hook_rewake_turn() -> None:
    """An asyncRewake Stop hook exits 2 while the session idles with stdin
    open (P5-A): the CLI's self-started turn arrives as a ``hook_rewake``
    segment carrying the hook's event — no hook frame becomes an event."""
    with capture_logs() as logs:
        events = await _collect("async_rewake_idle", until=2)
    completed = [e for e in events if isinstance(e, CompletedEvent)]
    assert len(completed) == 1 and completed[0].answer == "DONE"
    turns = _turns(events)
    assert [t.phase for t in turns] == ["started", "completed"]
    assert turns[0].reason == "hook_rewake"
    assert turns[0].detail == {"hook": "Stop", "hook_event": "Stop"}
    assert turns[1].reason == "hook_rewake"
    assert turns[1].answer == "HOOK: finding: key leak"
    # Only the two answers' actions/turns — hook frames surface nothing.
    assert not any(
        getattr(getattr(e, "action", None), "title", "").startswith("hook")
        for e in events
    )
    started = [e for e in logs if e["event"] == "claude.turn.started"]
    assert [e["reason"] for e in started] == ["hook_rewake"]
    assert any(e["event"] == "claude.hook.rewake_signal" for e in logs)


# ── #816: a /continue run releases its session registries ──────────────────

_CONTINUE = ResumeToken(engine=ENGINE, value="", is_continue=True)


def _watchdog_settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    from types import SimpleNamespace

    from untether.settings import WatchdogSettings

    values = {
        "post_result_limbo_grace": 0.3,
        "post_result_idle_timeout": 30,
        **overrides,
    }
    watchdog = WatchdogSettings.model_construct(
        **{**WatchdogSettings().model_dump(), **values}
    )
    monkeypatch.setattr(
        claude_mod,
        "load_settings_if_exists",
        lambda *a, **k: (SimpleNamespace(watchdog=watchdog), Path("x")),
    )


def _fast_runner() -> ClaudeRunner:
    runner = _runner()
    # Slots dataclass: timing knobs must be set on the instance.
    runner._live_poll_s = 0.05
    runner._live_close_grace_s = 0.8
    runner._live_close_sigint_grace_s = 0.8
    return runner


async def _assert_session_released(sid: str) -> None:
    assert not claude_mod.is_session_alive(sid)
    assert sid not in claude_mod._SESSION_STDIN
    assert sid not in claude_mod._LIVE_SESSIONS
    assert sid not in claude_mod._SESSION_BG_STATE
    assert sid not in claude_mod._ACTIVE_RUNNERS
    assert await claude_mod.wait_for_session_handoff(sid, 0.5) == "free"


@pytest.fixture
def _fresh_registries():
    claude_mod._cleanup_session_registries(SID)
    yield
    claude_mod._cleanup_session_registries(SID)


@pytest.mark.usefixtures("_fresh_registries")
async def test_816_continue_run_idle_close_releases_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _watchdog_settings(monkeypatch)
    os.environ["FAKE_CLAUDE_SCENARIO"] = "followup"
    runner = _fast_runner()
    with capture_logs() as logs, anyio.fail_after(15):
        events = [evt async for evt in runner.run("hello", _CONTINUE)]
    assert isinstance(events[-1], CompletedEvent)
    assert events[-1].resume is not None and events[-1].resume.value == SID
    assert runner.current_stream.engine_state.live_close_reason == "idle_no_tasks"
    await _assert_session_released(SID)
    cleanups = [e for e in logs if e["event"] == "claude_runner.session_cleanup"]
    assert [e["session_id"] for e in cleanups] == [SID]


@pytest.mark.usefixtures("_fresh_registries")
async def test_816_continue_run_without_live_sessions_releases_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _watchdog_settings(monkeypatch, live_sessions=False)
    os.environ["FAKE_CLAUDE_SCENARIO"] = "followup"
    runner = _fast_runner()
    with anyio.fail_after(15):
        events = [evt async for evt in runner.run("hello", _CONTINUE)]
    assert not any(isinstance(e, TurnEvent) for e in events)
    assert isinstance(events[-1], CompletedEvent)
    await _assert_session_released(SID)


@pytest.mark.usefixtures("_fresh_registries")
async def test_816_cancelled_continue_run_releases_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/cancel of a /continue run mid-session (the bridge cancels the run's
    scope): the finally must still resolve the real session id."""
    _watchdog_settings(monkeypatch, post_result_limbo_grace=60)
    os.environ["FAKE_CLAUDE_SCENARIO"] = "followup"
    runner = _fast_runner()
    seen_alive: list[bool] = []
    with anyio.fail_after(15):
        with anyio.CancelScope() as scope:
            async for evt in runner.run("hello", _CONTINUE):
                if isinstance(evt, CompletedEvent):
                    seen_alive.append(claude_mod.is_session_alive(SID))
                    scope.cancel()
    assert seen_alive == [True]
    await _assert_session_released(SID)


async def test_816_cleanup_skips_session_owned_by_another_run() -> None:
    """Hardening: a late cleanup from an old run must not deregister a
    newer process that now owns the same session id."""
    sid = "owned-elsewhere"
    old_state = object()
    new_state = object()
    claude_mod._SESSION_BG_STATE[sid] = new_state  # type: ignore[assignment]
    claude_mod._SESSION_STDIN[sid] = object()  # type: ignore[assignment]
    try:
        with capture_logs() as logs:
            claude_mod._cleanup_session_registries(sid, owner_state=old_state)  # type: ignore[arg-type]
        assert claude_mod.is_session_alive(sid)
        assert claude_mod._SESSION_BG_STATE[sid] is new_state
        assert any(e["event"] == "claude_runner.session_cleanup_skipped" for e in logs)
        claude_mod._cleanup_session_registries(sid, owner_state=new_state)  # type: ignore[arg-type]
        assert not claude_mod.is_session_alive(sid)
        assert sid not in claude_mod._SESSION_BG_STATE
    finally:
        claude_mod._cleanup_session_registries(sid)
