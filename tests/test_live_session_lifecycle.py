"""#776 phase 03: when a live Claude session's stdin closes.

Real ``ClaudeRunner`` against ``tests/fake_clis/fake_claude_live.py`` with the
lifecycle timers shrunk to fractions of a second.
"""

from __future__ import annotations

import math
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from untether.model import CompletedEvent, TurnEvent
from untether.runners import claude as claude_mod
from untether.runners.claude import (
    ClaudeRunner,
    ClaudeStreamState,
    LiveSession,
    add_live_session_listener,
    close_live_session,
    is_session_accepting,
)
from untether.session_quarantine import QuarantineStore, set_quarantine_store
from untether.settings import WatchdogSettings

pytestmark = pytest.mark.anyio

FAKE_CLI = Path(__file__).parent / "fake_clis" / "fake_claude_live.py"
SID = "fake-live-session"
_ENV = (
    "FAKE_CLAUDE_SCENARIO",
    "FAKE_CLAUDE_WAKE_S",
    "FAKE_CLAUDE_IGNORE_SIGINT",
    "FAKE_CLAUDE_REWAKE_WAIT_S",
    "FAKE_CLAUDE_SYNC_HOOK_S",
    # #829
    "FAKE_CLAUDE_PROGRESS_S",
    "FAKE_CLAUDE_PROGRESS_FOR_S",
    "FAKE_CLAUDE_PROGRESS_THEN",
    "FAKE_CLAUDE_TOOL_S",
    "FAKE_CLAUDE_MARKER_FILE",
    "FAKE_CLAUDE_OUTPUT_FILE",
    "FAKE_CLAUDE_EOF_MODE",
    "FAKE_CLAUDE_SIGINT_RC",
    # #684
    "FAKE_CLAUDE_STDIN_LOG",
    "FAKE_CLAUDE_UNANSWERED_RESULT_S",
    # #872
    "FAKE_CLAUDE_BASH_TIMEOUT_MS",
    # #876
    "FAKE_CLAUDE_BG_PATCH",
    # #872 R17-01a
    "FAKE_CLAUDE_WAKE_HOOK_S",
    "FAKE_CLAUDE_WAKE_DELAY_S",
    # #1001
    "FAKE_CLAUDE_DEATH",
)


# ClaudeRunner is a slots dataclass, so these knobs are instance fields: a
# class-attribute override on a subclass is shadowed by the field default
# (the old overrides here were silently inert — 15 s grace, 1 s poll).
# ``_run`` applies them to the instance instead (#791).
_TIMINGS = {
    "_live_poll_s": 0.05,
    "_live_close_grace_s": 0.8,
    "_live_close_sigint_grace_s": 0.8,
    # #812: distinct from the plain grace so tests can tell which applied.
    "_live_close_grace_hooks_s": 1.6,
    "_subcountdown_sigterm_grace_s": 1.0,
    "_subcountdown_sigterm_grace_poll_s": 0.1,
    # #829: log every hold re-arm so tests can count them.
    "_hold_rearm_log_every_s": 0.0,
    # #872: the grace after a declared wait's deadline.
    "_declared_wait_grace_s": 0.2,
}


class _LiveRunner(ClaudeRunner):
    def env(self, *, state: Any) -> dict[str, str] | None:
        base = super().env(state=state) or {}
        for key in _ENV:
            if key in os.environ:
                base[key] = os.environ[key]
        return base


@pytest.fixture(autouse=True)
def _env_cleanup():
    yield
    for key in _ENV:
        os.environ.pop(key, None)


@pytest.fixture
def quarantine(tmp_path: Path):
    store = QuarantineStore(tmp_path / "session_quarantine.json")
    set_quarantine_store(store)
    yield store
    set_quarantine_store(None)


def _settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    values = {
        "post_result_limbo_grace": 0.3,
        "post_result_bg_max_hold": 1800.0,
        "live_session_max_s": 14400.0,
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


async def _run(
    scenario: str,
    *,
    on_event: Any = None,
    wake_s: float = 0.3,
    timeout: float = 20,
    permission_mode: str = "bypassPermissions",
) -> tuple[ClaudeRunner, list[Any]]:
    os.environ["FAKE_CLAUDE_SCENARIO"] = scenario
    os.environ["FAKE_CLAUDE_WAKE_S"] = str(wake_s)
    runner = _LiveRunner(claude_cmd=str(FAKE_CLI), permission_mode=permission_mode)
    for name, value in _TIMINGS.items():
        setattr(runner, name, value)
    events: list[Any] = []
    with anyio.fail_after(timeout):
        async for evt in runner.run("hello", None):
            events.append(evt)
            if on_event is not None:
                await on_event(evt)
    return runner, events


def _engine_state(runner: ClaudeRunner) -> ClaudeStreamState:
    return runner.current_stream.engine_state


async def test_idle_no_tasks_closes_after_grace_and_exits_rc0_no_quarantine(
    monkeypatch: pytest.MonkeyPatch, quarantine: QuarantineStore
) -> None:
    _settings(monkeypatch)
    runner, events = await _run("followup")
    assert isinstance(events[-1], CompletedEvent)
    assert _engine_state(runner).live_close_reason == "idle_no_tasks"
    assert runner.current_stream.proc_returncode == 0
    assert runner.current_stream.sigterm_sent is False
    assert not quarantine.is_quarantined("claude", SID)


async def test_live_task_holds_stdin_open_past_grace_until_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch)
    runner, events = await _run("bg_bash_wake", wake_s=1.5)
    finals = [e for e in events if isinstance(e, TurnEvent) and e.phase == "completed"]
    assert [f.answer for f in finals] == ["GOT: BG-FINISHED"]
    assert _engine_state(runner).live_close_reason == "idle_no_tasks"


async def test_resumed_agent_holds_stdin_open_until_it_finishes(
    monkeypatch: pytest.MonkeyPatch, quarantine: QuarantineStore
) -> None:
    """#801: Claude sends a finished background agent back to work (same
    task_id). The live session must not idle-close under the resumed agent —
    it closes only after the re-check finishes and its wake turn delivers."""
    from structlog.testing import capture_logs

    _settings(monkeypatch)  # 0.3 s idle grace, well under the 1.2 s re-check
    with capture_logs() as logs:
        runner, events = await _run("agent_resumed", wake_s=1.2)
    finals = [e for e in events if isinstance(e, TurnEvent) and e.phase == "completed"]
    assert [f.answer for f in finals] == [
        "I've sent it back to re-check, it's running now",
        "RECHECK DONE",
    ]
    state = _engine_state(runner)
    assert state.live_close_reason == "idle_no_tasks"
    task = state.tasks["a1"]
    assert task.revived_count == 1
    assert task.status == "completed"  # finished itself, not killed on EOF
    assert runner.current_stream.sigterm_sent is False
    assert not quarantine.is_quarantined("claude", SID)
    revived = _events(logs, "claude.task.revived")
    assert len(revived) == 1 and revived[0]["source"] == "task_started"
    # Stdin was closed only after the re-check's wake turn.
    closes = _events(logs, "claude.live_session.stdin_closed")
    assert len(closes) == 1 and closes[0]["live_tasks"] == 0
    recheck = next(
        e
        for e in logs
        if e.get("event") == "claude.turn.completed" and e.get("turn") == 3
    )
    assert logs.index(recheck) < logs.index(closes[0])


async def test_subagent_orphaned_bg_task_holds_stdin_open_until_it_ends(
    monkeypatch: pytest.MonkeyPatch, quarantine: QuarantineStore
) -> None:
    """#801 follow-up: a background agent backgrounds its own ``sleep`` and
    ends. That subagent-owned task outlives it — the session must not
    idle-close (and kill it) while it runs, and must close once it ends."""
    from structlog.testing import capture_logs

    _settings(monkeypatch)  # 0.3 s idle grace, well under the 1.2 s sleep
    with capture_logs() as logs:
        runner, events = await _run("agent_orphans_bg_task", wake_s=1.2)
    finals = [e for e in events if isinstance(e, TurnEvent) and e.phase == "completed"]
    assert [f.answer for f in finals] == [
        "The re-check is still running, I'll report when it finishes",
        "RECHECK: recheck",
    ]
    state = _engine_state(runner)
    assert state.tasks["bz1"].status == "completed"  # finished, not killed
    assert state.live_close_reason == "idle_no_tasks"
    assert runner.current_stream.sigterm_sent is False
    assert not quarantine.is_quarantined("claude", SID)
    closes = _events(logs, "claude.live_session.stdin_closed")
    assert len(closes) == 1 and closes[0]["live_tasks"] == 0
    orphan_end = next(
        e
        for e in logs
        if e.get("event") == "claude.task.ended" and e.get("task_id") == "bz1"
    )
    assert logs.index(orphan_end) < logs.index(closes[0])


async def test_max_hold_emits_notice_then_graceful_close_no_quarantine(
    monkeypatch: pytest.MonkeyPatch, quarantine: QuarantineStore
) -> None:
    _settings(monkeypatch, post_result_bg_max_hold=0.5)
    notices: list[tuple[str, dict]] = []

    async def subscribe(evt: Any) -> None:
        if isinstance(evt, CompletedEvent):
            add_live_session_listener(SID, lambda kind, p: notices.append((kind, p)))

    runner, events = await _run("bg_bash_wake", wake_s=30, on_event=subscribe)
    assert _engine_state(runner).live_close_reason == "max_hold"
    assert notices and notices[0][0] == "closing"
    assert notices[0][1]["reason"] == "max_hold"
    assert notices[0][1]["tasks"] == ["bg b1"]
    # The CLI stopped the task itself on EOF (F3): recorded, not SIGTERM'd.
    assert _engine_state(runner).tasks["b1"].status == "killed"
    assert runner.current_stream.sigterm_sent is False
    assert not quarantine.is_quarantined("claude", SID)
    assert not any(isinstance(e, TurnEvent) for e in events)


async def test_pending_wakeup_holds_until_it_fires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch)
    runner, events = await _run("scheduled_wakeup", wake_s=1.2)
    finals = [e for e in events if isinstance(e, TurnEvent) and e.phase == "completed"]
    assert [f.answer for f in finals] == ["WOKE"]
    assert _engine_state(runner).pending_wakeup_until is None


async def test_pending_control_request_pauses_timers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch)
    seen: dict[str, Any] = {}

    async def hold_then_release(evt: Any) -> None:
        if isinstance(evt, CompletedEvent):
            claude_mod._REQUEST_TO_SESSION["req_pending_776"] = SID
            await anyio.sleep(1.0)  # well past the 0.3 s grace
            seen["accepting_while_pending"] = is_session_accepting(SID)
            claude_mod._REQUEST_TO_SESSION.pop("req_pending_776", None)

    runner, _ = await _run("followup", on_event=hold_then_release)
    assert seen["accepting_while_pending"] is True
    assert _engine_state(runner).live_close_reason == "idle_no_tasks"


async def test_absolute_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, live_session_max_s=0.5)
    runner, _ = await _run("bg_bash_wake", wake_s=30)
    assert _engine_state(runner).live_close_reason == "abs_cap"


def _events(logs: list[dict], name: str) -> list[dict]:
    return [e for e in logs if e.get("event") == name]


