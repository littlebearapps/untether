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
from untether.live_followup import inject_live_followup
from untether.model import ActionEvent, ResumeToken, TurnEvent
from untether.runners import claude as claude_mod
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
    LiveSession,
    _absorb_injected,
    _claim_plan_rearm,
    _cleanup_session_registries,
    _open_followup_turn,
    inject_when_idle,
    steer_into_session,
    translate_claude_event,
)
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.scheduler import ThreadJob
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


# ── C2: effective-mode tracking ────────────────────────────────────────────


def _status(state: ClaudeStreamState, mode: str | None, **extra: Any) -> list[Any]:
    payload: dict[str, Any] = {"type": "system", "subtype": "status", **extra}
    if "status" not in extra:
        payload["status"] = None
    if mode is not None:
        payload["permissionMode"] = mode
    return _feed(state, payload)


def _init(state: ClaudeStreamState, mode: str) -> list[Any]:
    return _feed(
        state,
        {
            "type": "system",
            "subtype": "init",
            "model": "claude",
            "permissionMode": mode,
        },
    )


def _ack(state: ClaudeStreamState, request_id: str, mode: str | None = "plan") -> Any:
    response: dict[str, Any] = {"subtype": "success", "request_id": request_id}
    if mode is not None:
        response["response"] = {"mode": mode}
    return _feed(state, {"type": "control_response", "response": response})


def test_status_frame_tracks_effective_mode() -> None:
    state, _ = _live_session()
    state.live_mode = False  # first turn; StartedEvent path
    _init(state, "plan")
    assert state.effective_permission_mode == "plan"
    assert state.plan_mode_observed
    with capture_logs() as logs:
        assert _status(state, "default") == []
    assert state.effective_permission_mode == "default"
    assert state.plan_exited_at is not None
    assert state.plan_exit_turn == state.turn
    changed = [e for e in logs if e["event"] == "claude.permission_mode.changed"]
    assert len(changed) == 1
    assert changed[0]["from"] == "plan"
    assert changed[0]["to"] == "default"
    assert changed[0]["source"] == "status"


def test_status_without_permission_mode_is_ignored() -> None:
    """The shared handler's other shape (#819 compaction) renders its 🗜️ row
    but changes no permission-mode state."""
    state, _ = _live_session()
    _init(state, "plan")
    events = _status(state, None, status="compacting")
    assert [e.action.title for e in events if isinstance(e, ActionEvent)] == [
        "🗜️ Compacting context…"
    ]
    assert state.effective_permission_mode == "plan"
    assert state.plan_exited_at is None


def test_live_turn_init_reports_mode() -> None:
    state, _ = _live_session()
    _init(state, "plan")
    _status(state, "default")
    _result(state)
    _init(state, "plan")  # a live follow-up turn's init
    assert state.effective_permission_mode == "plan"
    assert state.plan_exited_at is None


async def test_fallback_without_status_frames() -> None:
    """An approval alone (no status frame) stamps the plan exit."""
    state, _ = _live_session()
    _control(state, "req-epm", "ExitPlanMode")
    await _tap("approve", "req-epm")
    assert state.plan_exited_at is not None
    assert state.plan_exit_turn == 1


async def test_bash_approval_does_not_stamp_plan_exit() -> None:
    state, _ = _live_session()
    state.prompting_mode = True
    _control(state, "req-bash", "Bash")
    await _tap("approve", "req-bash")
    assert state.plan_exited_at is None


def test_plan_auto_stamp_and_discuss_path_stamp_plan_exit() -> None:
    state, _ = _live_session()
    state.auto_approve_exit_plan_mode = True
    assert _control(state, "req-pa", "ExitPlanMode") == []
    assert state.plan_exited_at is not None
    other, _ = _live_session("sess-discuss")
    _DISCUSS_APPROVED.add("sess-discuss")
    assert _control(other, "req-da", "ExitPlanMode") == []
    assert other.plan_exited_at is not None


