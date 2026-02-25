"""Tests for Claude Code control channel: request translation, response routing,
registry lifecycle, auto-approve drain, and full tool-use lifecycle."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from untether.events import EventFactory
from untether.model import ActionEvent, ResumeToken
from untether.runners.claude import (
    DISCUSS_COOLDOWN_BASE_SECONDS,
    ClaudeRunner,
    ClaudeStreamState,
    ENGINE,
    _ACTIVE_RUNNERS,
    _DISCUSS_COOLDOWN,
    _HANDLED_REQUESTS,
    _REQUEST_TO_INPUT,
    _REQUEST_TO_SESSION,
    _SESSION_STDIN,
    check_discuss_cooldown,
    clear_discuss_cooldown,
    send_claude_control_response,
    set_discuss_cooldown,
    translate_claude_event,
)
from untether.schemas import claude as claude_schema


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decode_event(payload: dict) -> claude_schema.StreamJsonMessage:
    """Build a StreamJsonMessage from a minimal dict, filling in defaults."""
    data = dict(payload)
    data.setdefault("uuid", "uuid")
    data.setdefault("session_id", "session")
    match data.get("type"):
        case "assistant":
            message = dict(data.get("message", {}))
            message.setdefault("role", "assistant")
            message.setdefault("content", [])
            message.setdefault("model", "claude")
            data["message"] = message
        case "user":
            message = dict(data.get("message", {}))
            message.setdefault("role", "user")
            message.setdefault("content", [])
            data["message"] = message
    return claude_schema.decode_stream_json_line(json.dumps(data).encode())


def _make_state_with_session(
    session_id: str = "sess-1",
) -> tuple[ClaudeStreamState, EventFactory]:
    """Return a state whose factory already has a resume token set."""
    state = ClaudeStreamState()
    token = ResumeToken(engine=ENGINE, value=session_id)
    state.factory.started(token, title="claude")
    return state, state.factory


# ---------------------------------------------------------------------------
# Autouse fixture: clear global registries between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_registries():
    yield
    _ACTIVE_RUNNERS.clear()
    _SESSION_STDIN.clear()
    _REQUEST_TO_SESSION.clear()
    _REQUEST_TO_INPUT.clear()
    _HANDLED_REQUESTS.clear()
    _DISCUSS_COOLDOWN.clear()


# ===========================================================================
# A. Control Request Translation
# ===========================================================================


def test_can_use_tool_produces_warning_with_inline_keyboard() -> None:
    """ExitPlanMode CanUseTool request -> ActionEvent with kind='warning'
    and inline_keyboard containing Approve/Deny buttons with request_id."""
    state, factory = _make_state_with_session()
    event = _decode_event(
        {
            "type": "control_request",
            "request_id": "req-1",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "ExitPlanMode",
                "input": {},
            },
        }
    )
    events = translate_claude_event(event, title="claude", state=state, factory=factory)

    assert len(events) == 1
    evt = events[0]
    assert isinstance(evt, ActionEvent)
    assert evt.action.kind == "warning"
    assert evt.phase == "started"
    assert "CanUseTool" in evt.action.title

    kb = evt.action.detail["inline_keyboard"]
    buttons = kb["buttons"]
    assert len(buttons) == 2  # two rows for ExitPlanMode
    assert len(buttons[0]) == 2  # Approve + Deny
    assert buttons[0][0]["text"] == "Approve"
    assert "req-1" in buttons[0][0]["callback_data"]
    assert buttons[0][1]["text"] == "Deny"
    assert "req-1" in buttons[0][1]["callback_data"]
    # Second row: Outline Plan
    assert len(buttons[1]) == 1
    assert buttons[1][0]["text"] == "Pause & Outline Plan"
    assert "discuss" in buttons[1][0]["callback_data"]
    assert "req-1" in buttons[1][0]["callback_data"]


@pytest.mark.parametrize(
    "subtype,extra_fields",
    [
        ("initialize", {"hooks": None}),
        ("hook_callback", {"callback_id": "cb-1", "input": {}}),
        ("mcp_message", {"server_name": "srv", "message": {}}),
        ("rewind_files", {"user_message_id": "msg-1"}),
        ("interrupt", {}),
    ],
)
def test_auto_approve_types_add_to_queue(subtype: str, extra_fields: dict) -> None:
    """Auto-approve request types produce no events and queue the request_id."""
    state, factory = _make_state_with_session()
    request = {"subtype": subtype, **extra_fields}
    event = _decode_event(
        {
            "type": "control_request",
            "request_id": f"req-{subtype}",
            "request": request,
        }
    )
    events = translate_claude_event(event, title="claude", state=state, factory=factory)

    assert events == []
    assert f"req-{subtype}" in state.auto_approve_queue


@pytest.mark.parametrize(
    "tool_name",
    [
        "Bash",
        "Read",
        "Edit",
        "Write",
        "Glob",
        "Grep",
        "WebFetch",
        "WebSearch",
        "Task",
        "Skill",
        "ToolSearch",
    ],
)
def test_non_exit_plan_mode_tools_auto_approved(tool_name: str) -> None:
    """CanUseTool requests for tools other than ExitPlanMode are auto-approved."""
    state, factory = _make_state_with_session()
    event = _decode_event(
        {
            "type": "control_request",
            "request_id": f"req-auto-{tool_name}",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": tool_name,
                "input": {},
            },
        }
    )
    events = translate_claude_event(event, title="claude", state=state, factory=factory)

    assert events == []
    assert f"req-auto-{tool_name}" in state.auto_approve_queue


def test_exit_plan_mode_not_auto_approved() -> None:
    """ExitPlanMode CanUseTool requests are NOT auto-approved (require user interaction)."""
    state, factory = _make_state_with_session()
    event = _decode_event(
        {
            "type": "control_request",
            "request_id": "req-epm",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "ExitPlanMode",
                "input": {},
            },
        }
    )
    events = translate_claude_event(event, title="claude", state=state, factory=factory)

    assert len(events) == 1
    assert events[0].action.kind == "warning"
    assert "req-epm" not in state.auto_approve_queue


def test_request_to_session_populated() -> None:
    """A CanUseTool control request (requiring approval) populates _REQUEST_TO_SESSION."""
    state, factory = _make_state_with_session("sess-abc")
    event = _decode_event(
        {
            "type": "control_request",
            "request_id": "req-map",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "ExitPlanMode",
                "input": {},
            },
        }
    )
    translate_claude_event(event, title="claude", state=state, factory=factory)

    assert _REQUEST_TO_SESSION["req-map"] == "sess-abc"


def test_request_to_input_populated() -> None:
    """A CanUseTool control request (requiring approval) stores original tool input."""
    state, factory = _make_state_with_session()
    tool_input: dict[str, Any] = {}
    event = _decode_event(
        {
            "type": "control_request",
            "request_id": "req-inp",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "ExitPlanMode",
                "input": tool_input,
            },
        }
    )
    translate_claude_event(event, title="claude", state=state, factory=factory)

    assert _REQUEST_TO_INPUT["req-inp"] == tool_input


# ===========================================================================
# B. Control Response Routing
# ===========================================================================


@pytest.mark.anyio
async def test_send_control_response_success() -> None:
    """Registers runner + session + stdin, sends response, verifies cleanup."""
    runner = ClaudeRunner(claude_cmd="claude")
    session_id = "sess-resp"

    # Register runner and session stdin
    _ACTIVE_RUNNERS[session_id] = (runner, 0.0)
    fake_stdin = AsyncMock()
    _SESSION_STDIN[session_id] = fake_stdin
    _REQUEST_TO_SESSION["req-resp"] = session_id
    _REQUEST_TO_INPUT["req-resp"] = {"command": "ls"}

    result = await send_claude_control_response("req-resp", approved=True)

    assert result is True
    # Verify JSON payload sent to stdin
    fake_stdin.send.assert_awaited_once()
    payload = json.loads(fake_stdin.send.call_args[0][0].decode())
    assert payload["type"] == "control_response"
    assert payload["response"]["request_id"] == "req-resp"
    assert payload["response"]["response"]["behavior"] == "allow"
    assert payload["response"]["response"]["updatedInput"] == {"command": "ls"}

    # Cleanup: request removed from mapping, added to handled
    assert "req-resp" not in _REQUEST_TO_SESSION
    assert "req-resp" in _HANDLED_REQUESTS


@pytest.mark.anyio
async def test_duplicate_request_returns_true() -> None:
    """Already-handled request_id returns True (duplicate callback)."""
    _HANDLED_REQUESTS.add("req-dup")
    result = await send_claude_control_response("req-dup", approved=True)
    assert result is True


@pytest.mark.anyio
async def test_unknown_request_returns_false() -> None:
    """Unknown request_id returns False."""
    result = await send_claude_control_response("req-unknown", approved=True)
    assert result is False


@pytest.mark.anyio
async def test_write_control_response_deny_format() -> None:
    """Deny produces behavior='deny' with message."""
    runner = ClaudeRunner(claude_cmd="claude")
    session_id = "sess-deny"

    _ACTIVE_RUNNERS[session_id] = (runner, 0.0)
    fake_stdin = AsyncMock()
    _SESSION_STDIN[session_id] = fake_stdin
    _REQUEST_TO_SESSION["req-deny"] = session_id
    _REQUEST_TO_INPUT["req-deny"] = {"command": "rm -rf /"}

    result = await send_claude_control_response("req-deny", approved=False)

    assert result is True
    payload = json.loads(fake_stdin.send.call_args[0][0].decode())
    inner = payload["response"]["response"]
    assert inner["behavior"] == "deny"
    assert inner["message"] == "User denied"
    # updatedInput should NOT be present on deny
    assert "updatedInput" not in inner


# ===========================================================================
# C. Registry Lifecycle
# ===========================================================================


def test_session_stdin_different_entries() -> None:
    """Two sessions get distinct stdin entries."""
    fake_a = AsyncMock()
    fake_b = AsyncMock()
    _SESSION_STDIN["sess-a"] = fake_a
    _SESSION_STDIN["sess-b"] = fake_b

    assert _SESSION_STDIN["sess-a"] is fake_a
    assert _SESSION_STDIN["sess-b"] is fake_b
    assert _SESSION_STDIN["sess-a"] is not _SESSION_STDIN["sess-b"]


def test_process_error_events_cleans_registries() -> None:
    """process_error_events removes session from _ACTIVE_RUNNERS and _SESSION_STDIN."""
    runner = ClaudeRunner(claude_cmd="claude")
    session_id = "sess-err"
    token = ResumeToken(engine=ENGINE, value=session_id)

    _ACTIVE_RUNNERS[session_id] = (runner, 0.0)
    _SESSION_STDIN[session_id] = AsyncMock()

    state = ClaudeStreamState()
    runner.process_error_events(
        1,
        resume=token,
        found_session=token,
        state=state,
    )

    assert session_id not in _ACTIVE_RUNNERS
    assert session_id not in _SESSION_STDIN


def test_stream_end_events_cleans_registries() -> None:
    """stream_end_events removes session from _ACTIVE_RUNNERS and _SESSION_STDIN."""
    runner = ClaudeRunner(claude_cmd="claude")
    session_id = "sess-end"
    token = ResumeToken(engine=ENGINE, value=session_id)

    _ACTIVE_RUNNERS[session_id] = (runner, 0.0)
    _SESSION_STDIN[session_id] = AsyncMock()

    state = ClaudeStreamState()
    runner.stream_end_events(
        resume=token,
        found_session=token,
        state=state,
    )

    assert session_id not in _ACTIVE_RUNNERS
    assert session_id not in _SESSION_STDIN


# ===========================================================================
# D. Auto-approve Drain
# ===========================================================================


@pytest.mark.anyio
async def test_drain_auto_approve_uses_provided_stdin() -> None:
    """Drain writes to the provided stdin, not self._proc_stdin."""
    runner = ClaudeRunner(claude_cmd="claude")
    runner._proc_stdin = AsyncMock(name="proc_stdin")  # should NOT be used
    provided = AsyncMock(name="provided_stdin")

    state = ClaudeStreamState()
    state.auto_approve_queue.append("req-drain-1")

    await runner._drain_auto_approve(state, stdin=provided)

    provided.send.assert_awaited_once()
    runner._proc_stdin.send.assert_not_awaited()
    assert state.auto_approve_queue == []


@pytest.mark.anyio
async def test_drain_auto_approve_falls_back_to_proc_stdin() -> None:
    """Without explicit stdin, falls back to self._proc_stdin."""
    runner = ClaudeRunner(claude_cmd="claude")
    runner._proc_stdin = AsyncMock(name="proc_stdin")

    state = ClaudeStreamState()
    state.auto_approve_queue.extend(["req-fb-1", "req-fb-2"])

    await runner._drain_auto_approve(state)

    assert runner._proc_stdin.send.await_count == 2
    assert state.auto_approve_queue == []


# ===========================================================================
# E. Full Lifecycle
# ===========================================================================


def test_control_action_lifecycle_tool_use_to_result() -> None:
    """tool_use -> control_request -> tool_result: verifies last_tool_use_id,
    control_action_for_tool mapping, and completion of both actions.
    Uses ExitPlanMode since it's the only tool requiring interactive approval."""
    state, factory = _make_state_with_session()

    # Step 1: assistant message with tool_use
    tool_use_evt = _decode_event(
        {
            "type": "assistant",
            "message": {
                "id": "msg-1",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_lifecycle",
                        "name": "ExitPlanMode",
                        "input": {},
                    }
                ],
            },
        }
    )
    events_1 = translate_claude_event(
        tool_use_evt, title="claude", state=state, factory=factory
    )
    assert len(events_1) == 1
    assert events_1[0].phase == "started"
    assert state.last_tool_use_id == "toolu_lifecycle"

    # Step 2: control request (can_use_tool) — ExitPlanMode requires approval
    control_evt = _decode_event(
        {
            "type": "control_request",
            "request_id": "req-lifecycle",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "ExitPlanMode",
                "input": {},
            },
        }
    )
    events_2 = translate_claude_event(
        control_evt, title="claude", state=state, factory=factory
    )
    assert len(events_2) == 1
    assert events_2[0].action.kind == "warning"

    # Verify mapping
    assert "toolu_lifecycle" in state.control_action_for_tool
    control_action_id = state.control_action_for_tool["toolu_lifecycle"]

    # Step 3: tool result
    result_evt = _decode_event(
        {
            "type": "user",
            "message": {
                "id": "msg-2",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_lifecycle",
                        "content": "plan approved",
                        "is_error": False,
                    }
                ],
            },
        }
    )
    events_3 = translate_claude_event(
        result_evt, title="claude", state=state, factory=factory
    )

    # Should produce: tool result completion + control action completion
    assert len(events_3) == 2
    tool_result = events_3[0]
    control_resolved = events_3[1]

    assert tool_result.phase == "completed"
    assert tool_result.action.id == "toolu_lifecycle"

    assert control_resolved.phase == "completed"
    assert control_resolved.action.id == control_action_id
    assert control_resolved.action.kind == "warning"
    assert control_resolved.action.title == "Permission resolved"

    # Mapping cleaned up
    assert "toolu_lifecycle" not in state.control_action_for_tool