async def test_idle_close_overrunning_grace_logs_diag_sigterms_no_quarantine(
    monkeypatch: pytest.MonkeyPatch, quarantine: QuarantineStore
) -> None:
    """#791: an idle, no-task close whose CLI won't exit (deaf to EOF and
    SIGINT) is SIGTERM'd — but its transcript is complete, so the session is
    NOT quarantined, and a process snapshot is logged before any signal."""
    from structlog.testing import capture_logs

    _settings(monkeypatch)
    os.environ["FAKE_CLAUDE_IGNORE_SIGINT"] = "1"
    with capture_logs() as logs:
        runner, events = await _run("ignore_eof")
    assert isinstance(events[-1], CompletedEvent)
    assert _engine_state(runner).live_close_reason == "idle_no_tasks"
    assert runner.current_stream.sigterm_sent is True
    assert not quarantine.is_quarantined("claude", SID)
    expired = _events(logs, "claude.live_session.close_grace_expired")
    assert len(expired) == 1
    snap = expired[0]
    assert snap["log_level"] == "warning"
    assert snap["idle_clean"] is True
    assert snap["close_reason"] == "idle_no_tasks"
    assert snap["live_tasks"] == 0
    assert snap["diag"] and snap["diag"] != "dead"
    for key in ("process_state", "wchan", "fd_count", "children", "child_count"):
        assert key in snap
    teardown = _events(logs, "claude.live_session.forced_teardown")
    assert len(teardown) == 1
    assert teardown[0]["quarantined"] is False
    assert teardown[0]["idle_clean"] is True
    # The snapshot precedes the SIGTERM decision.
    assert logs.index(expired[0]) < logs.index(teardown[0])


async def test_idle_close_overrunning_grace_exits_on_sigint(
    monkeypatch: pytest.MonkeyPatch, quarantine: QuarantineStore
) -> None:
    """#791 (c): SIGINT — the CLI's Ctrl-C path — comes before SIGTERM."""
    from structlog.testing import capture_logs

    _settings(monkeypatch)
    with capture_logs() as logs:
        runner, events = await _run("ignore_eof")
    assert isinstance(events[-1], CompletedEvent)
    assert runner.current_stream.sigterm_sent is False
    assert not quarantine.is_quarantined("claude", SID)
    assert len(_events(logs, "claude.live_session.close_grace_expired")) == 1
    assert len(_events(logs, "claude.live_session.exited_after_sigint")) == 1
    assert _events(logs, "claude.live_session.forced_teardown") == []


async def test_forced_teardown_with_live_task_still_quarantines(
    monkeypatch: pytest.MonkeyPatch, quarantine: QuarantineStore
) -> None:
    """#791: only the clean idle close is exempt — a wedged close over a
    still-live background task keeps the #632 quarantine."""
    from structlog.testing import capture_logs

    _settings(monkeypatch, post_result_bg_max_hold=0.3)
    os.environ["FAKE_CLAUDE_IGNORE_SIGINT"] = "1"
    with capture_logs() as logs:
        runner, events = await _run("ignore_eof_with_task")
    assert isinstance(events[-1], CompletedEvent)
    assert _engine_state(runner).live_close_reason == "max_hold"
    assert runner.current_stream.sigterm_sent is True
    assert quarantine.is_quarantined("claude", SID)
    expired = _events(logs, "claude.live_session.close_grace_expired")
    assert len(expired) == 1 and expired[0]["idle_clean"] is False
    teardown = _events(logs, "claude.live_session.forced_teardown")
    assert teardown and teardown[0]["quarantined"] is True


async def test_accepting_input_false_once_closing() -> None:
    class _Pipe:
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    pipe = _Pipe()
    state = ClaudeStreamState()
    claude_mod._LIVE_SESSIONS["sid-race"] = LiveSession(
        session_id="sid-race", state=state, stdin=pipe
    )
    try:
        assert is_session_accepting("sid-race") is True
        assert await close_live_session("sid-race", "new") is True
        assert is_session_accepting("sid-race") is False
        assert pipe.closed is True
        assert state.live_close_reason == "new"
        # Idempotent.
        assert await close_live_session("sid-race", "drain") is False
    finally:
        claude_mod._LIVE_SESSIONS.pop("sid-race", None)


async def test_errored_first_result_does_not_keep_session_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review finding: an errored first result left a live session behind, so
    the error (delivered only post-return) waited for the idle close and
    follow-ups could be injected into the broken session."""
    import time as _time

    _settings(monkeypatch, post_result_limbo_grace=30.0)  # would hold 30 s
    started = _time.monotonic()
    runner, events = await _run("error_first")
    assert isinstance(events[-1], CompletedEvent) and events[-1].ok is False
    assert not any(isinstance(e, TurnEvent) for e in events)
    assert _engine_state(runner).live_close_reason == "error"
    assert runner.current_stream.sigterm_sent is False
    assert _time.monotonic() - started < 10


async def test_idle_close_backs_off_when_a_followup_was_just_written() -> None:
    """Review finding: the lifecycle's idle close must re-check under the
    injection lock, or it closes stdin right after a follow-up was written."""

    class _Pipe:
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    pipe = _Pipe()
    state = ClaudeStreamState()
    state.completed_turns = 1
    state.turn_open = False
    state.awaiting_injected["u1"] = 0.0
    claude_mod._LIVE_SESSIONS["sid-race2"] = LiveSession(
        session_id="sid-race2", state=state, stdin=pipe
    )
    try:
        assert (
            await close_live_session("sid-race2", "idle_no_tasks", only_if_idle=True)
            is False
        )
        assert pipe.closed is False and is_session_accepting("sid-race2")
        # An explicit close (cancel / drain) still goes through.
        assert await close_live_session("sid-race2", "cancel") is True
    finally:
        claude_mod._LIVE_SESSIONS.pop("sid-race2", None)


# ── #812: background hooks hold the live session ─────────────────────────


def _hook_notices() -> tuple[list[tuple[str, dict]], Any]:
    notices: list[tuple[str, dict]] = []

    async def subscribe(evt: Any) -> None:
        if isinstance(evt, CompletedEvent):
            add_live_session_listener(SID, lambda kind, p: notices.append((kind, p)))

    return notices, subscribe


async def test_812_pending_hook_holds_idle_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Stop hook outlives the 0.3 s idle grace several times over; stdin
    stays open until its ``hook_response`` lands, then the session idles
    out normally — nothing killed, nothing to report."""
    from structlog.testing import capture_logs

    _settings(monkeypatch)
    started = anyio.current_time()
    with capture_logs() as logs:
        runner, events = await _run("async_hook_success", wake_s=1.5)
    assert anyio.current_time() - started >= 1.5
    state = _engine_state(runner)
    assert state.live_close_reason == "idle_no_tasks"
    assert state.pending_hooks == {}  # the response arrived before the close
    assert state.hooks_started == 2  # UserPromptSubmit + Stop
    holds = _events(logs, "claude.hook.pending_hold")
    assert len(holds) == 1  # once per hold, not per poll
    assert holds[0]["hook_names"] == ["Stop"]
    assert holds[0]["log_level"] == "info"
    assert _events(logs, "claude.live_session.async_hook_killed") == []
    assert _events(logs, "claude.live_session.close_grace_expired") == []
    assert runner.current_stream.proc_returncode == 0
    assert not any(isinstance(e, TurnEvent) for e in events)
    # Hooks are not background tasks: nothing in the task map.
    assert state.tasks == {}


