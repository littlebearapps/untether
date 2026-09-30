"""#776 phase 03: when a live Claude session's stdin closes.

Real ``ClaudeRunner`` against ``tests/fake_clis/fake_claude_live.py`` with the
lifecycle timers shrunk to fractions of a second.
"""

from __future__ import annotations

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
    scenario: str, *, on_event: Any = None, wake_s: float = 0.3, timeout: float = 20
) -> tuple[ClaudeRunner, list[Any]]:
    os.environ["FAKE_CLAUDE_SCENARIO"] = scenario
    os.environ["FAKE_CLAUDE_WAKE_S"] = str(wake_s)
    runner = _LiveRunner(claude_cmd=str(FAKE_CLI), permission_mode="bypassPermissions")
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
    # At most one re-arm per 0.5 s window: ~4 while it prints, plus the one
    # that lands on the last write before the close.
    assert 2 <= len(rearmed) <= 6
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