# ===========================================================================
# F. Discuss Action & Custom Deny Message
# ===========================================================================


@pytest.mark.anyio
async def test_send_control_response_custom_deny_message() -> None:
    """Custom deny_message is included in the control response payload."""
    runner = ClaudeRunner(claude_cmd="claude")
    session_id = "sess-custom-deny"

    _ACTIVE_RUNNERS[session_id] = (runner, 0.0)
    fake_stdin = AsyncMock()
    _SESSION_STDIN[session_id] = fake_stdin
    _REQUEST_TO_SESSION["req-custom"] = session_id
    _REQUEST_TO_INPUT["req-custom"] = {}

    result = await send_claude_control_response(
        "req-custom", approved=False, deny_message="Please outline the plan"
    )

    assert result is True
    payload = json.loads(fake_stdin.send.call_args[0][0].decode())
    inner = payload["response"]["response"]
    assert inner["behavior"] == "deny"
    assert inner["message"] == "Please outline the plan"


@pytest.mark.anyio
async def test_send_control_response_default_deny_message() -> None:
    """Without custom deny_message, 'User denied' is used."""
    runner = ClaudeRunner(claude_cmd="claude")
    session_id = "sess-default-deny"

    _ACTIVE_RUNNERS[session_id] = (runner, 0.0)
    fake_stdin = AsyncMock()
    _SESSION_STDIN[session_id] = fake_stdin
    _REQUEST_TO_SESSION["req-default"] = session_id
    _REQUEST_TO_INPUT["req-default"] = {}

    await send_claude_control_response("req-default", approved=False)

    payload = json.loads(fake_stdin.send.call_args[0][0].decode())
    inner = payload["response"]["response"]
    assert inner["message"] == "User denied"


