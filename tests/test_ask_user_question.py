"""Tests for A1 AskUserQuestion support in Telegram."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from untether.events import EventFactory
from untether.model import ActionEvent, ResumeToken
from untether.runners.claude import (
    _ACTIVE_RUNNERS,
    _ANSWERED_ASK_FLOWS,
    _ASK_QUESTION_FLOWS,
    _HANDLED_REQUESTS,
    _PENDING_ASK_REQUESTS,
    _REQUEST_TO_INPUT,
    _REQUEST_TO_SESSION,
    _SESSION_STDIN,
    ENGINE,
    AskQuestionState,
    ClaudeStreamState,
    _record_answered_ask_flow,
    answer_ask_question,
    answer_ask_question_with_options,
    format_question_message,
    get_ask_question_flow,
    get_pending_ask_request,
    get_question_option_buttons,
    recently_answered_ask_flow,
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


CHAT_A = -100001
CHAT_B = -100002


@pytest.fixture(autouse=True)
def _clear_registries():
    from untether.utils.paths import reset_run_channel_id, set_run_channel_id

    token = set_run_channel_id(CHAT_A)
    yield
    reset_run_channel_id(token)
    _ACTIVE_RUNNERS.clear()
    _SESSION_STDIN.clear()
    _REQUEST_TO_SESSION.clear()
    _REQUEST_TO_INPUT.clear()
    _HANDLED_REQUESTS.clear()
    _PENDING_ASK_REQUESTS.clear()
    _ASK_QUESTION_FLOWS.clear()
    _ANSWERED_ASK_FLOWS.clear()


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
    assert _PENDING_ASK_REQUESTS["req-ask-3"] == (CHAT_A, "Which database?")


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
    assert "✅ Approve" in button_texts
    assert "❌ Deny" in button_texts


# ===========================================================================
# get_pending_ask_request / answer_ask_question
# ===========================================================================


def test_get_pending_ask_request_empty() -> None:
    assert get_pending_ask_request() is None


def test_get_pending_ask_request_returns_oldest() -> None:
    _PENDING_ASK_REQUESTS["req-1"] = (CHAT_A, "Question 1")
    _PENDING_ASK_REQUESTS["req-2"] = (CHAT_A, "Question 2")
    result = get_pending_ask_request(channel_id=CHAT_A)
    assert result is not None
    assert result[0] == "req-1"
    assert result[1] == "Question 1"


@pytest.mark.anyio
async def test_answer_ask_question_clears_pending() -> None:
    """Answering should clear the pending request."""
    _PENDING_ASK_REQUESTS["req-a"] = (CHAT_A, "What?")

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
    _PENDING_ASK_REQUESTS["req-b"] = (CHAT_A, "What colour?")

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
    assert _PENDING_ASK_REQUESTS["req-nested-1"] == (
        CHAT_A,
        "What is your favourite colour?",
    )


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
        channel_id=CHAT_A,
        questions=[{"question": "Q1", "options": [{"label": "A"}]}],
    )
    _ASK_QUESTION_FLOWS["req-flow-1"] = flow
    assert get_ask_question_flow(channel_id=CHAT_A) is flow


def test_format_question_message_single() -> None:
    flow = AskQuestionState(
        request_id="req-1",
        channel_id=CHAT_A,
        questions=[{"question": "Pick a colour"}],
    )
    msg = format_question_message(flow)
    assert msg == "❓ Pick a colour"


def test_format_question_message_multi() -> None:
    flow = AskQuestionState(
        request_id="req-1",
        channel_id=CHAT_A,
        questions=[{"question": "First?"}, {"question": "Second?"}],
    )
    assert "1 of 2" in format_question_message(flow)
    flow.current_index = 1
    assert "2 of 2" in format_question_message(flow)


# ---------------------------------------------------------------------------
# #713 — HTML escaping at the parse_mode="HTML" boundary
# ---------------------------------------------------------------------------


def test_format_question_message_html_escapes_agent_tags() -> None:
    """An agent-authored tag must not reach Telegram's HTML parser raw.

    Live repro (nsd 0.35.5rc4): a question containing a literal ``<svg>``
    produced ``400 Bad Request: can't parse entities: Unsupported start tag
    "svg"``, which dropped a keyboard-carrying edit and left the run
    unanswerable from Telegram.
    """
    flow = AskQuestionState(
        request_id="req-713",
        channel_id=CHAT_A,
        questions=[{"question": "Guard a blank line inside an inline `<svg>`?"}],
    )
    msg = format_question_message(flow, escape_html=True)
    assert "&lt;svg&gt;" in msg
    assert "<svg>" not in msg


def test_format_question_message_html_escapes_ampersand() -> None:
    """A bare ``&`` is equally fatal to Telegram's HTML parser."""
    flow = AskQuestionState(
        request_id="req-713",
        channel_id=CHAT_A,
        questions=[{"question": "Ship A & B, or just <b>A</b>?"}],
    )
    msg = format_question_message(flow, escape_html=True)
    assert "&amp;" in msg
    assert "&lt;b&gt;A&lt;/b&gt;" in msg
    # Quotes are legal in HTML text content — escaping them would surface
    # literal &quot; to the user, so they are deliberately left alone.
    assert "&quot;" not in format_question_message(
        AskQuestionState(
            request_id="req-713b",
            channel_id=CHAT_A,
            questions=[{"question": 'Use "double" quotes?'}],
        ),
        escape_html=True,
    )


