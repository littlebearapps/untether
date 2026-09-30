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
    "FAKE_CLAUDE_FOLLOWUP_DELAY_S",
    # #383
    "FAKE_CLAUDE_START_MODE",
    "FAKE_CLAUDE_REARM_ERROR",
    "FAKE_CLAUDE_NO_STATUS",
    "FAKE_CLAUDE_WAKE_AFTER_RESULT_S",
    "FAKE_CLAUDE_STDIN_LOG",
    # #751
    "FAKE_CLAUDE_INIT_PERMISSION_MODE",
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
    runner: ClaudeRunner | None = None,
) -> list[Any]:
    """Run the fake scenario and collect events until ``until`` completions
    (the run's CompletedEvent counts as 1, each TurnEvent(completed) as 1)."""
    os.environ["FAKE_CLAUDE_SCENARIO"] = scenario
    runner = runner if runner is not None else _runner()
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


async def test_815_followup_turn_carries_its_lead_time() -> None:
    """The real CLI announces a follow-up (``command_lifecycle{started}``)
    and sends no ``init``: a tool-free turn's first frame is its answer, so
    the turn opens late. The TurnEvent must say how long it had been running
    (the bridge times the header from it) — not ~0."""
    os.environ["FAKE_CLAUDE_FOLLOWUP_DELAY_S"] = "0.6"
    cmd = str(uuid.uuid4())

    async def inject(evt: Any, runner: ClaudeRunner) -> None:
        if isinstance(evt, CompletedEvent):
            assert await write_user_message(SID, "second", command_uuid=cmd)

    events = await _collect("followup", until=2, on_event=inject)
    start, end = _turns(events)
    assert start.reason == "followup" and start.command_uuid == cmd
    assert start.started_ago_s is not None and start.started_ago_s >= 0.5
    assert end.started_ago_s is None
    assert end.answer == "ECHO: second"


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


@pytest.mark.parametrize("hook", ["stream_end_events", "process_error_events"])
async def test_816_stream_end_and_error_cleanup_keep_a_newer_owner(hook: str) -> None:
    """#816 review: ``stream_end_events`` / ``process_error_events`` run for
    the finishing process and must pass its state as the owner — otherwise
    they deregister a newer process that now owns the same session id."""
    sid = "owned-by-newer"
    runner = ClaudeRunner(claude_cmd="claude")
    old_state = claude_mod.ClaudeStreamState()
    new_state = claude_mod.ClaudeStreamState()
    claude_mod._SESSION_BG_STATE[sid] = new_state
    claude_mod._SESSION_STDIN[sid] = object()  # type: ignore[assignment]
    token = ResumeToken(engine=ENGINE, value=sid)
    try:
        if hook == "stream_end_events":
            runner.stream_end_events(resume=None, found_session=token, state=old_state)
        else:
            runner.process_error_events(
                1, resume=None, found_session=token, state=old_state
            )
        assert claude_mod.is_session_alive(sid)
        assert claude_mod._SESSION_BG_STATE[sid] is new_state
        # The owner's own end still cleans up.
        runner.stream_end_events(resume=None, found_session=token, state=new_state)
        assert not claude_mod.is_session_alive(sid)
    finally:
        claude_mod._cleanup_session_registries(sid)


# ── #383: a plan approval must not outlive its turn ─────────────────────────


def _plan_runner(
    mode: str = "plan", cls: type[ClaudeRunner] = _LiveRunner
) -> ClaudeRunner:
    return cls(claude_cmd=str(FAKE_CLI), permission_mode=mode)


def _plan_env(tmp_path: Path, **extra: str) -> Path:
    log = tmp_path / "stdin.log"
    os.environ["FAKE_CLAUDE_START_MODE"] = "plan"
    os.environ["FAKE_CLAUDE_STDIN_LOG"] = str(log)
    os.environ.update(extra)
    return log


def _stdin_kinds(log: Path) -> list[str]:
    if not log.exists():
        return []
    return [line.split(" ", 1)[1] for line in log.read_text().splitlines()]


def _answers(events: list[Any]) -> list[str | None]:
    """Turn answers without #793's `📋 Plan (approved)` preamble."""
    return [
        (t.answer or "").rsplit("---\n\n", 1)[-1]
        for t in _turns(events)
        if t.phase == "completed"
    ]