# ===========================================================================
# G. ClaudeControlCommand: early_answer_toast & discuss handler
# ===========================================================================


def test_early_answer_toast_values() -> None:
    """early_answer_toast returns correct toast for each action."""
    from untether.telegram.commands.claude_control import ClaudeControlCommand

    cmd = ClaudeControlCommand()
    assert cmd.early_answer_toast("approve:req-1") == "Approved"
    assert cmd.early_answer_toast("deny:req-1") == "Denied"
    assert cmd.early_answer_toast("discuss:req-1") == "Outlining plan..."
    assert cmd.early_answer_toast("unknown:req-1") is None
    assert cmd.early_answer_toast("") is None


@pytest.mark.anyio
async def test_discuss_action_sends_deny_with_custom_message() -> None:
    """Discuss action sends a deny with the outline-plan deny message."""
    from untether.telegram.commands.claude_control import (
        ClaudeControlCommand,
        _DISCUSS_DENY_MESSAGE,
    )

    runner = ClaudeRunner(claude_cmd="claude")
    session_id = "sess-discuss"

    _ACTIVE_RUNNERS[session_id] = (runner, 0.0)
    fake_stdin = AsyncMock()
    _SESSION_STDIN[session_id] = fake_stdin
    _REQUEST_TO_SESSION["req-discuss"] = session_id
    _REQUEST_TO_INPUT["req-discuss"] = {}

    # Build a minimal CommandContext
    from untether.commands import CommandContext
    from untether.transport import MessageRef

    ctx = CommandContext(
        command="claude_control",
        text="claude_control:discuss:req-discuss",
        args_text="discuss:req-discuss",
        args=["discuss:req-discuss"],
        message=MessageRef(channel_id=123, message_id=1),
        reply_to=None,
        reply_text=None,
        config_path=None,
        plugin_config=None,
        runtime=None,
        executor=None,
    )

    cmd = ClaudeControlCommand()
    result = await cmd.handle(ctx)

    assert result is not None
    assert "outline" in result.text.lower()

    # Verify the stdin payload
    payload = json.loads(fake_stdin.send.call_args[0][0].decode())
    inner = payload["response"]["response"]
    assert inner["behavior"] == "deny"
    assert inner["message"] == _DISCUSS_DENY_MESSAGE