def test_format_question_message_default_stays_raw() -> None:
    """The default must NOT escape — it feeds the markdown/entities path.

    ``advance_ask_action_model`` stores this string as the progress action
    title, which is rendered via ``render_markdown`` (markdown-it with
    ``html: False`` already neutralises tags). Escaping here too would
    double-escape and show the user a literal ``&lt;svg&gt;``.
    """
    flow = AskQuestionState(
        request_id="req-713",
        channel_id=CHAT_A,
        questions=[{"question": "Guard an inline `<svg>`?"}],
    )
    assert "<svg>" in format_question_message(flow)
    assert "&lt;" not in format_question_message(flow)


def test_format_question_message_html_keeps_multi_question_prefix() -> None:
    """Escaping applies to agent text only, never the bot-authored prefix."""
    flow = AskQuestionState(
        request_id="req-713",
        channel_id=CHAT_A,
        questions=[{"question": "<one>"}, {"question": "<two>"}],
    )
    msg = format_question_message(flow, escape_html=True)
    assert msg == "❓ Question 1 of 2: &lt;one&gt;"


def test_get_question_option_buttons() -> None:
    flow = AskQuestionState(
        request_id="req-1",
        channel_id=CHAT_A,
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
    _PENDING_ASK_REQUESTS["req-opts-a"] = (CHAT_A, "Which DB?")

    flow = AskQuestionState(
        request_id="req-opts-a",
        channel_id=CHAT_A,
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
        channel_id=CHAT_A,
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
        EngineRunOptions,
        reset_run_options,
        set_run_options,
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
        EngineRunOptions,
        reset_run_options,
        set_run_options,
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


# ===========================================================================
# Cross-chat isolation (#144)
# ===========================================================================


def test_pending_ask_scoped_by_channel() -> None:
    """Pending ask in chat A should NOT be returned for chat B."""
    _PENDING_ASK_REQUESTS["req-x"] = (CHAT_A, "Question for A")
    assert get_pending_ask_request(channel_id=CHAT_A) is not None
    assert get_pending_ask_request(channel_id=CHAT_B) is None


def test_pending_ask_returns_correct_channel() -> None:
    """Each channel should only see its own pending asks."""
    _PENDING_ASK_REQUESTS["req-a"] = (CHAT_A, "Q for A")
    _PENDING_ASK_REQUESTS["req-b"] = (CHAT_B, "Q for B")
    result_a = get_pending_ask_request(channel_id=CHAT_A)
    result_b = get_pending_ask_request(channel_id=CHAT_B)
    assert result_a is not None and result_a[0] == "req-a"
    assert result_b is not None and result_b[0] == "req-b"


def test_ask_flow_scoped_by_channel() -> None:
    """Ask question flow in chat A should NOT be returned for chat B."""
    flow = AskQuestionState(
        request_id="req-flow-a",
        channel_id=CHAT_A,
        questions=[{"question": "Q?", "options": [{"label": "X"}]}],
    )
    _ASK_QUESTION_FLOWS["req-flow-a"] = flow
    assert get_ask_question_flow(channel_id=CHAT_A) is flow
    assert get_ask_question_flow(channel_id=CHAT_B) is None


def test_translate_registers_ask_with_channel_id() -> None:
    """AskUserQuestion should be registered with the current channel_id."""
    state, factory = _make_state_with_session()
    event = _decode_event(
        {
            "type": "control_request",
            "request_id": "req-chan-1",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "AskUserQuestion",
                "input": {"question": "Which?"},
            },
        }
    )
    translate_claude_event(event, title="claude", state=state, factory=factory)
    assert "req-chan-1" in _PENDING_ASK_REQUESTS
    channel_id, question = _PENDING_ASK_REQUESTS["req-chan-1"]
    assert channel_id == CHAT_A
    assert question == "Which?"


# ---------------------------------------------------------------------------
# Regression: #488 — multi-question flow text-reply continuation
# ---------------------------------------------------------------------------


class _RecordingTransport:
    """Minimal Transport stub that records send/edit/delete calls."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, object, object]] = []

    async def send(self, *, channel_id, message, options=None):  # type: ignore[no-untyped-def]
        self.sent.append((channel_id, message, options))
        return

    async def edit(self, ref, message, wait=True):  # type: ignore[no-untyped-def]
        return None

    async def delete(self, ref):  # type: ignore[no-untyped-def]
        return None


@pytest.mark.anyio
async def test_send_next_ask_question_message_uses_rendered_message() -> None:
    """Regression for #488: text-reply continuation must call transport.send
    with a RenderedMessage carrying the inline keyboard, NOT pass it to
    send_plain (which would TypeError on str-only `text` kwarg)."""
    from untether.telegram.commands.ask_question import (
        send_next_ask_question_message,
    )
    from untether.transport import MessageRef, RenderedMessage, SendOptions

    flow = AskQuestionState(
        request_id="req-488",
        channel_id=-12345,
        questions=[
            {
                "question": "First?",
                "options": [{"label": "A"}, {"label": "B"}],
            },
            {
                "question": "Second?",
                "options": [{"label": "C"}, {"label": "D"}],
            },
        ],
        current_index=1,  # user already answered Q1 by typing
    )

    transport = _RecordingTransport()

    await send_next_ask_question_message(
        transport,  # type: ignore[arg-type]
        chat_id=-12345,
        user_msg_id=678,
        thread_id=42,
        flow=flow,
    )

    assert len(transport.sent) == 1
    channel_id, message, options = transport.sent[0]
    assert channel_id == -12345
    assert isinstance(message, RenderedMessage)
    assert "2 of 2" in message.text
    assert message.extra is not None
    assert message.extra["parse_mode"] == "HTML"
    assert "inline_keyboard" in message.extra["reply_markup"]
    # Buttons present for question 2's options:
    keyboard = message.extra["reply_markup"]["inline_keyboard"]
    assert len(keyboard) >= 1
    assert isinstance(options, SendOptions)
    assert options.reply_to == MessageRef(channel_id=-12345, message_id=678)
    assert options.thread_id == 42


@pytest.mark.anyio
async def test_send_next_ask_question_message_no_thread() -> None:
    """thread_id=None passes through to SendOptions (private chats / non-forum groups)."""
    from untether.telegram.commands.ask_question import (
        send_next_ask_question_message,
    )

    flow = AskQuestionState(
        request_id="req-488-b",
        channel_id=-9999,
        questions=[
            {"question": "Q1", "options": [{"label": "A"}]},
            {"question": "Q2", "options": [{"label": "B"}]},
        ],
        current_index=1,
    )
    transport = _RecordingTransport()

    await send_next_ask_question_message(
        transport,  # type: ignore[arg-type]
        chat_id=-9999,
        user_msg_id=1,
        thread_id=None,
        flow=flow,
    )

    _, _, options = transport.sent[0]
    assert options.thread_id is None


# ---------------------------------------------------------------------------
# #550 — AskQuestionCommand.handle clears inline keyboard on final answer
# ---------------------------------------------------------------------------


def _make_command_ctx(args_text: str, *, channel_id: int = CHAT_A):
    """Build a minimal CommandContext-like mock for AskQuestionCommand.handle tests.

    #715: ``ctx.message.channel_id`` must be a real int — the handler now
    scopes its flow lookup by it, and a bare ``MagicMock`` attribute would
    silently match no flow.
    """
    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.args_text = args_text
    ctx.executor = AsyncMock()
    ctx.executor.edit = AsyncMock(return_value=None)
    ctx.message = MagicMock()
    ctx.message.channel_id = channel_id
    return ctx


@pytest.mark.anyio
async def test_550_final_answer_clears_inline_keyboard(monkeypatch) -> None:
    """#550: After the user answers the last question in a multi-Q flow, the
    inline keyboard on the question message must be stripped via executor.edit."""
    from untether.telegram.commands import ask_question as cmd_mod
    from untether.transport import RenderedMessage

    flow = AskQuestionState(
        request_id="req-550-a",
        channel_id=CHAT_A,
        questions=[
            {"question": "First?", "options": [{"label": "A"}, {"label": "B"}]},
            {"question": "Second?", "options": [{"label": "C"}, {"label": "D"}]},
        ],
        current_index=1,  # already answered Q1, about to answer Q2
        answers={"First?": "A"},
    )
    _ASK_QUESTION_FLOWS[flow.request_id] = flow

    async def _fake_answer(rid: str) -> bool:
        # Mirror real cleanup so subsequent get_ask_question_flow() returns None.
        _ASK_QUESTION_FLOWS.pop(rid, None)
        return True

    monkeypatch.setattr(
        "untether.runners.claude.answer_ask_question_with_options",
        _fake_answer,
    )

    ctx = _make_command_ctx("opt:0")  # final answer = option 0 ("C")
    result = await cmd_mod.AskQuestionCommand().handle(ctx)

    # Exactly one edit call with empty inline_keyboard.
    assert ctx.executor.edit.await_count == 1
    edit_args, edit_kwargs = ctx.executor.edit.await_args
    # First positional is ctx.message, second is the cleared RenderedMessage.
    assert edit_args[0] is ctx.message
    cleared = edit_args[1]
    assert isinstance(cleared, RenderedMessage)
    assert cleared.extra["reply_markup"]["inline_keyboard"] == []

    # The CommandResult still includes the Q&A summary toast.
    assert result is not None
    assert "Answers sent" in result.text
    assert "First?" in result.text and "Second?" in result.text


@pytest.mark.anyio
async def test_550_keyboard_not_cleared_when_answer_fails(monkeypatch) -> None:
    """#550: If answer_ask_question_with_options returns False (session ended),
    leave the buttons in place so the user knows the answer didn't land."""
    from untether.telegram.commands import ask_question as cmd_mod

    flow = AskQuestionState(
        request_id="req-550-b",
        channel_id=CHAT_A,
        questions=[
            {"question": "Q1", "options": [{"label": "A"}]},
            {"question": "Q2", "options": [{"label": "B"}]},
        ],
        current_index=1,
        answers={"Q1": "A"},
    )
    _ASK_QUESTION_FLOWS[flow.request_id] = flow

    async def _fail_answer(rid: str) -> bool:
        _ASK_QUESTION_FLOWS.pop(rid, None)
        return False

    monkeypatch.setattr(
        "untether.runners.claude.answer_ask_question_with_options",
        _fail_answer,
    )

    ctx = _make_command_ctx("opt:0")
    result = await cmd_mod.AskQuestionCommand().handle(ctx)

    ctx.executor.edit.assert_not_awaited()
    assert result is not None
    assert "Failed to send answers" in result.text


@pytest.mark.anyio
async def test_550_edit_failure_does_not_block_answer(monkeypatch, capsys) -> None:
    """#550: If executor.edit raises (e.g. message-not-found), the warning is
    logged but the answer-sent CommandResult is still returned."""
    from untether.telegram.commands import ask_question as cmd_mod

    flow = AskQuestionState(
        request_id="req-550-c",
        channel_id=CHAT_A,
        questions=[
            {"question": "Q1", "options": [{"label": "A"}]},
            {"question": "Q2", "options": [{"label": "B"}]},
        ],
        current_index=1,
        answers={"Q1": "A"},
    )
    _ASK_QUESTION_FLOWS[flow.request_id] = flow

    async def _ok_answer(rid: str) -> bool:
        _ASK_QUESTION_FLOWS.pop(rid, None)
        return True

    monkeypatch.setattr(
        "untether.runners.claude.answer_ask_question_with_options",
        _ok_answer,
    )

    ctx = _make_command_ctx("opt:0")
    ctx.executor.edit = AsyncMock(side_effect=RuntimeError("message not found"))

    result = await cmd_mod.AskQuestionCommand().handle(ctx)

    assert ctx.executor.edit.await_count == 1
    assert result is not None
    assert "Answers sent" in result.text
    # Warning was logged (structlog routes the event name to stdout in tests).
    out = capsys.readouterr().out
    assert "ask_question.keyboard_clear_failed" in out


@pytest.mark.anyio
async def test_550_multi_question_edits_twice(monkeypatch) -> None:
    """#550: 2-question flow: Q1->Q2 edit fires the question swap (existing
    behavior), then Q2 final answer fires the keyboard-clear edit (new)."""
    from untether.telegram.commands import ask_question as cmd_mod
    from untether.transport import RenderedMessage

    flow = AskQuestionState(
        request_id="req-550-d",
        channel_id=CHAT_A,
        questions=[
            {"question": "Q1", "options": [{"label": "A"}, {"label": "B"}]},
            {"question": "Q2", "options": [{"label": "C"}, {"label": "D"}]},
        ],
        current_index=0,
        answers={},
    )
    _ASK_QUESTION_FLOWS[flow.request_id] = flow

    async def _ok_answer(rid: str) -> bool:
        _ASK_QUESTION_FLOWS.pop(rid, None)
        return True

    monkeypatch.setattr(
        "untether.runners.claude.answer_ask_question_with_options",
        _ok_answer,
    )

    # Click Q1 option 0 -> Q1->Q2 transition edit
    ctx1 = _make_command_ctx("opt:0")
    result1 = await cmd_mod.AskQuestionCommand().handle(ctx1)
    assert result1 is None  # transition returns None
    assert ctx1.executor.edit.await_count == 1
    transition_msg = ctx1.executor.edit.await_args[0][1]
    assert isinstance(transition_msg, RenderedMessage)
    # Q2 question buttons present (non-empty keyboard)
    assert transition_msg.extra["reply_markup"]["inline_keyboard"]

    # Click Q2 option 0 -> final clear edit
    ctx2 = _make_command_ctx("opt:0")
    result2 = await cmd_mod.AskQuestionCommand().handle(ctx2)
    assert result2 is not None  # final returns CommandResult
    assert ctx2.executor.edit.await_count == 1
    cleared_msg = ctx2.executor.edit.await_args[0][1]
    assert cleared_msg.extra["reply_markup"]["inline_keyboard"] == []


# ---------------------------------------------------------------------------
# #698 — a late option tap after the flow is torn down
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_698_late_tap_reports_already_answered(monkeypatch) -> None:
    """#698: #550's keyboard strip is an async outbox edit issued *after* the
    flow is torn down, so a tap landing in the gap (1s on nsd, wider in a busy
    group chat) hit ``flow is None`` — WARNING plus a success toast for a tap
    that did nothing. The answered flow is now remembered briefly, so the late
    tap resolves to a truthful "Already answered".
    """
    from structlog.testing import capture_logs

    from untether.telegram.commands import ask_question as cmd_mod

    flow = AskQuestionState(
        request_id="req-698-a",
        channel_id=CHAT_A,
        questions=[{"question": "Q1", "options": [{"label": "A"}, {"label": "B"}]}],
    )
    _ASK_QUESTION_FLOWS[flow.request_id] = flow
    _ACTIVE_RUNNERS["sess-698a"] = (AsyncMock(), 0.0)
    _REQUEST_TO_SESSION[flow.request_id] = "sess-698a"

    ctx = _make_command_ctx("opt:0")
    with capture_logs() as logs:
        first = await cmd_mod.AskQuestionCommand().handle(ctx)
        # The user taps a second option before the keyboard-clear edit lands.
        late = await cmd_mod.AskQuestionCommand().handle(_make_command_ctx("opt:1"))

    assert first is not None and "Answers sent" in first.text
    assert late is not None
    assert late.text == "Already answered"

    assert [r for r in logs if r.get("event") == "ask_question.flow_missing"] == []
    already = [
        r for r in logs if r.get("event") == "ask_question.flow_already_answered"
    ]
    assert len(already) == 1
    assert already[0]["log_level"] == "info"
    assert already[0]["action"] == "opt"
    assert already[0]["request_id"] == "req-698-a"


@pytest.mark.anyio
async def test_698_late_tap_toast_is_truthful() -> None:
    """The early-answer toast fires before ``handle`` runs, so it is the only
    thing the user sees on a late tap. It must not claim "Selected" for a tap
    that did nothing (same user-visible defect as #685)."""
    from untether.telegram.commands import ask_question as cmd_mod

    backend = cmd_mod.AskQuestionCommand()
    assert backend.early_answer_toast("opt:0") == "Selected"

    _record_answered_ask_flow("req-698-b", CHAT_A)
    assert backend.early_answer_toast("opt:0") == "Already answered"
    assert backend.early_answer_toast("other") == "Already answered"


@pytest.mark.anyio
async def test_698_unknown_tap_still_warns() -> None:
    """A tap with no flow and no recent answer is genuinely unexplained — keep
    the WARNING so it stays visible in warn-level triage."""
    from structlog.testing import capture_logs

    from untether.telegram.commands import ask_question as cmd_mod

    with capture_logs() as logs:
        result = await cmd_mod.AskQuestionCommand().handle(_make_command_ctx("opt:0"))

    assert result is not None
    assert result.text == "No active question"
    missing = [r for r in logs if r.get("event") == "ask_question.flow_missing"]
    assert len(missing) == 1
    assert missing[0]["log_level"] == "warning"


@pytest.mark.anyio
async def test_698_answered_flow_recorded_with_channel() -> None:
    """``answer_ask_question_with_options`` records the answered flow, scoped by
    channel so a late tap in one chat cannot claim another chat's answer."""
    flow = AskQuestionState(
        request_id="req-698-c",
        channel_id=CHAT_A,
        questions=[{"question": "Q1", "options": [{"label": "A"}]}],
        answers={"Q1": "A"},
    )
    _ASK_QUESTION_FLOWS[flow.request_id] = flow
    mock_runner = AsyncMock()
    mock_runner.write_control_response.return_value = True
    _ACTIVE_RUNNERS["sess-698c"] = (mock_runner, 0.0)
    _REQUEST_TO_SESSION[flow.request_id] = "sess-698c"

    assert await answer_ask_question_with_options(flow.request_id) is True

    assert recently_answered_ask_flow(CHAT_A) == "req-698-c"
    assert recently_answered_ask_flow(CHAT_B) is None
    assert recently_answered_ask_flow() == "req-698-c"


def test_698_answered_memo_expires_and_is_bounded(monkeypatch) -> None:
    """The memo is TTL-pruned (a tap hours later is not "recently answered")
    and hard-capped so a long-lived process cannot accumulate entries."""
    import untether.runners.claude as claude_mod

    fake_now = 1000.0
    monkeypatch.setattr(claude_mod.time, "monotonic", lambda: fake_now)

    _record_answered_ask_flow("req-ttl", CHAT_A)
    assert recently_answered_ask_flow(CHAT_A) == "req-ttl"

    fake_now += claude_mod.ANSWERED_ASK_FLOW_TTL_S + 1.0
    assert recently_answered_ask_flow(CHAT_A) is None

    for i in range(claude_mod._ANSWERED_ASK_FLOWS_MAX + 10):
        _record_answered_ask_flow(f"req-{i}", CHAT_A)
    assert len(_ANSWERED_ASK_FLOWS) <= claude_mod._ANSWERED_ASK_FLOWS_MAX
    # Oldest evicted first, newest retained.
    assert "req-0" not in _ANSWERED_ASK_FLOWS
    assert f"req-{claude_mod._ANSWERED_ASK_FLOWS_MAX + 9}" in _ANSWERED_ASK_FLOWS


# ---------------------------------------------------------------------------
# #709 — the heartbeat re-render must not clobber the in-place question edit
# ---------------------------------------------------------------------------


def _tracked_ask_action(request_id: str, *, title: str, buttons):
    """Build a ProgressTracker holding one AskUserQuestion control action and
    bind it the way ProgressEdits.on_event does."""
    from untether.model import Action, ActionEvent
    from untether.progress import ProgressTracker
    from untether.runner_bridge import register_ask_action_model

    tracker = ProgressTracker(engine="claude")
    action = Action(
        id="claude.control.1",
        kind="warning",
        title=title,
        detail={
            "request_id": request_id,
            "request_type": "can_use_tool",
            "ask_flow": True,
            "inline_keyboard": {"buttons": buttons},
        },
    )
    tracker.note_event(
        ActionEvent(engine="claude", action=action, phase="started", ok=None)
    )
    register_ask_action_model(request_id, tracker, "claude.control.1")
    return tracker


def _rendered_keyboard(tracker):
    """The buttons the progress renderer's newest-first scan would emit."""
    for action_state in reversed(tracker.snapshot().actions):
        if action_state.completed:
            continue
        kb = action_state.action.detail.get("inline_keyboard")
        if kb and isinstance(kb, dict) and "buttons" in kb:
            return [row[0]["text"] for row in kb["buttons"]]
    return None


@pytest.mark.anyio
async def test_709_answering_q1_advances_the_tracked_action(monkeypatch) -> None:
    """#709: answering Q1 edits the message to Q2 — the tracked action must
    move with it, or the next 30s heartbeat re-renders Q1's title and Q1's
    option labels over the top while Q2 is outstanding."""
    from untether.telegram.commands import ask_question as cmd_mod

    flow = AskQuestionState(
        request_id="req-709-a",
        channel_id=CHAT_A,
        questions=[
            {
                "question": "QA colour?",
                "options": [{"label": "Red"}, {"label": "Blue"}],
            },
            {
                "question": "QA size?",
                "options": [{"label": "Small"}, {"label": "Large"}],
            },
        ],
    )
    _ASK_QUESTION_FLOWS[flow.request_id] = flow
    tracker = _tracked_ask_action(
        flow.request_id,
        title="❓ Question 1 of 2: QA colour?",
        buttons=[
            [{"text": "Red", "callback_data": "aq:opt:0"}],
            [{"text": "Blue", "callback_data": "aq:opt:1"}],
            [{"text": "Other (type reply)", "callback_data": "aq:other"}],
        ],
    )
    # Pre-condition: the model is showing Q1.
    assert _rendered_keyboard(tracker) == ["Red", "Blue", "Other (type reply)"]

    await cmd_mod.AskQuestionCommand().handle(_make_command_ctx("opt:0"))

    # Post-condition: the model is showing Q2 — so a heartbeat re-render is a
    # no-op instead of the regression this issue reported.
    action = tracker.snapshot().actions[0].action
    assert action.title == "❓ Question 2 of 2: QA size?"
    assert _rendered_keyboard(tracker) == ["Small", "Large", "Other (type reply)"]


@pytest.mark.anyio
async def test_709_final_answer_drops_the_keyboard_from_the_model(
    monkeypatch,
) -> None:
    """The #550 keyboard strip becomes a model change, so a heartbeat landing
    mid-teardown cannot repaint the answered question's buttons."""
    from untether.telegram.commands import ask_question as cmd_mod

    flow = AskQuestionState(
        request_id="req-709-b",
        channel_id=CHAT_A,
        questions=[{"question": "Only?", "options": [{"label": "A"}]}],
    )
    _ASK_QUESTION_FLOWS[flow.request_id] = flow
    tracker = _tracked_ask_action(
        flow.request_id,
        title="❓ Only?",
        buttons=[[{"text": "A", "callback_data": "aq:opt:0"}]],
    )

    async def _fake_answer(rid: str) -> bool:
        _ASK_QUESTION_FLOWS.pop(rid, None)
        return True

    monkeypatch.setattr(
        "untether.runners.claude.answer_ask_question_with_options", _fake_answer
    )

    await cmd_mod.AskQuestionCommand().handle(_make_command_ctx("opt:0"))

    assert _rendered_keyboard(tracker) is None
    assert tracker.snapshot().actions[0].action.title == "✅ All questions answered"


@pytest.mark.anyio
async def test_709_untracked_flow_still_edits(monkeypatch) -> None:
    """No registry entry (e.g. a non-Claude presenter path) must degrade to the
    plain edit, never raise."""
    from untether.telegram.commands import ask_question as cmd_mod

    flow = AskQuestionState(
        request_id="req-709-c",
        channel_id=CHAT_A,
        questions=[
            {"question": "Q1", "options": [{"label": "A"}]},
            {"question": "Q2", "options": [{"label": "B"}]},
        ],
    )
    _ASK_QUESTION_FLOWS[flow.request_id] = flow

    ctx = _make_command_ctx("opt:0")
    assert await cmd_mod.AskQuestionCommand().handle(ctx) is None
    assert ctx.executor.edit.await_count == 1


def test_709_claude_marks_ask_actions_with_ask_flow() -> None:
    """The bridge binds on ``detail["ask_flow"]`` — set only when an
    AskQuestionState was created, i.e. when the `aq` handler drives the
    action. ``ask_question`` alone is absent when extraction yields "".
    """
    state, factory = _make_state_with_session()
    event = _decode_event(
        {
            "type": "control_request",
            "request_id": "req-709-d",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "AskUserQuestion",
                "input": {
                    "questions": [{"question": "Pick?", "options": [{"label": "A"}]}]
                },
            },
        }
    )
    events = translate_claude_event(event, title="claude", state=state, factory=factory)
    detail = events[-1].action.detail
    assert detail["ask_flow"] is True
    assert detail["request_id"] == "req-709-d"


