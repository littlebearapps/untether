"""Tests for the Pause & Outline Plan outline-gate mechanism.

When the user clicks "Pause & Outline Plan", the session is marked
outline-pending. Subsequent ExitPlanMode calls are handled differently
depending on whether Claude Code has written substantial outline text
(>= 200 chars):

- With outline: hold request open + synthetic Approve/Deny buttons (real request_id)
- Without outline: auto-deny with the outline instruction + synthetic buttons (da: prefix)

#570: the additional time-based progressive cooldown that used to be armed
here was a workaround for the v2.1.72-74 upstream immediate-retry loop —
verified fixed on CLI 2.1.215 and removed. The text-based gate stays.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from untether.model import ActionEvent, ResumeToken
from untether.runners.claude import (
    _ACTIVE_RUNNERS,
    _DISCUSS_APPROVED,
    _OUTLINE_MIN_CHARS,
    _OUTLINE_PENDING,
    _REQUEST_TO_INPUT,
    _REQUEST_TO_SESSION,
    _REQUEST_TO_TOOL_NAME,
    _SESSION_STDIN,
    ClaudeRunner,
    ClaudeStreamState,
    mark_outline_pending,
    translate_claude_event,
)
from untether.schemas import claude as claude_schema


@pytest.fixture(autouse=True)
def _clear_registries():
    """Clear global registries before each test."""
    _DISCUSS_APPROVED.clear()
    _OUTLINE_PENDING.clear()
    _REQUEST_TO_SESSION.clear()
    _REQUEST_TO_INPUT.clear()
    _REQUEST_TO_TOOL_NAME.clear()
    _ACTIVE_RUNNERS.clear()
    _SESSION_STDIN.clear()
    yield
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
    mark_outline_pending("sess-1")
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
    mark_outline_pending("sess-2")
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
    # 2 rows: [Approve Plan, Deny], [Let's discuss]
    assert len(buttons) == 2
    assert len(buttons[0]) == 2
    assert buttons[0][0]["text"] == "✅ Approve Plan"
    assert buttons[0][1]["text"] == "❌ Deny"
    # Callback data uses REAL request_id (not da: prefix)
    assert buttons[0][0]["callback_data"] == f"claude_control:approve:{request_id}"
    assert buttons[0][1]["callback_data"] == f"claude_control:deny:{request_id}"
    # Second row: Let's discuss button
    assert len(buttons[1]) == 1
    assert buttons[1][0]["text"] == "💬 Let's discuss"
    assert buttons[1][0]["callback_data"] == f"claude_control:chat:{request_id}"


def test_bypass_clears_outline_pending():
    """Bypass should clear _OUTLINE_PENDING for the session."""
    state = _make_state("sess-3")
    mark_outline_pending("sess-3")
    assert "sess-3" in _OUTLINE_PENDING  # set by mark_outline_pending

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
    mark_outline_pending("sess-overwrite")

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


# --- #659: ExitPlanMode plan input satisfies the outline gate ---


def _make_exit_plan_mode_request_with_plan(
    request_id: str, plan: str
) -> claude_schema.StreamControlRequest:
    return claude_schema.StreamControlRequest(
        request_id=request_id,
        request=claude_schema.ControlCanUseToolRequest(
            tool_name="ExitPlanMode",
            input={"plan": plan},
        ),
    )


def test_plan_input_satisfies_outline_gate():
    """#659: on plan-file CLIs the plan body arrives in ExitPlanMode's input
    and NO chat text is written — the gate must hold the request open, not
    deny-loop until Claude gives up."""
    state = _make_state("sess-planinput")
    mark_outline_pending("sess-planinput")
    # No assistant text at all (the observed CLI 2.1.215 behaviour)
    assert state.max_text_len_since_cooldown == 0

    request_id = "req_exit_plan"
    plan = "Step 1: create notes.txt\nStep 2: append the line\n" * 10
    event = _make_exit_plan_mode_request_with_plan(request_id, plan)
    events = translate_claude_event(
        event, title="claude", state=state, factory=state.factory
    )

    # Held open, not auto-denied
    assert len(state.auto_deny_queue) == 0
    assert request_id in state.pending_control_requests
    # The plan body is surfaced as the outline for the standalone message
    action_events = [e for e in events if isinstance(e, ActionEvent)]
    assert len(action_events) == 1
    assert action_events[0].action.detail.get("outline_full_text") == plan
    assert "sess-planinput" not in _OUTLINE_PENDING


def test_short_plan_input_still_denied():
    """#659: a trivially short plan input (< 200 chars) with no text does not
    satisfy the gate — auto-deny with the outline instruction as before."""
    state = _make_state("sess-shortplan")
    mark_outline_pending("sess-shortplan")

    request_id = "req_exit_plan"
    _REQUEST_TO_SESSION[request_id] = "sess-shortplan"
    _REQUEST_TO_INPUT[request_id] = {}
    event = _make_exit_plan_mode_request_with_plan(request_id, "do the thing")
    translate_claude_event(event, title="claude", state=state, factory=state.factory)

    assert len(state.auto_deny_queue) == 1


def test_chat_text_outline_preferred_over_plan_input():
    """#659: when Claude DID write a chat-text outline, that text remains the
    outline shown (legacy behaviour unchanged); plan input is the fallback."""
    state = _make_state("sess-textwins")
    mark_outline_pending("sess-textwins")
    chat_outline = "Written in chat: " + "y" * 300
    state.outline_text = chat_outline
    state.max_text_len_since_cooldown = len(chat_outline)

    request_id = "req_exit_plan"
    plan = "plan input body " * 30
    event = _make_exit_plan_mode_request_with_plan(request_id, plan)
    events = translate_claude_event(
        event, title="claude", state=state, factory=state.factory
    )

    action_events = [e for e in events if isinstance(e, ActionEvent)]
    assert action_events[0].action.detail.get("outline_full_text") == chat_outline


# --- No-bypass path: no outline written ---


def test_auto_deny_without_outline():
    """ExitPlanMode should be auto-denied when no substantial text was written."""
    state = _make_state("sess-4")
    mark_outline_pending("sess-4")
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
    mark_outline_pending("sess-5")
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
    mark_outline_pending("sess-esc")
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
    # Second row: Let's discuss button with da: prefix
    assert len(buttons) == 2
    assert buttons[1][0]["text"] == "💬 Let's discuss"
    assert buttons[1][0]["callback_data"].startswith("claude_control:chat:da:")
    # Should have auto-denied
    assert len(state.auto_deny_queue) == 1


# --- Outline text storage: text block stores on state ---


def test_outline_text_stored_on_state_during_cooldown():
    """StreamTextBlock stores outline text on state when pending and text >= 200 chars."""
    state = _make_state("sess-note")
    mark_outline_pending("sess-note")
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
    # No mark_outline_pending → _OUTLINE_PENDING is empty

    text_event = _make_text_block("A" * 300)
    translate_claude_event(
        text_event, title="claude", state=state, factory=state.factory
    )

    assert state.outline_text is None


def test_outline_text_not_stored_for_short_text():
    """Short text (< 200 chars) should NOT be stored even when outline is pending."""
    state = _make_state("sess-short")
    mark_outline_pending("sess-short")

    text_event = _make_text_block("Short text")
    translate_claude_event(
        text_event, title="claude", state=state, factory=state.factory
    )

    assert state.outline_text is None


def test_outline_in_synthetic_action_detail():
    """Synthetic Approve/Deny action should include full outline in detail dict."""
    state = _make_state("sess-embed")
    mark_outline_pending("sess-embed")

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
    action = action_events[0].action
    # Short reference title (not the full outline)
    assert "see above" in action.title
    # Full outline text in detail dict
    assert action.detail["outline_full_text"] == outline
    # Outline text should be cleared after use
    assert state.outline_text is None


def test_long_outline_not_truncated_in_detail():
    """Outline text of any length should be passed fully in detail dict (no truncation)."""
    state = _make_state("sess-trunc")
    mark_outline_pending("sess-trunc")

    long_text = "B" * 5000
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
    action = action_events[0].action
    # Full text preserved — no truncation
    assert action.detail["outline_full_text"] == long_text
    assert len(action.detail["outline_full_text"]) == 5000


# --- _OUTLINE_PENDING lifecycle ---


def test_mark_outline_pending_adds_outline_pending():
    """mark_outline_pending should add session to _OUTLINE_PENDING."""
    mark_outline_pending("sess-pending")
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


def test_hold_open_long_after_outline_request():
    """Outline-pending sessions hold the request open regardless of elapsed time.

    Regression lineage #114 (updated for #570): the gate is purely text-based
    now — an outline-pending session with enough written text must enter the
    hold-open path with synthetic buttons no matter how long ago the user
    clicked Pause & Outline, never fall through to the 3-button flow.
    """
    import time
    from unittest.mock import patch

    state = _make_state("sess-expired")
    mark_outline_pending("sess-expired")
    assert "sess-expired" in _OUTLINE_PENDING

    # Simulate outline written
    state.max_text_len_since_cooldown = 400
    state.outline_text = "Step 1: Do this\nStep 2: Do that\n" * 15

    # Far in the future — elapsed time must not matter (#570)
    with patch.object(time, "time", return_value=time.time() + 200):
        request_id = "req_exit_plan"
        _REQUEST_TO_SESSION[request_id] = "sess-expired"
        _REQUEST_TO_INPUT[request_id] = {}

        event = _make_exit_plan_mode_request(request_id)
        events = translate_claude_event(
            event, title="claude", state=state, factory=state.factory
        )

    # Should still produce synthetic action (not 3-button ExitPlanMode)
    action_events = [e for e in events if isinstance(e, ActionEvent)]
    assert len(action_events) == 1
    detail = action_events[0].action.detail
    assert detail["request_type"] == "DiscussApproval"
    buttons = detail["inline_keyboard"]["buttons"]
    assert len(buttons) == 2  # [Approve Plan, Deny], [Let's discuss]
    assert len(buttons[0]) == 2
    assert buttons[0][0]["text"] == "✅ Approve Plan"
    assert buttons[0][1]["text"] == "❌ Deny"
    # Request should be held open (not auto-denied)
    assert len(state.auto_deny_queue) == 0
    assert request_id in state.pending_control_requests
    # Buttons should use real request_id
    assert buttons[0][0]["callback_data"] == f"claude_control:approve:{request_id}"
    # _OUTLINE_PENDING should be cleared
    assert "sess-expired" not in _OUTLINE_PENDING


@pytest.mark.anyio
async def test_chat_on_synthetic_after_session_ends():
    """Clicking 'Let's discuss' on da: prefix after session ends should return error."""
    from untether.commands import CommandContext
    from untether.telegram.commands.claude_control import ClaudeControlCommand
    from untether.transport import MessageRef

    session_id = "sess-dead-chat"
    synth_request_id = f"da:{session_id}"

    _REQUEST_TO_SESSION[synth_request_id] = session_id
    # No _ACTIVE_RUNNERS entry — session ended

    ctx = CommandContext(
        command="claude_control",
        text=f"claude_control:chat:{synth_request_id}",
        args_text=f"chat:{synth_request_id}",
        args=(f"chat:{synth_request_id}",),
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
async def test_chat_on_synthetic_with_active_session():
    """Clicking 'Let's discuss' on da: prefix with active session should succeed."""
    from untether.commands import CommandContext
    from untether.telegram.commands.claude_control import ClaudeControlCommand
    from untether.transport import MessageRef

    runner = ClaudeRunner(claude_cmd="claude")
    session_id = "sess-alive-chat"
    synth_request_id = f"da:{session_id}"

    _ACTIVE_RUNNERS[session_id] = (runner, 0.0)
    _SESSION_STDIN[session_id] = AsyncMock()
    _REQUEST_TO_SESSION[synth_request_id] = session_id
    mark_outline_pending(session_id)

    ctx = CommandContext(
        command="claude_control",
        text=f"claude_control:chat:{synth_request_id}",
        args_text=f"chat:{synth_request_id}",
        args=(f"chat:{synth_request_id}",),
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
    assert "discuss" in result.text.lower()
    # Should clear outline_pending
    assert session_id not in _OUTLINE_PENDING


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