@pytest.mark.parametrize("via", ["status", "ack"])
def test_plan_observed_clears_plan_exit_approved(via: str) -> None:
    state, _ = _live_session()
    _init(state, "plan")
    _status(state, "default")
    _PLAN_EXIT_APPROVED.add(SID)
    _DISCUSS_APPROVED.add(SID)
    with capture_logs() as logs:
        if via == "status":
            _status(state, "plan")
        else:
            state.plan_rearm_inflight = "ut_plan_rearm_sess-383_1"
            _ack(state, "ut_plan_rearm_sess-383_1")
            assert state.plan_rearm_inflight is None
    assert SID not in _PLAN_EXIT_APPROVED
    assert SID in _DISCUSS_APPROVED  # pre-exit approval: carry rule only
    assert state.plan_exited_at is None
    assert state.plan_exit_turn is None
    cleared = [e for e in logs if e["event"] == "claude.plan_approval.cleared"]
    assert cleared and cleared[0]["reason"] == "plan_rearmed"


def test_foreign_ack_ignored() -> None:
    """A #365 catalog-refresh ack is not ours: no state change."""
    state, _ = _live_session()
    _init(state, "plan")
    _status(state, "default")
    assert _ack(state, "ut_catalog_refresh_sess-383_1") == []
    assert state.effective_permission_mode == "default"
    assert state.plan_exited_at is not None


def test_non_plan_chat_never_stamps_exit() -> None:
    state, _ = _live_session()
    state.configured_plan_mode = False
    _init(state, "acceptEdits")
    _status(state, "plan")
    _status(state, "acceptEdits")
    assert state.plan_exited_at is None


def test_error_ack_marks_failed() -> None:
    state, _ = _live_session()
    state.plan_rearm_inflight = "ut_plan_rearm_sess-383_1"
    with capture_logs() as logs:
        _feed(
            state,
            {
                "type": "control_response",
                "response": {
                    "subtype": "error",
                    "request_id": "ut_plan_rearm_sess-383_1",
                    "error": "Cannot set permission mode",
                    "error_code": "invalid_mode",
                },
            },
        )
    failed = [e for e in logs if e["event"] == "claude.permission_mode.rearm_failed"]
    assert failed and failed[0]["error_code"] == "invalid_mode"
    assert failed[0]["log_level"] == "warning"
    assert state.plan_rearm_failed
    assert state.plan_rearm_inflight is None


# ── C3: the plan re-arm ──────────────────────────────────────────────────────


def _left_plan(state: ClaudeStreamState) -> None:
    """Turn 1 started in plan and an approval moved the CLI to default."""
    _init(state, "plan")
    _status(state, "default")


def _register_live(state: ClaudeStreamState, stdin: AsyncMock) -> LiveSession:
    live = LiveSession(session_id=SID, state=state, stdin=stdin)
    _LIVE_SESSIONS[SID] = live
    return live


def _lines(stdin: AsyncMock) -> list[dict[str, Any]]:
    return [json.loads(call.args[0]) for call in stdin.send.await_args_list]


def _kinds(stdin: AsyncMock) -> list[str]:
    out = []
    for line in _lines(stdin):
        if line["type"] == "control_request":
            out.append(line["request"]["subtype"])
        else:
            out.append(line["type"])
    return out


async def test_turn_close_queues_rearm_after_plan_exit() -> None:
    state, _ = _live_session()
    _init(state, "plan")
    _control(state, "req-epm", "ExitPlanMode")
    await _tap("approve", "req-epm")
    _status(state, "default")
    _result(state)
    assert state.plan_rearm_pending


@pytest.mark.parametrize(
    "result_extra",
    [
        {"is_error": True, "subtype": "error_during_execution"},
        {
            "is_error": True,
            "subtype": "error_during_execution",
            "terminal_reason": "aborted_tools",
        },
    ],
)
def test_turn_close_queues_rearm_whatever_the_outcome(
    result_extra: dict[str, Any],
) -> None:
    state, _ = _live_session()
    _left_plan(state)
    _result(state, **result_extra)  # first turn, errored / interrupted
    assert state.plan_rearm_pending
    state.plan_rearm_pending = False
    _open_turn(state)
    _result(state, **result_extra)  # a live follow-up turn, cancelled
    assert state.plan_rearm_pending


