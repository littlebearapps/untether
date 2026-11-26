"""Codex app-server live sessions (#964, phase 964-3b).

The live-session backend (``live_sessions._CodexLive``) over the 3a turn
state machine: follow-ups as ``turn/start`` (or ``thread/compact/start``)
written only once the session is idle, the lifecycle (idle close after
``post_result_limbo_grace``, ``live_session_max_s`` deferred to the turn
boundary), close paths and their notices (never quarantined), the #1001
death notice, options / cwd changes, the handoff wait, the stuck
``pending_start`` bound, and the kill switches (``[watchdog] live_sessions =
false``, unattended runs, ``transport = "exec"``) keeping per-run behaviour.
All against the fake app-server in ``test_codex_app_server_runner``.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path
from typing import Any

import anyio
import pytest
from structlog.testing import capture_logs

from tests import test_codex_app_server_runner as _rt
from tests.test_codex_app_server_runner import THREAD, Fake
from tests.test_codex_app_server_turns import (
    RESUME,
    LiveRun,
    _completed_run,
    _live,
    _live_runner,
    _turn_done,
    _turns,
)
from untether.live_followup import inject_live_followup
from untether.live_sessions import live_backend
from untether.model import CompletedEvent, TurnEvent
from untether.runners import codex_app_server as gate
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.scheduler import ThreadJob

project = _rt.project
gate_calls = _rt.gate_calls
_fresh_cache = _rt._fresh_cache

BACKEND = live_backend("codex")


@pytest.fixture(autouse=True)
def _app_server_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(gate, "APP_SERVER_ACTIVE", True)
    gate._RUNS_BY_THREAD.clear()
    yield
    gate._RUNS_BY_THREAD.clear()


def _settings(monkeypatch: pytest.MonkeyPatch, grace: float, cap: float = 14400.0):
    monkeypatch.setattr(gate, "_live_settings", lambda: (True, grace, cap))


def _events(logs: list[dict], name: str) -> list[dict]:
    return [e for e in logs if e["event"] == name]


class Listener:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, kind: str, payload: dict[str, Any]) -> None:
        self.calls.append((kind, payload))


async def _run_until_idle(run: LiveRun, listener: Listener | None = None) -> None:
    await run.until(_completed_run)
    if listener is not None:
        assert BACKEND.add_listener(THREAD, listener)


# --- the backend's shape -------------------------------------------------------


@pytest.mark.anyio
async def test_backend_without_a_session() -> None:
    assert BACKEND.get(THREAD) is None
    assert BACKEND.is_accepting(THREAD) is False
    assert BACKEND.idle_count() == 0
    assert BACKEND.linger_info(THREAD) is None
    assert BACKEND.turn_pending(THREAD) is False
    assert BACKEND.live_bg_count(THREAD) == 0
    assert BACKEND.holds_background_jobs(THREAD) is False
    assert BACKEND.spawn_options(THREAD) is None
    assert BACKEND.spawn_cwd(THREAD) is None
    assert BACKEND.pre_inject_close_reason(THREAD) is None
    assert await BACKEND.close(THREAD, "cancel", notice=True) is False
    assert BACKEND.add_listener(THREAD, Listener()) is False
    assert await BACKEND.close_steer_window(THREAD, "cancel") is False
    assert await BACKEND.interrupt_pending_turn(THREAD, any_turn=True) is False
    assert await BACKEND.inject_when_idle(THREAD, "x", command_uuid="u") is False
    assert await BACKEND.steer(THREAD, "x", command_uuid="u") == "window_closed"
    assert await BACKEND.wait_for_handoff(THREAD, 1.0) == "free"
    assert await BACKEND.close_idle_for_continue(None) == 0


@pytest.mark.anyio
async def test_idle_session_reads_through_the_backend(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path)

    async def body(run: LiveRun) -> None:
        assert BACKEND.get(THREAD) is run.state
        assert BACKEND.is_accepting(THREAD) is True
        assert BACKEND.idle_count() == 1
        assert BACKEND.linger_info(THREAD) == (True, 0)
        assert BACKEND.turn_pending(THREAD) is False
        assert BACKEND.spawn_cwd(THREAD) == project
        assert BACKEND.spawn_options(THREAD) is None  # apply_run_options(None)

    await _live(fake, body)


# --- follow-ups -------------------------------------------------------------


@pytest.mark.anyio
async def test_followup_waits_for_turn_completed_then_turn_start(
    tmp_path: Path,
    project: Path,
    gate_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§7 exit gate: a follow-up queued while the first turn runs is written
    only once that turn completed, as a turn/start carrying only the thread,
    the text and its clientUserMessageId; its turn is a followup segment."""
    fake = Fake(tmp_path, complete_delay=0.6)
    seen: list[tuple[bool, int]] = []
    real = gate.start_followup

    async def spy(state: Any, text: str, **kw: Any) -> str:
        seen.append((state.turn_open, state.completed_turns))
        return await real(state, text, **kw)

    monkeypatch.setattr(gate, "start_followup", spy)
    run = LiveRun(_live_runner(fake))
    outcomes: list[bool] = []
    with apply_run_options(None), anyio.fail_after(30):
        async with anyio.create_task_group() as tg:
            tg.start_soon(run.consume)
            with anyio.fail_after(10):
                while THREAD not in gate._RUNS_BY_THREAD or not run.state.turn_open:
                    await anyio.sleep(0.01)
            assert BACKEND.turn_pending(THREAD) is True
            outcomes.append(
                await BACKEND.inject_when_idle(THREAD, "next", command_uuid="f1")
            )
            await run.until(_turn_done(2))
            await run.close()
    assert outcomes == [True]
    assert seen == [(False, 1)]  # never while turn 1 was open
    received = [m.get("method") for m in fake.received()]
    assert received.count("turn/start") == 2
    assert fake.requests("turn/start")[1] == {
        "threadId": THREAD,
        "input": [{"type": "text", "text": "next"}],
        "clientUserMessageId": "f1",
    }
    assert _turns(run.events) == [
        ("started", 2, "followup", "f1", None, None),
        ("completed", 2, "followup", "f1", True, "reply 2"),
    ]
    assert sum(isinstance(e, CompletedEvent) for e in run.events) == 1


