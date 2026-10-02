"""#835 — unattended (cron / webhook) Claude runs fail closed.

Nobody is present to tap a Telegram button in a cron or webhook run, so every
stage-6 request that would wait for one is denied at once (with a message
telling Claude to carry on without it or stop and report). Unattended runs
also never approve what an attended run would have asked about: the
diff-preview gate denies, `plan` denies mutating tools, and `auto` /
`bypassPermissions` deny every request that still reaches Untether (ask-class
requests, and auto mode's prompting fallback after repeated classifier
blocks). Interactive runs are unchanged.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from structlog.testing import capture_logs

from untether.context import RunContext, unattended_trigger
from untether.model import ActionEvent, ResumeToken
from untether.runners.claude import (
    _PLAN_EXIT_APPROVED,
    _REQUEST_TO_INPUT,
    _REQUEST_TO_SESSION,
    _REQUEST_TO_TOOL_NAME,
    ClaudeRunner,
    ClaudeStreamState,
    translate_claude_event,
)
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.schemas import claude as claude_schema

SESSION = "sess-835"


@pytest.fixture(autouse=True)
def _clear_registries():
    def _wipe() -> None:
        _REQUEST_TO_SESSION.clear()
        _REQUEST_TO_INPUT.clear()
        _REQUEST_TO_TOOL_NAME.clear()
        _PLAN_EXIT_APPROVED.clear()

    _wipe()
    yield
    _wipe()


def _decode(payload: dict[str, Any]) -> claude_schema.StreamJsonMessage:
    data = dict(payload)
    data.setdefault("uuid", "uuid")
    data.setdefault("session_id", SESSION)
    return claude_schema.decode_stream_json_line(json.dumps(data).encode())


def _init(mode: str, model: str = "claude-opus-5-5") -> Any:
    return _decode(
        {
            "type": "system",
            "subtype": "init",
            "cwd": "/tmp",
            "model": model,
            "tools": ["Bash", "Glob"],
            "permissionMode": mode,
        }
    )


def _can_use_tool(rid: str, tool: str, **tool_input: Any) -> Any:
    return _decode(
        {
            "type": "control_request",
            "request_id": rid,
            "request": {
                "subtype": "can_use_tool",
                "tool_name": tool,
                "input": tool_input or {"command": "ls"},
            },
        }
    )


def _result() -> Any:
    return _decode(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 1000,
            "duration_api_ms": 900,
            "num_turns": 2,
            "result": "done",
            "total_cost_usd": 0.01,
        }
    )


def _options(trigger: str | None, **kw: Any) -> EngineRunOptions:
    return EngineRunOptions(unattended_trigger=trigger, **kw)


def _state(
    mode: str | None, trigger: str | None = "cron:c1", **runner_kwargs: Any
) -> ClaudeStreamState:
    runner = ClaudeRunner(claude_cmd="claude", permission_mode=mode, **runner_kwargs)
    with apply_run_options(_options(trigger)):
        state = runner.new_state("hi", None)
    # The factory needs a session (Phase 2 registration keys on it).
    state.factory.started(ResumeToken(engine="claude", value=SESSION), title="claude")
    return state


def _feed(
    state: ClaudeStreamState, event: Any, options: EngineRunOptions | None = None
) -> list[Any]:
    with apply_run_options(options):
        return translate_claude_event(
            event, title="claude", state=state, factory=state.factory
        )


def _denied(state: ClaudeStreamState, rid: str) -> str | None:
    for req_id, message in state.auto_deny_queue:
        if req_id == rid:
            return message
    return None


def _keyboards(events: list[Any]) -> list[Any]:
    return [
        e
        for e in events
        if isinstance(e, ActionEvent) and e.action.detail.get("inline_keyboard")
    ]


def _deny_logs(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in logs if e["event"] == "permission.unattended_deny"]


# --- the predicate and plumbing -------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("cron:c1", "cron:c1"),
        ("webhook:gh", "webhook:gh"),
        ("at:abc", None),
        ("loop:abc", None),
        (None, None),
    ],
)
def test_835_unattended_predicate(source: str | None, expected: str | None) -> None:
    assert unattended_trigger(RunContext(trigger_source=source)) == expected
    assert unattended_trigger(None) is None


def test_835_legacy_p_mode_ignores_flag() -> None:
    state = _state(None)
    assert state.unattended_trigger is None


def test_835_new_state_arms_trigger_and_mode() -> None:
    state = _state("plan-auto")
    assert state.unattended_trigger == "cron:c1"
    assert state.unattended_mode == "plan-auto"
    bypass = _state("plan", dangerously_skip_permissions=True)
    assert bypass.unattended_mode == "bypassPermissions"


# --- would-wait requests are denied, never offered ------------------------


def test_835_cron_default_mode_write_denied_not_offered() -> None:
    state = _state("default")
    with capture_logs() as logs:
        events = _feed(state, _can_use_tool("r1", "Write", file_path="/tmp/x"))
    msg = _denied(state, "r1")
    assert msg is not None
    assert "unattended" in msg and "cron:c1" in msg and "Write" in msg
    assert "stop and report" in msg
    assert "r1" not in _REQUEST_TO_SESSION
    assert "r1" not in state.pending_control_requests
    assert _keyboards(events) == []
    (warn,) = _deny_logs(logs)
    assert warn["log_level"] == "warning"
    assert warn["tool_name"] == "Write"
    assert warn["trigger_source"] == "cron:c1"
    assert warn["reason"] == "would_wait"
    assert warn["permission_mode"] == "default"


def test_835_webhook_run_denied_too() -> None:
    state = _state("acceptEdits", trigger="webhook:gh")
    _feed(state, _can_use_tool("r1", "WebFetch", url="https://x"))
    msg = _denied(state, "r1")
    assert msg is not None and "webhook run (webhook:gh)" in msg


def test_835_interactive_run_still_shows_buttons() -> None:
    """Guard: the same request in an attended run gets a keyboard."""
    state = _state("default", trigger=None)
    assert state.unattended_trigger is None
    events = _feed(state, _can_use_tool("r1", "Write", file_path="/tmp/x"))
    assert len(_keyboards(events)) == 1
    assert _REQUEST_TO_SESSION.get("r1") == SESSION
    assert state.auto_deny_queue == []


def test_835_plan_cron_exitplanmode_denied_with_plan_message() -> None:
    state = _state("plan")
    state.exitplanmode_plans.clear()
    events = _feed(state, _can_use_tool("r1", "ExitPlanMode", plan="do it"))
    msg = _denied(state, "r1")
    assert msg is not None
    assert "Do not call ExitPlanMode again" in msg
    assert "complete plan as your final answer" in msg
    assert "r1" not in state.exitplanmode_plans
    assert "r1" not in state.auto_approve_queue
    assert _keyboards(events) == []


def test_835_plan_auto_cron_exitplanmode_still_rubber_stamped() -> None:
    state = _state("plan-auto")
    _feed(state, _can_use_tool("r1", "ExitPlanMode", plan="do it"))
    assert "r1" in state.auto_approve_queue
    assert _denied(state, "r1") is None


def test_835_ask_user_question_denied_with_defaults_message() -> None:
    state = _state("plan")
    _feed(state, _can_use_tool("r1", "AskUserQuestion", questions=[]))
    msg = _denied(state, "r1")
    assert msg is not None and "reasonable defaults" in msg


def test_835_set_permission_mode_request_denied() -> None:
    state = _state("plan")
    event = _decode(
        {
            "type": "control_request",
            "request_id": "r-spm",
            "request": {"subtype": "set_permission_mode", "mode": "default"},
        }
    )
    with capture_logs() as logs:
        _feed(state, event)
    assert _denied(state, "r-spm") is not None
    assert _deny_logs(logs)[0]["tool_name"] == "SetPermissionMode"


# --- unattended never approves what an attended run would ask about --------


def test_835_plan_cron_read_only_tools_still_auto_approved() -> None:
    """Unattended `plan` keeps today's approval for non-mutating tools."""
    state = _state("plan")
    for rid, tool in (("g", "Glob"), ("w", "WebFetch"), ("t", "Task")):
        _feed(state, _can_use_tool(rid, tool))
        assert rid in state.auto_approve_queue
        assert _denied(state, rid) is None


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"])
def test_835_plan_cron_write_after_exitplanmode_denied(tool: str) -> None:
    """A `plan` cron's model that tries a mutating tool anyway (plan mode on
    current CLIs sends it to stage 6 with reason "mode") is denied."""
    state = _state("plan")
    _feed(state, _can_use_tool("r0", "ExitPlanMode", plan="p"))
    with capture_logs() as logs:
        _feed(state, _can_use_tool("r1", tool, file_path="/tmp/x"))
    msg = _denied(state, "r1")
    assert msg is not None and "plan mode" in msg
    assert "r1" not in state.auto_approve_queue
    assert _deny_logs(logs)[0]["reason"] == "plan_mode"