async def test_denied_plan_queues_nothing() -> None:
    state, _ = _live_session()
    _init(state, "plan")
    _control(state, "req-epm", "ExitPlanMode")
    await _tap("deny", "req-epm")
    _result(state)
    assert not state.plan_rearm_pending


@pytest.mark.parametrize(
    "mode",
    ["auto", "dontAsk", "bypassPermissions", "default", "manual", "acceptEdits", None],
)
def test_non_plan_modes_never_rearm(mode: str | None) -> None:
    state, _ = _live_session()
    state.configured_plan_mode = False
    state.prompting_mode = mode in {"default", "manual", "acceptEdits"}
    _init(state, "plan")  # Claude entered plan mode itself
    _status(state, "default")
    with capture_logs() as logs:
        _result(state)
    assert not state.plan_rearm_pending
    assert _claim_plan_rearm(state, SID, reason="followup") is None
    assert not [
        e for e in logs if e["event"].startswith("claude.permission_mode.rearm")
    ]


def test_plan_never_observed_never_rearms() -> None:
    """--dangerously-skip-permissions overrides plan: never fight it."""
    state, _ = _live_session()
    _init(state, "bypassPermissions")
    _result(state)
    assert not state.plan_mode_observed
    assert not state.plan_rearm_pending
    assert _claim_plan_rearm(state, SID, reason="followup") is None


async def test_plan_auto_rearms_on_followup_only() -> None:
    """Decision 6: plan-auto is re-armed before follow-ups, never at idle."""
    state, stdin = _live_session()
    state.auto_approve_exit_plan_mode = True
    _init(state, "plan")
    assert _control(state, "req-pa", "ExitPlanMode") == []  # the stamp
    _status(state, "default")
    _result(state)
    assert not state.plan_rearm_pending
    _register_live(state, stdin)
    assert await inject_when_idle(SID, "next", command_uuid="cmd-pa")
    assert _kinds(stdin) == ["set_permission_mode", "user"]
    # The next ExitPlanMode is still rubber-stamped.
    state.plan_rearm_inflight = None
    _open_turn(state)
    assert _control(state, "req-pa-2", "ExitPlanMode") == []
    assert "req-pa-2" in state.auto_approve_queue


def test_plan_auto_idle_rearm_when_decision_flips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_mod, "_PLAN_AUTO_REARM_AT_IDLE", True)
    state, _ = _live_session()
    state.auto_approve_exit_plan_mode = True
    _left_plan(state)
    _result(state)
    assert state.plan_rearm_pending


def test_claim_is_single_flight() -> None:
    state, _ = _live_session()
    _left_plan(state)
    _result(state)
    assert _claim_plan_rearm(state, SID, reason="idle") is not None
    assert _claim_plan_rearm(state, SID, reason="followup") is None


def test_rearm_request_shape_and_namespace() -> None:
    state, _ = _live_session()
    _left_plan(state)
    _result(state)
    payload = _claim_plan_rearm(state, SID, reason="idle")
    assert payload is not None and payload.endswith(b"\n")
    line = json.loads(payload)
    assert line == {
        "type": "control_request",
        "request_id": line["request_id"],
        "request": {"subtype": "set_permission_mode", "mode": "plan"},
    }
    import re

    assert re.fullmatch(rf"ut_plan_rearm_{SID}_\d+", line["request_id"])
    assert not line["request_id"].startswith("req_")
    assert state.plan_rearm_inflight == line["request_id"]
    assert not state.plan_rearm_pending


def test_stale_ack_ignored() -> None:
    state, _ = _live_session()
    _left_plan(state)
    state.plan_rearm_inflight = "ut_plan_rearm_sess-383_2"
    _ack(state, "ut_plan_rearm_sess-383_1")  # an older request
    assert state.plan_rearm_inflight == "ut_plan_rearm_sess-383_2"
    assert state.effective_permission_mode == "default"


