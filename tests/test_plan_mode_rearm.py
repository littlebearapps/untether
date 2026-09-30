"""#383: a plan approval must not outlive its turn in a live Claude session.

C1 — approvals are turn-scoped: every live-session turn open clears
``_PLAN_EXIT_APPROVED`` (the #283 diff-preview skip), and an unconsumed
post-outline approval (``_DISCUSS_APPROVED``) survives exactly one boundary
(``_DISCUSS_CARRY``).
"""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from untether.commands import CommandContext
from untether.model import ActionEvent, ResumeToken, TurnEvent
from untether.runners.claude import (
    _ACTIVE_RUNNERS,
    _DISCUSS_APPROVED,
    _DISCUSS_CARRY,
    _HANDLED_REQUESTS,
    _LIVE_SESSIONS,
    _OUTLINE_PENDING,
    _PLAN_EXIT_APPROVED,
    _REQUEST_TO_INPUT,
    _REQUEST_TO_SESSION,
    _REQUEST_TO_TOOL_NAME,
    _SESSION_BG_STATE,
    _SESSION_STDIN,
    ENGINE,
    ClaudeRunner,
    ClaudeStreamState,
    _absorb_injected,
    _cleanup_session_registries,
    _open_followup_turn,
    translate_claude_event,
)
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.schemas import claude as claude_schema
from untether.telegram.commands.claude_control import (
    _DISCUSS_FEEDBACK_REFS,
    ClaudeControlCommand,
)
from untether.transport import MessageRef

pytestmark = pytest.mark.anyio

SID = "sess-383"


@pytest.fixture(autouse=True)
def _clear_registries():
    def _wipe() -> None:
        _ACTIVE_RUNNERS.clear()
        _SESSION_STDIN.clear()
        _SESSION_BG_STATE.clear()
        _LIVE_SESSIONS.clear()
        _REQUEST_TO_SESSION.clear()
        _REQUEST_TO_INPUT.clear()
        _REQUEST_TO_TOOL_NAME.clear()
        _HANDLED_REQUESTS.clear()
        _PLAN_EXIT_APPROVED.clear()
        _DISCUSS_APPROVED.clear()
        _DISCUSS_CARRY.clear()
        _OUTLINE_PENDING.clear()
        _DISCUSS_FEEDBACK_REFS.clear()

    _wipe()
    yield
    _wipe()


def _live_session(session_id: str = SID) -> tuple[ClaudeStreamState, AsyncMock]:
    """A registered live run in a plan chat, reachable by session id."""
    state = ClaudeStreamState()
    state.factory.started(ResumeToken(engine=ENGINE, value=session_id), title="claude")
    state.live_mode = True
    state.configured_plan_mode = True
    stdin = AsyncMock()
    _ACTIVE_RUNNERS[session_id] = (ClaudeRunner(claude_cmd="claude"), 0.0)
    _SESSION_STDIN[session_id] = stdin
    _SESSION_BG_STATE[session_id] = state
    return state, stdin


def _feed(state: ClaudeStreamState, payload: dict[str, Any]) -> list[Any]:
    session_id = state.factory.resume.value if state.factory.resume else SID
    event = claude_schema.decode_stream_json_line(
        json.dumps({"uuid": "u", "session_id": session_id, **payload}).encode()
    )
    return translate_claude_event(
        event, title="claude", state=state, factory=state.factory
    )


def _control(state: ClaudeStreamState, request_id: str, tool: str) -> list[Any]:
    return _feed(
        state,
        {
            "type": "control_request",
            "request_id": request_id,
            "request": {
                "subtype": "can_use_tool",
                "tool_name": tool,
                "input": {"file_path": "/tmp/x", "old_string": "a", "new_string": "b"}
                if tool == "Edit"
                else {},
            },
        },
    )


def _result(state: ClaudeStreamState, **extra: Any) -> list[Any]:
    return _feed(
        state,
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 100,
            "duration_api_ms": 50,
            "num_turns": 1,
            "result": "done",
            **extra,
        },
    )