async def _answer_plans(evt: Any, *, approve: bool = True) -> None:
    """Tap Approve (or Deny) on every ExitPlanMode approval that surfaces."""
    action = getattr(evt, "action", None)
    if action is None or getattr(evt, "phase", None) != "started":
        return
    detail = action.detail or {}
    request_id = detail.get("request_id")
    if request_id and "ExitPlanMode" in (action.title or ""):
        from untether.runners.claude import send_claude_control_response

        assert await send_claude_control_response(request_id, approve)


def _followup_after(n_completions: int, *, approve: bool = True) -> Any:
    """on_event: answer plans; after ``n_completions`` turn closes, inject one
    follow-up via the queue path (it writes at once: the session is idle)."""
    seen = {"done": 0}

    async def on_event(evt: Any, runner: ClaudeRunner) -> None:
        await _answer_plans(evt, approve=approve)
        if isinstance(evt, CompletedEvent) or (
            isinstance(evt, TurnEvent) and evt.phase == "completed"
        ):
            seen["done"] += 1
            if seen["done"] == n_completions:
                assert await claude_mod.inject_when_idle(
                    SID, "next", command_uuid=str(uuid.uuid4())
                )

    return on_event


def _rearm_logs(logs: list[dict[str, Any]], event: str = "rearm_sent") -> list[Any]:
    return [e for e in logs if e["event"] == f"claude.permission_mode.{event}"]


async def test_383_followup_after_approval_runs_in_plan(tmp_path: Path) -> None:
    log = _plan_env(tmp_path)
    with capture_logs() as logs:
        events = await _collect(
            "plan_approve_followup",
            until=2,
            on_event=_followup_after(1),
            runner=_plan_runner(),
        )
    completed = next(e for e in events if isinstance(e, CompletedEvent))
    assert completed.answer.endswith("PLANNED")  # (#793 re-shows the plan)
    assert _answers(events) == ["MODE: plan"]
    kinds = _stdin_kinds(log)
    rearm = kinds.index("control_request:set_permission_mode")
    assert rearm < len(kinds) - 1 - kinds[::-1].index("user")  # before the follow-up
    sent = _rearm_logs(logs)
    assert [e["reason"] for e in sent] == ["idle"]
    assert _rearm_logs(logs, "rearm_ack")
    assert SID not in claude_mod._PLAN_EXIT_APPROVED


async def test_383_wake_turn_after_approval_runs_in_plan(tmp_path: Path) -> None:
    _plan_env(tmp_path)

    async def on_event(evt: Any, runner: ClaudeRunner) -> None:
        await _answer_plans(evt)

    with capture_logs() as logs:
        events = await _collect(
            "plan_approve_bg_bash_wake",
            until=2,
            on_event=on_event,
            runner=_plan_runner(),
        )
    starts = [t for t in _turns(events) if t.phase == "started"]
    assert starts[0].reason == "task_finished"
    assert _answers(events) == ["MODE: plan"]
    assert [e["reason"] for e in _rearm_logs(logs)] == ["idle"]


class _LateRearmRunner(_LiveRunner):
    """The rejected placement (#383 §4 alt. 10): the idle re-arm only goes
    out with the post-yield drains, i.e. after the bridge's on_completed."""

    async def _drain_plan_rearm_pre_yield(
        self, state: Any, *, stdin: Any = None
    ) -> None:
        return None


@pytest.mark.parametrize(
    ("cls", "expected"),
    [(_LiveRunner, "MODE: plan"), (_LateRearmRunner, "MODE: default")],
)
async def test_383_queued_wake_beats_a_slow_consumer(
    tmp_path: Path, cls: type[ClaudeRunner], expected: str
) -> None:
    """The wake-turn race: the CLI starts a queued wake turn 50 ms after the
    result, reading no stdin line first, while the bridge sits in
    on_completed for 2 s. Only the pre-yield re-arm wins; the negative
    control proves the test detects the late placement."""
    log = _plan_env(tmp_path, FAKE_CLAUDE_WAKE_AFTER_RESULT_S="0.05")

    async def on_event(evt: Any, runner: ClaudeRunner) -> None:
        await _answer_plans(evt)
        if isinstance(evt, CompletedEvent):
            await anyio.sleep(2.0)  # the bridge's on_completed

    events = await _collect(
        "plan_approve_queued_wake",
        until=2,
        on_event=on_event,
        runner=_plan_runner(cls=cls),
    )
    assert _answers(events) == [expected]
    lines = log.read_text().splitlines()
    rearm_at = next(float(ln.split()[0]) for ln in lines if "set_permission_mode" in ln)
    wake_at = next(float(ln.split()[0]) for ln in lines if "turn_start:" in ln)
    assert (rearm_at < wake_at) is (expected == "MODE: plan")