async def test_812_plain_async_hooks_do_not_hold_past_their_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live regression (CLI 2.1.285, ``moshi-hook`` ``async: true`` on
    UserPromptSubmit + Stop): the CLI withholds a plain async hook's
    ``hook_response`` while the session is idle — it lands only at the next
    turn or at teardown — so waiting for it held every session to the
    630 s bound. Once no hook process is left, the stragglers are deferred
    and the idle close proceeds at the idle grace; the asyncRewake hook
    alongside still holds until it reports back."""
    from structlog.testing import capture_logs

    _settings(monkeypatch, async_hook_max_hold=5.0)
    started = anyio.current_time()
    with capture_logs() as logs:
        runner, events = await _run(
            "async_hook_post_result_response", wake_s=1.0, timeout=15
        )
    elapsed = anyio.current_time() - started
    state = _engine_state(runner)
    holds = _events(logs, "claude.hook.pending_hold")
    assert len(holds) == 1
    assert sorted(holds[0]["hook_names"]) == ["Stop", "Stop", "UserPromptSubmit"]
    released = _events(logs, "claude.hook.hold_released")
    assert len(released) == 1
    # All or nothing: released only once no hook process is left.
    assert released[0]["reason"] == "no_hook_process"
    assert sorted(released[0]["hook_ids"]) == ["h-stop-b", "h-ups-b"]
    closed = _events(logs, "claude.live_session.stdin_closed")
    assert len(closed) == 1 and closed[0]["reason"] == "idle_no_tasks"
    assert logs.index(holds[0]) < logs.index(released[0]) < logs.index(closed[0])
    assert state.live_close_reason == "idle_no_tasks"
    # The rewake hook held (1 s), no hook process for the 1 s settle, then
    # the plain 0.3 s idle grace — well short of the 5 s bound.
    assert 2.0 <= elapsed < 4.5
    assert _events(logs, "claude.hook.hold_expired") == []
    assert _events(logs, "claude.live_session.async_hook_killed") == []
    assert _events(logs, "claude.live_session.close_grace_expired") == []
    # The withheld responses arrived at teardown and paired off.
    assert state.pending_hooks == {}
    assert state.deferred_hooks == {}
    assert state.expired_hooks == {}
    assert runner.current_stream.proc_returncode == 0
    assert not any(isinstance(e, TurnEvent) for e in events)


async def test_812_live_hook_shell_holds_every_unpaired_hook_until_rewake(
    monkeypatch: pytest.MonkeyPatch, quarantine: QuarantineStore
) -> None:
    """Live regression (CLI 2.1.285, @untether_dev_bot, 271bb96): plain
    ``async`` UserPromptSubmit + Stop hooks (exit at once, response
    withheld), a sync Stop hook with no visible ``<shell> -c`` answering
    1.5 s after the result, and an ``asyncRewake`` Stop hook whose inline
    command (no ``/hooks/``) is still running — all started in the same few
    ms. The per-hook PID binding tied the one live shell to the sync hook,
    released the rewake hook after 1 s, and once the sync hook answered the
    session idled out and the rewake was lost. All or nothing: while any
    hook shell lives nothing is released, so the rewake turn arrives."""
    from structlog.testing import capture_logs

    _settings(monkeypatch)
    started = anyio.current_time()
    with capture_logs() as logs:
        runner, events = await _run(
            "async_hook_live_mix_rewake", wake_s=3.0, timeout=20
        )
    elapsed = anyio.current_time() - started
    state = _engine_state(runner)
    holds = _events(logs, "claude.hook.pending_hold")
    assert holds and sorted(holds[0]["hook_names"]) == [
        "Stop",
        "Stop",
        "Stop",
        "UserPromptSubmit",
    ]
    # Nothing released while the rewake hook's shell was alive.
    for released in _events(logs, "claude.hook.hold_released"):
        assert "h-stop-rewake" not in released["hook_ids"]
    rewake = [
        e
        for e in events
        if isinstance(e, TurnEvent)
        and e.phase == "started"
        and e.reason == "hook_rewake"
    ]
    assert len(rewake) == 1
    finals = [e for e in events if isinstance(e, TurnEvent) and e.phase == "completed"]
    assert [f.answer for f in finals] == ["HOOK: finding: key leak"]
    closed = _events(logs, "claude.live_session.stdin_closed")
    assert len(closed) == 1 and closed[0]["reason"] == "idle_no_tasks"
    assert logs.index(holds[0]) < logs.index(closed[0])
    # Held past the 0.3 s idle grace until the rewake (3 s after spawn).
    assert elapsed >= 3.0
    assert _events(logs, "claude.hook.hold_expired") == []
    assert _events(logs, "claude.live_session.async_hook_killed") == []
    assert _events(logs, "claude.live_session.close_grace_expired") == []
    assert state.pending_hooks == {} and state.deferred_hooks == {}
    assert state.expired_hooks == {}
    assert runner.current_stream.proc_returncode == 0
    assert not quarantine.is_quarantined("claude", SID)


async def test_812_hold_expiry_counts_live_hook_processes_not_candidates(
    monkeypatch: pytest.MonkeyPatch, quarantine: QuarantineStore
) -> None:
    """The same mix, but the rewake hook outlives the bound. The expiry is
    one WARN for the hold (1 live hook process among 3 unpaired
    candidates), the close gets the hooks grace from the live shell, and
    the user hears about ONE hook that could be either event — not "3
    background hooks" (8cf35e5's labelling defect)."""
    from structlog.testing import capture_logs

    from untether.runner_bridge import _live_closing_notice

    _settings(monkeypatch, async_hook_max_hold=2.5)
    # The CLI outwaits the plain grace but not the hooks grace... then some:
    # force the grace to expire so the log shows which one applied.
    os.environ["FAKE_CLAUDE_REWAKE_WAIT_S"] = "6"
    notices, subscribe = _hook_notices()
    with capture_logs() as logs:
        runner, _ = await _run(
            "async_hook_live_mix_running", on_event=subscribe, timeout=25
        )
    state = _engine_state(runner)
    assert _events(logs, "claude.hook.hold_released") == []
    expired = _events(logs, "claude.hook.hold_expired")
    assert len(expired) == 1
    assert expired[0]["log_level"] == "warning"
    assert expired[0]["live_hook_processes"] == 1
    assert expired[0]["pending_hooks"] == 3
    assert expired[0]["hook_events"] == ["Stop", "UserPromptSubmit"]
    assert expired[0]["held_s"] >= expired[0]["max_hold_s"] == 2.5
    closed = _events(logs, "claude.live_session.stdin_closed")
    assert len(closed) == 1 and closed[0]["reason"] == "idle_no_tasks"
    killed = _events(logs, "claude.live_session.async_hook_killed")
    assert len(killed) == 1
    assert killed[0]["log_level"] == "warning"
    assert killed[0]["hook_count"] == 1
    assert killed[0]["live_hook_processes"] == 1
    assert killed[0]["hook_events"] == ["Stop", "UserPromptSubmit"]
    assert sorted(killed[0]["hook_ids"]) == ["h-stop-plain", "h-stop-rewake", "h-ups"]
    assert "candidates" in killed[0]["note"]
    grace = _events(logs, "claude.live_session.close_grace_expired")
    assert len(grace) == 1
    assert grace[0]["grace_s"] == runner._live_close_grace_hooks_s == 1.6
    assert (
        logs.index(expired[0])
        < logs.index(closed[0])
        < logs.index(killed[0])
        < logs.index(grace[0])
    )
    payloads = [p for kind, p in notices if kind == "closing"]
    assert len(payloads) == 1
    assert payloads[0]["hooks"] == ["Stop", "UserPromptSubmit"]
    assert payloads[0]["hook_count"] == 1
    assert _live_closing_notice(
        payloads[0]["reason"], [], payloads[0]["hooks"], payloads[0]["hook_count"]
    ) == (
        "\N{HOURGLASS WITH FLOWING SAND} Closing session — a background hook "
        "(Stop or UserPromptSubmit) was still running; its feedback wasn't "
        "delivered."
    )
    assert state.pending_hooks == {}
    # A clean idle close overrunning its grace is not quarantined (#791).
    assert not quarantine.is_quarantined("claude", SID)


async def test_812_execd_hook_command_is_held_until_its_rewake(
    monkeypatch: pytest.MonkeyPatch, quarantine: QuarantineStore
) -> None:
    """macOS regression (033ca86): ``/bin/sh`` is bash there, and bash execs
    ``sh -c '<single command>'`` — the security-guidance asyncRewake Stop
    hook (``bash …/sg-python.sh …``) runs with no ``<shell> -c`` left. Only
    counting shells, the hold saw no hook process, released at ~1 s and the
    session idled out: rewake lost. Any non-baseline CLI child counts now."""
    from structlog.testing import capture_logs

    _settings(monkeypatch)
    started = anyio.current_time()
    with capture_logs() as logs:
        runner, events = await _run("async_hook_exec_rewake", wake_s=3.0, timeout=20)
    elapsed = anyio.current_time() - started
    state = _engine_state(runner)
    for released in _events(logs, "claude.hook.hold_released"):
        assert "h-stop-rewake" not in released["hook_ids"]
    rewake = [
        e
        for e in events
        if isinstance(e, TurnEvent)
        and e.phase == "started"
        and e.reason == "hook_rewake"
    ]
    assert len(rewake) == 1
    finals = [e for e in events if isinstance(e, TurnEvent) and e.phase == "completed"]
    assert [f.answer for f in finals] == ["HOOK: finding: key leak"]
    assert elapsed >= 3.0
    assert _events(logs, "claude.live_session.async_hook_killed") == []
    assert _events(logs, "claude.live_session.close_grace_expired") == []
    assert state.pending_hooks == {} and state.deferred_hooks == {}
    assert runner.current_stream.proc_returncode == 0


@pytest.mark.parametrize(
    ("scenario", "hook_id"),
    [
        # #812 review 2: UserPromptSubmit's hook_started precedes
        # system/init, so an exec'd asyncRewake UPS hook is still alive when
        # the baseline is taken — it used to be baselined (never evidence)
        # and released at ~1 s, losing the rewake.
        ("async_hook_ups_rewake", "h-ups-rewake"),
        # #812 review 3: a hook whose argv looks like an MCP server
        # (``… mcp-scan``) used to be exempt as a "late service".
        ("async_hook_service_named_rewake", "h-stop-rewake"),
    ],
)
async def test_812_hook_that_looks_exempt_is_held_until_its_rewake(
    monkeypatch: pytest.MonkeyPatch,
    quarantine: QuarantineStore,
    scenario: str,
    hook_id: str,
) -> None:
    """Hooks are spawned detached (their own process group): neither the
    init baseline nor the MCP/LSP-name heuristic may exempt one."""
    from structlog.testing import capture_logs

    _settings(monkeypatch)
    started = anyio.current_time()
    with capture_logs() as logs:
        runner, events = await _run(scenario, wake_s=3.0, timeout=20)
    elapsed = anyio.current_time() - started
    state = _engine_state(runner)
    for released in _events(logs, "claude.hook.hold_released"):
        assert hook_id not in released["hook_ids"]
    rewake = [
        e
        for e in events
        if isinstance(e, TurnEvent)
        and e.phase == "started"
        and e.reason == "hook_rewake"
    ]
    assert len(rewake) == 1
    finals = [e for e in events if isinstance(e, TurnEvent) and e.phase == "completed"]
    assert [f.answer for f in finals] == ["HOOK: finding: key leak"]
    assert elapsed >= 3.0
    assert _events(logs, "claude.live_session.async_hook_killed") == []
    assert state.pending_hooks == {} and state.deferred_hooks == {}
    assert runner.current_stream.proc_returncode == 0


