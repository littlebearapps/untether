"""#776 phase 01: native background-task map from Claude's ``system/task_*`` events.

Event shapes are verbatim from live probes on Claude Code 2.1.283 (see
``docs/findings/2026-09-27-claude-live-session-probes.md``), with uuid and
session_id normalised.
"""

from __future__ import annotations

import json
import time

import pytest
from structlog.testing import capture_logs

from untether.runners.claude import (
    BG_BASH_MAX_KEEP_S,
    ClaudeStreamState,
    background_task_summary,
    has_live_background_work,
    translate_claude_event,
)
from untether.schemas import claude as claude_schema

SID = "sess-776"


def _feed(state: ClaudeStreamState, payload: dict) -> list:
    payload = {"session_id": SID, **payload}
    event = claude_schema.decode_stream_json_line(json.dumps(payload))
    return translate_claude_event(
        event, title="claude", state=state, factory=state.factory
    )


def _tool_use(name: str, tool_id: str, raw_input: dict) -> dict:
    return {
        "type": "assistant",
        "message": {
            "id": "msg_1",
            "role": "assistant",
            "model": "claude-haiku",
            "content": [
                {"type": "tool_use", "id": tool_id, "name": name, "input": raw_input}
            ],
        },
    }


def _snapshot(*tasks: tuple[str, str, str]) -> dict:
    return {
        "type": "system",
        "subtype": "background_tasks_changed",
        "tasks": [
            {"task_id": tid, "task_type": ttype, "description": desc}
            for tid, ttype, desc in tasks
        ],
    }


def _started_bash(task_id: str, tool_use_id: str, desc: str = "sleep 20") -> dict:
    return {
        "type": "system",
        "subtype": "task_started",
        "task_id": task_id,
        "tool_use_id": tool_use_id,
        "description": desc,
        "is_backgrounded": True,
        "task_type": "local_bash",
    }


def _started_agent(task_id: str, tool_use_id: str) -> dict:
    return {
        "type": "system",
        "subtype": "task_started",
        "task_id": task_id,
        "tool_use_id": tool_use_id,
        "description": "Research the thing",
        "subagent_type": "general-purpose",
        "is_backgrounded": True,
        "spawn_depth": 1,
        "task_type": "local_agent",
        "prompt": "Go research the thing",
    }


def _updated(task_id: str, status: str) -> dict:
    return {
        "type": "system",
        "subtype": "task_updated",
        "task_id": task_id,
        "patch": {"status": status, "end_time": 1790500249636},
    }


def _notification(task_id: str, tool_use_id: str, status: str) -> dict:
    return {
        "type": "system",
        "subtype": "task_notification",
        "task_id": task_id,
        "tool_use_id": tool_use_id,
        "status": status,
        "output_file": "/tmp/x.output",
        "summary": "done",
    }


# ── schema ──────────────────────────────────────────────────────────────────


def test_task_event_fields_decode() -> None:
    event = claude_schema.decode_stream_json_line(
        json.dumps({"session_id": SID, **_started_agent("a1", "toolu_a")})
    )
    assert isinstance(event, claude_schema.StreamSystemMessage)
    assert event.task_id == "a1"
    assert event.task_type == "local_agent"
    assert event.is_backgrounded is True
    assert event.subagent_type == "general-purpose"
    assert event.spawn_depth == 1


def test_command_lifecycle_decodes() -> None:
    event = claude_schema.decode_stream_json_line(
        json.dumps(
            {
                "type": "command_lifecycle",
                "command_uuid": "11111111-1111-4111-8111-111111111111",
                "state": "queued",
                "uuid": "u",
                "session_id": SID,
            }
        )
    )
    assert isinstance(event, claude_schema.StreamCommandLifecycleMessage)
    assert event.command_uuid == "11111111-1111-4111-8111-111111111111"
    assert event.state == "queued"


def test_command_lifecycle_translates_to_nothing() -> None:
    state = ClaudeStreamState()
    assert (
        _feed(state, {"type": "command_lifecycle", "command_uuid": "x", "state": "x"})
        == []
    )


# ── liveness ────────────────────────────────────────────────────────────────


def test_task_started_bg_bash_is_live() -> None:
    state = ClaudeStreamState()
    assert _feed(state, _started_bash("b1", "toolu_b")) == []
    assert state.tasks["b1"].is_backgrounded is True
    assert has_live_background_work(state) is True
    assert state.background_observed is True