# ===========================================================================
# H. Discuss Cooldown / Rate-Limiting
# ===========================================================================


def test_set_discuss_cooldown_creates_entry() -> None:
    """set_discuss_cooldown creates a cooldown entry with count=1."""
    set_discuss_cooldown("sess-cd-1")
    assert "sess-cd-1" in _DISCUSS_COOLDOWN
    _, count = _DISCUSS_COOLDOWN["sess-cd-1"]
    assert count == 1


def test_set_discuss_cooldown_increments_count() -> None:
    """Repeated calls increment the deny count."""
    set_discuss_cooldown("sess-cd-2")
    set_discuss_cooldown("sess-cd-2")
    set_discuss_cooldown("sess-cd-2")
    _, count = _DISCUSS_COOLDOWN["sess-cd-2"]
    assert count == 3


def test_check_discuss_cooldown_returns_escalation_within_window() -> None:
    """check_discuss_cooldown returns escalation message within the cooldown window."""
    set_discuss_cooldown("sess-cd-3")
    result = check_discuss_cooldown("sess-cd-3")
    assert result is not None
    assert "BLOCKED" in result
    assert "30s" in result  # count=1 -> 30s cooldown


def test_check_discuss_cooldown_returns_none_when_not_set() -> None:
    """check_discuss_cooldown returns None for unknown sessions."""
    result = check_discuss_cooldown("sess-unknown")
    assert result is None


