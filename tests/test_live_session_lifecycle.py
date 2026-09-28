"""#776 phase 03: when a live Claude session's stdin closes.

Real ``ClaudeRunner`` against ``tests/fake_clis/fake_claude_live.py`` with the
lifecycle timers shrunk to fractions of a second.
"""

from __future__ import annotations

import os
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
_ENV = ("FAKE_CLAUDE_SCENARIO", "FAKE_CLAUDE_WAKE_S", "FAKE_CLAUDE_IGNORE_SIGINT")


# ClaudeRunner is a slots dataclass, so these knobs are instance fields: a
# class-attribute override on a subclass is shadowed by the field default
# (the old overrides here were silently inert — 15 s grace, 1 s poll).
# ``_run`` applies them to the instance instead (#791).
_TIMINGS = {
    "_live_poll_s": 0.05,
    "_live_close_grace_s": 0.8,
    "_live_close_sigint_grace_s": 0.8,
    "_subcountdown_sigterm_grace_s": 1.0,
    "_subcountdown_sigterm_grace_poll_s": 0.1,
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