async def test_inject_writes_rearm_before_user_line() -> None:
    state, stdin = _live_session()
    _left_plan(state)
    _result(state)
    state.plan_rearm_pending = False  # e.g. the idle write failed
    _register_live(state, stdin)
    with capture_logs() as logs:
        assert await inject_when_idle(SID, "next", command_uuid="cmd-1")
    assert _kinds(stdin) == ["set_permission_mode", "user"]
    sent = [e for e in logs if e["event"] == "claude.permission_mode.rearm_sent"]
    assert sent and sent[0]["reason"] == "followup"


async def test_inject_without_need_writes_only_user_line() -> None:
    state, stdin = _live_session()
    _init(state, "plan")
    _result(state)
    _register_live(state, stdin)
    assert await inject_when_idle(SID, "next", command_uuid="cmd-1")
    assert _kinds(stdin) == ["user"]


def _thread_job() -> ThreadJob:
    return ThreadJob(
        chat_id=123,
        user_msg_id=20,
        text="again",
        resume_token=ResumeToken(engine="claude", value=SID),
        progress_ref=MessageRef(channel_id=123, message_id=99),
    )


async def test_inject_refuses_after_failed_rearm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, stdin = _live_session()
    _left_plan(state)
    _result(state)
    state.plan_rearm_failed = True
    _register_live(state, stdin)
    assert not await inject_when_idle(SID, "next", command_uuid="cmd-1")
    close = AsyncMock(return_value=True)
    monkeypatch.setattr(claude_mod, "close_live_session", close)
    assert not await inject_live_followup(_thread_job())
    close.assert_awaited_once_with(
        SID, "plan_rearm_failed", notice=False, only_if_idle=True
    )
    assert _kinds(stdin) == []


async def test_idle_steer_rearms_mid_turn_steer_does_not() -> None:
    state, stdin = _live_session()
    _left_plan(state)
    _result(state)
    state.plan_rearm_pending = False
    _register_live(state, stdin)
    assert await steer_into_session(SID, "idle steer", command_uuid="c1") == (
        "written_idle"
    )
    assert _kinds(stdin) == ["set_permission_mode", "user"]
    # Mid-turn: a fold, not a boundary.
    stdin.send.reset_mock()
    state.plan_rearm_inflight = None
    _open_turn(state)
    _status(state, "default")
    assert await steer_into_session(SID, "mid steer", command_uuid="c2") == "steered"
    assert _kinds(stdin) == ["user"]


def test_rearm_only_in_live_mode() -> None:
    state, _ = _live_session()
    state.live_mode = False
    _left_plan(state)
    _result(state)
    assert not state.plan_rearm_pending
    assert _claim_plan_rearm(state, SID, reason="followup") is None


async def test_kill_switch_disables_rearm_but_not_clearing() -> None:
    state, stdin = _live_session()
    state.rearm_plan_mode = False
    _init(state, "plan")
    _control(state, "req-epm", "ExitPlanMode")
    await _tap("approve", "req-epm")
    _status(state, "default")
    _result(state)
    assert not state.plan_rearm_pending
    _register_live(state, stdin)
    stdin.send.reset_mock()
    assert await inject_when_idle(SID, "next", command_uuid="cmd-1")
    assert _kinds(stdin) == ["user"]
    _open_turn(state)
    assert SID not in _PLAN_EXIT_APPROVED


async def test_followup_after_idle_rearm_writes_nothing_extra() -> None:
    state, stdin = _live_session()
    _left_plan(state)
    _result(state)
    payload = _claim_plan_rearm(state, SID, reason="idle")
    assert payload is not None
    _ack(state, state.plan_rearm_inflight or "")
    _status(state, "plan")
    assert state.effective_permission_mode == "plan"
    _register_live(state, stdin)
    assert await inject_when_idle(SID, "next", command_uuid="cmd-1")
    assert _kinds(stdin) == ["user"]