async def test_812_mcp_like_children_never_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long-lived child present at ``system/init`` (an MCP server, the
    session baseline — one behind a non-exec'ing ``sh -c``, #812 review 4)
    and an MCP-looking child started later (a reconnect) are not hook
    evidence: the finished plain async hooks are released on the normal
    settle and the session idles out, far inside the bound. (A baselined
    ``sh -c`` used to count forever: a hold to the bound and a false
    "feedback wasn't delivered" notice on every session.)"""
    from structlog.testing import capture_logs

    _settings(monkeypatch, async_hook_max_hold=8.0)
    started = anyio.current_time()
    with capture_logs() as logs:
        runner, _ = await _run("async_hook_with_services", timeout=20)
    elapsed = anyio.current_time() - started
    state = _engine_state(runner)
    assert state.cli_baseline_children  # the pre-init service was recorded
    released = _events(logs, "claude.hook.hold_released")
    assert len(released) == 1 and released[0]["reason"] == "no_hook_process"
    assert sorted(released[0]["hook_ids"]) == ["h-stop-plain", "h-ups"]
    closed = _events(logs, "claude.live_session.stdin_closed")
    assert len(closed) == 1 and closed[0]["reason"] == "idle_no_tasks"
    assert elapsed < 5.0
    assert _events(logs, "claude.hook.hold_expired") == []
    assert _events(logs, "claude.live_session.async_hook_killed") == []
    assert _events(logs, "claude.live_session.hook_process_at_close") == []
    assert _events(logs, "claude.live_session.close_grace_expired") == []


async def test_812_close_with_no_hook_process_names_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N = 0 at close: unpaired candidates but no live hook process (and an
    idle scan too slow to have released them) → no async_hook_killed, no
    hooks notice."""
    from structlog.testing import capture_logs

    state = ClaudeStreamState()
    state.completed_turns = 1
    state.turn_open = False
    # Too young to release, so they stay candidates at the close.
    now = time.monotonic()
    for hook_id, event in (("h-ups", "UserPromptSubmit"), ("h-stop", "Stop")):
        state.pending_hooks[hook_id] = claude_mod.PendingHook(
            hook_id=hook_id, name=event, event=event, started_at=now, turn=1
        )

    class _Pipe:
        async def aclose(self) -> None:
            pass

    async def no_shells(state: Any, pid: Any) -> list[int]:
        return []

    monkeypatch.setattr(claude_mod, "_hook_processes", no_shells)
    sid = "sess-812-no-proc"
    claude_mod._LIVE_SESSIONS[sid] = LiveSession(
        session_id=sid, state=state, stdin=_Pipe(), pid=4242
    )
    notices: list[dict] = []
    add_live_session_listener(sid, lambda kind, p: notices.append(p))
    try:
        with capture_logs() as logs:
            assert await close_live_session(sid, "idle_no_tasks") is True
        live = claude_mod._LIVE_SESSIONS[sid]
        assert live.close_hooks == [] and live.close_hook_count == 0
        assert live.close_hook_procs == 0
    finally:
        claude_mod._LIVE_SESSIONS.pop(sid, None)
    assert _events(logs, "claude.live_session.async_hook_killed") == []
    assert notices == []


async def test_812_cancel_does_not_report_finished_plain_async_hooks_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/cancel before the lifecycle's next scan: plain async hooks whose
    process already exited are not "killed" — the close re-checks the
    process table itself (their withheld responses land at teardown)."""
    from structlog.testing import capture_logs

    _settings(monkeypatch)
    # The lifecycle never polls in time: only the close-time check can tell.
    monkeypatch.setitem(_TIMINGS, "_live_poll_s", 30.0)

    completed = anyio.Event()
    result: list[Any] = []

    async def note_result(evt: Any) -> None:
        if isinstance(evt, CompletedEvent):
            completed.set()

    async def drive() -> None:
        result.append(
            await _run(
                "async_hook_post_result_response",
                wake_s=1.0,
                timeout=15,
                on_event=note_result,
            )
        )

    async def cancel_later() -> None:
        await completed.wait()
        await anyio.sleep(1.4)  # the rewake hook (1 s) has reported back
        assert await close_live_session(SID, "cancel") is True

    with capture_logs() as logs:
        async with anyio.create_task_group() as tg:
            tg.start_soon(drive)
            tg.start_soon(cancel_later)
    runner, _ = result[0]
    state = _engine_state(runner)
    assert state.live_close_reason == "cancel"
    assert _events(logs, "claude.live_session.async_hook_killed") == []
    assert state.pending_hooks == {} and state.deferred_hooks == {}


async def test_812_hook_hold_expiry_logs_async_hook_killed(
    monkeypatch: pytest.MonkeyPatch, quarantine: QuarantineStore
) -> None:
    """Bound variant: a hook that never reports back holds only
    ``async_hook_max_hold``; the close then says loudly what it cut short."""
    from structlog.testing import capture_logs

    _settings(monkeypatch, async_hook_max_hold=0.6)
    notices, subscribe = _hook_notices()
    with capture_logs() as logs:
        runner, _ = await _run("async_hook_no_response", on_event=subscribe)
    state = _engine_state(runner)
    assert state.live_close_reason == "idle_no_tasks"
    hold = _events(logs, "claude.hook.pending_hold")
    expired = _events(logs, "claude.hook.hold_expired")
    killed = _events(logs, "claude.live_session.async_hook_killed")
    assert len(hold) == 1 and len(expired) == 1 and len(killed) == 1
    assert expired[0]["log_level"] == "warning"
    assert expired[0]["hook_events"] == ["Stop"]
    assert expired[0]["live_hook_processes"] == 1
    assert killed[0]["log_level"] == "warning"
    assert killed[0]["hook_count"] == 1
    assert killed[0]["source"] == "stream"
    assert killed[0]["close_reason"] == "idle_no_tasks"
    assert killed[0]["hook_events"] == ["Stop"]
    assert logs.index(hold[0]) < logs.index(expired[0]) < logs.index(killed[0])
    # The user hears about it even on a routine idle close.
    assert notices and notices[0][0] == "closing"
    assert notices[0][1]["hooks"] == ["Stop"]
    assert notices[0][1]["hook_count"] == 1
    assert notices[0][1]["reason"] == "idle_no_tasks"
    # The CLI's own asyncRewake wait fits inside the stretched grace, so it
    # exits by itself: no signal, no quarantine.
    assert _events(logs, "claude.live_session.close_grace_expired") == []
    assert runner.current_stream.proc_returncode == 0
    assert not quarantine.is_quarantined("claude", SID)


async def test_812_hooks_child_extends_grace_to_35s(
    monkeypatch: pytest.MonkeyPatch, quarantine: QuarantineStore
) -> None:
    """Fallback (no hook frames — flag off or CLI too old): a hook script
    found under the CLI at close stretches the grace to the hooks value."""
    from structlog.testing import capture_logs

    assert ClaudeRunner()._live_close_grace_hooks_s == 35.0  # CLI 30 s + 5 s
    _settings(monkeypatch)
    scanned: list[list[int]] = []

    def fake_scan(child_pids: Any) -> list[str]:
        scanned.append(list(child_pids))
        return ["security_reminder_hook.py"]

    monkeypatch.setattr(claude_mod, "_scan_hook_children", fake_scan)
    notices, subscribe = _hook_notices()
    with capture_logs() as logs:
        runner, _ = await _run("ignore_eof", on_event=subscribe)
    assert scanned
    killed = _events(logs, "claude.live_session.async_hook_killed")
    assert len(killed) == 1
    assert killed[0]["source"] == "proc"
    assert killed[0]["hook_names"] == ["security_reminder_hook.py"]
    assert killed[0]["log_level"] == "warning"
    expired = _events(logs, "claude.live_session.close_grace_expired")
    assert len(expired) == 1
    assert expired[0]["grace_s"] == runner._live_close_grace_hooks_s == 1.6
    assert notices and notices[0][1]["hooks"] == ["security_reminder_hook.py"]


async def test_812_no_hook_evidence_keeps_the_plain_grace(
    monkeypatch: pytest.MonkeyPatch, quarantine: QuarantineStore
) -> None:
    from structlog.testing import capture_logs

    _settings(monkeypatch)
    monkeypatch.setattr(claude_mod, "_scan_hook_children", lambda pids: [])
    with capture_logs() as logs:
        runner, _ = await _run("ignore_eof")
    expired = _events(logs, "claude.live_session.close_grace_expired")
    assert expired[0]["grace_s"] == runner._live_close_grace_s == 0.8
    assert _events(logs, "claude.live_session.async_hook_killed") == []


async def test_812_kill_switch_closes_at_idle_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``hold_for_async_hooks = false`` restores today's behaviour: the idle
    close lands on the plain grace and the pending rewake is lost (P5-B) —
    but now it is logged."""
    from structlog.testing import capture_logs

    _settings(monkeypatch, hold_for_async_hooks=False)
    started = anyio.current_time()
    with capture_logs() as logs:
        runner, events = await _run("async_hook_success", wake_s=5)
    assert anyio.current_time() - started < 4
    state = _engine_state(runner)
    assert state.live_close_reason == "idle_no_tasks"
    assert _events(logs, "claude.hook.pending_hold") == []
    assert "h-stop" in state.pending_hooks  # never answered (dropped by the CLI)
    killed = _events(logs, "claude.live_session.async_hook_killed")
    assert len(killed) == 1 and killed[0]["source"] == "stream"
    assert not any(isinstance(e, TurnEvent) for e in events)


async def test_812_plain_async_hook_cancelled_on_close_logs_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from structlog.testing import capture_logs

    _settings(monkeypatch, hold_for_async_hooks=False)
    with capture_logs() as logs:
        runner, _ = await _run("plain_async_cancelled_on_eof")
    cancelled = _events(logs, "claude.hook.cancelled")
    assert len(cancelled) == 1
    assert cancelled[0]["log_level"] == "info"
    assert cancelled[0]["hook_event"] == "PostToolUse"
    assert _engine_state(runner).pending_hooks == {}


async def test_812_user_close_over_pending_hook_logs_info_without_hook_notice() -> None:
    """/cancel, /new, drain: cutting a hook short is expected — INFO, and no
    extra hooks notice (the user-initiated close has its own)."""
    from structlog.testing import capture_logs

    class _Pipe:
        async def aclose(self) -> None:
            pass

    state = ClaudeStreamState()
    state.completed_turns = 1
    state.turn_open = False
    state.pending_hooks["h1"] = claude_mod.PendingHook(
        hook_id="h1", name="Stop", event="Stop", started_at=0.0, turn=1
    )
    sid = "sess-812-user-close"
    claude_mod._LIVE_SESSIONS[sid] = LiveSession(
        session_id=sid, state=state, stdin=_Pipe()
    )
    notices: list[dict] = []
    add_live_session_listener(sid, lambda kind, p: notices.append(p))
    try:
        with capture_logs() as logs:
            assert await close_live_session(sid, "cancel") is True
    finally:
        claude_mod._LIVE_SESSIONS.pop(sid, None)
    killed = _events(logs, "claude.live_session.async_hook_killed")
    assert killed and killed[0]["log_level"] == "info"
    assert killed[0]["close_reason"] == "cancel"
    assert notices == []  # notice=False and a user close: nothing extra


# ── #829: activity-based background hold, honest close ────────────────────


class _CloseClock:
    """Wall-clock times of the run's first result and of each live-session
    notice, keyed by kind (``closing`` / ``closed``)."""

    def __init__(self) -> None:
        self.result_at: float | None = None
        self.notices: list[tuple[str, dict, float]] = []

    async def on_event(self, evt: Any) -> None:
        if isinstance(evt, CompletedEvent) and self.result_at is None:
            self.result_at = time.time()
            add_live_session_listener(
                SID, lambda kind, p: self.notices.append((kind, p, time.time()))
            )

    def first(self, kind: str) -> tuple[dict, float]:
        payload, at = next((p, t) for k, p, t in self.notices if k == kind)
        return payload, at

    def kinds(self) -> list[str]:
        return [k for k, _, _ in self.notices]


def _progress_env(**values: Any) -> None:
    for key, value in values.items():
        os.environ[f"FAKE_CLAUDE_{key.upper()}"] = str(value)


async def test_829_progress_keeps_the_session_open(
    monkeypatch: pytest.MonkeyPatch, quarantine: QuarantineStore
) -> None:
    """F on rc14: an agent reporting progress every 0.1 s for 2 s outlives a
    0.5 s hold; its wake turn is delivered and nothing closes at max_hold."""
    from structlog.testing import capture_logs

    _settings(monkeypatch, post_result_bg_max_hold=0.5)
    _progress_env(progress_s=0.1, progress_for_s=2.0)
    with capture_logs() as logs:
        runner, events = await _run("bg_agent_progressing")
    finals = [e for e in events if isinstance(e, TurnEvent) and e.phase == "completed"]
    assert [f.answer for f in finals] == ["GOT: AGENT-DONE"]
    assert _engine_state(runner).live_close_reason == "idle_no_tasks"
    rearmed = _events(logs, "claude.live_session.hold_rearmed")
    assert rearmed and {e["source"] for e in rearmed} == {"task_progress"}
    assert all(e["task_id"] == "a1" for e in rearmed)
    closes = _events(logs, "claude.live_session.stdin_closed")
    assert [c["reason"] for c in closes] == ["idle_no_tasks"]
    assert not quarantine.is_quarantined("claude", SID)


async def test_829_silent_agent_still_closes_at_max_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, post_result_bg_max_hold=0.5)
    clock = _CloseClock()
    runner, _ = await _run("bg_agent_silent", on_event=clock.on_event)
    assert _engine_state(runner).live_close_reason == "max_hold"
    payload, closed_at = clock.first("closing")
    assert payload["tasks"] == ["bg a1"]
    assert payload["max_hold_s"] == 0.5 and payload["rearm_on_progress"] is True
    assert clock.result_at is not None
    assert 0.5 <= closed_at - clock.result_at < 1.5


async def test_829_close_is_timed_from_the_last_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """F on rc14: progress for 1 s, then silence — the 0.5 s hold counts from
    the last frame, not from the result."""
    from structlog.testing import capture_logs

    marker = tmp_path / "last_progress"
    _settings(monkeypatch, post_result_bg_max_hold=0.5)
    _progress_env(
        progress_s=0.1, progress_for_s=1.0, progress_then="silent", marker_file=marker
    )
    clock = _CloseClock()
    with capture_logs() as logs:
        runner, _ = await _run("bg_agent_progressing", on_event=clock.on_event)
    assert _engine_state(runner).live_close_reason == "max_hold"
    _, closed_at = clock.first("closing")
    last_progress = float(marker.read_text())
    assert 0.5 <= closed_at - last_progress < 1.2
    assert clock.result_at is not None and closed_at - clock.result_at >= 1.3
    closes = _events(logs, "claude.live_session.stdin_closed")
    assert closes[0]["reason"] == "max_hold"
    assert closes[0]["last_progress_age_s"] >= 0.5