async def test_383_errored_turn_still_rearms(tmp_path: Path) -> None:
    _plan_env(tmp_path)
    seen = {"done": 0}

    async def on_event(evt: Any, runner: ClaudeRunner) -> None:
        await _answer_plans(evt)
        if isinstance(evt, CompletedEvent) or (
            isinstance(evt, TurnEvent) and evt.phase == "completed"
        ):
            seen["done"] += 1
            if seen["done"] in (1, 2):
                assert await claude_mod.inject_when_idle(
                    SID, f"msg {seen['done']}", command_uuid=str(uuid.uuid4())
                )

    events = await _collect(
        "plan_approve_error_turn", until=3, on_event=on_event, runner=_plan_runner()
    )
    finals = [t for t in _turns(events) if t.phase == "completed"]
    assert finals[0].ok is False  # the approved turn errored
    assert finals[1].answer == "MODE: plan"


async def test_383_monitor_ticks_after_approval_run_in_plan(tmp_path: Path) -> None:
    _plan_env(tmp_path)

    async def on_event(evt: Any, runner: ClaudeRunner) -> None:
        await _answer_plans(evt)

    events = await _collect(
        "plan_approve_monitor_ticks",
        until=4,
        on_event=on_event,
        runner=_plan_runner(),
    )
    assert _answers(events) == [
        "TICK 1 MODE: plan",
        "TICK 2 MODE: plan",
        "TICK 3 MODE: plan",
    ]


async def test_383_plan_auto_monitor_ticks(tmp_path: Path) -> None:
    """Decision 6: plan-auto ticks are not re-armed (no gate to add, only
    cost); the user's follow-up is."""
    _plan_env(tmp_path)
    with capture_logs() as logs:
        events = await _collect(
            "plan_approve_monitor_ticks",
            until=5,
            on_event=_followup_after(4),
            runner=_plan_runner("plan-auto"),
        )
    assert _answers(events) == [
        "TICK 1 MODE: default",
        "TICK 2 MODE: default",
        "TICK 3 MODE: default",
        "MODE: plan",
    ]
    assert [e["reason"] for e in _rearm_logs(logs)] == ["followup"]


async def test_383_denied_plan_sends_no_rearm(tmp_path: Path) -> None:
    log = _plan_env(tmp_path)
    events = await _collect(
        "plan_deny_followup",
        until=2,
        on_event=_followup_after(1, approve=False),
        runner=_plan_runner(),
    )
    assert _answers(events) == ["MODE: plan"]
    assert "control_request:set_permission_mode" not in _stdin_kinds(log)


async def test_383_bypass_chat_sends_no_rearm(tmp_path: Path) -> None:
    log = tmp_path / "stdin.log"
    os.environ["FAKE_CLAUDE_STDIN_LOG"] = str(log)

    async def inject(evt: Any, runner: ClaudeRunner) -> None:
        if isinstance(evt, CompletedEvent):
            assert await claude_mod.inject_when_idle(
                SID, "second", command_uuid=str(uuid.uuid4())
            )

    await _collect("followup", until=2, on_event=inject)
    kinds = _stdin_kinds(log)
    assert kinds.count("user") == 2
    assert "control_request:set_permission_mode" not in kinds


async def test_383_no_status_frames_still_rearms(tmp_path: Path) -> None:
    _plan_env(tmp_path, FAKE_CLAUDE_NO_STATUS="1")
    with capture_logs() as logs:
        events = await _collect(
            "plan_approve_followup",
            until=2,
            on_event=_followup_after(1),
            runner=_plan_runner(),
        )
    assert _answers(events) == ["MODE: plan"]
    acks = _rearm_logs(logs, "rearm_ack")
    assert acks and acks[0]["mode"] == "plan"
    assert not [
        e
        for e in logs
        if e["event"] == "claude.permission_mode.changed" and e["source"] == "status"
    ]