def test_check_discuss_cooldown_returns_none_after_expiry() -> None:
    """check_discuss_cooldown returns None after the cooldown expires,
    but preserves the count with zeroed timestamp for progressive escalation."""
    import time as _time

    set_discuss_cooldown("sess-cd-4")
    # Manually backdate the timestamp
    _, count = _DISCUSS_COOLDOWN["sess-cd-4"]
    _DISCUSS_COOLDOWN["sess-cd-4"] = (
        _time.time() - DISCUSS_COOLDOWN_BASE_SECONDS - 1,
        count,
    )

    result = check_discuss_cooldown("sess-cd-4")
    assert result is None
    # Entry preserved with zeroed timestamp so next click escalates further
    assert "sess-cd-4" in _DISCUSS_COOLDOWN
    ts, preserved_count = _DISCUSS_COOLDOWN["sess-cd-4"]
    assert ts == 0.0
    assert preserved_count == count


def test_clear_discuss_cooldown_removes_entry() -> None:
    """clear_discuss_cooldown removes the cooldown entry."""
    set_discuss_cooldown("sess-cd-5")
    assert "sess-cd-5" in _DISCUSS_COOLDOWN
    clear_discuss_cooldown("sess-cd-5")
    assert "sess-cd-5" not in _DISCUSS_COOLDOWN