def test_task_started_bg_agent_is_live() -> None:
    state = ClaudeStreamState()
    _feed(state, _started_agent("a1", "toolu_a"))
    assert state.tasks["a1"].subagent_type == "general-purpose"
    assert has_live_background_work(state) is True


def test_subagent_owned_foreground_task_is_not_live() -> None:
    state = ClaudeStreamState()
    _feed(
        state,
        {
            "type": "system",
            "subtype": "task_started",
            "task_id": "f1",
            "owned_by_subagent": True,
            "is_backgrounded": False,
            "task_type": "local_bash",
            "description": "ls",
        },
    )
    assert has_live_background_work(state) is False


@pytest.mark.parametrize("status", ["completed", "killed"])
def test_task_updated_terminal_ends_task(status: str) -> None:
    state = ClaudeStreamState()
    _feed(state, _started_bash("b1", "toolu_b"))
    _feed(state, _updated("b1", status))
    assert state.tasks["b1"].status == status
    assert has_live_background_work(state) is False


def test_task_notification_stopped_ends_task() -> None:
    state = ClaudeStreamState()
    _feed(state, _started_bash("b1", "toolu_b"))
    _feed(state, _notification("b1", "toolu_b", "stopped"))
    assert has_live_background_work(state) is False


def test_task_notification_for_unknown_task_is_harmless() -> None:
    """Path B (F11): on --resume the stopped notification for the previous
    process's task arrives before init, for a task this state never saw."""
    state = ClaudeStreamState()
    assert _feed(state, _notification("gone", "toolu_x", "stopped")) == []
    assert has_live_background_work(state) is False


def test_task_progress_records_usage() -> None:
    state = ClaudeStreamState()
    _feed(state, _started_agent("a1", "toolu_a"))
    _feed(
        state,
        {
            "type": "system",
            "subtype": "task_progress",
            "task_id": "a1",
            "tool_use_id": "toolu_a",
            "description": "Running step",
            "usage": {"total_tokens": 52470, "tool_uses": 2, "duration_ms": 4314},
            "last_tool_name": "Bash",
        },
    )
    task = state.tasks["a1"]
    assert task.last_usage == {
        "total_tokens": 52470,
        "tool_uses": 2,
        "duration_ms": 4314,
    }
    assert task.last_tool_name == "Bash"


def test_background_tasks_changed_snapshot_reconciles_missing_task() -> None:
    state = ClaudeStreamState()
    _feed(state, _snapshot(("b1", "local_bash", "sleep")))
    _feed(state, _started_bash("b1", "toolu_b"))
    assert has_live_background_work(state) is True
    # Snapshot without b1 and no task_updated: the snapshot is authoritative.
    _feed(state, _snapshot())
    assert has_live_background_work(state) is False
    assert state.tasks["b1"].status == "ended"


def test_snapshot_before_task_started_registers_placeholder() -> None:
    """The CLI emits background_tasks_changed a moment before task_started."""
    state = ClaudeStreamState()
    _feed(state, _snapshot(("b1", "local_bash", "sleep 20")))
    assert has_live_background_work(state) is True
    _feed(state, _started_bash("b1", "toolu_b"))
    assert state.tasks["b1"].tool_use_id == "toolu_b"
    assert len(state.tasks) == 1


def test_monitor_registers_as_local_bash_and_ends_on_stream_end() -> None:
    """F10: Monitor is a local_bash background task; per-line ticks carry no
    task event; the stream end emits task_updated completed."""
    state = ClaudeStreamState()
    _feed(
        state,
        _tool_use("Monitor", "toolu_m", {"command": "tail -f x", "timeout_ms": 30000}),
    )
    _feed(state, _snapshot(("m1", "local_bash", "tick counter")))
    _feed(state, _started_bash("m1", "toolu_m", desc="tick counter"))
    assert has_live_background_work(state) is True
    assert background_task_summary(state) == "⏳ 1 watcher"
    _feed(state, _updated("m1", "completed"))
    assert has_live_background_work(state) is False
    assert background_task_summary(state) is None


# ── native vs legacy handles (D-4) ──────────────────────────────────────────


def test_native_events_make_legacy_bash_agent_handles_ignored() -> None:
    """Once the CLI speaks task events, the tool_use heuristics no longer
    decide liveness: a bg Agent whose native task completed is not live even
    though its legacy handle (900s bounded keep) would still say so."""
    state = ClaudeStreamState()
    _feed(state, _tool_use("Agent", "toolu_a", {"description": "x", "prompt": "y"}))
    assert "toolu_a" in state.live_bg_agents  # legacy handle registered
    _feed(state, _started_agent("a1", "toolu_a"))
    _feed(state, _notification("a1", "toolu_a", "completed"))
    assert has_live_background_work(state) is False


