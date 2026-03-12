"""Tests for the Pause & Outline Plan cooldown bypass mechanism.

When the user clicks "Pause & Outline Plan", a cooldown is set.
Subsequent ExitPlanMode calls are handled differently depending on
whether Claude Code has written substantial outline text (>= 200 chars):

- With outline: hold request open + synthetic Approve/Deny buttons (real request_id)
- Without outline: auto-deny with escalation message + synthetic Approve/Deny buttons (da: prefix)
"""

from __future__ import annotations

import pytest

from unittest.mock import AsyncMock

from untether.model import ActionEvent, ResumeToken
from untether.runners.claude import (
    ClaudeRunner,
    ClaudeStreamState,
    _ACTIVE_RUNNERS,
    _DISCUSS_APPROVED,
    _DISCUSS_COOLDOWN,
    _OUTLINE_PENDING,
    _REQUEST_TO_INPUT,
    _REQUEST_TO_SESSION,
    _REQUEST_TO_TOOL_NAME,
    _SESSION_STDIN,
    _OUTLINE_MIN_CHARS,
    set_discuss_cooldown,
    translate_claude_event,
)
from untether.schemas import claude as claude_schema


@pytest.fixture(autouse=True)
def _clear_registries():
    """Clear global registries before each test."""
    _DISCUSS_COOLDOWN.clear()
    _DISCUSS_APPROVED.clear()
    _OUTLINE_PENDING.clear()
    _REQUEST_TO_SESSION.clear()
    _REQUEST_TO_INPUT.clear()
    _REQUEST_TO_TOOL_NAME.clear()
    _ACTIVE_RUNNERS.clear()
    _SESSION_STDIN.clear()
    yield
    _DISCUSS_COOLDOWN.clear()
    _DISCUSS_APPROVED.clear()
    _OUTLINE_PENDING.clear()
    _REQUEST_TO_SESSION.clear()
    _REQUEST_TO_INPUT.clear()
    _REQUEST_TO_TOOL_NAME.clear()
    _ACTIVE_RUNNERS.clear()
    _SESSION_STDIN.clear()


def _make_resume(session_id: str) -> ResumeToken:
    return ResumeToken(engine="claude", value=session_id)


def _make_state(session_id: str) -> ClaudeStreamState:
    state = ClaudeStreamState()
    state.factory._resume = _make_resume(session_id)
    return state


def _make_exit_plan_mode_request(
    request_id: str = "req_1",
) -> claude_schema.StreamControlRequest:
    """Create a fake ExitPlanMode control request."""
    return claude_schema.StreamControlRequest(
        request_id=request_id,
        request=claude_schema.ControlCanUseToolRequest(
            tool_name="ExitPlanMode",
            input={},
        ),
    )


def _make_text_block(text: str) -> claude_schema.StreamAssistantMessage:
    """Create a StreamAssistantMessage containing a text block."""
    return claude_schema.StreamAssistantMessage(
        message=claude_schema.StreamAssistantMessageBody(
            role="assistant",
            content=[claude_schema.StreamTextBlock(text=text)],
            model="claude-sonnet-4-20250514",
        )
    )


# --- Hold-open path: outline written (text >= 200 chars) ---


def test_outline_ready_holds_request_open():
    """ExitPlanMode after outline text should hold request open, not auto-deny.

    The hold-open path returns early (before the normal registration at ~line 779),
    so it must register _REQUEST_TO_SESSION / _REQUEST_TO_INPUT / _REQUEST_TO_TOOL_NAME
    itself.  This test does NOT pre-set those mappings to verify the code creates them.
    """
    state = _make_state("sess-1")
    set_discuss_cooldown("sess-1")
    state.last_assistant_text = "x" * 300
    state.max_text_len_since_cooldown = 300

    request_id = "req_exit_plan"
    # Do NOT pre-set _REQUEST_TO_SESSION etc. — the hold-open path must register them.

    event = _make_exit_plan_mode_request(request_id)
    translate_claude_event(event, title="claude", state=state, factory=state.factory)

    # Request should be held open (pending), NOT auto-denied
    assert len(state.auto_deny_queue) == 0
    assert request_id in state.pending_control_requests
    # Hold-open path must register session/input/tool-name for callback routing
    assert _REQUEST_TO_SESSION[request_id] == "sess-1"
    assert _REQUEST_TO_TOOL_NAME[request_id] == "ExitPlanMode"
    assert request_id in _REQUEST_TO_INPUT
    # Counter should be reset after hold-open
    assert state.max_text_len_since_cooldown == 0


