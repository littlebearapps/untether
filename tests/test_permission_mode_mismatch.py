"""#751 — requested vs effective Claude permission mode (``system/init``).

The CLI does not warn when it can't honour ``--permission-mode`` (findings Q3,
probe Z9: ``auto`` on Haiku silently runs as ``default``). Untether compares
the first ``system/init.permissionMode`` with the mode it asked for, shows a
warning row, and — the security half — re-arms the stage-6 gate when the CLI
actually runs a prompting mode that Untether had classed autonomous.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from structlog.testing import capture_logs

from untether.model import ActionEvent, StartedEvent
from untether.runners.claude import (
    _PLAN_EXIT_APPROVED,
    _REQUEST_TO_INPUT,
    _REQUEST_TO_SESSION,
    _REQUEST_TO_TOOL_NAME,
    ClaudeRunner,
    ClaudeStreamState,
    translate_claude_event,
)
from untether.schemas import claude as claude_schema

SESSION = "sess-751"


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


def _init(mode: str | None, model: str = "claude-haiku-4-5") -> Any:
    payload: dict[str, Any] = {
        "type": "system",
        "subtype": "init",
        "cwd": "/tmp",
        "model": model,
        "tools": ["Bash", "Glob"],
    }
    if mode is not None:
        payload["permissionMode"] = mode
    return _decode(payload)


def _can_use_tool(rid: str, tool: str) -> Any:
    return _decode(
        {
            "type": "control_request",
            "request_id": rid,
            "request": {
                "subtype": "can_use_tool",
                "tool_name": tool,
                "input": {"command": "ls", "pattern": "*"},
            },
        }
    )


def _state(mode: str | None, **runner_kwargs: Any) -> ClaudeStreamState:
    runner = ClaudeRunner(claude_cmd="claude", permission_mode=mode, **runner_kwargs)
    return runner.new_state("hi", None)


def _feed(state: ClaudeStreamState, event: Any) -> list[Any]:
    return translate_claude_event(
        event, title="claude", state=state, factory=state.factory
    )


def _mismatch_warnings(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in logs if e["event"] == "claude.permission_mode.mismatch"]


def test_751_init_mismatch_auto_to_default_emits_warning_row() -> None:
    state = _state("auto")
    assert state.requested_permission_mode == "auto"
    with capture_logs() as logs:
        events = _feed(state, _init("default"))
    assert [type(e) for e in events] == [StartedEvent, ActionEvent, ActionEvent]
    started, completed = events[1], events[2]
    assert started.phase == "started"
    assert completed.phase == "completed"
    assert completed.action.kind == "note"
    assert completed.level == "warning"
    title = completed.action.title
    assert "auto" in title and "default" in title
    assert "isn't available" in title
    (warn,) = _mismatch_warnings(logs)
    assert warn["log_level"] == "warning"
    assert warn["requested"] == "auto"
    assert warn["effective"] == "default"
    assert warn["model"] == "claude-haiku-4-5"
    assert warn["resumed"] is False


def test_751_init_match_no_row() -> None:
    state = _state("auto")
    with capture_logs() as logs:
        events = _feed(state, _init("auto", model="claude-opus-5-5"))
    assert [type(e) for e in events] == [StartedEvent]
    assert _mismatch_warnings(logs) == []
    assert state.prompting_mode is False


def test_751_mismatch_into_prompting_mode_rearms_gate() -> None:
    state = _state("auto")
    assert state.prompting_mode is False
    with capture_logs() as logs:
        events = _feed(state, _init("default"))
    assert state.prompting_mode is True
    assert events[-1].action.title.endswith("approvals will be requested")
    (warn,) = _mismatch_warnings(logs)
    assert warn["prompting_rearmed"] is True


def test_751_mismatch_into_autonomous_mode_never_disarms() -> None:
    state = _state("default")
    assert state.prompting_mode is True
    with capture_logs() as logs:
        events = _feed(state, _init("bypassPermissions"))
    assert state.prompting_mode is True
    title = events[-1].action.title
    assert "approvals" not in title
    assert "isn't available" not in title  # only claimed for `auto`
    (warn,) = _mismatch_warnings(logs)
    assert warn["prompting_rearmed"] is False

    # `plan` requested, CLI reports `default` (prompting) → re-armed.
    state = _state("plan")
    assert state.prompting_mode is False
    _feed(state, _init("default"))
    assert state.prompting_mode is True


def test_751_after_mismatch_glob_and_bash_queue_telegram_approval() -> None:
    """Gate level: after the re-arm, neither request is auto-approved."""
    state = _state("auto")
    _feed(state, _init("default"))
    for rid, tool in (("req-751-glob", "Glob"), ("req-751-bash", "Bash")):
        events = _feed(state, _can_use_tool(rid, tool))
        assert len(events) == 1
        assert isinstance(events[0], ActionEvent)
        assert events[0].action.kind == "warning"  # the approval action
        assert rid not in state.auto_approve_queue
        assert _REQUEST_TO_SESSION.get(rid) == SESSION


def test_751_control_without_mismatch_glob_is_auto_approved() -> None:
    """Control case: on a model that honours `auto`, stage 6 still
    blanket-approves (so the gate-level test fails on the old code)."""
    state = _state("auto")
    _feed(state, _init("auto", model="claude-opus-5-5"))
    events = _feed(state, _can_use_tool("req-751-ctl", "Glob"))
    assert events == []
    assert "req-751-ctl" in state.auto_approve_queue


def test_751_manual_requested_default_reported_no_row() -> None:
    # Probe P1b (CLI 2.1.285): `--permission-mode manual` reports `default`.
    state = _state("manual")
    assert state.requested_permission_mode == "default"
    with capture_logs() as logs:
        events = _feed(state, _init("default"))
    assert [type(e) for e in events] == [StartedEvent]
    assert _mismatch_warnings(logs) == []


def test_751_plan_auto_requested_plan_reported_no_row() -> None:
    state = _state("plan-auto")
    assert state.requested_permission_mode == "plan"
    with capture_logs() as logs:
        events = _feed(state, _init("plan"))
    assert [type(e) for e in events] == [StartedEvent]
    assert _mismatch_warnings(logs) == []


def test_751_dangerously_skip_expects_bypass() -> None:
    state = _state("plan", dangerously_skip_permissions=True)
    assert state.requested_permission_mode == "bypassPermissions"
    with capture_logs() as logs:
        events = _feed(state, _init("bypassPermissions"))
    assert [type(e) for e in events] == [StartedEvent]
    assert _mismatch_warnings(logs) == []


def test_751_second_init_after_compaction_not_rechecked() -> None:
    state = _state("auto")
    _feed(state, _init("auto", model="claude-opus-5-5"))
    with capture_logs() as logs:
        events = _feed(state, _init("default"))
    assert not [e for e in events if isinstance(e, ActionEvent)]
    assert _mismatch_warnings(logs) == []
    assert state.prompting_mode is False


def test_751_requested_none_never_checks() -> None:
    """Legacy `-p` path: no mode requested, no control channel."""
    state = _state(None)
    assert state.requested_permission_mode is None
    with capture_logs() as logs:
        events = _feed(state, _init("default"))
    assert [type(e) for e in events] == [StartedEvent]
    assert _mismatch_warnings(logs) == []


def test_751_missing_permission_mode_field_never_checks() -> None:
    state = _state("auto")
    with capture_logs() as logs:
        events = _feed(state, _init(None))
    assert [type(e) for e in events] == [StartedEvent]
    assert _mismatch_warnings(logs) == []
    # Not consumed: a later init that does carry the field is still checked.
    assert state.permission_mode_checked is False


def test_751_started_event_still_first() -> None:
    """3-event contract on the mismatch path."""
    state = _state("auto")
    events = _feed(state, _init("default"))
    assert isinstance(events[0], StartedEvent)
    assert all(isinstance(e, ActionEvent) for e in events[1:])


def test_751_resumed_run_is_checked_too() -> None:
    """Probe P1 (CLI 2.1.285): a resume reports the flag's mode, not the
    stored one, so resumed runs are compared like fresh ones."""
    from untether.model import ResumeToken

    runner = ClaudeRunner(claude_cmd="claude", permission_mode="auto")
    state = runner.new_state("hi", ResumeToken(engine="claude", value=SESSION))
    with capture_logs() as logs:
        _feed(state, _init("default"))
    (warn,) = _mismatch_warnings(logs)
    assert warn["resumed"] is True
    assert state.prompting_mode is True