# ---------------------------------------------------------------------------
# #710 — concurrent taps on the final option must not IndexError
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_710_concurrent_final_taps_do_not_raise(monkeypatch) -> None:
    """#710: callback A increments the index then suspends on the answer
    await; callback B lands inside that window and read
    ``flow.questions[flow.current_index]`` unguarded → IndexError, surfacing
    as an ERROR traceback (`callback.failed`, a watcher-tracked signature)
    plus a failure toast for an action that in fact succeeded."""
    import anyio

    from untether.telegram.commands import ask_question as cmd_mod

    flow = AskQuestionState(
        request_id="req-710-a",
        channel_id=CHAT_A,
        questions=[{"question": "Only?", "options": [{"label": "A"}, {"label": "B"}]}],
    )
    _ASK_QUESTION_FLOWS[flow.request_id] = flow

    gate = anyio.Event()

    async def _slow_answer(rid: str) -> bool:
        # Hold the flow alive inside the await, exactly as the real control
        # response does while it round-trips to the subprocess.
        await gate.wait()
        _ASK_QUESTION_FLOWS.pop(rid, None)
        return True

    monkeypatch.setattr(
        "untether.runners.claude.answer_ask_question_with_options", _slow_answer
    )

    results: list[object] = []

    async def _tap(args_text: str) -> None:
        results.append(
            await cmd_mod.AskQuestionCommand().handle(_make_command_ctx(args_text))
        )

    async with anyio.create_task_group() as tg:
        tg.start_soon(_tap, "opt:0")
        await anyio.lowlevel.checkpoint()  # let A reach the await
        tg.start_soon(_tap, "opt:1")
        await anyio.lowlevel.checkpoint()
        gate.set()

    texts = [getattr(r, "text", None) for r in results]
    assert "Already answered" in texts
    assert any(t and "Answers sent" in t for t in texts)