def test_clear_discuss_cooldown_noop_for_unknown() -> None:
    """clear_discuss_cooldown is a no-op for unknown sessions."""
    clear_discuss_cooldown("sess-nonexistent")  # Should not raise


def test_exit_plan_mode_auto_denied_during_cooldown() -> None:
    """ExitPlanMode request during discuss cooldown produces no events
    and queues an auto-deny."""
    state, factory = _make_state_with_session("sess-cooldown")
    set_discuss_cooldown("sess-cooldown")

    event = _decode_event(
        {
            "type": "control_request",
            "request_id": "req-cd-deny",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "ExitPlanMode",
                "input": {},
            },
        }
    )
    events = translate_claude_event(event, title="claude", state=state, factory=factory)

    assert events == []
    assert len(state.auto_deny_queue) == 1
    assert state.auto_deny_queue[0][0] == "req-cd-deny"


def test_exit_plan_mode_not_auto_denied_after_cooldown_expires() -> None:
    """ExitPlanMode request after cooldown expires is handled normally."""
    import time as _time

    state, factory = _make_state_with_session("sess-cd-expired")
    set_discuss_cooldown("sess-cd-expired")
    # Backdate to expire
    _, count = _DISCUSS_COOLDOWN["sess-cd-expired"]
    _DISCUSS_COOLDOWN["sess-cd-expired"] = (
        _time.time() - DISCUSS_COOLDOWN_BASE_SECONDS - 1,
        count,
    )

    event = _decode_event(
        {
            "type": "control_request",
            "request_id": "req-cd-ok",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "ExitPlanMode",
                "input": {},
            },
        }
    )
    events = translate_claude_event(event, title="claude", state=state, factory=factory)

    # Should produce a normal approval-required event (not auto-denied)
    assert len(events) == 1
    assert events[0].action.kind == "warning"
    assert state.auto_deny_queue == []