@pytest.mark.anyio
async def test_inject_live_followup_logs_codex_injected(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path)
    job = ThreadJob(chat_id=1, user_msg_id=2, text="more", resume_token=RESUME)

    async def body(run: LiveRun) -> None:
        with capture_logs() as logs:
            assert await inject_live_followup(job) is True
        assert len(_events(logs, "codex.live_session.injected")) == 1
        await run.until(_turn_done(2))

    run = await _live(fake, body)
    assert _turns(run.events)[0][:3] == ("started", 2, "followup")


@pytest.mark.anyio
async def test_idle_steer_is_written_idle_followup(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path)
    outcomes: list[str] = []

    async def body(run: LiveRun) -> None:
        with capture_logs() as logs:
            outcomes.append(await BACKEND.steer(THREAD, "also", command_uuid="s1"))
        assert [e["mid_turn"] for e in _events(logs, "codex.live_session.steered")] == [
            False
        ]
        await run.until(_turn_done(2))

    run = await _live(fake, body)
    assert outcomes == ["written_idle"]
    assert _turns(run.events)[0] == ("started", 2, "followup", "s1", None, None)


@pytest.mark.anyio
async def test_compact_injected_into_live_session(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path)

    async def body(run: LiveRun) -> None:
        assert await BACKEND.inject_when_idle(THREAD, "/compact", command_uuid="c1")
        await run.until(_turn_done(2))

    run = await _live(fake, body)
    assert fake.requests("thread/compact/start") == [{"threadId": THREAD}]
    assert len(fake.requests("turn/start")) == 1
    done = [
        e for e in run.events if isinstance(e, TurnEvent) and e.phase == "completed"
    ]
    assert done[0].command_uuid == "c1" and done[0].ok is True
    assert done[0].usage["compaction"]["manual_success"] is True