async def test_383_rearm_error_closes_live_session_when_idle(tmp_path: Path) -> None:
    _plan_env(tmp_path, FAKE_CLAUDE_REARM_ERROR="1")
    runner = _plan_runner()
    runner._live_poll_s = 0.05

    async def on_event(evt: Any, runner: ClaudeRunner) -> None:
        await _answer_plans(evt)

    with capture_logs() as logs:
        events = await _collect(
            "plan_approve_followup", until=99, on_event=on_event, runner=runner
        )
    failed = _rearm_logs(logs, "rearm_failed")
    assert failed and failed[0]["log_level"] == "warning"
    closed = [e for e in logs if e["event"] == "claude.live_session.stdin_closed"]
    assert [e["reason"] for e in closed] == ["plan_rearm_failed"]
    completed = next(e for e in events if isinstance(e, CompletedEvent))
    assert completed.ok
    assert not [e for e in logs if e["event"] == "session.quarantined"]


class _NoRearmRunner(_LiveRunner):
    def new_state(self, prompt: str, resume: ResumeToken | None) -> Any:
        state = super().new_state(prompt, resume)
        state.rearm_plan_mode = False  # `[watchdog] rearm_plan_mode = false`
        return state


async def test_383_kill_switch(tmp_path: Path) -> None:
    log = _plan_env(tmp_path)
    events = await _collect(
        "plan_approve_followup",
        until=2,
        on_event=_followup_after(1),
        runner=_plan_runner(cls=_NoRearmRunner),
    )
    assert _answers(events) == ["MODE: default"]
    assert "control_request:set_permission_mode" not in _stdin_kinds(log)


# ── #383 C4: the approved plan's background agents defer the re-arm ────────


def _inject_at(*completions: int) -> Any:
    """on_event: answer plans; after each listed turn-close count, inject one
    follow-up via the queue path (the session is idle, so it writes at once)."""
    seen = {"done": 0}

    async def on_event(evt: Any, runner: ClaudeRunner) -> None:
        await _answer_plans(evt)
        if isinstance(evt, CompletedEvent) or (
            isinstance(evt, TurnEvent) and evt.phase == "completed"
        ):
            seen["done"] += 1
            if seen["done"] in completions:
                assert await claude_mod.inject_when_idle(
                    SID, f"msg {seen['done']}", command_uuid=str(uuid.uuid4())
                )

    return on_event


def _starts(events: list[Any]) -> list[TurnEvent]:
    return [t for t in _turns(events) if t.phase == "started"]


async def test_383_agent_wake_deferred_then_rearmed(tmp_path: Path) -> None:
    """P-3: a running subagent inherits the parent's mode, so the re-arm
    waits for the agent the approved turn launched. A follow-up meanwhile
    runs unplanned and says so; the agent's own wake turn and later
    follow-ups are planned again."""
    log = _plan_env(tmp_path, FAKE_CLAUDE_WAKE_S="1.5")
    with capture_logs() as logs:
        events = await _collect(
            "plan_approve_agent_wake",
            until=4,
            on_event=_inject_at(1, 3),
            runner=_plan_runner(),
        )
    assert _answers(events) == ["MODE: default", "MODE: plan", "MODE: plan"]
    starts = _starts(events)
    assert [t.reason for t in starts] == ["followup", "task_finished", "followup"]
    assert starts[0].detail["plan_deferred"] == {"agents": 1}
    assert not any("plan_deferred" in t.detail for t in starts[1:])
    kinds = _stdin_kinds(log)
    # The approved agent ran to the end unplanned; the re-arm followed its
    # end and beat the wake turn the CLI started by itself.
    assert "agent_end:a1:default" in kinds
    rearm = kinds.index("control_request:set_permission_mode")
    assert kinds.index("agent_end:a1:default") < rearm
    assert rearm < kinds.index("turn_start:plan")
    assert kinds.count("control_request:set_permission_mode") == 1
    deferred = _rearm_logs(logs, "rearm_deferred")
    assert deferred and deferred[0]["reason"] == "live_agents"
    assert deferred[0]["agents"] == 1
    assert [e["reason"] for e in _rearm_logs(logs)] == ["agents_done"]


async def test_383_deferral_does_not_chain(tmp_path: Path) -> None:
    """The unplanned follow-up launches a second agent; once the approved
    turn's agent ends, plan mode comes back although the second still runs."""
    log = _plan_env(tmp_path, FAKE_CLAUDE_WAKE_S="1.5")
    with capture_logs() as logs:
        events = await _collect(
            "plan_approve_agent_chain",
            until=4,
            on_event=_inject_at(1),
            runner=_plan_runner(),
        )
    assert _answers(events) == ["MODE: default", "MODE: plan", "MODE: plan"]
    kinds = _stdin_kinds(log)
    assert "agent_end:a1:default" in kinds
    assert "agent_end:a2:plan" in kinds  # a later turn's agent: not deferred for
    assert [e["reason"] for e in _rearm_logs(logs)] == ["agents_done"]