@pytest.mark.anyio
async def test_drain_auto_deny_sends_deny_response() -> None:
    """_drain_auto_deny writes deny payloads to stdin and clears the queue."""
    runner = ClaudeRunner(claude_cmd="claude")
    provided = AsyncMock(name="provided_stdin")

    state = ClaudeStreamState()
    state.auto_deny_queue.append(("req-ad-1", "Test escalation message"))

    await runner._drain_auto_deny(state, stdin=provided)

    provided.send.assert_awaited_once()
    payload = json.loads(provided.send.call_args[0][0].decode())
    assert payload["type"] == "control_response"
    assert payload["response"]["request_id"] == "req-ad-1"
    assert payload["response"]["response"]["behavior"] == "deny"
    assert payload["response"]["response"]["message"] == "Test escalation message"
    assert state.auto_deny_queue == []


@pytest.mark.anyio
async def test_drain_auto_deny_multiple_items() -> None:
    """_drain_auto_deny processes all queued items."""
    runner = ClaudeRunner(claude_cmd="claude")
    provided = AsyncMock(name="provided_stdin")

    state = ClaudeStreamState()
    state.auto_deny_queue.append(("req-ad-2", "msg-2"))
    state.auto_deny_queue.append(("req-ad-3", "msg-3"))

    await runner._drain_auto_deny(state, stdin=provided)

    assert provided.send.await_count == 2
    assert state.auto_deny_queue == []


@pytest.mark.anyio
async def test_discuss_handler_sets_cooldown() -> None:
    """Discuss action sets the discuss cooldown for the session."""
    from untether.telegram.commands.claude_control import ClaudeControlCommand

    runner = ClaudeRunner(claude_cmd="claude")
    session_id = "sess-discuss-cd"

    _ACTIVE_RUNNERS[session_id] = (runner, 0.0)
    fake_stdin = AsyncMock()
    _SESSION_STDIN[session_id] = fake_stdin
    _REQUEST_TO_SESSION["req-discuss-cd"] = session_id
    _REQUEST_TO_INPUT["req-discuss-cd"] = {}

    from untether.commands import CommandContext
    from untether.transport import MessageRef

    ctx = CommandContext(
        command="claude_control",
        text="claude_control:discuss:req-discuss-cd",
        args_text="discuss:req-discuss-cd",
        args=["discuss:req-discuss-cd"],
        message=MessageRef(channel_id=123, message_id=1),
        reply_to=None,
        reply_text=None,
        config_path=None,
        plugin_config=None,
        runtime=None,
        executor=None,
    )

    cmd = ClaudeControlCommand()
    await cmd.handle(ctx)

    # Cooldown should be set for the session
    assert session_id in _DISCUSS_COOLDOWN


@pytest.mark.anyio
async def test_approve_handler_clears_cooldown() -> None:
    """Approve action clears any discuss cooldown for the session."""
    from untether.telegram.commands.claude_control import ClaudeControlCommand

    runner = ClaudeRunner(claude_cmd="claude")
    session_id = "sess-approve-cd"

    _ACTIVE_RUNNERS[session_id] = (runner, 0.0)
    fake_stdin = AsyncMock()
    _SESSION_STDIN[session_id] = fake_stdin
    _REQUEST_TO_SESSION["req-approve-cd"] = session_id
    _REQUEST_TO_INPUT["req-approve-cd"] = {}

    # Pre-set a cooldown
    set_discuss_cooldown(session_id)
    assert session_id in _DISCUSS_COOLDOWN

    from untether.commands import CommandContext
    from untether.transport import MessageRef

    ctx = CommandContext(
        command="claude_control",
        text="claude_control:approve:req-approve-cd",
        args_text="approve:req-approve-cd",
        args=["approve:req-approve-cd"],
        message=MessageRef(channel_id=123, message_id=1),
        reply_to=None,
        reply_text=None,
        config_path=None,
        plugin_config=None,
        runtime=None,
        executor=None,
    )

    cmd = ClaudeControlCommand()
    await cmd.handle(ctx)

    # Cooldown should be cleared
    assert session_id not in _DISCUSS_COOLDOWN