@pytest.mark.anyio
async def test_refused_followup_closes_the_idle_session(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    """A refusal is returned once (the message takes the resume path) and
    the session closes, so the scheduler's retries never write it again."""
    fake = Fake(tmp_path, followup={"error": "thread is busy"})

    async def body(run: LiveRun) -> None:
        assert await BACKEND.inject_when_idle(THREAD, "x", command_uuid="f1") is False
        assert BACKEND.is_accepting(THREAD) is False
        with anyio.fail_after(10):
            await run.done.wait()

    await _live(fake, body)
    assert len(fake.requests("turn/start")) == 2


# --- the stuck pending_start (3a's flagged edge) ------------------------------


@pytest.mark.anyio
async def test_a_followup_whose_turn_never_starts_stops_counting_as_pending(
    tmp_path: Path,
    project: Path,
    gate_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Written, never answered, no turn: after ``FOLLOWUP_TURN_TIMEOUT_S`` it
    no longer holds the session busy, so the next follow-up isn't refused
    as busy forever and the idle close can run."""
    monkeypatch.setattr(gate, "FOLLOWUP_RESPONSE_S", 0.2)
    monkeypatch.setattr(gate, "FOLLOWUP_TURN_TIMEOUT_S", 0.5)
    _settings(monkeypatch, grace=0.3)
    fake = Fake(tmp_path, followup={"swallow": True})
    run = LiveRun(_live_runner(fake, live_poll_s=0.05))
    with apply_run_options(None), anyio.fail_after(30), capture_logs() as logs:
        async with anyio.create_task_group() as tg:
            tg.start_soon(run.consume)
            await _run_until_idle(run)
            state = run.state
            assert await gate.start_followup(state, "x", command_uuid="f1") == (
                "unconfirmed"
            )
            assert BACKEND.turn_pending(THREAD) is True
            assert await gate.start_followup(state, "y", command_uuid="f2") == "busy"
            with anyio.fail_after(10):
                await run.done.wait()  # expired, then idle-closed
    assert [
        e["command_uuid"]
        for e in _events(logs, "codex.live_session.followup_turn_timeout")
    ] == ["f1"]
    assert [e["reason"] for e in _events(logs, "codex.live_session.stdin_closed")] == [
        "idle_no_tasks"
    ]


def test_expire_stale_start_keeps_a_started_or_young_start() -> None:
    state = _rt._app_state(_rt._bare_runner())
    assert gate._expire_stale_start(state) is False
    wait = gate._StartWait(command_uuid="f", text="t")
    state.pending_start = wait
    assert gate._expire_stale_start(state) is True
    assert gate._expire_stale_start(state, wait.at + 1e6) is False
    assert state.pending_start is None and wait.done.is_set()
    started = gate._StartWait(command_uuid="g", text="t", started=True)
    state.pending_start = started
    assert gate._expire_stale_start(state, started.at + 1e6) is True


# --- lifecycle ----------------------------------------------------------------


@pytest.mark.anyio
async def test_idle_close_after_grace(
    tmp_path: Path,
    project: Path,
    gate_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, grace=0.3)
    fake = Fake(tmp_path)
    run = LiveRun(_live_runner(fake, live_poll_s=0.05))
    listener = Listener()
    with apply_run_options(None), anyio.fail_after(30), capture_logs() as logs:
        async with anyio.create_task_group() as tg:
            tg.start_soon(run.consume)
            await _run_until_idle(run, listener)
            with anyio.fail_after(10):
                await run.done.wait()
    assert [e["reason"] for e in _events(logs, "codex.live_session.stdin_closed")] == [
        "idle_no_tasks"
    ]
    assert listener.calls == [
        ("closed", {"reason": "idle_no_tasks", "quarantined": False, "tasks": []})
    ]
    assert gate._RUNS_BY_THREAD == {}


def _idle_state() -> Any:
    state = _rt._app_state(_rt._bare_runner())
    state.live_mode = True
    state.completed_turns = 1
    state.idle_grace_s = 0.0
    return state


@pytest.mark.anyio
async def test_pending_approval_pauses_idle_timer() -> None:
    runner = _rt._bare_runner()
    state = _idle_state()
    gate._RUNS_BY_THREAD[THREAD] = state
    state.waiting[THREAD] = ("waitingOnApproval",)
    for _ in range(3):
        assert await runner._live_tick(state) is False
    assert state.closing is False and state.idle_since is None
    state.waiting.clear()
    await runner._live_tick(state)  # starts the idle period
    await runner._live_tick(state)
    assert state.closing is True and state.live_close_reason == "idle_no_tasks"


@pytest.mark.anyio
async def test_tick_never_waits_for_a_followup_being_written() -> None:
    """The reader runs the tick and reads the follow-up's answer: it must
    not block on the lock the writer holds."""
    runner = _rt._bare_runner()
    state = _idle_state()
    gate._RUNS_BY_THREAD[THREAD] = state
    state.idle_since = 0.0
    async with state.lock:
        with anyio.fail_after(1):
            assert await runner._live_tick(state) is False
    assert state.closing is False


@pytest.mark.anyio
async def test_abs_cap_defers_to_turn_boundary() -> None:
    runner = _rt._bare_runner()
    runner.abs_cap_turn_grace_s = 1800.0
    state = _idle_state()
    state.idle_grace_s = 1e9
    state.live_max_s = 1.0
    state.spawned_at -= 5
    state.turn_open = True
    gate._RUNS_BY_THREAD[THREAD] = state
    listener = Listener()
    state.listeners.append(listener)
    with capture_logs() as logs:
        await runner._live_tick(state)
        await runner._live_tick(state)
    assert state.closing is False
    assert len(_events(logs, "codex.live_session.abs_cap_deferred")) == 1
    # The turn ends: closed at the boundary, no notice (nothing was cut).
    state.turn_open = False
    await runner._live_tick(state)
    assert state.live_close_reason == "abs_cap" and listener.calls == []


@pytest.mark.anyio
async def test_abs_cap_closes_mid_turn_after_its_grace_with_a_notice() -> None:
    runner = _rt._bare_runner()
    runner.abs_cap_turn_grace_s = 0.0
    state = _idle_state()
    state.live_max_s = 1.0
    state.spawned_at -= 5
    state.turn_open = True
    gate._RUNS_BY_THREAD[THREAD] = state
    listener = Listener()
    state.listeners.append(listener)
    await runner._live_tick(state)
    assert listener.calls == [
        ("closing", {"reason": "abs_cap", "tasks": [], "mid_turn": True, "max_s": 1.0})
    ]


@pytest.mark.anyio
async def test_a_close_the_process_ignores_ends_the_read_loop() -> None:
    runner = _rt._bare_runner()
    runner.exit_grace_s = 0.0
    state = _idle_state()
    state.closing = True
    state.closed_at = 0.0
    assert await runner._live_tick(state) is True


# --- close paths -----------------------------------------------------------------


@pytest.mark.anyio
async def test_cancel_idle_closes(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path)
    listener = Listener()
    run = LiveRun(_live_runner(fake))
    with apply_run_options(None), anyio.fail_after(30), capture_logs() as logs:
        async with anyio.create_task_group() as tg:
            tg.start_soon(run.consume)
            await _run_until_idle(run, listener)
            assert await BACKEND.close(THREAD, "cancel", notice=True) is True
            assert await BACKEND.close(THREAD, "cancel", notice=True) is False
            with anyio.fail_after(10):
                await run.done.wait()
    assert [e["reason"] for e in _events(logs, "codex.live_session.stdin_closed")] == [
        "cancel"
    ]
    assert listener.calls == [
        ("closing", {"reason": "cancel", "tasks": []}),
        ("closed", {"reason": "cancel", "quarantined": False, "tasks": []}),
    ]
    assert gate._RUNS_BY_THREAD == {}


@pytest.mark.anyio
async def test_close_never_quarantines_and_only_if_idle_refuses_mid_turn(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, followup={"hold": True})

    async def body(run: LiveRun) -> None:
        assert await gate.start_followup(run.state, "x", command_uuid="f1") == "started"
        await run.until(lambda e: isinstance(e, TurnEvent) and e.phase == "started")
        assert (
            await BACKEND.close(THREAD, "options_changed", only_if_idle=True) is False
        )
        assert BACKEND.is_accepting(THREAD) is True

    await _live(fake, body)


@pytest.mark.anyio
async def test_cancel_mid_turn_interrupts_then_closes(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    """D4: the bridge interrupts the open turn natively, then closes."""
    fake = Fake(tmp_path, followup={"hold": True})

    async def body(run: LiveRun) -> None:
        await gate.start_followup(run.state, "x", command_uuid="f1")
        await run.until(lambda e: isinstance(e, TurnEvent) and e.phase == "started")
        # Nothing pending and no time limit: the caller's stop path runs.
        assert await BACKEND.interrupt_pending_turn(THREAD) is False
        assert await BACKEND.close_steer_window(THREAD, "cancel") is True
        assert BACKEND.get(THREAD).steer_closed is True
        assert await BACKEND.steer(THREAD, "late", command_uuid="s") == "window_closed"
        assert await BACKEND.interrupt_pending_turn(THREAD, any_turn=True) is True
        assert BACKEND.idle_count() == 1
        assert await BACKEND.close(THREAD, "cancel", notice=True) is True

    run = await _live(fake, body)
    assert fake.requests("turn/interrupt") == [{"threadId": THREAD, "turnId": "turn-2"}]
    done = [
        e for e in run.events if isinstance(e, TurnEvent) and e.phase == "completed"
    ]
    assert [(e.turn, e.ok) for e in done] == [(2, False)]
    assert sum(isinstance(e, CompletedEvent) for e in run.events) == 1


@pytest.mark.anyio
async def test_options_change_closes_and_resumes(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path)
    job = ThreadJob(chat_id=1, user_msg_id=2, text="more", resume_token=RESUME)
    other = EngineRunOptions(model="gpt-other")

    async def options_for(_job: ThreadJob) -> Any:
        return other

    async def body(run: LiveRun) -> None:
        # Steer while idle: refused, the queue path decides.
        assert (
            await BACKEND.steer(THREAD, "x", command_uuid="s", run_options=other)
            == "options_changed"
        )
        with capture_logs() as logs:
            assert await inject_live_followup(job, options_for=options_for) is False
        assert len(_events(logs, "codex.live_session.options_changed")) == 1
        with anyio.fail_after(10):
            await run.done.wait()

    await _live(fake, body)
    assert len(fake.requests("turn/start")) == 1  # nothing written into it
    assert gate._RUNS_BY_THREAD == {}  # the resume finds the thread free
    assert await BACKEND.wait_for_handoff(THREAD, 1.0) == "free"


@pytest.mark.anyio
async def test_cwd_change_closes(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path)
    job = ThreadJob(chat_id=1, user_msg_id=2, text="more", resume_token=RESUME)

    async def cwd_for(_job: ThreadJob) -> Path:
        return tmp_path

    async def body(run: LiveRun) -> None:
        assert (
            await BACKEND.steer(THREAD, "x", command_uuid="s", cwd=tmp_path)
            == "options_changed"
        )
        assert await inject_live_followup(job, cwd_for=cwd_for) is False
        with anyio.fail_after(10):
            await run.done.wait()

    await _live(fake, body)
    assert len(fake.requests("turn/start")) == 1


@pytest.mark.anyio
async def test_handoff_waits_for_old_process(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path)
    outcomes: list[str] = []

    async def body(run: LiveRun) -> None:
        outcomes.append(await BACKEND.wait_for_handoff(THREAD, 0.3))
        await gate.close_live_session(THREAD, "options_changed")
        outcomes.append(await BACKEND.wait_for_handoff(THREAD, 10))

    await _live(fake, body)
    assert outcomes == ["timed_out", "exited"]


@pytest.mark.anyio
async def test_close_idle_for_continue_closes_sessions_in_that_cwd(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path)

    async def body(run: LiveRun) -> None:
        assert await BACKEND.close_idle_for_continue(tmp_path) == 0
        assert await BACKEND.close_idle_for_continue(project) == 1
        assert run.state.live_close_reason == "continue"

    await _live(fake, body)


# --- death notice ------------------------------------------------------------------


@pytest.mark.anyio
async def test_unexpected_exit_while_idle_emits_died(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path)
    listener = Listener()
    run = LiveRun(_live_runner(fake))
    with apply_run_options(None), anyio.fail_after(30), capture_logs() as logs:
        async with anyio.create_task_group() as tg:
            tg.start_soon(run.consume)
            await _run_until_idle(run, listener)
            state = run.state
            os.kill(state.proc.pid, signal.SIGTERM)
            with anyio.fail_after(10):
                await run.done.wait()
    assert state.live_exit_cause == "external_signal"
    [(kind, payload)] = listener.calls
    assert kind == "died"
    assert payload["signal_name"] == "SIGTERM" and payload["quarantined"] is False
    assert payload["tasks"] == []
    assert len(_events(logs, "codex.live_session.died_unexpectedly")) == 1


# --- kill switches: per-run behaviour unchanged ------------------------------


async def _per_run(fake: Fake, options: EngineRunOptions | None = None) -> list[Any]:
    runner = _live_runner(fake)
    with apply_run_options(options), anyio.fail_after(20):
        return [evt async for evt in runner.run("hello", RESUME)]


@pytest.mark.anyio
async def test_live_sessions_off_keeps_per_run_close(
    tmp_path: Path,
    project: Path,
    gate_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``[watchdog] live_sessions = false`` (read from the settings)."""
    config = tmp_path / "untether.toml"
    config.write_text(
        '[transports.telegram]\nbot_token = "tok"\nchat_id = 123\n'
        "allow_any_user = true\n\n[watchdog]\nlive_sessions = false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UNTETHER_CONFIG_PATH", str(config))
    assert gate._live_settings()[0] is False
    events = await _per_run(Fake(tmp_path))
    assert isinstance(events[-1], CompletedEvent) and events[-1].ok
    assert gate._RUNS_BY_THREAD == {}


@pytest.mark.anyio
async def test_unattended_run_not_kept_live(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    events = await _per_run(
        Fake(tmp_path), EngineRunOptions(unattended_trigger="cron:nightly")
    )
    assert isinstance(events[-1], CompletedEvent) and events[-1].ok
    assert gate._RUNS_BY_THREAD == {}


def test_exec_transport_has_no_live_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from untether.live_sessions import supports_live_session
    from untether.runners.codex import CodexRunner, build_runner

    runner = build_runner({}, tmp_path)
    assert isinstance(runner, CodexRunner)
    assert not isinstance(runner, gate.CodexAppServerRunner)
    assert supports_live_session("codex") is False
    assert BACKEND.supports_steer is False


@pytest.mark.anyio
async def test_a_live_run_that_fails_before_any_turn_ends(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    """No turn ran (a refused thread/resume): nothing to keep live."""
    events = await _per_run(Fake(tmp_path, resume_error=True))
    assert isinstance(events[-1], CompletedEvent) and not events[-1].ok
    assert gate._RUNS_BY_THREAD == {}