@pytest.mark.anyio
async def test_710_out_of_range_index_reports_already_answered() -> None:
    """The bounds check reuses #698's vocabulary, so both orderings of a
    double-tap produce the same truthful outcome."""
    from structlog.testing import capture_logs

    from untether.telegram.commands import ask_question as cmd_mod

    flow = AskQuestionState(
        request_id="req-710-b",
        channel_id=CHAT_A,
        questions=[{"question": "Only?", "options": [{"label": "A"}]}],
        current_index=1,  # already past the end
        answers={"Only?": "A"},
    )
    _ASK_QUESTION_FLOWS[flow.request_id] = flow

    with capture_logs() as logs:
        result = await cmd_mod.AskQuestionCommand().handle(_make_command_ctx("opt:0"))

    assert result is not None
    assert result.text == "Already answered"
    already = [
        r for r in logs if r.get("event") == "ask_question.flow_already_answered"
    ]
    assert len(already) == 1
    assert already[0]["log_level"] == "info"
    assert already[0]["request_id"] == "req-710-b"


# ---------------------------------------------------------------------------
# #713 — the two call sites must escape, and only at the HTML boundary
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_713_in_place_edit_escapes_but_model_title_stays_raw() -> None:
    """The in-place Q2 edit is sent with ``parse_mode="HTML"``, so its text
    must be escaped — while the SAME string stored on the tracked action
    (#709) must stay raw, because that one renders through
    ``render_markdown``. Escaping both would show a literal ``&lt;svg&gt;``.
    """
    from untether.telegram.commands import ask_question as cmd_mod

    flow = AskQuestionState(
        request_id="req-713-a",
        channel_id=CHAT_A,
        questions=[
            {"question": "Q1?", "options": [{"label": "A"}, {"label": "B"}]},
            {
                "question": "Guard a blank line inside an inline `<svg>`?",
                "options": [{"label": "Yes"}, {"label": "No"}],
            },
        ],
    )
    _ASK_QUESTION_FLOWS[flow.request_id] = flow
    tracker = _tracked_ask_action(
        flow.request_id,
        title="❓ Question 1 of 2: Q1?",
        buttons=[[{"text": "A", "callback_data": "aq:opt:0"}]],
    )

    ctx = _make_command_ctx("opt:0")
    await cmd_mod.AskQuestionCommand().handle(ctx)

    # The wire message: escaped, because parse_mode is HTML.
    assert ctx.executor.edit.await_count == 1
    sent = ctx.executor.edit.await_args[0][1]
    assert sent.extra["parse_mode"] == "HTML"
    assert "&lt;svg&gt;" in sent.text
    assert "<svg>" not in sent.text

    # The progress model: raw, because it renders through markdown.
    assert "<svg>" in tracker.snapshot().actions[0].action.title