# ===========================================================================
# I. Progressive Cooldown Timing
# ===========================================================================


def test_progressive_cooldown_increases_with_count() -> None:
    """Cooldown duration increases with each discuss click: 30s, 60s, 90s, 120s."""
    from untether.runners.claude import _cooldown_seconds

    assert _cooldown_seconds(1) == 30.0
    assert _cooldown_seconds(2) == 60.0
    assert _cooldown_seconds(3) == 90.0
    assert _cooldown_seconds(4) == 120.0
    # Capped at 120s
    assert _cooldown_seconds(5) == 120.0
    assert _cooldown_seconds(10) == 120.0


def test_progressive_cooldown_escalation_message_shows_duration() -> None:
    """Escalation message includes the current cooldown duration."""
    set_discuss_cooldown("sess-prog-1")
    set_discuss_cooldown("sess-prog-1")  # count=2 -> 60s

    msg = check_discuss_cooldown("sess-prog-1")
    assert msg is not None
    assert "60s" in msg


def test_progressive_cooldown_count_preserved_after_expiry() -> None:
    """After cooldown expires, count is preserved so next click escalates."""
    import time as _time

    set_discuss_cooldown("sess-prog-2")  # count=1
    # Expire the cooldown
    _, count = _DISCUSS_COOLDOWN["sess-prog-2"]
    _DISCUSS_COOLDOWN["sess-prog-2"] = (
        _time.time() - DISCUSS_COOLDOWN_BASE_SECONDS - 1,
        count,
    )
    check_discuss_cooldown("sess-prog-2")  # returns None, preserves count

    # Next click should be count=2 (60s cooldown)
    set_discuss_cooldown("sess-prog-2")
    _, new_count = _DISCUSS_COOLDOWN["sess-prog-2"]
    assert new_count == 2

    msg = check_discuss_cooldown("sess-prog-2")
    assert msg is not None
    assert "60s" in msg


# ===========================================================================
# J. Auto-approve ExitPlanMode in "auto" permission mode
# ===========================================================================


def test_exit_plan_mode_auto_approved_in_auto_mode() -> None:
    """ExitPlanMode is auto-approved when auto_approve_exit_plan_mode is True."""
    state, factory = _make_state_with_session()
    state.auto_approve_exit_plan_mode = True

    event = _decode_event(
        {
            "type": "control_request",
            "request_id": "req-auto-epm",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "ExitPlanMode",
                "input": {},
            },
        }
    )
    events = translate_claude_event(event, title="claude", state=state, factory=factory)

    assert events == []
    assert "req-auto-epm" in state.auto_approve_queue


def test_exit_plan_mode_not_auto_approved_in_plan_mode() -> None:
    """ExitPlanMode still requires approval when auto_approve_exit_plan_mode is False."""
    state, factory = _make_state_with_session()
    state.auto_approve_exit_plan_mode = False

    event = _decode_event(
        {
            "type": "control_request",
            "request_id": "req-plan-epm",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "ExitPlanMode",
                "input": {},
            },
        }
    )
    events = translate_claude_event(event, title="claude", state=state, factory=factory)

    assert len(events) == 1
    assert events[0].action.kind == "warning"
    assert "req-plan-epm" not in state.auto_approve_queue


def test_exit_plan_mode_auto_mode_skips_cooldown() -> None:
    """Auto mode bypasses discuss cooldown — auto-approves even during cooldown."""
    state, factory = _make_state_with_session("sess-auto-cd")
    state.auto_approve_exit_plan_mode = True

    # Set a discuss cooldown for this session
    set_discuss_cooldown("sess-auto-cd")

    event = _decode_event(
        {
            "type": "control_request",
            "request_id": "req-auto-cd",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "ExitPlanMode",
                "input": {},
            },
        }
    )
    events = translate_claude_event(event, title="claude", state=state, factory=factory)

    # Should be auto-approved, not auto-denied by cooldown
    assert events == []
    assert "req-auto-cd" in state.auto_approve_queue
    assert state.auto_deny_queue == []