async def test_829_kill_switch_restores_turn_based_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from structlog.testing import capture_logs

    _settings(monkeypatch, post_result_bg_max_hold=0.5, bg_hold_rearm_on_progress=False)
    _progress_env(progress_s=0.1, progress_for_s=3.0)
    clock = _CloseClock()
    with capture_logs() as logs:
        runner, events = await _run("bg_agent_progressing", on_event=clock.on_event)
    state = _engine_state(runner)
    assert state.bg_hold_rearm_on_progress is False
    assert state.live_close_reason == "max_hold"
    payload, closed_at = clock.first("closing")
    assert payload["rearm_on_progress"] is False
    assert clock.result_at is not None and 0.5 <= closed_at - clock.result_at < 1.2
    assert _events(logs, "claude.live_session.hold_rearmed") == []
    assert not any(isinstance(e, TurnEvent) for e in events)


async def test_829_absolute_cap_wins_over_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, post_result_bg_max_hold=0.5, live_session_max_s=1.0)
    _progress_env(progress_s=0.1, progress_for_s=4.0)
    runner, _ = await _run("bg_agent_progressing")
    assert _engine_state(runner).live_close_reason == "abs_cap"


async def test_829_agent_in_a_long_foreground_tool_is_not_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """F on rc14 (A.2): no ``task_progress`` while the agent runs one 2 s
    tool, but its subagent-owned foreground task is live — the session holds,
    and closes a hold after the tool ends."""
    from structlog.testing import capture_logs

    marker = tmp_path / "tool_end"
    _settings(monkeypatch, post_result_bg_max_hold=0.5)
    _progress_env(tool_s=2.0, marker_file=marker)
    clock = _CloseClock()
    with capture_logs() as logs:
        runner, _ = await _run("bg_agent_long_tool", on_event=clock.on_event)
    state = _engine_state(runner)
    assert state.live_close_reason == "max_hold"
    assert state.tasks["a1"].owner_tool_use_id is None
    assert state.tasks["bfg1"].owner_tool_use_id == "toolu_ag"
    _, closed_at = clock.first("closing")
    tool_end = float(marker.read_text())
    assert clock.result_at is not None and closed_at - clock.result_at >= 2.0
    assert 0.4 <= closed_at - tool_end < 1.2
    sources = {e["source"] for e in _events(logs, "claude.live_session.hold_rearmed")}
    assert "agent_tool" in sources


async def test_829_non_holding_progress_does_not_rearm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from structlog.testing import capture_logs

    _settings(monkeypatch, post_result_bg_max_hold=0.5)
    _progress_env(progress_s=0.1)
    clock = _CloseClock()
    with capture_logs() as logs:
        runner, _ = await _run("nonholding_progress", on_event=clock.on_event)
    assert _engine_state(runner).live_close_reason == "max_hold"
    _, closed_at = clock.first("closing")
    assert clock.result_at is not None and 0.5 <= closed_at - clock.result_at < 1.2
    assert _events(logs, "claude.live_session.hold_rearmed") == []


async def test_829_printing_background_bash_rearms_once_per_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """F on rc14: ``local_bash`` has no progress frames, but its output file
    grows — checked only when the hold would expire, one re-arm per window."""
    from structlog.testing import capture_logs

    marker = tmp_path / "last_write"
    _settings(monkeypatch, post_result_bg_max_hold=0.5)
    _progress_env(
        progress_s=0.1,
        progress_for_s=2.0,
        marker_file=marker,
        output_file=tmp_path / "b1.output",
    )
    clock = _CloseClock()
    with capture_logs() as logs:
        runner, _ = await _run("bg_bash_printing", on_event=clock.on_event)
    state = _engine_state(runner)
    assert state.live_close_reason == "max_hold"
    assert state.bg_output_files == {"toolu_bg": str(tmp_path / "b1.output")}
    _, closed_at = clock.first("closing")
    assert clock.result_at is not None and closed_at - clock.result_at >= 2.0
    assert 0.4 <= closed_at - float(marker.read_text()) < 1.3
    rearmed = _events(logs, "claude.live_session.hold_rearmed")
    assert rearmed and {e["source"] for e in rearmed} == {"bash_output"}
    # At most one re-arm per 0.5 s window (less the 0.1 s write interval):
    # ~4 while it prints, plus the one on the last write before the close.
    # #959: an unchanged file no longer re-arms on later polls (see
    # test_959_unchanged_output_file_maps_to_the_same_activity_time); a slow
    # runner can still stretch the printing, so bound by its measured span.
    print_span = float(marker.read_text()) - clock.result_at
    assert 2 <= len(rearmed) <= max(6, math.ceil(print_span / 0.4) + 1), rearmed
    # Re-armed to a write newer than the previous re-arm: never older than
    # one hold (+ a poll), or the check ran on a stale mtime.
    assert all(e["activity_age_s"] < 0.7 for e in rearmed), rearmed