def test_outline_ready_buttons_use_real_request_id():
    """Outline-ready path should use the real request_id in button callbacks."""
    state = _make_state("sess-2")
    set_discuss_cooldown("sess-2")
    state.max_text_len_since_cooldown = 300

    request_id = "req_exit_plan"
    _REQUEST_TO_SESSION[request_id] = "sess-2"
    _REQUEST_TO_INPUT[request_id] = {}

    event = _make_exit_plan_mode_request(request_id)
    events = translate_claude_event(
        event, title="claude", state=state, factory=state.factory
    )

    # Should return a synthetic action with inline keyboard
    action_events = [e for e in events if isinstance(e, ActionEvent)]
    assert len(action_events) == 1
    detail = action_events[0].action.detail
    assert detail["request_type"] == "DiscussApproval"
    buttons = detail["inline_keyboard"]["buttons"]
    # Only 1 row with 2 buttons: Approve Plan, Deny
    assert len(buttons) == 1
    assert len(buttons[0]) == 2
    assert buttons[0][0]["text"] == "Approve Plan"
    assert buttons[0][1]["text"] == "Deny"
    # Callback data uses REAL request_id (not da: prefix)
    assert buttons[0][0]["callback_data"] == f"claude_control:approve:{request_id}"
    assert buttons[0][1]["callback_data"] == f"claude_control:deny:{request_id}"


def test_bypass_clears_outline_pending():
    """Bypass should clear _OUTLINE_PENDING for the session."""
    state = _make_state("sess-3")
    set_discuss_cooldown("sess-3")
    assert "sess-3" in _OUTLINE_PENDING  # set by set_discuss_cooldown

    state.max_text_len_since_cooldown = 300
    request_id = "req_exit_plan"
    _REQUEST_TO_SESSION[request_id] = "sess-3"
    _REQUEST_TO_INPUT[request_id] = {}

    event = _make_exit_plan_mode_request(request_id)
    translate_claude_event(event, title="claude", state=state, factory=state.factory)

    assert "sess-3" not in _OUTLINE_PENDING


def test_bypass_survives_text_overwrite():
    """Bypass works even if a short text block overwrites the long outline.

    Claude Code may write a 500-char outline in message #1, then a short "Calling
    ExitPlanMode" in message #2.  ``last_assistant_text`` gets overwritten, but
    ``max_text_len_since_cooldown`` preserves the peak length.
    """
    state = _make_state("sess-overwrite")
    set_discuss_cooldown("sess-overwrite")

    # First text block: long outline (500 chars)
    state.last_assistant_text = "x" * 500
    state.max_text_len_since_cooldown = 500

    # Second text block: short message that overwrites last_assistant_text
    state.last_assistant_text = "Calling ExitPlanMode now."
    if len("Calling ExitPlanMode now.") > state.max_text_len_since_cooldown:
        state.max_text_len_since_cooldown = len("Calling ExitPlanMode now.")

    assert state.max_text_len_since_cooldown == 500  # preserved peak
    assert len(state.last_assistant_text) < 200  # current text is short

    request_id = "req_exit_plan"
    _REQUEST_TO_SESSION[request_id] = "sess-overwrite"
    _REQUEST_TO_INPUT[request_id] = {}

    event = _make_exit_plan_mode_request(request_id)
    translate_claude_event(event, title="claude", state=state, factory=state.factory)

    # Should still trigger hold-open (max_text_len_since_cooldown=500 >= 200)
    assert len(state.auto_deny_queue) == 0
    assert request_id in state.pending_control_requests
    assert state.max_text_len_since_cooldown == 0


# --- No-bypass path: no outline written ---