async def test_383_agent_before_approval_not_deferred(tmp_path: Path) -> None:
    """An agent launched in an earlier, planning turn ran under plan mode
    anyway: the approved turn's close re-arms at once."""
    _plan_env(tmp_path, FAKE_CLAUDE_WAKE_S="1.5")
    with capture_logs() as logs:
        events = await _collect(
            "plan_approve_agent_before",
            until=4,
            on_event=_inject_at(1, 2),
            runner=_plan_runner(),
        )
    assert _answers(events) == ["PLANNED", "MODE: plan", "MODE: plan"]
    assert not _rearm_logs(logs, "rearm_deferred")
    assert [e["reason"] for e in _rearm_logs(logs)] == ["idle"]


# ---------------------------------------------------------------------------
# #684: a withdrawn request in a follow-up turn
# ---------------------------------------------------------------------------


async def test_684_cancel_in_followup_turn_records_channel() -> None:
    """A follow-up turn is translated by the run's reader tasks, which inherit
    the run's chat (``get_run_channel_id`` ContextVar, set by the executor
    around ``handle_message``) — so a withdrawn request's record is scoped to
    the right chat and a late tap there reads "No longer needed"."""
    from untether.runners.claude import ControlRequestStatus, classify_control_request
    from untether.utils.paths import reset_run_channel_id, set_run_channel_id

    cmd = str(uuid.uuid4())

    async def inject(evt: Any, runner: ClaudeRunner) -> None:
        if isinstance(evt, CompletedEvent):
            assert await write_user_message(SID, "do it", command_uuid=cmd)

    runner = _LiveRunner(claude_cmd=str(FAKE_CLI), permission_mode="default")
    token = set_run_channel_id(68_400)
    try:
        with capture_logs() as logs:
            events = await _collect(
                "control_cancel_followup", until=2, on_event=inject, runner=runner
            )
    finally:
        reset_run_channel_id(token)

    start, end = [t for t in _turns(events) if t.phase in ("started", "completed")]
    assert end.answer == "Stopped."
    withdrawn = [
        e
        for e in events[events.index(start) : events.index(end)]
        if getattr(e, "phase", None) == "completed"
        and "withdrawn" in getattr(getattr(e, "action", None), "title", "")
    ]
    assert len(withdrawn) == 1
    record = claude_mod._HANDLED_REQUESTS["req-cancel-1"]
    assert record is not None
    assert (record.outcome, record.channel_id) == ("cancelled", 68_400)
    lookup = classify_control_request("req-cancel-1", channel_id=68_400)
    assert lookup.status is ControlRequestStatus.CANCELLED
    assert any(e["event"] == "control_request.cancelled_by_cli" for e in logs)


async def test_751_fake_cli_init_reports_different_mode() -> None:
    """`auto` requested, the CLI's first init reports `default` (as on
    Haiku): a warning row right after StartedEvent, the gate re-armed, and no
    re-check on the follow-up turn's init."""
    from untether.model import ActionEvent

    os.environ["FAKE_CLAUDE_INIT_PERMISSION_MODE"] = "default"
    runner = _LiveRunner(claude_cmd=str(FAKE_CLI), permission_mode="auto")
    cmd = str(uuid.uuid4())

    async def inject(evt: Any, _runner: ClaudeRunner) -> None:
        if isinstance(evt, CompletedEvent):
            assert await write_user_message(SID, "second", command_uuid=cmd)

    with capture_logs() as logs:
        events = await _collect("followup", until=2, on_event=inject, runner=runner)
    assert isinstance(events[0], StartedEvent)
    rows = [
        e
        for e in events
        if isinstance(e, ActionEvent)
        and e.action.id.startswith("claude.permission_mode_mismatch")
    ]
    assert [r.phase for r in rows] == ["started", "completed"]
    assert events.index(rows[0]) == 1
    assert rows[-1].action.title.endswith("approvals will be requested")
    assert any(isinstance(e, CompletedEvent) for e in events)
    assert _turns(events)[-1].answer == "ECHO: second"
    mismatch = [e for e in logs if e["event"] == "claude.permission_mode.mismatch"]
    assert len(mismatch) == 1
    assert mismatch[0]["prompting_rearmed"] is True