def test_835_attended_plan_write_unchanged() -> None:
    """Guard: the attended Probe-G gap (#882) is not changed here."""
    state = _state("plan", trigger=None)
    _feed(state, _can_use_tool("r1", "Write", file_path="/tmp/x"))
    assert "r1" in state.auto_approve_queue


@pytest.mark.parametrize("mode", ["bypassPermissions", "auto"])
@pytest.mark.parametrize("tool", ["Bash", "Glob", "mcp__x__send"])
def test_835_bypass_and_auto_cron_ask_class_request_denied(
    mode: str, tool: str
) -> None:
    """Anything reaching stage 6 under `bypassPermissions` / `auto` is an
    ask-class request (ask rule, hook ask, interaction-required tool, auto's
    prompting fallback) — denied in an unattended run."""
    state = _state(mode)
    with capture_logs() as logs:
        _feed(state, _can_use_tool("r1", tool))
    assert _denied(state, "r1") is not None
    assert "r1" not in state.auto_approve_queue
    assert _deny_logs(logs)[0]["reason"] == "ask_class"


@pytest.mark.parametrize("mode", ["bypassPermissions", "auto"])
def test_835_attended_bypass_and_auto_unchanged(mode: str) -> None:
    state = _state(mode, trigger=None)
    _feed(state, _can_use_tool("r1", "Bash"))
    assert "r1" in state.auto_approve_queue