def test_auto_deny_without_outline():
    """ExitPlanMode should be auto-denied when no substantial text was written."""
    state = _make_state("sess-4")
    set_discuss_cooldown("sess-4")
    state.last_assistant_text = "ok"

    request_id = "req_exit_plan"
    _REQUEST_TO_SESSION[request_id] = "sess-4"
    _REQUEST_TO_INPUT[request_id] = {}

    event = _make_exit_plan_mode_request(request_id)
    translate_claude_event(event, title="claude", state=state, factory=state.factory)

    assert len(state.auto_deny_queue) == 1
    assert state.auto_deny_queue[0][0] == request_id


def test_auto_deny_no_text():
    """ExitPlanMode should be auto-denied when no text at all."""
    state = _make_state("sess-5")
    set_discuss_cooldown("sess-5")
    state.last_assistant_text = None

    request_id = "req_exit_plan"
    _REQUEST_TO_SESSION[request_id] = "sess-5"
    _REQUEST_TO_INPUT[request_id] = {}

    event = _make_exit_plan_mode_request(request_id)
    translate_claude_event(event, title="claude", state=state, factory=state.factory)

    assert len(state.auto_deny_queue) == 1


def test_escalation_path_uses_da_prefix():
    """No-outline escalation path should use da: prefix in button callbacks."""
    state = _make_state("sess-esc")
    set_discuss_cooldown("sess-esc")
    state.max_text_len_since_cooldown = 50  # below threshold

    request_id = "req_exit_plan"
    _REQUEST_TO_SESSION[request_id] = "sess-esc"
    _REQUEST_TO_INPUT[request_id] = {}

    event = _make_exit_plan_mode_request(request_id)
    events = translate_claude_event(
        event, title="claude", state=state, factory=state.factory
    )

    action_events = [e for e in events if isinstance(e, ActionEvent)]
    assert len(action_events) == 1
    detail = action_events[0].action.detail
    buttons = detail["inline_keyboard"]["buttons"]
    # Escalation path uses da: prefix
    assert buttons[0][0]["callback_data"].startswith("claude_control:approve:da:")
    assert buttons[0][1]["callback_data"].startswith("claude_control:deny:da:")
    # Should have auto-denied
    assert len(state.auto_deny_queue) == 1


# --- Outline text storage: text block stores on state ---


def test_outline_text_stored_on_state_during_cooldown():
    """StreamTextBlock stores outline text on state when pending and text >= 200 chars."""
    state = _make_state("sess-note")
    set_discuss_cooldown("sess-note")
    assert "sess-note" in _OUTLINE_PENDING

    outline = "A" * 250
    text_event = _make_text_block(outline)
    events = translate_claude_event(
        text_event, title="claude", state=state, factory=state.factory
    )

    # No separate note action emitted — outline is stored on state for embedding
    action_events = [e for e in events if isinstance(e, ActionEvent)]
    assert len(action_events) == 0
    assert state.outline_text == outline


def test_outline_text_not_stored_without_pending():
    """StreamTextBlock should NOT store outline text during normal operation."""
    state = _make_state("sess-normal")
    # No set_discuss_cooldown → _OUTLINE_PENDING is empty

    text_event = _make_text_block("A" * 300)
    translate_claude_event(
        text_event, title="claude", state=state, factory=state.factory
    )

    assert state.outline_text is None


def test_outline_text_not_stored_for_short_text():
    """Short text (< 200 chars) should NOT be stored even when outline is pending."""
    state = _make_state("sess-short")
    set_discuss_cooldown("sess-short")

    text_event = _make_text_block("Short text")
    translate_claude_event(
        text_event, title="claude", state=state, factory=state.factory
    )

    assert state.outline_text is None


def test_outline_embedded_in_synthetic_action():
    """Synthetic Approve/Deny action should include outline text in title."""
    state = _make_state("sess-embed")
    set_discuss_cooldown("sess-embed")

    # Simulate outline capture
    outline = "Step 1: Do X\nStep 2: Do Y\n" * 20  # ~520 chars
    state.outline_text = outline
    state.max_text_len_since_cooldown = len(outline)

    request_id = "req_exit_plan"
    _REQUEST_TO_SESSION[request_id] = "sess-embed"
    _REQUEST_TO_INPUT[request_id] = {}

    event = _make_exit_plan_mode_request(request_id)
    events = translate_claude_event(
        event, title="claude", state=state, factory=state.factory
    )

    action_events = [e for e in events if isinstance(e, ActionEvent)]
    assert len(action_events) == 1
    title = action_events[0].action.title
    assert title.startswith("Plan outline:\n")
    assert "Step 1: Do X" in title
    # Outline text should be cleared after use
    assert state.outline_text is None