def test_legacy_fallback_when_no_task_events_seen() -> None:
    state = ClaudeStreamState()
    state.live_bg_bashes.add("toolu_X")
    state.bg_bash_deadlines["toolu_X"] = time.monotonic() + BG_BASH_MAX_KEEP_S
    assert state.native_tasks_seen is False
    assert has_live_background_work(state) is True


def test_schedule_wakeup_legacy_handle_still_counts_with_native_events() -> None:
    """F9: ScheduleWakeup emits no task events, so its handle stays live."""
    state = ClaudeStreamState()
    _feed(state, _started_bash("b1", "toolu_b"))
    _feed(state, _updated("b1", "completed"))
    state.live_wakeups["toolu_w"] = time.monotonic() + 90.0
    assert has_live_background_work(state) is True
    assert background_task_summary(state) == "⏳ 1 watcher"


def test_session_live_bg_count_counts_native_tasks() -> None:
    from untether.runners import claude as claude_mod

    state = ClaudeStreamState()
    _feed(state, _started_bash("b1", "toolu_b"))
    _feed(state, _started_agent("a1", "toolu_a"))
    claude_mod._SESSION_BG_STATE[SID] = state
    try:
        assert claude_mod.session_live_bg_count(SID) == 2
        _feed(state, _updated("b1", "completed"))
        assert claude_mod.session_live_bg_count(SID) == 1
    finally:
        claude_mod._SESSION_BG_STATE.pop(SID, None)


def test_background_task_summary_mixed() -> None:
    state = ClaudeStreamState()
    _feed(state, _started_bash("b1", "toolu_b"))
    _feed(state, _started_agent("a1", "toolu_a"))
    assert background_task_summary(state) == "⏳ 2 bg tasks"


# ── observability (#662) ────────────────────────────────────────────────────


def test_task_registered_and_ended_logs_emitted() -> None:
    state = ClaudeStreamState()
    with capture_logs() as logs:
        _feed(state, _started_agent("a1", "toolu_a"))
        _feed(state, _notification("a1", "toolu_a", "completed"))
    registered = [e for e in logs if e["event"] == "claude.task.registered"]
    ended = [e for e in logs if e["event"] == "claude.task.ended"]
    assert len(registered) == 1
    assert registered[0]["task_id"] == "a1"
    assert registered[0]["task_type"] == "local_agent"
    assert registered[0]["is_backgrounded"] is True
    assert registered[0]["log_level"] == "info"
    assert len(ended) == 1
    assert ended[0]["status"] == "completed"
    assert ended[0]["reason"] == "task_notification"


def test_task_ended_logged_once_for_updated_then_notification() -> None:
    state = ClaudeStreamState()
    with capture_logs() as logs:
        _feed(state, _started_bash("b1", "toolu_b"))
        _feed(state, _updated("b1", "completed"))
        _feed(state, _notification("b1", "toolu_b", "completed"))
    assert sum(1 for e in logs if e["event"] == "claude.task.ended") == 1


def test_foreground_subagent_task_logged_at_debug() -> None:
    state = ClaudeStreamState()
    with capture_logs() as logs:
        _feed(
            state,
            {
                "type": "system",
                "subtype": "task_started",
                "task_id": "f1",
                "owned_by_subagent": True,
                "is_backgrounded": False,
                "task_type": "local_bash",
            },
        )
    registered = [e for e in logs if e["event"] == "claude.task.registered"]
    assert registered[0]["log_level"] == "debug"


def test_verbatim_bg_bash_wake_sequence_flips_liveness_on_task_updated() -> None:
    """Exit gate: F1 sequence. Liveness follows task_updated, not the
    'running in background' tool_result."""
    state = ClaudeStreamState()
    _feed(
        state,
        _tool_use(
            "Bash", "toolu_b", {"command": "sleep 20", "run_in_background": True}
        ),
    )
    _feed(state, _snapshot(("b1", "local_bash", "sleep 20")))
    _feed(state, _started_bash("b1", "toolu_b"))
    _feed(
        state,
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_b",
                        "content": "Command running in background with ID: b1.",
                    }
                ],
            },
        },
    )
    assert has_live_background_work(state) is True  # still running
    _feed(state, _snapshot())
    _feed(state, _updated("b1", "completed"))
    _feed(state, _notification("b1", "toolu_b", "completed"))
    assert has_live_background_work(state) is False