def test_835_diff_preview_gate_denied_when_unattended() -> None:
    """⚑11-D3: the diff-preview gate becomes a deny (skipping it would
    approve the write unseen); attended control still gated (keyboard)."""
    state = _state("plan-auto")
    opts = _options("cron:c1", diff_preview=True)
    with capture_logs() as logs:
        events = _feed(state, _can_use_tool("r1", "Edit", file_path="/tmp/x"), opts)
    msg = _denied(state, "r1")
    assert msg is not None and "nobody can review this change" in msg
    assert "r1" not in state.auto_approve_queue
    assert _keyboards(events) == []
    assert _deny_logs(logs)[0]["reason"] == "diff_preview"

    attended = _state("plan-auto", trigger=None)
    events = _feed(
        attended,
        _can_use_tool("r2", "Edit", file_path="/tmp/x"),
        _options(None, diff_preview=True),
    )
    assert len(_keyboards(events)) == 1
    assert attended.auto_deny_queue == []


def test_835_unattended_glob_with_diff_preview_still_approved() -> None:
    state = _state("plan-auto")
    _feed(state, _can_use_tool("r1", "Glob"), _options("cron:c1", diff_preview=True))
    assert "r1" in state.auto_approve_queue


# --- notice: one row per turn, usage payload ------------------------------


def test_835_one_note_row_per_turn() -> None:
    state = _state("default")
    first = _feed(state, _can_use_tool("r1", "Write", file_path="/tmp/a"))
    second = _feed(state, _can_use_tool("r2", "Write", file_path="/tmp/b"))
    notes = [e for e in first if isinstance(e, ActionEvent)]
    assert [e.phase for e in notes] == ["started", "completed"]
    assert notes[1].action.kind == "note"
    assert notes[1].level == "warning"
    assert "Unattended run" in notes[1].action.title
    assert "Write" in notes[1].action.title
    assert second == []
    # A new turn opens a new row.
    state.unattended_denials = []
    third = _feed(state, _can_use_tool("r3", "Bash"))
    assert any(isinstance(e, ActionEvent) for e in third)


def test_835_usage_payload_counts_denials() -> None:
    state = _state("default")
    _feed(state, _init("default"))
    _feed(state, _can_use_tool("r1", "Write", file_path="/tmp/a"))
    _feed(state, _can_use_tool("r2", "Write", file_path="/tmp/b"))
    events = _feed(state, _result())
    completed = events[-1]
    assert completed.usage["unattended"] == {
        "trigger": "cron:c1",
        "mode": "default",
        "denied": {"Write": 2},
    }


def test_835_usage_payload_absent_without_denials() -> None:
    state = _state("default")
    _feed(state, _init("default"))
    events = _feed(state, _result())
    assert "unattended" not in (events[-1].usage or {})


def test_835_mismatch_rearm_then_unattended_deny() -> None:
    """`auto` requested, CLI runs `default` (#751 re-arm) → a Bash request
    is denied (unattended), never offered."""
    state = _state("auto")
    _feed(state, _init("default", model="claude-haiku-4-5"))
    assert state.prompting_mode is True
    events = _feed(state, _can_use_tool("r1", "Bash"))
    assert _denied(state, "r1") is not None
    assert _keyboards(events) == []


def test_835_received_log_carries_unattended() -> None:
    state = _state("default")
    with capture_logs() as logs:
        _feed(state, _can_use_tool("r1", "Write", file_path="/tmp/a"))
    (received,) = [e for e in logs if e["event"] == "control_request.received"]
    assert received["unattended"] == "cron:c1"