def test_outline_truncated_in_synthetic_action():
    """Outline text longer than 1500 chars should be truncated in synthetic action."""
    state = _make_state("sess-trunc")
    set_discuss_cooldown("sess-trunc")

    long_text = "B" * 2000
    state.outline_text = long_text
    state.max_text_len_since_cooldown = len(long_text)

    request_id = "req_exit_plan"
    _REQUEST_TO_SESSION[request_id] = "sess-trunc"
    _REQUEST_TO_INPUT[request_id] = {}

    event = _make_exit_plan_mode_request(request_id)
    events = translate_claude_event(
        event, title="claude", state=state, factory=state.factory
    )

    action_events = [e for e in events if isinstance(e, ActionEvent)]
    assert len(action_events) == 1
    title = action_events[0].action.title
    # "Plan outline:\n" prefix + 1500 chars + "…"
    assert title.startswith("Plan outline:\n")
    assert title.endswith("…")
    assert len(title) < 1520


# --- _OUTLINE_PENDING lifecycle ---


def test_set_discuss_cooldown_adds_outline_pending():
    """set_discuss_cooldown should add session to _OUTLINE_PENDING."""
    set_discuss_cooldown("sess-pending")
    assert "sess-pending" in _OUTLINE_PENDING


def test_outline_min_chars_constant():
    """_OUTLINE_MIN_CHARS should be 200."""
    assert _OUTLINE_MIN_CHARS == 200


# --- Synthetic button after session ends (#50) ---


@pytest.mark.anyio
async def test_synthetic_approve_after_session_ends():
    """Clicking synthetic approve after session ends should return error, not success."""
    from untether.commands import CommandContext
    from untether.telegram.commands.claude_control import ClaudeControlCommand
    from untether.transport import MessageRef

    session_id = "sess-dead"
    synth_request_id = f"da:{session_id}"

    # Register synthetic request but do NOT add to _ACTIVE_RUNNERS (session ended)
    _REQUEST_TO_SESSION[synth_request_id] = session_id

    ctx = CommandContext(
        command="claude_control",
        text=f"claude_control:approve:{synth_request_id}",
        args_text=f"approve:{synth_request_id}",
        args=(f"approve:{synth_request_id}",),
        message=MessageRef(channel_id=123, message_id=1),
        reply_to=None,
        reply_text=None,
        config_path=None,
        plugin_config=None,  # type: ignore[arg-type]
        runtime=None,  # type: ignore[arg-type]
        executor=None,  # type: ignore[arg-type]
    )

    cmd = ClaudeControlCommand()
    result = await cmd.handle(ctx)

    assert result is not None
    assert "Session has ended" in result.text
    # Should NOT be in _DISCUSS_APPROVED
    assert session_id not in _DISCUSS_APPROVED


@pytest.mark.anyio
async def test_synthetic_deny_after_session_ends():
    """Clicking synthetic deny after session ends should return error."""
    from untether.commands import CommandContext
    from untether.telegram.commands.claude_control import ClaudeControlCommand
    from untether.transport import MessageRef

    session_id = "sess-dead-deny"
    synth_request_id = f"da:{session_id}"

    _REQUEST_TO_SESSION[synth_request_id] = session_id
    # No _ACTIVE_RUNNERS entry — session ended

    ctx = CommandContext(
        command="claude_control",
        text=f"claude_control:deny:{synth_request_id}",
        args_text=f"deny:{synth_request_id}",
        args=(f"deny:{synth_request_id}",),
        message=MessageRef(channel_id=123, message_id=1),
        reply_to=None,
        reply_text=None,
        config_path=None,
        plugin_config=None,  # type: ignore[arg-type]
        runtime=None,  # type: ignore[arg-type]
        executor=None,  # type: ignore[arg-type]
    )

    cmd = ClaudeControlCommand()
    result = await cmd.handle(ctx)

    assert result is not None
    assert "Session has ended" in result.text