def _open_turn(state: ClaudeStreamState) -> list[Any]:
    """A live-session init after a result opens the next turn."""
    events = _feed(state, {"type": "system", "subtype": "init", "model": "claude"})
    assert any(isinstance(e, TurnEvent) and e.phase == "started" for e in events), (
        events
    )
    return events


async def _tap(action: str, request_id: str) -> Any:
    executor = AsyncMock()
    executor.send = AsyncMock(return_value=MessageRef(channel_id=1, message_id=99))
    ctx = CommandContext(
        command="claude_control",
        text=f"claude_control:{action}:{request_id}",
        args_text=f"{action}:{request_id}",
        args=(f"{action}:{request_id}",),
        message=MessageRef(channel_id=1, message_id=1),
        reply_to=None,
        reply_text=None,
        config_path=None,
        plugin_config=None,  # type: ignore[arg-type]
        runtime=None,  # type: ignore[arg-type]
        executor=executor,
    )
    return await ClaudeControlCommand().handle(ctx)


def _gated(events: list[Any]) -> bool:
    """An approval keyboard was raised (not auto-approved)."""
    return any(
        isinstance(e, ActionEvent) and "inline_keyboard" in (e.action.detail or {})
        for e in events
    )


# ── C1: flag scoping ────────────────────────────────────────────────────────


def _hint_followup(state: ClaudeStreamState) -> None:
    state.pending_command_uuid = "cmd-1"
    state.injected_commands["cmd-1"] = time.monotonic()


def _hint_task(state: ClaudeStreamState) -> None:
    state.turn_notifications = ["bg job"]


def _hint_wakeup(state: ClaudeStreamState) -> None:
    state.pending_command_uuid = "cmd-wake"


def _hint_hook(state: ClaudeStreamState) -> None:
    state.hook_rewake_hint = ("sg-review", "Stop", time.monotonic())


def _hint_none(state: ClaudeStreamState) -> None:
    return None


@pytest.mark.parametrize(
    ("hint", "reason"),
    [
        (_hint_followup, "followup"),
        (_hint_task, "task_finished"),
        (_hint_wakeup, "scheduled_wakeup"),
        (_hint_hook, "hook_rewake"),
        (_hint_none, "unknown"),
    ],
)
def test_turn_open_clears_plan_exit_approved(hint: Any, reason: str) -> None:
    state, _ = _live_session()
    state.completed_turns = 1
    state.turn_open = False
    _PLAN_EXIT_APPROVED.add(SID)
    hint(state)
    with capture_logs() as logs:
        evt = _open_followup_turn(state, state.factory)
    assert evt.reason == reason
    assert SID not in _PLAN_EXIT_APPROVED
    cleared = [e for e in logs if e["event"] == "claude.plan_approval.cleared"]
    assert len(cleared) == 1
    assert cleared[0]["reason"] == "turn_boundary"
    assert cleared[0]["cleared"] == ["plan_exit_approved"]
    assert cleared[0]["turn_reason"] == reason


def test_turn_open_without_approval_logs_nothing() -> None:
    state, _ = _live_session()
    state.completed_turns = 1
    state.turn_open = False
    with capture_logs() as logs:
        _open_followup_turn(state, state.factory)
    assert not [e for e in logs if e["event"].startswith("claude.plan_approval")]


def test_turn_open_leaves_other_sessions_alone() -> None:
    state, _ = _live_session()
    other, _ = _live_session("sess-other")
    _PLAN_EXIT_APPROVED.update({SID, "sess-other"})
    _DISCUSS_APPROVED.add("sess-other")
    state.completed_turns = 1
    state.turn_open = False
    _open_followup_turn(state, state.factory)
    assert SID not in _PLAN_EXIT_APPROVED
    assert "sess-other" in _PLAN_EXIT_APPROVED
    assert "sess-other" in _DISCUSS_APPROVED
    assert "sess-other" not in _DISCUSS_CARRY


def test_mid_turn_steer_fold_is_not_a_boundary() -> None:
    state, _ = _live_session()
    _PLAN_EXIT_APPROVED.add(SID)
    _DISCUSS_APPROVED.add(SID)
    _absorb_injected(state, state.factory, "cmd-steer")
    assert SID in _PLAN_EXIT_APPROVED
    assert SID in _DISCUSS_APPROVED
    assert SID not in _DISCUSS_CARRY


