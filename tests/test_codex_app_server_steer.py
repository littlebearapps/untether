"""Codex steer into a running app-server turn, and thread status (#964, 964-2).

A steer goes into the running per-run turn with the native ``turn/steer``
(``expectedTurnId`` = the running turn, ``clientUserMessageId`` = the steer's
anchor). Codex echoes it as a ``userMessage`` item with that ``clientId``
(964-0 P-S1, codex-cli 0.171.0), which becomes the ``↪️ steer received`` row
the bridge uses to drop the anchor. Codex's "not now" errors fall back to the
queue and never end the run. A steer that was sent but never confirmed is
reported as steered (never queued twice: plan §16 item 1).

``thread/status/changed`` waiting flags (live-confirmed during an approval,
probe 1412-1) drive ``awaiting_user_approval()`` for the stall watchdogs
(#1412 item 1).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest
from structlog.testing import capture_logs

from tests import test_codex_app_server_runner as _rt
from tests.test_codex_app_server_runner import (
    OTHER_THREAD,
    THREAD,
    Fake,
    _app_state,
    _bare_runner,
    _cmd_started,
    _command_started,
    _completed,
    _run,
    _runner,
    _translate,
)
from untether.live_sessions import live_backend, steer_backend
from untether.model import ActionEvent, ResumeToken
from untether.runner import JsonlStreamState, _approval_pending
from untether.runners import codex_app_server as gate
from untether.runners.codex import build_runner
from untether.runners.codex_rpc import CodexRpcPeer
from untether.runners.run_options import apply_run_options
from untether.telegram.steer import fallback_notice

project = _rt.project
gate_calls = _rt.gate_calls
_fresh_cache = _rt._fresh_cache

RESUME = ResumeToken(engine="codex", value=THREAD)
STEER = gate.STEER_BACKEND


@pytest.fixture(autouse=True)
def _app_server_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "APP_SERVER_ACTIVE", True)
    gate._RUNS_BY_THREAD.clear()


async def _steer_mid_turn(
    runner: Any, *texts: str, uuids: tuple[str, ...] = ("u1",), stop_early: bool = False
) -> tuple[list[Any], list[Any]]:
    """Run until the command row starts, then steer each text from another
    task, as the bridge does (the run's reader must keep reading to see
    Codex's answer). Collects the run to its end, or cancels it (the /cancel
    path) once every steer has its outcome when ``stop_early``. Returns
    (events, outcomes)."""
    events: list[Any] = []
    outcomes: list[Any] = []
    consume = anyio.CancelScope()

    async def steer_all() -> None:
        for text, uuid in zip(texts, uuids, strict=True):
            outcomes.append(await STEER.steer(THREAD, text, command_uuid=uuid))
        if stop_early:
            consume.cancel()

    with apply_run_options(None), anyio.fail_after(20):
        async with anyio.create_task_group() as tg:
            with consume:
                agen = runner.run("hello", RESUME)
                try:
                    async for evt in agen:
                        events.append(evt)
                        if _command_started(evt):
                            tg.start_soon(steer_all)
                finally:
                    with anyio.CancelScope(shield=True):
                        await agen.aclose()
    return events, outcomes


def _steer_rows(events: list[Any]) -> list[ActionEvent]:
    return [
        e
        for e in events
        if isinstance(e, ActionEvent) and (e.action.detail or {}).get("steer")
    ]


@pytest.mark.anyio
async def test_steer_writes_turn_steer_with_expected_turn_id(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(
        tmp_path,
        hold_turn=True,
        notifications=[_cmd_started()],
        complete_after_steer=True,
    )
    with capture_logs() as logs:
        events, outcomes = await _steer_mid_turn(_runner(fake), "also the hostname")
    assert outcomes == ["steered"]
    assert fake.requests("turn/steer") == [
        {
            "threadId": THREAD,
            "expectedTurnId": "turn-1",
            "input": [{"type": "text", "text": "also the hostname"}],
            "clientUserMessageId": "u1",
        }
    ]
    assert [
        e["command_uuid"]
        for e in logs
        if e["event"] == "codex.app_server.steer_accepted"
    ] == ["u1"]
    done = _completed(events)
    assert done.ok and done.answer == "done steered"


@pytest.mark.anyio
async def test_steer_sends_client_user_message_id(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, hold_turn=True, notifications=[_cmd_started()])
    events, outcomes = await _steer_mid_turn(
        _runner(fake), "a", "b", uuids=("u1", "u2"), stop_early=True
    )
    assert outcomes == ["steered", "steered"]
    assert [r.action.detail["absorbed_command_uuid"] for r in _steer_rows(events)][
        ::2
    ] == ["u1", "u2"]
    assert [p["clientUserMessageId"] for p in fake.requests("turn/steer")] == [
        "u1",
        "u2",
    ]


@pytest.mark.anyio
async def test_steer_received_row_carries_absorbed_command_uuid(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(
        tmp_path,
        hold_turn=True,
        notifications=[_cmd_started()],
        complete_after_steer=True,
    )
    events, _ = await _steer_mid_turn(_runner(fake), "also   the\nhostname")
    rows = _steer_rows(events)
    assert [r.phase for r in rows] == ["started", "completed"]
    for row in rows:
        assert row.action.kind == "note"
        assert row.action.detail == {"absorbed_command_uuid": "u1", "steer": True}
        assert row.action.title == (
            "\N{RIGHTWARDS ARROW WITH HOOK}\N{VARIATION SELECTOR-16}"
            " steer received: also the hostname"
        )


def test_user_message_without_a_steer_gives_no_row() -> None:
    runner = _bare_runner()
    state = _app_state(runner)
    item = {"type": "userMessage", "id": "um", "clientId": "other", "content": []}
    params = {"threadId": THREAD, "turnId": "turn-1", "item": item}
    assert _translate(runner, state, "item/started", params) == []
    item["clientId"] = None
    assert _translate(runner, state, "item/started", params) == []


# The five "not now" refusals from codex-cli 0.171.0 (SRC turn_steer_inner;
# the first, second and compact strings were seen live in 964-0).
REFUSALS = [
    "no active turn to steer",
    "expected active turn id `turn-0` but found `turn-1`",
    "cannot steer a compact turn",
    "active turn uses a different output schema",
    "expectedTurnId must not be empty",
]


@pytest.mark.anyio
@pytest.mark.parametrize("message", REFUSALS)
async def test_steer_refusals_fall_back_to_queue(
    tmp_path: Path, project: Path, gate_calls: list[str], message: str
) -> None:
    fake = Fake(
        tmp_path, hold_turn=True, notifications=[_cmd_started()], steer_error=message
    )
    with capture_logs() as logs:
        events, outcomes = await _steer_mid_turn(_runner(fake), "x", stop_early=True)
    [outcome] = outcomes
    assert outcome == "window_closed"
    [refused] = [e for e in logs if e["event"] == "codex.app_server.steer_refused"]
    assert refused["reason"] == message and refused["command_uuid"] == "u1"
    assert not _steer_rows(events)


@pytest.mark.anyio
async def test_steer_error_never_ends_the_run(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    """A refused steer is the steer's answer, not the run's error: the turn
    goes on and completes normally afterwards."""
    fake = Fake(
        tmp_path,
        hold_turn=True,
        notifications=[_cmd_started()],
        steer_error="cannot steer a compact turn",
        complete_after_steer=True,
    )
    events, outcomes = await _steer_mid_turn(_runner(fake), "x")
    assert outcomes == ["window_closed"]
    done = _completed(events)
    assert done.ok and done.error is None
    assert "turn/steer" not in (done.error or "")


@pytest.mark.anyio
async def test_steer_response_timeout_reports_steered_and_warns(
    tmp_path: Path,
    project: Path,
    gate_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate, "STEER_RESPONSE_S", 0.2)
    fake = Fake(
        tmp_path, hold_turn=True, notifications=[_cmd_started()], steer_no_response=True
    )
    with capture_logs() as logs:
        _, [outcome] = await _steer_mid_turn(_runner(fake), "x", stop_early=True)
    assert outcome == "steered"  # sent: never queued a second time
    [warn] = [e for e in logs if e["event"] == "codex.app_server.steer_unconfirmed"]
    assert warn["log_level"] == "warning" and warn["command_uuid"] == "u1"


@pytest.mark.anyio
async def test_steer_completion_before_response_is_not_requeued(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    """§16 item 1: the turn completes before Codex answers the steer; the
    reader stops at the final, so the steer stays unconfirmed — reported as
    steered (the run-end sweep tells the user if it never ran)."""
    fake = Fake(
        tmp_path,
        hold_turn=True,
        notifications=[_cmd_started()],
        steer_complete_first=True,
        steer_no_response=True,
    )
    events, outcomes = await _steer_mid_turn(_runner(fake), "x")
    assert outcomes == ["steered"]
    assert _completed(events).ok
    assert gate._RUNS_BY_THREAD == {}


@pytest.mark.anyio
async def test_steer_accepted_then_eof_is_steered(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(
        tmp_path, hold_turn=True, notifications=[_cmd_started()], steer_then_exit=True
    )
    events, outcomes = await _steer_mid_turn(_runner(fake), "x")
    assert outcomes == ["steered"]
    assert not _completed(events).ok  # exited before turn/completed


@pytest.mark.anyio
async def test_steer_after_turn_completed_is_window_closed(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path)
    done = _completed(await _run(_runner(fake), resume=RESUME))
    assert done.ok
    assert STEER.get(THREAD) is None
    assert await STEER.steer(THREAD, "x", command_uuid="u1") == "window_closed"
    assert fake.requests("turn/steer") == []


def test_get_refuses_a_finished_or_unstarted_turn() -> None:
    runner = _bare_runner()
    state = _app_state(runner)
    state.peer = CodexRpcPeer(lambda _b: anyio.lowlevel.checkpoint())
    gate._RUNS_BY_THREAD[THREAD] = state
    assert STEER.get(THREAD) is None  # no turn yet
    state.turn_id, state.turn_open = "turn-1", True
    assert STEER.get(THREAD) is state
    state.turn_open = False  # turn/completed
    assert STEER.get(THREAD) is None
    state.turn_open = True
    state.closing = True  # the run is ending (964-3a)
    assert STEER.get(THREAD) is None
    state.closing = False
    state.peer.close("done")
    assert STEER.get(THREAD) is None


def test_exec_transport_reports_engine_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = build_runner({"transport": "app-server"}, tmp_path)
    assert gate.APP_SERVER_ACTIVE is True
    # #964-3b: the live-session backend is the steer target too.
    assert steer_backend("codex") is live_backend("codex")
    assert steer_backend("codex").supports_steer and STEER.supports_steer
    assert runner.keep_live is True
    build_runner({}, tmp_path)
    assert gate.APP_SERVER_ACTIVE is False
    assert not steer_backend("codex").supports_steer
    assert STEER.engine == "codex" and STEER.display_name == "Codex"


def test_no_live_notice_names_codex() -> None:
    text = fallback_notice("no_live", "codex")
    assert "Codex" in text and "Claude" not in text
    assert fallback_notice("no_live", "claude") == (
        "\N{RIGHTWARDS ARROW WITH HOOK}\N{VARIATION SELECTOR-16}"
        " No live Claude run to steer — queued instead."
    )


# --- thread/status/changed (#1412 item 1) ------------------------------------


def _status(status: dict, thread: str = THREAD) -> tuple[str, dict]:
    return "thread/status/changed", {"threadId": thread, "status": status}


def test_thread_status_waiting_flags_mark_approval_pending() -> None:
    """Probe 1412-1 (0.171.0): ``active[waitingOnApproval]`` arrives just
    before the approval request and ``active[]`` once it is answered."""
    runner = _bare_runner()
    state = _app_state(runner)
    assert state.awaiting_user_approval() is False
    _translate(runner, state, *_status({"type": "active", "activeFlags": []}))
    assert state.awaiting_user_approval() is False
    _translate(
        runner,
        state,
        *_status({"type": "active", "activeFlags": ["waitingOnApproval"]}),
    )
    assert state.awaiting_user_approval() is True
    _translate(runner, state, *_status({"type": "active", "activeFlags": []}))
    assert state.awaiting_user_approval() is False
    _translate(
        runner,
        state,
        *_status({"type": "active", "activeFlags": ["waitingOnUserInput"]}),
    )
    assert state.awaiting_user_approval() is True
    _translate(runner, state, *_status({"type": "idle"}))
    assert state.awaiting_user_approval() is False


def test_subagent_waiting_flag_counts() -> None:
    runner = _bare_runner()
    state = _app_state(runner)
    flags = {"type": "active", "activeFlags": ["waitingOnApproval"]}
    assert _translate(runner, state, *_status(flags, OTHER_THREAD)) == []
    assert state.awaiting_user_approval() is True
    _translate(runner, state, *_status({"type": "idle"}, OTHER_THREAD))
    assert state.awaiting_user_approval() is False


def test_system_error_status_shows_warning_once() -> None:
    runner = _bare_runner()
    state = _app_state(runner)
    first = _translate(runner, state, *_status({"type": "systemError"}))
    second = _translate(runner, state, *_status({"type": "systemError"}))
    rows = [e for e in first + second if isinstance(e, ActionEvent)]
    assert len(rows) == 1
    assert rows[0].action.kind == "warning"
    # A sub-agent's error is not this thread's.
    assert (
        _translate(runner, state, *_status({"type": "systemError"}, OTHER_THREAD)) == []
    )


def test_unknown_status_shape_is_ignored() -> None:
    runner = _bare_runner()
    state = _app_state(runner)
    assert _translate(runner, state, *_status({"type": "brandNew", "x": 1})) == []
    assert state.awaiting_user_approval() is False


def test_app_server_exposes_engine_state() -> None:
    assert gate.CodexAppServerRunner._EXPOSE_ENGINE_STATE is True


def test_approval_pending_uses_codex_engine_state() -> None:
    """The subprocess watchdog sees the flag even when the last frame label
    is not a Claude ``control_request``."""
    runner = _bare_runner()
    state = _app_state(runner)
    stream = JsonlStreamState(expected_session=RESUME)
    stream.engine_state = state
    stream.recent_events.append((0.0, "item/commandExecution/requestApproval"))
    assert _approval_pending(stream) is False
    _translate(
        runner,
        state,
        *_status({"type": "active", "activeFlags": ["waitingOnApproval"]}),
    )
    assert _approval_pending(stream) is True