@pytest.mark.anyio
async def test_synthetic_approve_with_active_session():
    """Clicking synthetic approve with active session should succeed normally."""
    from untether.commands import CommandContext
    from untether.telegram.commands.claude_control import ClaudeControlCommand
    from untether.transport import MessageRef

    runner = ClaudeRunner(claude_cmd="claude")
    session_id = "sess-alive"
    synth_request_id = f"da:{session_id}"

    # Session IS alive
    _ACTIVE_RUNNERS[session_id] = (runner, 0.0)
    _SESSION_STDIN[session_id] = AsyncMock()
    _REQUEST_TO_SESSION[synth_request_id] = session_id

    ctx = CommandContext(
        command="claude_control",
        text=f"claude_control:approve:{synth_request_id}",
        args_text=f"approve:{synth_request_id}",
        args=(f"approve:{synth_request_id}",),
        message=MessageRef(channel_id=123, message_id=1),
        reply_to=None,
        reply_text=None,
        config_path=None,
        plugin_config=None,  # type: ignore[arg-type]
        runtime=None,  # type: ignore[arg-type]
        executor=None,  # type: ignore[arg-type]
    )

    cmd = ClaudeControlCommand()
    result = await cmd.handle(ctx)

    assert result is not None
    assert "Plan approved" in result.text
    assert session_id in _DISCUSS_APPROVED


def test_hold_open_after_cooldown_expires_with_outline():
    """Request should be held open even after cooldown expires.

    Regression test for #114: when cooldown expires before Claude calls
    ExitPlanMode but the outline has been written (text >= 200 chars),
    the code should still enter the hold-open path with synthetic buttons
    — not fall through to the normal 3-button ExitPlanMode flow.
    """
    import time
    from unittest.mock import patch

    state = _make_state("sess-expired")
    set_discuss_cooldown("sess-expired")
    assert "sess-expired" in _OUTLINE_PENDING

    # Simulate outline written
    state.max_text_len_since_cooldown = 400
    state.outline_text = "Step 1: Do this\nStep 2: Do that\n" * 15

    # Advance time past max cooldown (120s)
    with patch.object(time, "time", return_value=time.time() + 200):
        request_id = "req_exit_plan"
        _REQUEST_TO_SESSION[request_id] = "sess-expired"
        _REQUEST_TO_INPUT[request_id] = {}

        event = _make_exit_plan_mode_request(request_id)
        events = translate_claude_event(
            event, title="claude", state=state, factory=state.factory
        )

    # Should still produce synthetic 2-button action (not 3-button)
    action_events = [e for e in events if isinstance(e, ActionEvent)]
    assert len(action_events) == 1
    detail = action_events[0].action.detail
    assert detail["request_type"] == "DiscussApproval"
    buttons = detail["inline_keyboard"]["buttons"]
    assert len(buttons) == 1
    assert len(buttons[0]) == 2
    assert buttons[0][0]["text"] == "Approve Plan"
    assert buttons[0][1]["text"] == "Deny"
    # Request should be held open (not auto-denied)
    assert len(state.auto_deny_queue) == 0
    assert request_id in state.pending_control_requests
    # Buttons should use real request_id
    assert buttons[0][0]["callback_data"] == f"claude_control:approve:{request_id}"
    # _OUTLINE_PENDING should be cleared
    assert "sess-expired" not in _OUTLINE_PENDING


def test_session_cleanup_removes_synthetic_requests():
    """stream_end_events should remove stale _REQUEST_TO_SESSION entries for the session."""
    runner = ClaudeRunner(claude_cmd="claude")
    session_id = "sess-cleanup"
    resume = _make_resume(session_id)

    # Simulate active session with a synthetic request
    _ACTIVE_RUNNERS[session_id] = (runner, 0.0)
    _SESSION_STDIN[session_id] = AsyncMock()
    _REQUEST_TO_SESSION[f"da:{session_id}"] = session_id
    _REQUEST_TO_SESSION["req_normal"] = session_id

    state = _make_state(session_id)
    runner.stream_end_events(resume=resume, found_session=resume, state=state)

    # Both entries should be cleaned up
    assert f"da:{session_id}" not in _REQUEST_TO_SESSION
    assert "req_normal" not in _REQUEST_TO_SESSION
    assert session_id not in _ACTIVE_RUNNERS