async def test_intra_turn_bypass_still_works() -> None:
    """#283/#369 regression: approve the plan, then an Edit in the SAME turn
    skips the diff-preview gate."""
    state, _ = _live_session()
    with apply_run_options(EngineRunOptions(diff_preview=True)):
        assert _gated(_control(state, "req-epm", "ExitPlanMode"))
        await _tap("approve", "req-epm")
        assert SID in _PLAN_EXIT_APPROVED
        events = _control(state, "req-edit-1", "Edit")
    assert events == []
    assert "req-edit-1" in state.auto_approve_queue


async def test_diff_preview_gate_returns_next_turn() -> None:
    """THE #383 regression: the next live turn must gate the Edit again."""
    state, _ = _live_session()
    with apply_run_options(EngineRunOptions(diff_preview=True)):
        _control(state, "req-epm", "ExitPlanMode")
        await _tap("approve", "req-epm")
        _result(state)
        _open_turn(state)
        assert SID not in _PLAN_EXIT_APPROVED
        events = _control(state, "req-edit-2", "Edit")
    assert _gated(events)
    assert "req-edit-2" not in state.auto_approve_queue


# ── C1: the post-outline one-boundary carry ────────────────────────────────


def test_unconsumed_discuss_approval_carries_one_boundary() -> None:
    state, _ = _live_session()
    _DISCUSS_APPROVED.add(SID)  # tapped during turn 1
    _result(state)  # turn 1 ends without ExitPlanMode
    with capture_logs() as logs:
        _open_turn(state)  # the user's "go ahead"
    assert SID in _DISCUSS_APPROVED
    assert SID in _DISCUSS_CARRY
    assert [e for e in logs if e["event"] == "claude.plan_approval.carried"]
    events = _control(state, "req-epm-2", "ExitPlanMode")
    assert events == []  # consumed via the discuss auto-approve
    assert "req-epm-2" in state.auto_approve_queue
    assert SID not in _DISCUSS_APPROVED
    assert SID not in _DISCUSS_CARRY
    assert SID in _PLAN_EXIT_APPROVED


def test_discuss_carry_expires_after_second_boundary() -> None:
    state, _ = _live_session()
    _DISCUSS_APPROVED.add(SID)
    _result(state)
    _open_turn(state)  # carried
    _result(state)  # turn 2 ends, not consumed
    with capture_logs() as logs:
        _open_turn(state)
    assert SID not in _DISCUSS_APPROVED
    assert SID not in _DISCUSS_CARRY
    cleared = [e for e in logs if e["event"] == "claude.plan_approval.cleared"]
    assert cleared and cleared[0]["cleared"] == ["discuss_approved"]
    assert _gated(_control(state, "req-epm-3", "ExitPlanMode"))


def test_consumed_discuss_approval_does_not_carry() -> None:
    state, _ = _live_session()
    _DISCUSS_APPROVED.add(SID)
    assert _control(state, "req-epm-1", "ExitPlanMode") == []  # consumed
    _result(state)
    _open_turn(state)
    assert SID not in _DISCUSS_CARRY
    assert _gated(_control(state, "req-epm-2", "ExitPlanMode"))


def test_wake_turn_uses_up_the_carry() -> None:
    """Fail-safe direction: a wake turn spends the one boundary, so the
    user's follow-up needs a fresh approval."""
    state, _ = _live_session()
    _result(state)
    _DISCUSS_APPROVED.add(SID)  # tapped while idle
    state.turn_notifications = ["bg job"]
    _open_turn(state)  # wake turn: carry used
    assert SID in _DISCUSS_CARRY
    _result(state)
    _open_turn(state)  # the follow-up
    assert SID not in _DISCUSS_APPROVED
    assert SID not in _DISCUSS_CARRY


def test_cleanup_discards_the_carry() -> None:
    _DISCUSS_APPROVED.add(SID)
    _DISCUSS_CARRY.add(SID)
    _cleanup_session_registries(SID)
    assert SID not in _DISCUSS_APPROVED
    assert SID not in _DISCUSS_CARRY