@pytest.mark.anyio
async def test_713_send_next_question_escapes() -> None:
    """The "Other → typed reply" continuation path sends (not edits) the next
    question under ``parse_mode="HTML"`` and needs the same escaping."""
    from untether.telegram.commands import ask_question as cmd_mod

    flow = AskQuestionState(
        request_id="req-713-b",
        channel_id=CHAT_A,
        questions=[{"question": "Ship <b>A</b> & B?", "options": [{"label": "Yes"}]}],
    )
    transport = AsyncMock()

    await cmd_mod.send_next_ask_question_message(
        transport,
        chat_id=CHAT_A,
        user_msg_id=1,
        thread_id=None,
        flow=flow,
    )

    assert transport.send.await_count == 1
    message = transport.send.await_args.kwargs["message"]
    assert message.extra["parse_mode"] == "HTML"
    assert "&lt;b&gt;A&lt;/b&gt;" in message.text
    assert "&amp;" in message.text


# ── #715: option taps must be channel-scoped ──


@pytest.mark.anyio
async def test_715_option_tap_answers_its_own_chats_flow(monkeypatch) -> None:
    """Two concurrent AskUserQuestion flows in different chats; a tap in the
    SECOND chat must answer the second flow and leave the first untouched.

    Before the fix the handler called ``get_ask_question_flow()`` with no
    scope, and the resolver returns the FIRST flow in the registry when
    ``channel_id`` is None — so chat B's tap was recorded against chat A's
    question. Callback data is positional (``aq:opt:N``), so nothing failed:
    the wrong question was silently answered with the option at that index.
    """
    from untether.runners import claude as claude_mod
    from untether.telegram.commands import ask_question as cmd_mod

    flow_a = AskQuestionState(
        request_id="req-715-a",
        channel_id=CHAT_A,
        questions=[
            {
                "question": "Deploy to prod?",
                "options": [{"label": "Yes"}, {"label": "No"}],
            }
        ],
    )
    flow_b = AskQuestionState(
        request_id="req-715-b",
        channel_id=CHAT_B,
        questions=[
            {
                "question": "Delete the branch?",
                "options": [{"label": "Keep"}, {"label": "Delete"}],
            }
        ],
    )
    # A is inserted first, so it is what an unscoped lookup would return.
    _ASK_QUESTION_FLOWS[flow_a.request_id] = flow_a
    _ASK_QUESTION_FLOWS[flow_b.request_id] = flow_b

    answered: list[str] = []

    async def fake_answer(request_id: str) -> bool:
        answered.append(request_id)
        _ASK_QUESTION_FLOWS.pop(request_id, None)
        return True

    # `handle` imports the symbol from the runner module at call time, so
    # patch it there rather than on the command module.
    monkeypatch.setattr(
        claude_mod, "answer_ask_question_with_options", fake_answer, raising=True
    )

    # Tap option 1 in chat B — "Delete".
    ctx = _make_command_ctx("opt:1", channel_id=CHAT_B)
    await cmd_mod.AskQuestionCommand().handle(ctx)

    assert answered == ["req-715-b"], "the tap must answer the tapping chat's flow"
    assert flow_b.answers == {"Delete the branch?": "Delete"}
    # Chat A's flow is untouched: still live, no answer recorded.
    assert flow_a.answers == {}
    assert flow_a.current_index == 0
    assert _ASK_QUESTION_FLOWS.get("req-715-a") is flow_a


