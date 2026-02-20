"""Tests for Claude Code control channel: request translation, response routing,
registry lifecycle, auto-approve drain, and full tool-use lifecycle."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import anyio
import pytest

import takopi.runners.claude as claude_mod
from takopi.events import EventFactory
from takopi.model import ActionEvent, ResumeToken, StartedEvent
from takopi.runners.claude import (
    ClaudeRunner,
    ClaudeStreamState,
    ENGINE,
    _ACTIVE_RUNNERS,
    _HANDLED_REQUESTS,
    _REQUEST_TO_INPUT,
    _REQUEST_TO_SESSION,
    _SESSION_STDIN,
    send_claude_control_response,
    translate_claude_event,
)
from takopi.schemas import claude as claude_schema


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


def _make_state_with_session(session_id: str = "sess-1") -> tuple[ClaudeStreamState, EventFactory]:
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


# ===========================================================================
# A. Control Request Translation
# ===========================================================================

def test_can_use_tool_produces_warning_with_inline_keyboard() -> None:
    """ExitPlanMode / CanUseTool request -> ActionEvent with kind='warning'
    and inline_keyboard containing Approve/Deny buttons with request_id."""
    state, factory = _make_state_with_session()
    event = _decode_event({
        "type": "control_request",
        "request_id": "req-1",
        "request": {
            "subtype": "can_use_tool",
            "tool_name": "Bash",
            "input": {"command": "echo hello"},
        },
    })
    events = translate_claude_event(event, title="claude", state=state, factory=factory)

    assert len(events) == 1
    evt = events[0]
    assert isinstance(evt, ActionEvent)
    assert evt.action.kind == "warning"
    assert evt.phase == "started"
    assert "CanUseTool" in evt.action.title

    kb = evt.action.detail["inline_keyboard"]
    buttons = kb["buttons"]
    assert len(buttons) == 1  # one row
    assert len(buttons[0]) == 2  # Approve + Deny
    assert buttons[0][0]["text"] == "Approve"
    assert "req-1" in buttons[0][0]["callback_data"]
    assert buttons[0][1]["text"] == "Deny"
    assert "req-1" in buttons[0][1]["callback_data"]


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
    event = _decode_event({
        "type": "control_request",
        "request_id": f"req-{subtype}",
        "request": request,
    })
    events = translate_claude_event(event, title="claude", state=state, factory=factory)

    assert events == []
    assert f"req-{subtype}" in state.auto_approve_queue


def test_request_to_session_populated() -> None:
    """A CanUseTool control request populates _REQUEST_TO_SESSION."""
    state, factory = _make_state_with_session("sess-abc")
    event = _decode_event({
        "type": "control_request",
        "request_id": "req-map",
        "request": {
            "subtype": "can_use_tool",
            "tool_name": "Read",
            "input": {"file_path": "/tmp/x"},
        },
    })
    translate_claude_event(event, title="claude", state=state, factory=factory)

    assert _REQUEST_TO_SESSION["req-map"] == "sess-abc"


def test_request_to_input_populated() -> None:
    """A CanUseTool control request stores original tool input."""
    state, factory = _make_state_with_session()
    tool_input = {"file_path": "/tmp/y", "limit": 100}
    event = _decode_event({
        "type": "control_request",
        "request_id": "req-inp",
        "request": {
            "subtype": "can_use_tool",
            "tool_name": "Read",
            "input": tool_input,
        },
    })
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
    control_action_for_tool mapping, and completion of both actions."""
    state, factory = _make_state_with_session()

    # Step 1: assistant message with tool_use
    tool_use_evt = _decode_event({
        "type": "assistant",
        "message": {
            "id": "msg-1",
            "content": [{
                "type": "tool_use",
                "id": "toolu_lifecycle",
                "name": "Bash",
                "input": {"command": "echo test"},
            }],
        },
    })
    events_1 = translate_claude_event(
        tool_use_evt, title="claude", state=state, factory=factory
    )
    assert len(events_1) == 1
    assert events_1[0].phase == "started"
    assert state.last_tool_use_id == "toolu_lifecycle"

    # Step 2: control request (can_use_tool)
    control_evt = _decode_event({
        "type": "control_request",
        "request_id": "req-lifecycle",
        "request": {
            "subtype": "can_use_tool",
            "tool_name": "Bash",
            "input": {"command": "echo test"},
        },
    })
    events_2 = translate_claude_event(
        control_evt, title="claude", state=state, factory=factory
    )
    assert len(events_2) == 1
    assert events_2[0].action.kind == "warning"

    # Verify mapping
    assert "toolu_lifecycle" in state.control_action_for_tool
    control_action_id = state.control_action_for_tool["toolu_lifecycle"]

    # Step 3: tool result
    result_evt = _decode_event({
        "type": "user",
        "message": {
            "id": "msg-2",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "toolu_lifecycle",
                "content": "test output",
                "is_error": False,
            }],
        },
    })
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