async def test_rearm_write_failure_resets_inflight() -> None:
    state, stdin = _live_session()
    _left_plan(state)
    _result(state)
    state.plan_rearm_pending = False
    stdin.send.side_effect = [OSError("closed"), None]
    live = _register_live(state, stdin)
    with capture_logs() as logs:
        assert not await claude_mod._write_plan_rearm_if_needed(live, reason="x")
    assert state.plan_rearm_inflight is None
    assert [
        e for e in logs if e["event"] == "claude.permission_mode.rearm_write_failed"
    ]


async def test_idle_steer_after_failed_rearm_falls_back() -> None:
    """A refused re-arm must not let an idle steer run unplanned: it falls
    back to the queue path, which closes the session and resumes fresh."""
    state, stdin = _live_session()
    _left_plan(state)
    _result(state)
    state.plan_rearm_pending = False
    state.plan_rearm_failed = True
    _register_live(state, stdin)
    assert await steer_into_session(SID, "x", command_uuid="c1") == "options_changed"
    assert _kinds(stdin) == []


# ── C4: defer the re-arm while the approved plan's agents run ───────────────
#
# Probe P-3 (findings addendum, CLI 2.1.285): a running background subagent
# inherits the parent's live mode, so re-arming plan under it would switch
# the approved plan's workers back into planning. The re-arm waits for the
# agents launched in the plan-exit turn (``origin_turn``), bounded by their
# inactivity (``post_result_bg_max_hold``, plan 21 D7) and, as a ceiling,
# ``live_session_max_s``.


def _task_started(
    state: ClaudeStreamState,
    task_id: str = "a1",
    *,
    task_type: str = "local_agent",
    tool: str | None = None,
) -> Any:
    payload: dict[str, Any] = {
        "type": "system",
        "subtype": "task_started",
        "task_id": task_id,
        "tool_use_id": tool or f"toolu_{task_id}",
        "description": f"worker {task_id}",
        "task_type": task_type,
        "is_backgrounded": True,
    }
    if task_type == "local_agent":
        payload["subagent_type"] = "general-purpose"
    _feed(state, payload)
    return state.tasks[task_id]


def _task_done(state: ClaudeStreamState, task_id: str = "a1") -> None:
    _feed(
        state,
        {
            "type": "system",
            "subtype": "task_updated",
            "task_id": task_id,
            "patch": {"status": "completed"},
        },
    )


def _task_progress(state: ClaudeStreamState, task_id: str = "a1") -> None:
    _feed(
        state,
        {
            "type": "system",
            "subtype": "task_progress",
            "task_id": task_id,
            "tool_use_id": f"toolu_{task_id}",
            "description": "Running step",
            "usage": {"total_tokens": 1200, "tool_uses": 2, "duration_ms": 5},
        },
    )


def _approved_with_agent(state: ClaudeStreamState) -> Any:
    """Turn 1: plan approved (status default), then the approved work
    launches a background agent; the turn closes."""
    _left_plan(state)
    agent = _task_started(state)
    _result(state)
    return agent


def _deferred_logs(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in logs if e["event"] == "claude.permission_mode.rearm_deferred"]


def test_agent_launched_in_exit_turn_defers_rearm() -> None:
    state, _ = _live_session()
    with capture_logs() as logs:
        agent = _approved_with_agent(state)
        assert agent.origin_turn == state.plan_exit_turn
        assert state.plan_rearm_pending  # the turn close still asks
        assert _claim_plan_rearm(state, SID, reason="idle") is None
        assert _claim_plan_rearm(state, SID, reason="followup") is None
    deferred = _deferred_logs(logs)
    assert len(deferred) == 1  # once per boundary
    assert deferred[0]["reason"] == "live_agents"
    assert deferred[0]["agents"] == 1
    assert deferred[0]["plan_exit_turn"] == state.plan_exit_turn
    assert state.plan_rearm_deferred == 1
    assert state.plan_rearm_inflight is None
    # The agent finishes: the next claim goes out.
    _task_done(state)
    with capture_logs() as logs:
        payload = _claim_plan_rearm(state, SID, reason="idle")
    assert payload is not None
    ended = [
        e for e in logs if e["event"] == "claude.permission_mode.rearm_deferral_ended"
    ]
    assert ended and ended[0]["reason"] == "agents_done"
    assert state.plan_rearm_deferred == 0