@pytest.mark.anyio
async def test_715_tap_in_chat_with_no_flow_does_not_steal_another(monkeypatch) -> None:
    """A tap from a chat with no outstanding question must report "no active
    question" rather than answering whichever flow happens to be first."""
    from untether.telegram.commands import ask_question as cmd_mod

    flow_a = AskQuestionState(
        request_id="req-715-c",
        channel_id=CHAT_A,
        questions=[
            {"question": "Proceed?", "options": [{"label": "Yes"}, {"label": "No"}]}
        ],
    )
    _ASK_QUESTION_FLOWS[flow_a.request_id] = flow_a

    ctx = _make_command_ctx("opt:0", channel_id=CHAT_B)
    result = await cmd_mod.AskQuestionCommand().handle(ctx)

    assert result is not None
    assert result.text == "No active question"
    assert flow_a.answers == {}
    assert _ASK_QUESTION_FLOWS.get("req-715-c") is flow_a


def test_715_early_toast_is_channel_scoped() -> None:
    """The pre-``handle`` toast is chosen from per-chat registry state, so it
    must be scoped too — otherwise a chat with no question of its own reads
    another chat's live flow and toasts "Selected" for a no-op tap."""
    from untether.telegram.commands.ask_question import AskQuestionCommand

    flow_a = AskQuestionState(
        request_id="req-715-d",
        channel_id=CHAT_A,
        questions=[{"question": "Go?", "options": [{"label": "Yes"}]}],
    )
    _ASK_QUESTION_FLOWS[flow_a.request_id] = flow_a
    # Chat B answered a flow a moment ago and has nothing live.
    _record_answered_ask_flow("req-715-e", CHAT_B)

    assert (
        AskQuestionCommand.early_answer_toast("opt:0", channel_id=CHAT_B)
        == "Already answered"
    )
    # Chat A has a live flow, so it gets the normal selection toast.
    assert (
        AskQuestionCommand.early_answer_toast("opt:0", channel_id=CHAT_A) == "Selected"
    )


def test_715_dispatch_hook_falls_back_to_legacy_signature() -> None:
    """``early_answer_toast`` is a duck-typed internal hook, not part of the
    ``CommandBackend`` Protocol. A backend still carrying the old
    ``(args_text)`` signature must degrade to that call rather than raising a
    TypeError out of dispatch — which would kill the whole callback."""
    from untether.telegram.commands.dispatch import _early_answer_toast

    class LegacyBackend:
        answer_early = True

        @staticmethod
        def early_answer_toast(args_text: str) -> str | None:
            return f"legacy:{args_text}"

    class ScopedBackend:
        answer_early = True

        @staticmethod
        def early_answer_toast(args_text: str, *, channel_id: int | None = None):
            return f"scoped:{args_text}:{channel_id}"

    class NoHookBackend:
        answer_early = True

    assert _early_answer_toast(LegacyBackend(), "opt:0", CHAT_A) == "legacy:opt:0"
    assert (
        _early_answer_toast(ScopedBackend(), "opt:0", CHAT_A)
        == f"scoped:opt:0:{CHAT_A}"
    )
    assert _early_answer_toast(NoHookBackend(), "opt:0", CHAT_A) is None
