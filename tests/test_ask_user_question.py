"""Tests for A1 AskUserQuestion support in Telegram."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from untether.events import EventFactory
from untether.model import ActionEvent, ResumeToken
from untether.runners.claude import (
    ClaudeStreamState,
    ENGINE,
    _PENDING_ASK_REQUESTS,
    _REQUEST_TO_SESSION,
    _REQUEST_TO_INPUT,
    _HANDLED_REQUESTS,
    _ACTIVE_RUNNERS,
    _SESSION_STDIN,
    _DISCUSS_COOLDOWN,
    answer_ask_question,
    get_pending_ask_request,
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
    state = ClaudeStreamState()
    token = ResumeToken(engine=ENGINE, value=session_id)
    state.factory.started(token, title="claude")
    return state, state.factory


@pytest.fixture(autouse=True)
def _clear_registries():
    yield
    _ACTIVE_RUNNERS.clear()
    _SESSION_STDIN.clear()
    _REQUEST_TO_SESSION.clear()
    _REQUEST_TO_INPUT.clear()
    _HANDLED_REQUESTS.clear()
    _DISCUSS_COOLDOWN.clear()
    _PENDING_ASK_REQUESTS.clear()


# ===========================================================================
# AskUserQuestion is NOT auto-approved
# ===========================================================================


def test_ask_user_question_not_auto_approved() -> None:
    """AskUserQuestion should produce a warning event (not be auto-approved)."""
    state, factory = _make_state_with_session()
    event = _decode_event(
        {
            "type": "control_request",
            "request_id": "req-ask-1",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "AskUserQuestion",
                "input": {"question": "What colour should the button be?"},
            },
        }
    )
    events = translate_claude_event(event, title="claude", state=state, factory=factory)

    # Should produce a warning event (not be silently auto-approved)
    assert len(events) == 1
    evt = events[0]
    assert isinstance(evt, ActionEvent)
    assert evt.action.kind == "warning"


def test_ask_user_question_shows_question_text() -> None:
    """The question text should appear in the warning title."""
    state, factory = _make_state_with_session()
    event = _decode_event(
        {
            "type": "control_request",
            "request_id": "req-ask-2",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "AskUserQuestion",
                "input": {"question": "Should I add tests?"},
            },
        }
    )
    events = translate_claude_event(event, title="claude", state=state, factory=factory)
    assert len(events) == 1
    assert "Should I add tests?" in events[0].action.title


def test_ask_user_question_registered_pending() -> None:
    """AskUserQuestion should be registered in _PENDING_ASK_REQUESTS."""
    state, factory = _make_state_with_session()
    event = _decode_event(
        {
            "type": "control_request",
            "request_id": "req-ask-3",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "AskUserQuestion",
                "input": {"question": "Which database?"},
            },
        }
    )
    translate_claude_event(event, title="claude", state=state, factory=factory)
    assert "req-ask-3" in _PENDING_ASK_REQUESTS
    assert _PENDING_ASK_REQUESTS["req-ask-3"] == "Which database?"


def test_ask_user_question_has_inline_keyboard() -> None:
    """AskUserQuestion events should have approve/deny buttons."""
    state, factory = _make_state_with_session()
    event = _decode_event(
        {
            "type": "control_request",
            "request_id": "req-ask-4",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "AskUserQuestion",
                "input": {"question": "Continue?"},
            },
        }
    )
    events = translate_claude_event(event, title="claude", state=state, factory=factory)
    detail = events[0].action.detail
    kb = detail["inline_keyboard"]
    assert "buttons" in kb
    # Should have approve/deny buttons
    button_texts = [b["text"] for row in kb["buttons"] for b in row]
    assert "Approve" in button_texts
    assert "Deny" in button_texts


# ===========================================================================
# get_pending_ask_request / answer_ask_question
# ===========================================================================


def test_get_pending_ask_request_empty() -> None:
    assert get_pending_ask_request() is None


def test_get_pending_ask_request_returns_oldest() -> None:
    _PENDING_ASK_REQUESTS["req-1"] = "Question 1"
    _PENDING_ASK_REQUESTS["req-2"] = "Question 2"
    result = get_pending_ask_request()
    assert result is not None
    assert result[0] == "req-1"
    assert result[1] == "Question 1"


@pytest.mark.anyio
async def test_answer_ask_question_clears_pending() -> None:
    """Answering should clear the pending request."""
    _PENDING_ASK_REQUESTS["req-a"] = "What?"

    # Need an active runner for the response to work
    mock_runner = AsyncMock()
    _ACTIVE_RUNNERS["sess-1"] = (mock_runner, 0.0)
    _REQUEST_TO_SESSION["req-a"] = "sess-1"

    result = await answer_ask_question("req-a", "The answer is 42")
    assert "req-a" not in _PENDING_ASK_REQUESTS
    assert result is True


@pytest.mark.anyio
async def test_answer_ask_question_sends_deny_with_answer() -> None:
    """The answer should be sent as a deny message containing the user's text."""
    mock_runner = AsyncMock()
    _ACTIVE_RUNNERS["sess-1"] = (mock_runner, 0.0)
    _REQUEST_TO_SESSION["req-b"] = "sess-1"
    _PENDING_ASK_REQUESTS["req-b"] = "What colour?"

    await answer_ask_question("req-b", "Blue")

    # Should have called write_control_response with approved=False
    mock_runner.write_control_response.assert_called_once()
    call_args = mock_runner.write_control_response.call_args
    assert call_args[0][1] is False  # approved=False
    deny_msg = call_args[1]["deny_message"]
    assert "Blue" in deny_msg
    assert "answered your question" in deny_msg


# ===========================================================================
# Nested questions array format (real Claude AskUserQuestion input)
# ===========================================================================


def test_ask_question_nested_questions_array() -> None:
    """Claude sends AskUserQuestion with {"questions": [{"question": "..."}]}."""
    state, factory = _make_state_with_session()
    event = _decode_event(
        {
            "type": "control_request",
            "request_id": "req-nested-1",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "AskUserQuestion",
                "input": {
                    "questions": [{"question": "What is your favourite colour?"}]
                },
            },
        }
    )
    events = translate_claude_event(event, title="claude", state=state, factory=factory)
    assert len(events) == 1
    # Question text should be extracted and shown
    assert "What is your favourite colour?" in events[0].action.title
    # Should be registered in pending
    assert "req-nested-1" in _PENDING_ASK_REQUESTS
    assert _PENDING_ASK_REQUESTS["req-nested-1"] == "What is your favourite colour?"


def test_ask_question_nested_empty_questions() -> None:
    """Empty questions array should not crash."""
    state, factory = _make_state_with_session()
    event = _decode_event(
        {
            "type": "control_request",
            "request_id": "req-nested-2",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "AskUserQuestion",
                "input": {"questions": []},
            },
        }
    )
    events = translate_claude_event(event, title="claude", state=state, factory=factory)
    assert len(events) == 1
    # Should still register (empty question)
    assert "req-nested-2" in _PENDING_ASK_REQUESTS