async def test_829_silent_background_bash_still_closes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fallback's negative guard (R15-21b): a silent command never
    re-arms the hold."""
    from structlog.testing import capture_logs

    _settings(monkeypatch, post_result_bg_max_hold=0.5)
    _progress_env(progress_for_s=0, output_file=tmp_path / "b1.output")
    clock = _CloseClock()
    with capture_logs() as logs:
        runner, _ = await _run("bg_bash_printing", on_event=clock.on_event)
    assert _engine_state(runner).live_close_reason == "max_hold"
    _, closed_at = clock.first("closing")
    assert clock.result_at is not None and 0.5 <= closed_at - clock.result_at < 1.2
    assert _events(logs, "claude.live_session.hold_rearmed") == []


# B2 (P0 G6-G9): background agents ignore stdin EOF, so a close over one
# runs into the grace and SIGINT — which the CLI answers with rc 0 and a
# resumable transcript.


async def test_829_b2_max_hold_close_stopped_by_sigint_is_not_quarantined(
    monkeypatch: pytest.MonkeyPatch, quarantine: QuarantineStore
) -> None:
    from structlog.testing import capture_logs

    _settings(monkeypatch, post_result_bg_max_hold=0.5)
    _progress_env(eof_mode="until_sigint")
    clock = _CloseClock()
    with capture_logs() as logs:
        runner, _ = await _run("bg_agent_silent", on_event=clock.on_event)
    assert _engine_state(runner).live_close_reason == "max_hold"
    assert runner.current_stream.sigterm_sent is False
    assert runner.current_stream.proc_returncode == 0
    assert not quarantine.is_quarantined("claude", SID)
    assert _events(logs, "session.quarantined") == []
    assert len(_events(logs, "claude.live_session.close_grace_expired")) == 1
    sigint = _events(logs, "claude.live_session.exited_after_sigint")
    assert len(sigint) == 1
    assert sigint[0]["stopped_clean"] is True and sigint[0]["quarantined"] is False
    assert clock.kinds() == ["closing", "closed"]
    closed, _ = clock.first("closed")
    assert closed == {"reason": "max_hold", "quarantined": False, "tasks": ["bg a1"]}


async def test_829_b2_cancel_close_stopped_by_sigint_is_not_quarantined(
    monkeypatch: pytest.MonkeyPatch, quarantine: QuarantineStore
) -> None:
    """/cancel of an idle session over a running agent keeps the plain grace
    (it must fit /cancel's 20 s wait) and still stops cleanly."""
    from structlog.testing import capture_logs

    _settings(monkeypatch)
    _progress_env(eof_mode="until_sigint")
    notices: list[tuple[str, dict]] = []

    async def cancel(evt: Any) -> None:
        if isinstance(evt, CompletedEvent):
            add_live_session_listener(SID, lambda kind, p: notices.append((kind, p)))
            await close_live_session(SID, "cancel", notice=True)

    with capture_logs() as logs:
        runner, _ = await _run("bg_agent_silent", on_event=cancel)
    assert _engine_state(runner).live_close_reason == "cancel"
    assert not quarantine.is_quarantined("claude", SID)
    expired = _events(logs, "claude.live_session.close_grace_expired")
    assert (
        len(expired) == 1 and expired[0]["grace_s"] == _TIMINGS["_live_close_grace_s"]
    )
    sigint = _events(logs, "claude.live_session.exited_after_sigint")
    assert sigint and sigint[0]["stopped_clean"] is True
    assert [k for k, _ in notices] == ["closing", "closed"]
    assert notices[1][1]["quarantined"] is False


async def test_829_b2_nonzero_exit_on_sigint_still_quarantines(
    monkeypatch: pytest.MonkeyPatch, quarantine: QuarantineStore
) -> None:
    from structlog.testing import capture_logs

    _settings(monkeypatch, post_result_bg_max_hold=0.5)
    _progress_env(eof_mode="until_sigint", sigint_rc=1)
    clock = _CloseClock()
    with capture_logs() as logs:
        runner, _ = await _run("bg_agent_silent", on_event=clock.on_event)
    assert quarantine.is_quarantined("claude", SID)
    sigint = _events(logs, "claude.live_session.exited_after_sigint")
    assert sigint[0]["stopped_clean"] is False and sigint[0]["quarantined"] is True
    closed, _ = clock.first("closed")
    assert closed["quarantined"] is True


async def test_829_b2_abs_cap_close_still_quarantines(
    monkeypatch: pytest.MonkeyPatch, quarantine: QuarantineStore
) -> None:
    from structlog.testing import capture_logs

    _settings(monkeypatch, live_session_max_s=0.5)
    _progress_env(eof_mode="until_sigint")
    with capture_logs() as logs:
        runner, _ = await _run("bg_agent_silent")
    assert _engine_state(runner).live_close_reason == "abs_cap"
    assert quarantine.is_quarantined("claude", SID)
    sigint = _events(logs, "claude.live_session.exited_after_sigint")
    assert sigint[0]["stopped_clean"] is False and sigint[0]["quarantined"] is True


@pytest.mark.parametrize(
    ("reason", "turn_idle", "expected"),
    [
        ("max_hold", True, True),
        ("cancel", True, True),
        ("new", True, True),
        ("drain", True, True),
        ("options_changed", True, True),
        ("max_hold", False, False),  # a turn (or injected line) was open
        ("abs_cap", True, False),  # closes mid-turn too — never exempt
        ("error", True, False),  # follows a failed run
        ("idle_no_tasks", True, False),  # nothing live: #791's idle_clean covers it
    ],
)
def test_829_b2_eligibility(reason: str, turn_idle: bool, expected: bool) -> None:
    live = LiveSession(session_id="s", state=ClaudeStreamState(), stdin=None)
    live.close_reason = reason
    live.closed_turn_idle = turn_idle
    assert claude_mod._may_stop_clean(live) is expected


async def test_829_b2_sigint_deaf_cli_keeps_quarantine_on_sigterm(
    monkeypatch: pytest.MonkeyPatch, quarantine: QuarantineStore
) -> None:
    from structlog.testing import capture_logs

    _settings(monkeypatch, post_result_bg_max_hold=0.5)
    _progress_env(eof_mode="until_sigint", ignore_sigint=1)
    with capture_logs() as logs:
        runner, _ = await _run("bg_agent_silent")
    assert runner.current_stream.sigterm_sent is True
    assert quarantine.is_quarantined("claude", SID)
    teardown = _events(logs, "claude.live_session.forced_teardown")
    assert teardown and teardown[0]["quarantined"] is True


async def test_829_closed_notice_once_when_cli_exits_on_eof(
    monkeypatch: pytest.MonkeyPatch, quarantine: QuarantineStore
) -> None:
    """A background Bash stops on EOF (F3) and the CLI exits within a poll —
    the early-return path still sends exactly one ``closed``."""
    _settings(monkeypatch, post_result_bg_max_hold=0.5)
    clock = _CloseClock()
    await _run("bg_bash_wake", wake_s=30, on_event=clock.on_event)
    assert clock.kinds() == ["closing", "closed"]
    closed, _ = clock.first("closed")
    assert closed == {"reason": "max_hold", "quarantined": False, "tasks": ["bg b1"]}


async def test_829_idle_close_without_notice_still_reports_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``closed`` follows every Untether close; the bridge only speaks when
    the closing notice named tasks."""
    _settings(monkeypatch)
    clock = _CloseClock()
    await _run("followup", on_event=clock.on_event)
    assert clock.kinds() == ["closed"]
    closed, _ = clock.first("closed")
    assert closed["reason"] == "idle_no_tasks" and closed["tasks"] == []


def test_829_settings_defaults_and_round_trip() -> None:
    import pydantic

    wd = WatchdogSettings()
    assert wd.bg_hold_rearm_on_progress is True
    assert wd.post_result_bg_max_hold == 1800.0  # name, default and range kept
    off = WatchdogSettings.model_validate({"bg_hold_rearm_on_progress": False})
    assert off.bg_hold_rearm_on_progress is False
    assert WatchdogSettings.model_validate(off.model_dump()) == off
    with pytest.raises(pydantic.ValidationError):
        WatchdogSettings(post_result_bg_max_hold=7201)


# ---------------------------------------------------------------------------
# #684: a withdrawn request no longer holds the live session
# ---------------------------------------------------------------------------


async def test_684_cancelled_request_does_not_hold_live_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, quarantine: QuarantineStore
) -> None:
    """The CLI withdraws a pending Bash approval (``control_cancel_request``):
    after the result the session idle-closes within the grace instead of
    holding until the 4 h cap, and Untether writes no answer for it."""
    from structlog.testing import capture_logs

    _settings(monkeypatch)
    stdin_log = tmp_path / "stdin.log"
    os.environ["FAKE_CLAUDE_STDIN_LOG"] = str(stdin_log)
    with capture_logs() as logs:
        # permission_mode=default (#749): the Bash request reaches Telegram.
        runner, events = await _run("control_cancel", permission_mode="default")
    assert isinstance(events[-1], CompletedEvent)
    assert _engine_state(runner).live_close_reason == "idle_no_tasks"
    lines = stdin_log.read_text().split()
    assert "cancel_sent:req-cancel-1" in lines
    assert "control_response" not in lines
    (info,) = _events(logs, "control_request.cancelled_by_cli")
    assert info["request_id"] == "req-cancel-1" and info["kind"] == "tool"
    assert "req-cancel-1" not in claude_mod._REQUEST_TO_SESSION
    assert not quarantine.is_quarantined("claude", SID)


async def test_684_unanswered_request_still_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D4: detection never releases the hold — a request nobody answered
    still pauses the idle close (here until a shrunk absolute cap)."""
    _settings(monkeypatch, live_session_max_s=1.5)
    os.environ["FAKE_CLAUDE_UNANSWERED_RESULT_S"] = "0.05"
    seen: dict[str, Any] = {}

    async def probe(evt: Any) -> None:
        if isinstance(evt, CompletedEvent):
            await anyio.sleep(0.8)  # well past the 0.3 s idle grace
            state = claude_mod._SESSION_BG_STATE.get(SID)
            seen["awaiting"] = state.awaiting_user_approval() if state else None
            seen["accepting"] = is_session_accepting(SID)

    runner, _ = await _run(
        "control_unanswered", on_event=probe, permission_mode="default"
    )
    assert seen == {"awaiting": True, "accepting": True}
    assert _engine_state(runner).live_close_reason == "abs_cap"


# ---------------------------------------------------------------------------
# #820: lifecycle_exited says how the live session actually ended
# ---------------------------------------------------------------------------


def _lifecycle_exits(logs: list[dict]) -> list[dict]:
    return _events(logs, "claude.live_session.lifecycle_exited")


async def test_820_clean_idle_close_logs_exited_after_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F on rc16: the CLI exits on EOF while the lifecycle sleeps (the
    production race, made deterministic by a 0.5 s poll); the run's teardown
    cancels that sleep, which used to log ``cancelled``."""
    from structlog.testing import capture_logs

    _settings(monkeypatch)
    monkeypatch.setitem(_TIMINGS, "_live_poll_s", 0.5)
    with capture_logs() as logs:
        runner, _ = await _run("followup")
    assert _engine_state(runner).live_close_reason == "idle_no_tasks"
    (exited,) = _lifecycle_exits(logs)
    assert exited["reason"] == "exited_after_close"
    assert exited["close_reason"] == "idle_no_tasks"


@pytest.mark.parametrize("poll_s", [0.05, 0.5])
async def test_820_bash_max_hold_close_logs_exited_after_close(
    monkeypatch: pytest.MonkeyPatch, poll_s: float
) -> None:
    """Whichever side wins the race (lifecycle in its grace wait or parked in
    its poll sleep), a clean max_hold close says so."""
    from structlog.testing import capture_logs

    _settings(monkeypatch, post_result_bg_max_hold=0.5)
    monkeypatch.setitem(_TIMINGS, "_live_poll_s", poll_s)
    with capture_logs() as logs:
        runner, _ = await _run("bg_bash_wake", wake_s=30)
    assert _engine_state(runner).live_close_reason == "max_hold"
    (exited,) = _lifecycle_exits(logs)
    assert exited["reason"] == "exited_after_close"
    assert exited["close_reason"] == "max_hold"


async def test_820_forced_teardown_logs_sigterm(
    monkeypatch: pytest.MonkeyPatch, quarantine: QuarantineStore
) -> None:
    """F on rc16: a CLI deaf to EOF and SIGINT is SIGTERM'd — the stage
    marker survives the cancellation of the SIGTERM poll."""
    from structlog.testing import capture_logs

    _settings(monkeypatch, post_result_bg_max_hold=0.3)
    os.environ["FAKE_CLAUDE_IGNORE_SIGINT"] = "1"
    with capture_logs() as logs:
        runner, _ = await _run("ignore_eof_with_task")
    assert runner.current_stream.sigterm_sent is True
    assert quarantine.is_quarantined("claude", SID)  # unchanged (#791)
    (exited,) = _lifecycle_exits(logs)
    assert exited["reason"] == "sigterm"
    assert exited["close_reason"] == "max_hold"


async def test_820_sigint_exit_logs_sigint(
    monkeypatch: pytest.MonkeyPatch, quarantine: QuarantineStore
) -> None:
    from structlog.testing import capture_logs

    _settings(monkeypatch, post_result_bg_max_hold=0.5)
    _progress_env(eof_mode="until_sigint")
    with capture_logs() as logs:
        runner, _ = await _run("bg_agent_silent")
    assert runner.current_stream.sigterm_sent is False
    (exited,) = _lifecycle_exits(logs)
    assert exited["reason"] == "sigint"
    assert exited["close_reason"] == "max_hold"


async def test_820_cli_exit_without_close_logs_reader_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from structlog.testing import capture_logs

    _settings(monkeypatch)
    clock = _CloseClock()
    with capture_logs() as logs:
        await _run("exit_after_result", on_event=clock.on_event)
    (exited,) = _lifecycle_exits(logs)
    assert exited["reason"] == "reader_done"
    assert exited["close_reason"] is None
    assert clock.kinds() == []


async def test_820_cancel_while_cli_alive_logs_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancellation that lands while the CLI is still running (no close)
    keeps ``cancelled`` and sends no ``closed`` notice. Driven from an outer
    task group, never from inside the ``async for``."""
    from structlog.testing import capture_logs

    _settings(monkeypatch)
    os.environ["FAKE_CLAUDE_SCENARIO"] = "bg_bash_wake"
    os.environ["FAKE_CLAUDE_WAKE_S"] = "30"
    runner = _LiveRunner(claude_cmd=str(FAKE_CLI), permission_mode="bypassPermissions")
    for name, value in _TIMINGS.items():
        setattr(runner, name, value)
    completed = anyio.Event()
    notices: list[str] = []

    async def drive() -> None:
        async for evt in runner.run("hello", None):
            if isinstance(evt, CompletedEvent):
                add_live_session_listener(SID, lambda kind, _p: notices.append(kind))
                completed.set()

    with capture_logs() as logs, anyio.fail_after(20):
        async with anyio.create_task_group() as tg:
            tg.start_soon(drive)
            await completed.wait()
            await anyio.sleep(0.2)  # the lifecycle is polling, CLI alive
            tg.cancel_scope.cancel()
    (exited,) = _lifecycle_exits(logs)
    assert exited["reason"] == "cancelled"
    assert exited["close_reason"] is None
    assert "closed" not in notices


_STAGES = (None, "sigint", "sigterm")
_EXIT_REASONS = ("reader_done", "exited_after_close", "sigint")


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("process_gone", [False, True])
@pytest.mark.parametrize("closing", [False, True])
@pytest.mark.parametrize("stage", _STAGES)
@pytest.mark.parametrize("exit_reason", _EXIT_REASONS)
def test_820_exit_reason_table(
    cancelled: bool,
    process_gone: bool,
    closing: bool,
    stage: str | None,
    exit_reason: str,
) -> None:
    reason = claude_mod._lifecycle_exit_reason(
        exit_reason=exit_reason,
        cancelled=cancelled,
        process_gone=process_gone,
        closing=closing,
        stage=stage,
    )
    if cancelled and not process_gone:
        expected = "cancelled"
    elif cancelled or exit_reason == "reader_done":
        expected = (stage or "exited_after_close") if closing else "reader_done"
    else:
        expected = exit_reason
    assert reason == expected
    # The ``closed`` notice gate is unchanged: the old condition was
    # ``exit_reason != "cancelled" or reader_done or returncode``.
    assert (reason != "cancelled") == (not cancelled or process_gone)


# ---------------------------------------------------------------------------
# #872: a declared wait (background Bash timeout, pending wake-up) is honoured
# ---------------------------------------------------------------------------


async def test_872_declared_bash_timeout_holds_past_max_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from structlog.testing import capture_logs

    _settings(monkeypatch, post_result_bg_max_hold=0.5)
    os.environ["FAKE_CLAUDE_BASH_TIMEOUT_MS"] = "3000"
    with capture_logs() as logs:
        runner, events = await _run("bg_bash_wake", wake_s=1.5)
    finals = [e for e in events if isinstance(e, TurnEvent) and e.phase == "completed"]
    assert [f.answer for f in finals] == ["GOT: BG-FINISHED"]
    closes = _events(logs, "claude.live_session.stdin_closed")
    assert all(c["reason"] != "max_hold" for c in closes)
    (extended,) = _events(logs, "claude.live_session.hold_extended")
    assert extended["source"] == "bash_timeout"
    assert extended["task_id"] == "b1"
    assert extended["declared_s"] == 3.0
    assert 0 < extended["remaining_s"] <= 3.2
    assert _engine_state(runner).live_close_reason == "idle_no_tasks"


async def test_872_wake_turn_after_declared_wait_gets_a_fresh_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R17-01a: the declared wait ends (task.ended), the CLI's task
    notification fires an async UserPromptSubmit hook (live work) and the
    wake turn opens 0.4 s later. The quiet-time clock dated from before the
    wait, so the session was closed ``max_hold`` at once, killing the hook.
    Now the clock restarts when the declared wait ends."""
    from structlog.testing import capture_logs

    _settings(monkeypatch, post_result_bg_max_hold=0.3)
    os.environ["FAKE_CLAUDE_BASH_TIMEOUT_MS"] = "3000"
    os.environ["FAKE_CLAUDE_WAKE_HOOK_S"] = "2"
    os.environ["FAKE_CLAUDE_WAKE_DELAY_S"] = "0.25"
    with capture_logs() as logs:
        runner, events = await _run("bg_bash_wake", wake_s=1.0)
    finals = [e for e in events if isinstance(e, TurnEvent) and e.phase == "completed"]
    assert [f.answer for f in finals] == ["GOT: BG-FINISHED"]
    closes = _events(logs, "claude.live_session.stdin_closed")
    assert all(c["reason"] != "max_hold" for c in closes)
    assert _events(logs, "claude.live_session.async_hook_killed") == []
    rearmed = [
        e
        for e in _events(logs, "claude.live_session.hold_rearmed")
        if e["source"] == "declared_wait_ended"
    ]
    assert len(rearmed) == 1
    assert _engine_state(runner).live_close_reason != "max_hold"


async def test_872_fresh_hold_after_declared_wait_is_still_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The restarted window is one ``max_hold``, not unbounded: a hook that
    keeps the session busy with no wake turn closes ``max_hold`` a window
    after the declared wait ended."""
    _settings(monkeypatch, post_result_bg_max_hold=0.3)
    os.environ["FAKE_CLAUDE_BASH_TIMEOUT_MS"] = "3000"
    os.environ["FAKE_CLAUDE_WAKE_HOOK_S"] = "30"
    os.environ["FAKE_CLAUDE_WAKE_DELAY_S"] = "30"  # the turn never opens
    clock = _CloseClock()
    runner, events = await _run("bg_bash_wake", wake_s=1.0, on_event=clock.on_event)
    assert _engine_state(runner).live_close_reason == "max_hold"
    _, closed_at = clock.first("closing")
    assert clock.result_at is not None
    # task ends ~1.0 s after the result; then a fresh 0.3 s window.
    assert 1.25 <= closed_at - clock.result_at < 2.5


async def test_872_silent_task_closes_once_its_declared_wait_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI enforcement off (the task never ends): the hold closes one fresh
    quiet-time window after the declared deadline + grace (1.0 + 0.2 + 0.5)
    — not at the 0.5 s quiet-time limit."""
    _settings(monkeypatch, post_result_bg_max_hold=0.5)
    os.environ["FAKE_CLAUDE_BASH_TIMEOUT_MS"] = "1000"
    clock = _CloseClock()
    runner, _ = await _run("bg_bash_wake", wake_s=30, on_event=clock.on_event)
    assert _engine_state(runner).live_close_reason == "max_hold"
    _, closed_at = clock.first("closing")
    assert clock.result_at is not None
    assert 1.6 <= closed_at - clock.result_at < 2.4


async def test_872_pending_wakeup_beyond_hold_is_not_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The issue's ask: a ScheduleWakeup further out than the hold keeps the
    session open until it fires (the fake announces "in 60s")."""
    from structlog.testing import capture_logs

    _settings(monkeypatch, post_result_bg_max_hold=0.5)
    with capture_logs() as logs:
        runner, events = await _run("scheduled_wakeup", wake_s=1.5)
    finals = [e for e in events if isinstance(e, TurnEvent) and e.phase == "completed"]
    assert [f.answer for f in finals] == ["WOKE"]
    (extended,) = _events(logs, "claude.live_session.hold_extended")
    assert extended["source"] == "scheduled_wakeup"
    assert extended["task_id"] is None
    assert 59 <= extended["declared_s"] <= 60
    closes = _events(logs, "claude.live_session.stdin_closed")
    assert all(c["reason"] != "max_hold" for c in closes)
    assert _engine_state(runner).pending_wakeup_until is None


async def test_872_no_declared_timeout_keeps_the_quiet_time_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from structlog.testing import capture_logs

    _settings(monkeypatch, post_result_bg_max_hold=0.5)
    clock = _CloseClock()
    with capture_logs() as logs:
        runner, _ = await _run("bg_bash_wake", wake_s=30, on_event=clock.on_event)
    assert _engine_state(runner).live_close_reason == "max_hold"
    _, closed_at = clock.first("closing")
    assert clock.result_at is not None and 0.5 <= closed_at - clock.result_at < 1.2
    assert _events(logs, "claude.live_session.hold_extended") == []


@pytest.mark.parametrize("scenario", ["bg_bash_wake", "scheduled_wakeup"])
async def test_872_kill_switch_ignores_declared_waits(
    monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    from structlog.testing import capture_logs

    _settings(monkeypatch, post_result_bg_max_hold=0.5, bg_hold_declared_waits=False)
    os.environ["FAKE_CLAUDE_BASH_TIMEOUT_MS"] = "3000"
    clock = _CloseClock()
    with capture_logs() as logs:
        runner, events = await _run(scenario, wake_s=1.5, on_event=clock.on_event)
    state = _engine_state(runner)
    assert state.bg_hold_declared_waits is False
    assert state.live_close_reason == "max_hold"
    _, closed_at = clock.first("closing")
    assert clock.result_at is not None and 0.5 <= closed_at - clock.result_at < 1.2
    assert _events(logs, "claude.live_session.hold_extended") == []
    assert not any(isinstance(e, TurnEvent) for e in events)  # no WOKE / wake


async def test_872_absolute_cap_wins_over_a_declared_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, post_result_bg_max_hold=0.3, live_session_max_s=1.0)
    os.environ["FAKE_CLAUDE_BASH_TIMEOUT_MS"] = "60000"
    runner, _ = await _run("bg_bash_wake", wake_s=30)
    assert _engine_state(runner).live_close_reason == "abs_cap"


def test_872_settings_default_and_round_trip() -> None:
    wd = WatchdogSettings()
    assert wd.bg_hold_declared_waits is True
    off = WatchdogSettings.model_validate({"bg_hold_declared_waits": False})
    assert off.bg_hold_declared_waits is False
    assert WatchdogSettings.model_validate(off.model_dump()) == off


# ---------------------------------------------------------------------------
# #876: a task the CLI moved to the background holds the live session
# ---------------------------------------------------------------------------


async def test_876_auto_backgrounded_task_holds_the_live_session(
    monkeypatch: pytest.MonkeyPatch, quarantine: QuarantineStore
) -> None:
    """F on rc16: the moved task stayed ``is_backgrounded=False``, so the
    session idle-closed after 0.3 s and the CLI killed the copy. Now it is
    held until the task finishes and its wake turn delivers."""
    from structlog.testing import capture_logs

    _settings(monkeypatch)  # 0.3 s idle grace, well under the 1.2 s task
    with capture_logs() as logs:
        runner, events = await _run("fg_task_backgrounded", wake_s=1.2)
    finals = [e for e in events if isinstance(e, TurnEvent) and e.phase == "completed"]
    assert [f.answer for f in finals] == ["COPY DONE"]
    state = _engine_state(runner)
    assert state.tasks["f1"].status == "completed"  # finished, not killed
    assert state.live_close_reason == "idle_no_tasks"
    closes = _events(logs, "claude.live_session.stdin_closed")
    ended = next(
        e
        for e in logs
        if e.get("event") == "claude.task.ended" and e.get("task_id") == "f1"
    )
    assert len(closes) == 1 and logs.index(ended) < logs.index(closes[0])
    assert not quarantine.is_quarantined("claude", SID)


# ---------------------------------------------------------------------------
# #925 (rc20): loop fires close a clean-idle session; wake cap; native fires
# ---------------------------------------------------------------------------


@pytest.fixture
def _loop_cleanup():
    from untether import loop_scheduler

    loop_scheduler.uninstall()
    yield
    loop_scheduler.uninstall()


def _install_live(session_id: str, state: ClaudeStreamState) -> LiveSession:
    """A registered live session around ``state`` (no process)."""
    live = LiveSession(session_id=session_id, state=state, stdin=_NullStdin(), pid=None)
    claude_mod._LIVE_SESSIONS[session_id] = live
    return live


class _NullStdin:
    async def send(self, _data: bytes) -> None:
        return None

    async def aclose(self) -> None:
        return None


def _idle_state() -> ClaudeStreamState:
    state = ClaudeStreamState()
    state.completed_turns = 1
    state.turn_open = False
    return state


async def test_live_session_loop_closeable_matrix() -> None:
    from untether.runners.claude import live_session_loop_closeable

    sid = "sess-closeable"
    try:
        assert live_session_loop_closeable(sid) is False  # no live session
        state = _idle_state()
        live = _install_live(sid, state)
        assert live_session_loop_closeable(sid) is True  # idle clean

        state.turn_open = True  # turn open
        assert live_session_loop_closeable(sid) is False
        state.turn_open = False

        state.live_bg_bashes.add("toolu_bg")  # background work
        state.bg_bash_deadlines["toolu_bg"] = time.monotonic() + 60
        assert live_session_loop_closeable(sid) is False
        state.live_bg_bashes.clear()
        state.bg_bash_deadlines.clear()

        state.pending_wakeup_until = time.monotonic() + 60  # pending wake-up
        assert live_session_loop_closeable(sid) is False
        state.pending_wakeup_until = None

        claude_mod._REQUEST_TO_SESSION["req-closeable"] = sid  # approval
        assert live_session_loop_closeable(sid) is False
        claude_mod._REQUEST_TO_SESSION.pop("req-closeable", None)

        state.awaiting_injected["cmd-x"] = time.monotonic()  # follow-up written
        assert live_session_loop_closeable(sid) is False
        state.awaiting_injected.clear()

        live.budget_stopped = True  # not accepting
        assert live_session_loop_closeable(sid) is False
        live.budget_stopped = False

        live.closing = True  # closing
        assert live_session_loop_closeable(sid) is False
        live.closing = False

        assert live_session_loop_closeable(sid) is True
    finally:
        claude_mod._LIVE_SESSIONS.pop(sid, None)
        claude_mod._REQUEST_TO_SESSION.pop("req-closeable", None)


async def test_live_session_loop_closeable_pending_async_hook(monkeypatch) -> None:
    from untether.runners.claude import live_session_loop_closeable

    sid = "sess-hooked"
    try:
        _install_live(sid, _idle_state())
        monkeypatch.setattr(claude_mod, "has_pending_async_hooks", lambda _s: True)
        assert live_session_loop_closeable(sid) is False
    finally:
        claude_mod._LIVE_SESSIONS.pop(sid, None)


async def test_loop_fire_close_is_stopped_clean() -> None:
    """``loop_fire`` is an Untether-initiated clean close: a SIGINT exit
    after it isn't quarantined (#829 B2)."""
    from untether.runners.claude import _STOPPED_CLEAN_REASONS

    assert "loop_fire" in _STOPPED_CLEAN_REASONS
    sid = "sess-loop-fire"
    try:
        live = _install_live(sid, _idle_state())
        assert await close_live_session(sid, "loop_fire", only_if_idle=True)
        assert live.close_reason == "loop_fire"
        assert claude_mod._may_stop_clean(live)
    finally:
        claude_mod._LIVE_SESSIONS.pop(sid, None)


@pytest.mark.usefixtures("_loop_cleanup")
async def test_wake_cap_stops_pending_wakeup_hold_at_max_iterations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§14.4 + 13b amendment 5: at ``max_iterations`` wake turns the pending
    wake-up stops holding the session; it closes at idle (``wake_cap``,
    with a notice). At most cap + 1 wake turns run."""
    from structlog.testing import capture_logs

    _settings(monkeypatch)
    real_caps = claude_mod._loop_caps
    monkeypatch.setattr(
        claude_mod, "_loop_caps", lambda: {**real_caps(), "max_iterations": 2}
    )
    notices: list[tuple[str, dict]] = []

    async def on_event(evt: Any) -> None:
        if isinstance(evt, CompletedEvent):
            add_live_session_listener(
                SID, lambda kind, payload: notices.append((kind, payload))
            )

    with capture_logs() as logs:
        runner, events = await _run("wake_chain", on_event=on_event, wake_s=1.0)
    finals = [e for e in events if isinstance(e, TurnEvent) and e.phase == "completed"]
    assert 2 <= len(finals) <= 3
    state = _engine_state(runner)
    assert state.live_close_reason == "wake_cap"
    (cap_log,) = _events(logs, "claude.live_session.wake_cap")
    assert cap_log["cap"] == 2
    assert cap_log["turns"] >= 2
    closing = [p for k, p in notices if k == "closing"]
    assert closing and closing[0]["reason"] == "wake_cap"
    assert closing[0]["wake_cap"] == 2


@pytest.mark.usefixtures("_loop_cleanup")
async def test_wake_cap_disabled_when_own_schedule_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the kill switch off a wake chain is held as before (rc19)."""
    from untether import loop_scheduler

    _settings(monkeypatch)
    monkeypatch.setattr(loop_scheduler, "own_schedule_enabled", lambda: False)
    real_caps = claude_mod._loop_caps
    monkeypatch.setattr(
        claude_mod, "_loop_caps", lambda: {**real_caps(), "max_iterations": 1}
    )

    async def stop_after_three(evt: Any) -> None:
        if (
            isinstance(evt, TurnEvent)
            and evt.phase == "completed"
            and evt.answer == "WOKE 3"
        ):
            await close_live_session(SID, "cancel")

    runner, events = await _run("wake_chain", on_event=stop_after_three, wake_s=0.4)
    finals = [e for e in events if isinstance(e, TurnEvent) and e.phase == "completed"]
    assert [f.answer for f in finals][:3] == ["WOKE 1", "WOKE 2", "WOKE 3"]
    assert _engine_state(runner).wake_cap is None


@pytest.mark.usefixtures("_loop_cleanup")
async def test_native_cron_fire_closes_session_cron_suppressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§14.3: a CLI cron fire (no ScheduleWakeup in this process) marks the
    session suppressed and closes it once the fire's turn is idle."""
    from structlog.testing import capture_logs

    from untether import loop_scheduler

    _settings(monkeypatch, post_result_limbo_grace=5.0)
    with capture_logs() as logs:
        runner, events = await _run("native_cron_fire", wake_s=0.3)
    finals = [e for e in events if isinstance(e, TurnEvent) and e.phase == "completed"]
    assert [f.answer for f in finals] == ["TICK"]
    assert _engine_state(runner).live_close_reason == "cron_suppressed"
    assert _events(logs, "claude.turn.native_cron_fire")
    marked = _events(logs, "loop.cron_suppressed_marked")
    assert marked and marked[0]["source"] == "native_fire"
    assert loop_scheduler.cron_suppressed_until(SID) is not None


@pytest.mark.usefixtures("_loop_cleanup")
@pytest.mark.parametrize(
    ("reason", "expect_expired"),
    [("wake_cap", True), ("idle_no_tasks", False)],
)
async def test_wake_cap_close_expires_pending_untether_wakeups(
    monkeypatch: pytest.MonkeyPatch, reason: str, expect_expired: bool
) -> None:
    """#925 review: a Loop-on ScheduleWakeup > 300 s also has an Untether
    wake-up entry. A ``wake_cap`` close must expire it (the notice says the
    pending wake-up was cancelled) or its timer resumes the session and the
    chain restarts at 0. Cron entries and other closes are untouched."""
    from untether import loop_scheduler

    monkeypatch.setattr(loop_scheduler, "own_schedule_enabled", lambda: True)

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    sid = "sess-wake-cap-expire"
    async with anyio.create_task_group() as tg:
        loop_scheduler.install(tg, _noop, SimpleNamespace(), 1)
        try:
            loop_scheduler.register_pending_wakeup(
                session_id=sid,
                tool_use_id="tu-wake",
                delay_seconds=900.0,
                prompt="again",
                chat_id=77,
            )
            cron = loop_scheduler.register_pending_cron(
                session_id=sid,
                tool_use_id="tu-cron",
                cron_expression="*/5 * * * *",
                prompt="check",
                recurring=True,
                chat_id=77,
            )
            _install_live(sid, _idle_state())
            assert await close_live_session(sid, reason, notice=True, only_if_idle=True)
            kinds = sorted(e.kind for e in loop_scheduler.pending_for_chat(77))
            assert kinds == (["cron"] if expect_expired else ["cron", "wakeup"])
            assert cron in {e.token for e in loop_scheduler.pending_for_chat(77)}
        finally:
            claude_mod._LIVE_SESSIONS.pop(sid, None)
            tg.cancel_scope.cancel()


# ── #1001: an idle live session killed outside Untether ───────────────────


@pytest.mark.parametrize(
    ("death", "rc", "sig", "cause"),
    [
        # The real CLI handles SIGTERM: it ends its tasks (snapshot +
        # ``task_updated{failed}``) and exits 143 (sl, 0.35.5rc20).
        ("term", 143, 15, "external_signal"),
        # SIGKILL / the OOM killer: nothing said, the tasks still "running".
        ("kill", -9, 9, "external_signal"),
        ("crash", 1, None, "crash"),
    ],
)
async def test_1001_idle_session_killed_externally_reports_died(
    monkeypatch: pytest.MonkeyPatch,
    quarantine: QuarantineStore,
    death: str,
    rc: int,
    sig: int | None,
    cause: str,
) -> None:
    from structlog.testing import capture_logs

    _settings(monkeypatch)
    os.environ["FAKE_CLAUDE_DEATH"] = death
    clock = _CloseClock()
    with capture_logs() as logs:
        runner, events = await _run("killed_while_idle", on_event=clock.on_event)
    assert isinstance(events[-1], CompletedEvent) and events[-1].ok is True
    state = _engine_state(runner)
    assert runner.current_stream.proc_returncode == rc
    assert state.live_close_reason is None
    assert state.live_exit_cause == cause
    # The tasks the exit took down read "stopped", not "failed" / "running".
    assert {t.task_id: t.status for t in state.tasks.values()} == {
        "b1": "stopped",
        "b2": "stopped",
    }
    assert all(t.ended_at is not None for t in state.tasks.values())
    assert clock.kinds() == ["died"]
    died, _ = clock.first("died")
    assert died["rc"] == rc and died["signal"] == sig
    assert died["exit_cause"] == cause
    assert died["tasks"] == ["bg b1", "bg b2"]
    assert died["task_ids"] == ["b1", "b2"]
    assert died["quarantined"] is False
    (logged,) = _events(logs, "claude.live_session.died_unexpectedly")
    assert logged["log_level"] == "warning"
    assert logged["rc"] == rc and logged["signal"] == sig
    assert logged["exit_cause"] == cause and logged["live_tasks"] == 2
    # #820's classification is unchanged: the CLI ended without a close.
    (exited,) = _lifecycle_exits(logs)
    assert exited["reason"] == "reader_done" and exited["close_reason"] is None
    assert not quarantine.is_quarantined("claude", SID)


async def test_1001_untether_close_of_the_same_session_is_not_a_death(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A max_hold close (Untether's own) keeps the #829 closing/closed pair
    and never reports ``died``."""
    from structlog.testing import capture_logs

    _settings(monkeypatch, post_result_bg_max_hold=0.5)
    clock = _CloseClock()
    with capture_logs() as logs:
        runner, _ = await _run("killed_while_idle", wake_s=30, on_event=clock.on_event)
    state = _engine_state(runner)
    assert state.live_close_reason == "max_hold"
    assert state.live_exit_cause is None
    assert clock.kinds() == ["closing", "closed"]
    assert not _events(logs, "claude.live_session.died_unexpectedly")


async def test_1001_no_death_notice_while_untether_is_shutting_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restart takes the CLIs down with it; the drain notice covers that."""
    from untether.shutdown import request_shutdown, reset_shutdown

    _settings(monkeypatch)
    clock = _CloseClock()
    request_shutdown()
    try:
        runner, _ = await _run("killed_while_idle", on_event=clock.on_event)
    finally:
        reset_shutdown()
    assert _engine_state(runner).live_exit_cause is None
    assert clock.kinds() == []


async def test_1001_clean_exit_without_tasks_is_not_a_death(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rc 0 with nothing left running is a natural end (#820), not a death."""
    _settings(monkeypatch)
    clock = _CloseClock()
    runner, _ = await _run("exit_after_result", on_event=clock.on_event)
    assert _engine_state(runner).live_exit_cause is None
    assert clock.kinds() == []


@pytest.mark.parametrize(
    ("rc", "sig", "cause"),
    [
        (143, 15, "external_signal"),
        (137, 9, "external_signal"),
        (-15, 15, "external_signal"),
        (-9, 9, "external_signal"),
        (1, None, "crash"),
        (255, None, "crash"),
        (0, None, "exited"),
        (None, None, "exited"),
    ],
)
def test_1001_exit_signal_and_cause(
    rc: int | None, sig: int | None, cause: str
) -> None:
    assert claude_mod._exit_signal(rc) == sig
    assert claude_mod._exit_cause(rc) == cause


def test_1001_signal_name() -> None:
    assert claude_mod._signal_name(15) == "SIGTERM"
    assert claude_mod._signal_name(9) == "SIGKILL"
    assert claude_mod._signal_name(None) is None
    assert claude_mod._signal_name(200) == "signal 200"


def test_1001_tasks_stopped_by_exit_window() -> None:
    """Live tasks and ones the dying CLI ended unsuccessfully inside the
    window count; a success, an older failure and a foreground task don't."""
    state = ClaudeStreamState()
    task = claude_mod.ClaudeTask
    state.tasks = {
        "live": task("live", is_backgrounded=True, status="running"),
        "dying": task("dying", is_backgrounded=True, status="failed", ended_at=105.0),
        "provisional": task(
            "provisional", is_backgrounded=True, status="ended", ended_at=104.0
        ),
        "done": task("done", is_backgrounded=True, status="completed", ended_at=105.0),
        "old_fail": task(
            "old_fail", is_backgrounded=True, status="failed", ended_at=50.0
        ),
        "fg": task("fg", is_backgrounded=False, status="running"),
    }
    stopped = claude_mod._tasks_stopped_by_exit(state, since=100.0)
    assert [t.task_id for t in stopped] == ["live", "dying", "provisional"]
