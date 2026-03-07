"""Tests for A1 AskUserQuestion support in Telegram."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from untether.events import EventFactory
from untether.model import ActionEvent, ResumeToken
from untether.runners.claude import (
    AskQuestionState,
    ClaudeStreamState,
    ENGINE,
    _ASK_QUESTION_FLOWS,
    _PENDING_ASK_REQUESTS,
    _REQUEST_TO_SESSION,
    _REQUEST_TO_INPUT,
    _HANDLED_REQUESTS,
    _ACTIVE_RUNNERS,
    _SESSION_STDIN,
    _DISCUSS_COOLDOWN,
    answer_ask_question,
    answer_ask_question_with_options,
    format_question_message,
    get_ask_question_flow,
    get_pending_ask_request,
    get_question_option_buttons,
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
    _ASK_QUESTION_FLOWS.clear()


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
    assert isinstance(events[0], ActionEvent)
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
    assert isinstance(events[0], ActionEvent)
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
    mock_runner.write_control_response.return_value = True
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
# Nested questions array format (real Claude Code AskUserQuestion input)
# ===========================================================================


def test_ask_question_nested_questions_array() -> None:
    """Claude Code sends AskUserQuestion with {"questions": [{"question": "..."}]}."""
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
    assert isinstance(events[0], ActionEvent)
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


# ===========================================================================
# Option buttons rendering
# ===========================================================================


def test_ask_question_with_options_renders_buttons() -> None:
    """Questions with options should render option buttons instead of Approve/Deny."""
    state, factory = _make_state_with_session()
    event = _decode_event(
        {
            "type": "control_request",
            "request_id": "req-opts-1",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "AskUserQuestion",
                "input": {
                    "questions": [
                        {
                            "question": "Which database?",
                            "header": "Database",
                            "options": [
                                {"label": "PostgreSQL", "description": "Relational"},
                                {"label": "MongoDB", "description": "Document store"},
                            ],
                            "multiSelect": False,
                        }
                    ]
                },
            },
        }
    )
    events = translate_claude_event(event, title="claude", state=state, factory=factory)
    assert len(events) == 1
    evt = events[0]
    assert isinstance(evt, ActionEvent)
    detail = evt.action.detail
    kb = detail["inline_keyboard"]["buttons"]
    button_texts = [b["text"] for row in kb for b in row]
    assert "PostgreSQL" in button_texts
    assert "MongoDB" in button_texts
    assert "Other (type reply)" in button_texts
    # Approve/Deny must NOT appear alongside option buttons
    assert "Approve" not in button_texts
    assert "Deny" not in button_texts


def test_ask_question_with_options_creates_flow() -> None:
    """Questions with options should create an AskQuestionState flow."""
    state, factory = _make_state_with_session()
    event = _decode_event(
        {
            "type": "control_request",
            "request_id": "req-opts-2",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "AskUserQuestion",
                "input": {
                    "questions": [
                        {
                            "question": "Which framework?",
                            "options": [
                                {"label": "FastAPI"},
                                {"label": "Django"},
                            ],
                            "multiSelect": False,
                        }
                    ]
                },
            },
        }
    )
    translate_claude_event(event, title="claude", state=state, factory=factory)
    assert "req-opts-2" in _ASK_QUESTION_FLOWS
    flow = _ASK_QUESTION_FLOWS["req-opts-2"]
    assert flow.current_index == 0
    assert len(flow.questions) == 1


def test_ask_question_multi_question_counter() -> None:
    """Multi-question flows should show '1 of N' counter."""
    state, factory = _make_state_with_session()
    event = _decode_event(
        {
            "type": "control_request",
            "request_id": "req-multi-1",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "AskUserQuestion",
                "input": {
                    "questions": [
                        {
                            "question": "Which database?",
                            "options": [{"label": "PostgreSQL"}, {"label": "MySQL"}],
                            "multiSelect": False,
                        },
                        {
                            "question": "Which cache?",
                            "options": [{"label": "Redis"}, {"label": "Memcached"}],
                            "multiSelect": False,
                        },
                    ]
                },
            },
        }
    )
    events = translate_claude_event(event, title="claude", state=state, factory=factory)
    assert len(events) == 1
    assert "1 of 2" in events[0].action.title


def test_ask_question_without_options_no_flow() -> None:
    """Questions without options should NOT create a flow (text-only reply)."""
    state, factory = _make_state_with_session()
    event = _decode_event(
        {
            "type": "control_request",
            "request_id": "req-noopt-1",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "AskUserQuestion",
                "input": {"question": "What should I do?"},
            },
        }
    )
    translate_claude_event(event, title="claude", state=state, factory=factory)
    assert "req-noopt-1" not in _ASK_QUESTION_FLOWS
    # But should still be in pending requests for text reply
    assert "req-noopt-1" in _PENDING_ASK_REQUESTS


def test_option_buttons_callback_data_format() -> None:
    """Option button callback_data should be 'aq:opt:N'."""
    state, factory = _make_state_with_session()
    event = _decode_event(
        {
            "type": "control_request",
            "request_id": "req-cb-1",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "AskUserQuestion",
                "input": {
                    "questions": [
                        {
                            "question": "Pick one",
                            "options": [
                                {"label": "A"},
                                {"label": "B"},
                                {"label": "C"},
                            ],
                            "multiSelect": False,
                        }
                    ]
                },
            },
        }
    )
    events = translate_claude_event(event, title="claude", state=state, factory=factory)
    detail = events[0].action.detail
    kb = detail["inline_keyboard"]["buttons"]
    cb_data = [b["callback_data"] for row in kb for b in row]
    assert "aq:opt:0" in cb_data
    assert "aq:opt:1" in cb_data
    assert "aq:opt:2" in cb_data
    assert "aq:other" in cb_data


# ===========================================================================
# Flow management helpers
# ===========================================================================


def test_get_ask_question_flow_empty() -> None:
    assert get_ask_question_flow() is None


def test_get_ask_question_flow_returns_active() -> None:
    flow = AskQuestionState(
        request_id="req-flow-1",
        questions=[{"question": "Q1", "options": [{"label": "A"}]}],
    )
    _ASK_QUESTION_FLOWS["req-flow-1"] = flow
    assert get_ask_question_flow() is flow


def test_format_question_message_single() -> None:
    flow = AskQuestionState(
        request_id="req-1",
        questions=[{"question": "Pick a colour"}],
    )
    msg = format_question_message(flow)
    assert msg == "❓ Pick a colour"


def test_format_question_message_multi() -> None:
    flow = AskQuestionState(
        request_id="req-1",
        questions=[{"question": "First?"}, {"question": "Second?"}],
    )
    assert "1 of 2" in format_question_message(flow)
    flow.current_index = 1
    assert "2 of 2" in format_question_message(flow)


def test_get_question_option_buttons() -> None:
    flow = AskQuestionState(
        request_id="req-1",
        questions=[
            {
                "question": "Pick",
                "options": [{"label": "Opt A"}, {"label": "Opt B"}],
            }
        ],
    )
    buttons = get_question_option_buttons(flow)
    labels = [b["text"] for row in buttons for b in row]
    assert "Opt A" in labels
    assert "Opt B" in labels
    assert "Other (type reply)" in labels


# ===========================================================================
# Structured answer response
# ===========================================================================


@pytest.mark.anyio
async def test_answer_with_options_approves_with_answers() -> None:
    """Answering all questions should approve with structured answers."""
    mock_runner = AsyncMock()
    mock_runner.write_control_response.return_value = True
    _ACTIVE_RUNNERS["sess-1"] = (mock_runner, 0.0)
    _REQUEST_TO_SESSION["req-opts-a"] = "sess-1"
    _REQUEST_TO_INPUT["req-opts-a"] = {
        "questions": [{"question": "Which DB?", "options": [{"label": "PG"}]}]
    }
    _PENDING_ASK_REQUESTS["req-opts-a"] = "Which DB?"

    flow = AskQuestionState(
        request_id="req-opts-a",
        questions=[{"question": "Which DB?", "options": [{"label": "PG"}]}],
        answers={"Which DB?": "PG"},
    )
    flow.current_index = 1  # Past last question
    _ASK_QUESTION_FLOWS["req-opts-a"] = flow

    success = await answer_ask_question_with_options("req-opts-a")
    assert success is True

    # Should have called write_control_response with approved=True
    mock_runner.write_control_response.assert_called_once()
    call_args = mock_runner.write_control_response.call_args
    assert call_args[0][1] is True  # approved=True

    # Flow and pending should be cleaned up
    assert "req-opts-a" not in _ASK_QUESTION_FLOWS
    assert "req-opts-a" not in _PENDING_ASK_REQUESTS


@pytest.mark.anyio
async def test_answer_with_options_includes_answers_in_input() -> None:
    """The updatedInput should contain the answers dict."""
    mock_runner = AsyncMock()
    _ACTIVE_RUNNERS["sess-1"] = (mock_runner, 0.0)
    _REQUEST_TO_SESSION["req-opts-b"] = "sess-1"
    stored_input = {
        "questions": [{"question": "Colour?", "options": [{"label": "Red"}]}]
    }
    _REQUEST_TO_INPUT["req-opts-b"] = stored_input

    flow = AskQuestionState(
        request_id="req-opts-b",
        questions=[{"question": "Colour?"}],
        answers={"Colour?": "Red"},
    )
    flow.current_index = 1
    _ASK_QUESTION_FLOWS["req-opts-b"] = flow

    await answer_ask_question_with_options("req-opts-b")

    # The stored_input should now have "answers" key
    assert "answers" in stored_input
    assert stored_input["answers"]["Colour?"] == "Red"


@pytest.mark.anyio
async def test_answer_with_options_missing_flow_returns_false() -> None:
    """Missing flow should return False."""
    success = await answer_ask_question_with_options("nonexistent")
    assert success is False


# ===========================================================================
# Auto-deny when toggle is OFF
# ===========================================================================


def test_ask_question_auto_denied_when_off() -> None:
    """AskUserQuestion should be auto-denied when ask_questions toggle is OFF."""
    from untether.runners.run_options import (
        set_run_options,
        reset_run_options,
        EngineRunOptions,
    )

    state, factory = _make_state_with_session()
    token = set_run_options(EngineRunOptions(ask_questions=False))
    try:
        event = _decode_event(
            {
                "type": "control_request",
                "request_id": "req-deny-1",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "AskUserQuestion",
                    "input": {"question": "Should I?"},
                },
            }
        )
        events = translate_claude_event(
            event, title="claude", state=state, factory=factory
        )
        # Should be auto-denied (returns empty list, queued in auto_deny_queue)
        assert len(events) == 0
        assert len(state.auto_deny_queue) == 1
        req_id, msg = state.auto_deny_queue[0]
        assert req_id == "req-deny-1"
        assert "disabled" in msg.lower()
    finally:
        reset_run_options(token)


def test_ask_question_not_denied_when_on() -> None:
    """AskUserQuestion should NOT be auto-denied when toggle is ON."""
    from untether.runners.run_options import (
        set_run_options,
        reset_run_options,
        EngineRunOptions,
    )

    state, factory = _make_state_with_session()
    token = set_run_options(EngineRunOptions(ask_questions=True))
    try:
        event = _decode_event(
            {
                "type": "control_request",
                "request_id": "req-on-1",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "AskUserQuestion",
                    "input": {"question": "Should I?"},
                },
            }
        )
        events = translate_claude_event(
            event, title="claude", state=state, factory=factory
        )
        # Should produce a normal warning event
        assert len(events) == 1
        assert isinstance(events[0], ActionEvent)
    finally:
        reset_run_options(token)