def test_agent_started_before_approval_in_same_turn_counts_but_earlier_turn_does_not() -> (
    None
):
    # Same turn, launched while still planning: it will run the approved work.
    state, _ = _live_session()
    _init(state, "plan")
    _task_started(state)
    _status(state, "default")
    _result(state)
    assert _claim_plan_rearm(state, SID, reason="idle") is None
    _cleanup_session_registries(SID)

    # An agent from an EARLIER turn ran under plan mode anyway.
    state, _ = _live_session()
    _init(state, "plan")
    _task_started(state, "a0")
    _result(state)
    _open_turn(state)
    _status(state, "default")  # the plan is approved in turn 2
    _result(state)
    assert state.tasks["a0"].origin_turn != state.plan_exit_turn
    assert _claim_plan_rearm(state, SID, reason="idle") is not None


def test_deferral_does_not_chain() -> None:
    """While deferred, an unplanned follow-up turn launches a second agent;
    it carries a later ``origin_turn`` and never extends the deferral."""
    state, _ = _live_session()
    _approved_with_agent(state)
    assert _claim_plan_rearm(state, SID, reason="idle") is None
    _open_turn(state)
    _task_started(state, "a2")
    _result(state)
    assert state.tasks["a2"].origin_turn == state.turn
    assert _claim_plan_rearm(state, SID, reason="idle") is None  # a1 still runs
    _task_done(state, "a1")
    assert state.tasks["a2"].holds_session
    assert _claim_plan_rearm(state, SID, reason="idle") is not None


def test_revived_agent_in_later_turn_stops_deferring() -> None:
    state, _ = _live_session()
    agent = _approved_with_agent(state)
    _task_done(state)
    _open_turn(state)  # still unplanned: e.g. the re-arm write failed
    # #801: Claude resumes the finished agent in this later turn.
    agent.ended_at = time.monotonic() - 10.0
    _task_started(state)
    assert agent.revived_count == 1
    assert agent.origin_turn == state.turn != state.plan_exit_turn
    _result(state)
    assert _claim_plan_rearm(state, SID, reason="idle") is not None


def test_deferral_hard_bound() -> None:
    """Plan 21 D7: the deferral ends when the exit-turn agents show no
    activity for ``post_result_bg_max_hold`` — not a fixed time since the
    plan exit — with ``live_session_max_s`` as the ceiling."""
    state, _ = _live_session()
    state.bg_max_hold_s = 1800.0
    state.live_session_max_s = 14400.0
    agent = _approved_with_agent(state)
    now = time.monotonic()
    # An hour past the exit but still working: the rc15 plan's old bound
    # (now - plan_exited_at > max_hold) would have re-armed under it.
    state.plan_exited_at = now - 3600.0
    agent.last_progress_at = now - 60.0
    _task_progress(state)  # a fresh frame
    assert _claim_plan_rearm(state, SID, reason="idle") is None
    # Quiet for the whole hold: the bound lifts the deferral.
    agent.last_progress_at = now - 1801.0
    with capture_logs() as logs:
        assert _claim_plan_rearm(state, SID, reason="idle") is not None
    ended = [
        e for e in logs if e["event"] == "claude.permission_mode.rearm_deferral_ended"
    ]
    assert ended and ended[0]["reason"] == "agents_idle"


def test_deferral_ceiling_and_unbounded_hold() -> None:
    state, _ = _live_session()
    state.bg_max_hold_s = 0.0  # no inactivity bound (as the lifecycle)
    state.live_session_max_s = 14400.0
    agent = _approved_with_agent(state)
    agent.last_progress_at = time.monotonic() - 10_000.0
    assert claude_mod._rearm_deferral(state) == (1, "live_agents")
    state.plan_exited_at = time.monotonic() - 14401.0
    assert claude_mod._rearm_deferral(state) == (0, "ceiling")


def test_agent_with_live_foreground_tool_counts_as_active() -> None:
    """#829 A.2 reused: an agent inside one long foreground tool sends no
    ``task_progress`` but is working — the bound must not lift."""
    state, _ = _live_session()
    state.bg_max_hold_s = 60.0
    agent = _approved_with_agent(state)
    agent.last_progress_at = time.monotonic() - 3600.0
    _feed(
        state,
        {
            "type": "system",
            "subtype": "task_started",
            "task_id": "fg1",
            "tool_use_id": "toolu_sub_bash",
            "description": "sleep 150",
            "task_type": "local_bash",
            "is_backgrounded": False,
            "owned_by_subagent": True,
        },
    )
    state.tasks["fg1"].owner_tool_use_id = agent.tool_use_id
    assert claude_mod._rearm_deferral(state) == (1, "live_agents")


@pytest.mark.parametrize("monitor", [False, True])
def test_bg_bash_and_monitor_do_not_defer(monitor: bool) -> None:
    """Shells make no permission checks: they never hold the re-arm back."""
    state, _ = _live_session()
    _left_plan(state)
    task = _task_started(state, "b1", task_type="local_bash")
    if monitor:
        state.live_monitors[task.tool_use_id] = time.monotonic() + 600
    _result(state)
    assert _claim_plan_rearm(state, SID, reason="idle") is not None


async def test_deferred_followup_turn_carries_plan_deferred_detail() -> None:
    state, stdin = _live_session()
    _approved_with_agent(state)
    state.plan_rearm_pending = False
    _register_live(state, stdin)
    assert await inject_when_idle(SID, "quick question", command_uuid="cmd-d")
    assert _kinds(stdin) == ["user"]  # no re-arm under the running agent
    state.pending_command_uuid = "cmd-d"
    events = _open_turn(state)
    started = next(e for e in events if isinstance(e, TurnEvent))
    assert started.reason == "followup"
    assert started.detail["plan_deferred"] == {"agents": 1}
    assert "cmd-d" not in state.unplanned_commands
    _result(state)
    # The agent finishes, the re-arm goes out; the next follow-up is plain.
    _task_done(state)
    state.plan_rearm_pending = False
    stdin.send.reset_mock()
    assert await inject_when_idle(SID, "again", command_uuid="cmd-e")
    assert _kinds(stdin) == ["set_permission_mode", "user"]
    state.pending_command_uuid = "cmd-e"
    events = _open_turn(state)
    started = next(e for e in events if isinstance(e, TurnEvent))
    assert "plan_deferred" not in started.detail


def test_wake_turn_during_deferral_is_flagged() -> None:
    state, _ = _live_session()
    _approved_with_agent(state)
    assert _claim_plan_rearm(state, SID, reason="idle") is None
    _hint_task(state)  # a different background job finished
    events = _open_turn(state)
    started = next(e for e in events if isinstance(e, TurnEvent))
    assert started.reason == "task_finished"
    assert started.detail["plan_deferred"] == {"agents": 1}


async def test_plan_auto_deferral_flags_followups_not_wakes() -> None:
    """Decision 6: plan-auto wake turns are never re-armed, so they carry no
    "not re-planned" line; a deferred follow-up does."""
    state, stdin = _live_session()
    state.auto_approve_exit_plan_mode = True
    _approved_with_agent(state)
    assert not state.plan_rearm_pending
    _register_live(state, stdin)
    assert await inject_when_idle(SID, "next", command_uuid="cmd-pa")
    assert _kinds(stdin) == ["user"]
    assert state.unplanned_commands == {"cmd-pa": 1}
    _hint_task(state)
    events = _open_turn(state)
    started = next(e for e in events if isinstance(e, TurnEvent))
    assert "plan_deferred" not in started.detail


def test_agent_end_while_idle_queues_rearm_right_away() -> None:
    """The exit-turn agent ending while the session idles queues the re-arm
    for the post-line drain — ahead of the wake turn the CLI starts for it,
    not at that wake turn's close."""
    state, _ = _live_session()
    _approved_with_agent(state)
    assert _claim_plan_rearm(state, SID, reason="idle") is None
    assert not state.plan_rearm_pending
    _task_done(state)
    assert state.plan_rearm_pending
    assert state.plan_rearm_pending_reason == "agents_done"


def test_agent_end_mid_turn_waits_for_the_turn_close() -> None:
    state, _ = _live_session()
    _approved_with_agent(state)
    assert _claim_plan_rearm(state, SID, reason="idle") is None
    _open_turn(state)
    _task_done(state)
    assert not state.plan_rearm_pending  # never re-armed mid-turn
    _result(state)
    assert state.plan_rearm_pending
    assert state.plan_rearm_pending_reason == "idle"


async def test_drain_uses_the_pending_reason() -> None:
    state, stdin = _live_session()
    _approved_with_agent(state)
    assert _claim_plan_rearm(state, SID, reason="idle") is None
    _register_live(state, stdin)
    _task_done(state)
    with capture_logs() as logs:
        await ClaudeRunner(claude_cmd="claude")._drain_plan_rearm(state, stdin=stdin)
    assert _kinds(stdin) == ["set_permission_mode"]
    sent = [e for e in logs if e["event"] == "claude.permission_mode.rearm_sent"]
    assert [e["reason"] for e in sent] == ["agents_done"]
    assert state.plan_rearm_pending_reason == "idle"


async def test_lifecycle_lifts_a_deferral_the_agents_went_quiet_on() -> None:
    state, stdin = _live_session()
    state.bg_max_hold_s = 60.0
    agent = _approved_with_agent(state)
    assert _claim_plan_rearm(state, SID, reason="idle") is None
    live = _register_live(state, stdin)
    runner = ClaudeRunner(claude_cmd="claude")
    now = time.monotonic()
    await runner._lift_idle_plan_deferral(live, now=now)
    assert _kinds(stdin) == []  # still working
    agent.last_progress_at = now - 61.0
    with capture_logs() as logs:
        await runner._lift_idle_plan_deferral(live, now=now)
    assert _kinds(stdin) == ["set_permission_mode"]
    sent = [e for e in logs if e["event"] == "claude.permission_mode.rearm_sent"]
    assert [e["reason"] for e in sent] == ["agents_idle"]


async def test_lifecycle_lift_skips_plan_auto_and_a_closing_session() -> None:
    state, stdin = _live_session()
    state.auto_approve_exit_plan_mode = True
    state.bg_max_hold_s = 60.0
    agent = _approved_with_agent(state)
    assert _claim_plan_rearm(state, SID, reason="followup") is None
    live = _register_live(state, stdin)
    agent.last_progress_at = time.monotonic() - 61.0
    runner = ClaudeRunner(claude_cmd="claude")
    await runner._lift_idle_plan_deferral(live, now=time.monotonic())
    assert _kinds(stdin) == []
    assert state.plan_rearm_deferred == 0

    state, stdin = _live_session()
    state.bg_max_hold_s = 60.0
    agent = _approved_with_agent(state)
    assert _claim_plan_rearm(state, SID, reason="idle") is None
    live = _register_live(state, stdin)
    live.closing = True
    agent.last_progress_at = time.monotonic() - 61.0
    await runner._lift_idle_plan_deferral(live, now=time.monotonic())
    assert _kinds(stdin) == []


def test_plan_observed_ends_the_deferral() -> None:
    state, _ = _live_session()
    _approved_with_agent(state)
    assert _claim_plan_rearm(state, SID, reason="idle") is None
    _status(state, "plan")  # e.g. Claude re-entered plan mode itself
    assert state.plan_rearm_deferred == 0
    assert state.plan_exit_turn is None
    assert claude_mod._rearm_deferral(state) == (0, "agents_done")


async def test_idle_steer_during_deferral_is_flagged() -> None:
    state, stdin = _live_session()
    _approved_with_agent(state)
    state.plan_rearm_pending = False
    _register_live(state, stdin)
    assert await steer_into_session(SID, "status?", command_uuid="c-s") == (
        "written_idle"
    )
    assert _kinds(stdin) == ["user"]
    assert state.unplanned_commands == {"c-s": 1}
