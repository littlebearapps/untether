"""#819 — Claude context compaction: 🗜️ rows, liveness, the summary frame.

Frame shapes are the Z10 (manual ``/compact``) and DOCS/BINARY (auto)
sequences from ``docs/findings/2026-09-30-claude-sdk-control-permissions-
context.md`` Q4:

    system/status {"status":"compacting"}            # start; 30 s heartbeat
    system/status {"status":null,"compact_result":"success"|"failed"}
    system/init   (fresh, mid-command)
    system/compact_boundary {"compact_metadata":{trigger, pre_tokens, …}}
    user  (compaction summary)
    user  {"isReplay":true, "<local-command-stdout>Compacted …"}   # manual
    result {"num_turns":0,"result":"", …}                          # manual
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from structlog.testing import capture_logs

from untether.model import ActionEvent, CompletedEvent, StartedEvent, TurnEvent
from untether.progress import ProgressTracker
from untether.runners import claude as claude_mod
from untether.runners.claude import (
    ClaudeStreamState,
    _completed_keeps_session_live,
    translate_claude_event,
)
from untether.schemas import claude as claude_schema

SID = "sess-819-compact"
HAIKU = "claude-haiku-4-5"
COMPACTING = "🗜️ Compacting context…"


@pytest.fixture(autouse=True)
def _clear_context_caches() -> None:
    claude_mod._CONTEXT_WINDOWS.clear()
    claude_mod._CONTEXT_WINDOW_MISSES.clear()
    claude_mod._CONTEXT_OVER_WINDOW_WARNED.clear()
    yield
    claude_mod._CONTEXT_WINDOWS.clear()
    claude_mod._CONTEXT_WINDOW_MISSES.clear()
    claude_mod._CONTEXT_OVER_WINDOW_WARNED.clear()


# ── frame builders ──────────────────────────────────────────────────────────


def _feed(state: ClaudeStreamState, payload: dict[str, Any]) -> list[Any]:
    event = claude_schema.decode_stream_json_line(
        json.dumps({"uuid": "u", "session_id": SID, **payload}).encode()
    )
    return translate_claude_event(
        event, title="claude", state=state, factory=state.factory
    )


def _init(model: str = HAIKU) -> dict[str, Any]:
    return {"type": "system", "subtype": "init", "model": model, "cwd": "/tmp"}


def _compacting() -> dict[str, Any]:
    return {"type": "system", "subtype": "status", "status": "compacting"}


def _status_done(result: str | None = "success", **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": "system", "subtype": "status", "status": None}
    if result is not None:
        payload["compact_result"] = result
    payload.update(extra)
    return payload


def _boundary(
    trigger: Any = "manual", pre: Any = 6336, post: Any = 277
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "trigger": trigger,
        "pre_tokens": pre,
        "cumulative_dropped_tokens": 6059,
        "duration_ms": 47,
    }
    if post is not None:
        meta["post_tokens"] = post
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "compact_metadata": meta,
        "logical_parent_uuid": "lp-1",
    }


def _summary_user(**flags: Any) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": "This session is being continued from…"}
            ],
        },
        **flags,
    }


def _replay_user() -> dict[str, Any]:
    return {
        "type": "user",
        "isReplay": True,
        "message": {
            "role": "user",
            "content": "<local-command-stdout>Compacted </local-command-stdout>",
        },
    }


def _prompt_user(text: str = "hello") -> dict[str, Any]:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _assistant(text: str = "ok", used: int | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "model": HAIKU,
        "content": [{"type": "text", "text": text}],
    }
    if used is not None:
        message["usage"] = {"input_tokens": used, "output_tokens": 3}
    return {"type": "assistant", "message": message}


def _result(
    answer: str = "",
    *,
    turns: int = 0,
    api_ms: int = 0,
    is_error: bool = False,
    model_usage: Any = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "result",
        "subtype": "error_during_execution" if is_error else "success",
        "is_error": is_error,
        "duration_ms": 60,
        "duration_api_ms": api_ms,
        "num_turns": turns,
        "result": answer,
        "total_cost_usd": 0.0,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    if model_usage is not None:
        payload["modelUsage"] = model_usage
    return payload


def _rows(events: list[Any]) -> list[ActionEvent]:
    return [
        e
        for e in events
        if isinstance(e, ActionEvent)
        and str(e.action.id).startswith("claude.compaction.")
    ]


def _telemetry(events: list[Any]) -> list[ActionEvent]:
    return [
        e for e in events if isinstance(e, ActionEvent) and e.action.kind == "telemetry"
    ]


def _first_run_state() -> ClaudeStreamState:
    state = ClaudeStreamState()
    _feed(state, _init())
    return state


def _live_state() -> ClaudeStreamState:
    """A live session whose first turn has already completed."""
    state = ClaudeStreamState()
    state.live_mode = True
    _feed(state, _init())
    _feed(state, _assistant("first answer"))
    _feed(state, _result("first answer", turns=1, api_ms=900))
    assert state.completed_turns == 1 and not state.turn_open
    return state


# ── rows ────────────────────────────────────────────────────────────────────


def test_status_compacting_opens_row() -> None:
    state = _first_run_state()
    with capture_logs() as logs:
        events = _feed(state, _compacting())
    [row] = _rows(events)
    assert row.phase == "started"
    assert row.action.kind == "note"
    assert row.action.title == COMPACTING
    assert state.compaction_action_id == row.action.id
    assert any(e["event"] == "claude.compaction.started" for e in logs)


def test_compacting_heartbeat_updates_same_row() -> None:
    state = _first_run_state()
    tracker = ProgressTracker(engine="claude")
    for evt in _feed(state, _compacting()):
        tracker.note_event(evt)
    [beat] = _rows(_feed(state, _compacting()))
    assert beat.phase == "updated"
    assert beat.action.id == state.compaction_action_id
    tracker.note_event(beat)
    assert tracker.action_count == 1


def test_status_null_success_completes_row() -> None:
    state = _first_run_state()
    [start] = _rows(_feed(state, _compacting()))
    [done] = _rows(_feed(state, _status_done("success")))
    assert done.phase == "completed"
    assert done.ok is True
    assert done.action.id == start.action.id
    assert done.action.title == "🗜️ Context compacted"
    assert state.compaction_action_id is None
    assert state.compaction_refine_id == start.action.id
    assert state.last_compact_result == "success"


def test_compact_boundary_refines_title_with_tokens() -> None:
    """D10: start, success and the boundary refinement are one step."""
    state = _first_run_state()
    tracker = ProgressTracker(engine="claude")
    events = [
        *_feed(state, _compacting()),
        *_feed(state, _status_done("success")),
        *_feed(state, _boundary()),
    ]
    for evt in events:
        tracker.note_event(evt)
    rows = _rows(events)
    assert {r.action.id for r in rows} == {rows[0].action.id}
    assert rows[-1].action.title == "🗜️ Context compacted · 6.3k → 277 tokens (manual)"
    assert rows[-1].phase == "completed"
    assert tracker.action_count == 1
    assert state.turn_compactions == [
        {
            "trigger": "manual",
            "pre_tokens": 6336,
            "post_tokens": 277,
            "result": "success",
        }
    ]
    assert state.compaction_refine_id is None


def test_boundary_without_post_tokens_omits_arrow() -> None:
    state = _first_run_state()
    [row] = _rows(_feed(state, _boundary(trigger="auto", pre=182_000, post=None)))
    assert "→" not in row.action.title
    assert row.action.title == "🗜️ Context compacted · 182k tokens before (auto)"


@pytest.mark.parametrize(
    ("meta_trigger", "pre", "title"),
    [
        (None, None, "🗜️ Context compacted"),
        (7, "x", "🗜️ Context compacted"),
        ("auto", True, "🗜️ Context compacted (auto)"),
    ],
)
def test_boundary_malformed_metadata(meta_trigger: Any, pre: Any, title: str) -> None:
    state = _first_run_state()
    [row] = _rows(_feed(state, _boundary(trigger=meta_trigger, pre=pre, post=None)))
    assert row.action.title == title


def test_boundary_metadata_not_a_dict() -> None:
    state = _first_run_state()
    events = _feed(
        state,
        {"type": "system", "subtype": "compact_boundary", "compact_metadata": "odd"},
    )
    [row] = _rows(events)
    assert row.action.title == "🗜️ Context compacted"
    assert state.turn_compactions[0]["trigger"] is None


def test_boundary_without_prior_status_creates_row() -> None:
    state = _first_run_state()
    [row] = _rows(_feed(state, _boundary()))
    assert row.phase == "completed"
    assert row.action.id.startswith("claude.compaction.")


def test_boundary_while_row_open_closes_it() -> None:
    """Defensive: a boundary before any ``status: null`` finishes the open
    row rather than leaving it running."""
    state = _first_run_state()
    [start] = _rows(_feed(state, _compacting()))
    [row] = _rows(_feed(state, _boundary()))
    assert row.action.id == start.action.id
    assert state.compaction_action_id is None
    assert not state.awaiting_compaction()


def test_compaction_failed_row_is_warning() -> None:
    state = _first_run_state()
    _feed(state, _compacting())
    long_error = "prompt too long " * 20
    with capture_logs() as logs:
        [row] = _rows(_feed(state, _status_done("failed", compact_error=long_error)))
    assert row.phase == "completed"
    assert row.ok is False
    assert row.level == "warning"
    assert row.action.title.startswith("🗜️ Compaction failed · prompt too long")
    assert len(row.action.title) <= len("🗜️ Compaction failed · ") + 80
    assert row.action.title.endswith("…")
    warns = [e for e in logs if e["event"] == "claude.compaction.failed"]
    assert warns and warns[0]["log_level"] == "warning"
    assert state.turn_compactions == [
        {"trigger": None, "pre_tokens": None, "post_tokens": None, "result": "failed"}
    ]
    assert not state.awaiting_compaction()


def test_compaction_failed_without_open_row_creates_one() -> None:
    state = _first_run_state()
    [row] = _rows(_feed(state, _status_done("failed")))
    assert row.action.title == "🗜️ Compaction failed"
    assert row.ok is False


def test_status_null_without_open_row_is_ignored() -> None:
    """The #383 permission-mode frame: no events, no compaction state."""
    state = _first_run_state()
    assert _feed(state, _status_done(None, permissionMode="default")) == []
    assert state.turn_compactions == []
    assert state.effective_permission_mode == "default"


def test_status_requesting_ignored() -> None:
    state = _first_run_state()
    assert (
        _feed(state, {"type": "system", "subtype": "status", "status": "requesting"})
        == []
    )
    assert state.compaction_action_id is None
    assert not state.awaiting_compaction()


def test_status_null_no_result_with_open_row_is_skipped() -> None:
    """A PreCompact hook skipped the compaction: plain ``status: null``."""
    state = _first_run_state()
    _feed(state, _compacting())
    [row] = _rows(_feed(state, _status_done(None)))
    assert row.action.title == "🗜️ Compaction skipped"
    assert row.ok is True
    assert state.compaction_action_id is None
    assert not state.awaiting_compaction()


def test_permission_mode_frame_while_compacting_keeps_row_open() -> None:
    state = _first_run_state()
    _feed(state, _compacting())
    assert _feed(state, _status_done(None, permissionMode="default")) == []
    assert state.compaction_action_id is not None
    assert state.awaiting_compaction()
    assert state.effective_permission_mode == "default"


def test_permission_mode_on_compacting_frame_still_noted() -> None:
    """#383's branch survives inside the shared handler."""
    state = _first_run_state()
    events = _feed(
        state,
        {
            "type": "system",
            "subtype": "status",
            "status": "compacting",
            "permissionMode": "plan",
        },
    )
    assert len(_rows(events)) == 1
    assert state.effective_permission_mode == "plan"


def test_boundary_clears_context_pct() -> None:
    claude_mod._CONTEXT_WINDOWS[HAIKU] = 200_000
    state = _first_run_state()
    _feed(state, _assistant(used=100_000))
    events = _feed(state, _boundary())
    [tel] = _telemetry(events)
    assert tel.action.detail["context_pct"] is None
    # The row comes first, then the cleared value.
    assert events.index(_rows(events)[0]) < events.index(tel)


# ── liveness latch ──────────────────────────────────────────────────────────


def test_awaiting_compaction_latch_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(claude_mod.time, "monotonic", lambda: now[0])
    state = _first_run_state()
    _feed(state, _compacting())
    assert state.awaiting_compaction()
    now[0] += 90.0
    _feed(state, _compacting())  # heartbeat refreshes the latch
    now[0] += 119.0
    assert state.awaiting_compaction()
    now[0] += 2.0  # 121 s after the last heartbeat
    assert not state.awaiting_compaction()


def test_awaiting_compaction_cleared_by_status_null() -> None:
    state = _first_run_state()
    _feed(state, _compacting())
    assert state.awaiting_compaction()
    _feed(state, _status_done("success"))
    assert not state.awaiting_compaction()


def test_awaiting_compaction_cleared_at_result() -> None:
    state = _first_run_state()
    _feed(state, _compacting())
    _feed(state, _result("x", turns=1, api_ms=5))
    assert not state.awaiting_compaction()
    assert state.compaction_action_id is None


# ── the summary frame vs the #544/#333 per-turn resets ─────────────────────


def test_summary_user_frame_does_not_reset_wakeup_scalar() -> None:
    state = _first_run_state()
    state.last_schedule_wakeup_arm_delay = 300.0
    state.last_bg_bash_launched_at = 5.0
    _feed(state, _compacting())
    _feed(state, _status_done("success"))
    _feed(state, _boundary(trigger="auto"))
    assert state.compaction_summary_pending
    _feed(state, _summary_user())
    assert not state.compaction_summary_pending
    assert state.last_schedule_wakeup_arm_delay == 300.0
    assert state.last_bg_bash_launched_at == 5.0


def test_is_compact_summary_flag_alone_skips_reset() -> None:
    state = _first_run_state()
    state.last_schedule_wakeup_arm_delay = 300.0
    _feed(state, _summary_user(isCompactSummary=True))
    assert state.last_schedule_wakeup_arm_delay == 300.0


def test_normal_user_prompt_still_resets_wakeup_scalar() -> None:
    state = _first_run_state()
    state.last_schedule_wakeup_arm_delay = 300.0
    state.last_bg_bash_launched_at = 5.0
    _feed(state, _prompt_user())
    assert state.last_schedule_wakeup_arm_delay is None
    assert state.last_bg_bash_launched_at is None


def test_summary_flag_cleared_at_result_and_turn_open() -> None:
    """A CLI that omits the summary frame can't make the next real prompt
    skip its reset."""
    state = _first_run_state()
    _feed(state, _boundary())
    assert state.compaction_summary_pending
    _feed(state, _result("x", turns=1, api_ms=5))
    assert not state.compaction_summary_pending
    state.last_schedule_wakeup_arm_delay = 300.0
    _feed(state, _prompt_user())
    assert state.last_schedule_wakeup_arm_delay is None

    live = _live_state()
    _feed(live, _init())  # turn 2 opens
    _feed(live, _boundary())
    assert live.compaction_summary_pending
    _feed(live, _assistant("done"))
    _feed(live, _result("done", turns=1, api_ms=5))
    live.compaction_summary_pending = True  # as if a boundary landed idle
    _feed(live, _init())  # turn 3 opens
    assert not live.compaction_summary_pending


def test_tool_result_frame_does_not_consume_summary_flag() -> None:
    state = _first_run_state()
    _feed(state, _boundary())
    _feed(
        state,
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t", "content": "x"}
                ],
            },
        },
    )
    assert state.compaction_summary_pending


# ── sequences ───────────────────────────────────────────────────────────────


def test_z10_sequence_first_run() -> None:
    """Probe Z10 as a first run: rows, one supplementary StartedEvent with
    the same resume for the fresh init, a 0-turn CompletedEvent."""
    state = ClaudeStreamState()
    events: list[Any] = []
    for frame in (
        _init(),
        _compacting(),
        _status_done("success"),
        _init(),
        _boundary(),
        _summary_user(),
        _replay_user(),
        _result(""),
    ):
        events.extend(_feed(state, frame))
    started = [e for e in events if isinstance(e, StartedEvent)]
    assert len({e.resume.value for e in started}) == 1
    rows = _rows(events)
    assert rows[0].phase == "started"
    assert rows[-1].action.title == "🗜️ Context compacted · 6.3k → 277 tokens (manual)"
    completed = events[-1]
    assert isinstance(completed, CompletedEvent)
    assert completed.ok is True
    assert completed.answer == ""
    assert state.turn_compactions == []  # reset at the result


def test_live_compact_followup_opens_turn_at_compacting() -> None:
    state = _live_state()
    state.pending_command_uuid = "cmd-compact"
    state.injected_commands["cmd-compact"] = time.monotonic()
    turns: list[TurnEvent] = []
    rows: list[ActionEvent] = []
    for frame in (
        _compacting(),
        _status_done("success"),
        _init(),
        _boundary(),
        _summary_user(),
        _replay_user(),
        _result(""),
    ):
        events = _feed(state, frame)
        turns.extend(e for e in events if isinstance(e, TurnEvent))
        rows.extend(_rows(events))
        assert not any(isinstance(e, (StartedEvent, CompletedEvent)) for e in events)
    assert [t.phase for t in turns] == ["started", "completed"]
    assert turns[0].reason == "followup"
    assert turns[0].turn == turns[1].turn
    assert rows and rows[0].phase == "started"
    assert rows[-1].action.title.endswith("(manual)")


def test_auto_compaction_mid_turn_single_turn() -> None:
    """Auto compaction inside a live follow-up turn: one turn, the rows in it,
    ``% ctx`` dropped then back lower."""
    claude_mod._CONTEXT_WINDOWS[HAIKU] = 200_000
    state = _live_state()
    events: list[Any] = []
    for frame in (
        _init(),
        _assistant("reading", used=160_000),
        _compacting(),
        _compacting(),
        _status_done("success"),
        _init(),
        _boundary(trigger="auto", pre=160_000, post=30_000),
        _summary_user(),
        _assistant("done", used=40_000),
        _result("done", turns=2, api_ms=900),
    ):
        events.extend(_feed(state, frame))
    turns = [e for e in events if isinstance(e, TurnEvent)]
    assert [t.phase for t in turns] == ["started", "completed"]
    pcts = [e.action.detail["context_pct"] for e in _telemetry(events)]
    assert pcts == [80, None, 20]
    assert _rows(events)[-1].action.title == (
        "🗜️ Context compacted · 160k → 30k tokens (auto)"
    )


# ── keep-live predicate negative control (C4 widens it) ─────────────────────


def test_plain_zero_turn_empty_result_not_kept_live() -> None:
    evt = CompletedEvent(
        engine="claude",
        ok=True,
        answer="",
        resume=None,
        usage={"num_turns": 0, "duration_api_ms": 0},
    )
    assert _completed_keeps_session_live(evt) is False


# ── C4: usage["compaction"] and the manual-only exemption ──────────────────


def _manual_compact(state: ClaudeStreamState) -> list[Any]:
    events: list[Any] = []
    for frame in (
        _compacting(),
        _status_done("success"),
        _init(),
        _boundary(),
        _summary_user(),
        _replay_user(),
        _result(""),
    ):
        events.extend(_feed(state, frame))
    return events


def test_usage_compaction_on_completed_event() -> None:
    state = _first_run_state()
    completed = _manual_compact(state)[-1]
    assert isinstance(completed, CompletedEvent)
    assert completed.usage["compaction"] == {
        "count": 1,
        "trigger": "manual",
        "pre_tokens": 6336,
        "post_tokens": 277,
        "result": "success",
        "manual_success": True,
    }
    assert _completed_keeps_session_live(completed) is True


def test_usage_compaction_on_live_turn_event() -> None:
    state = _live_state()
    events = _manual_compact(state)
    turn_done = [e for e in events if isinstance(e, TurnEvent)][-1]
    assert turn_done.phase == "completed"
    assert turn_done.usage["compaction"]["manual_success"] is True
    # The record ends with the segment: the next turn starts clean.
    assert state.turn_compactions == []


def test_auto_compaction_is_not_manual_success() -> None:
    state = _first_run_state()
    for frame in (_compacting(), _status_done("success"), _boundary(trigger="auto")):
        _feed(state, frame)
    completed = _feed(state, _result(""))[-1]
    assert completed.usage["compaction"]["trigger"] == "auto"
    assert completed.usage["compaction"]["manual_success"] is False
    assert _completed_keeps_session_live(completed) is False


def test_failed_compaction_is_not_manual_success() -> None:
    state = _first_run_state()
    _feed(state, _compacting())
    _feed(state, _status_done("failed", compact_error="nope"))
    completed = _feed(state, _result(""))[-1]
    assert completed.usage["compaction"]["result"] == "failed"
    assert completed.usage["compaction"]["manual_success"] is False


def test_manual_boundary_without_success_status_is_not_manual_success() -> None:
    """Condition 3: a boundary with no preceding ``compact_result: success``."""
    state = _first_run_state()
    _feed(state, _boundary())
    completed = _feed(state, _result(""))[-1]
    assert completed.usage["compaction"]["result"] is None
    assert completed.usage["compaction"]["manual_success"] is False


def test_error_result_is_not_manual_success() -> None:
    state = _first_run_state()
    for frame in (_compacting(), _status_done("success"), _boundary()):
        _feed(state, frame)
    completed = _feed(state, _result("boom", is_error=True))[-1]
    assert completed.ok is False
    assert completed.usage["compaction"]["manual_success"] is False


def test_auto_then_manual_in_one_segment_is_not_manual_success() -> None:
    state = _first_run_state()
    for trigger in ("auto", "manual"):
        for frame in (_compacting(), _status_done("success"), _boundary(trigger)):
            _feed(state, frame)
    completed = _feed(state, _result(""))[-1]
    assert completed.usage["compaction"]["count"] == 2
    assert completed.usage["compaction"]["trigger"] == "manual"
    assert completed.usage["compaction"]["manual_success"] is False


def test_no_compaction_no_usage_key() -> None:
    state = _first_run_state()
    _feed(state, _assistant("hi"))
    completed = _feed(state, _result("hi", turns=1, api_ms=5))[-1]
    assert "compaction" not in (completed.usage or {})


def test_resume_guard_never_absorbs_compaction_result() -> None:
    """A resumed run whose stopped-task replay looks like the 0-turn result
    is absorbed — unless the segment compacted."""
    state = ClaudeStreamState()
    state.resumed = True
    state.live_mode = True
    _feed(state, _init())
    _feed(
        state,
        {
            "type": "system",
            "subtype": "task_notification",
            "task_id": "t1",
            "status": "stopped",
        },
    )
    assert state.stopped_notification_pre_output
    for frame in (_compacting(), _status_done("success"), _boundary()):
        _feed(state, frame)
    with capture_logs() as logs:
        events = _feed(state, _result(""))
    assert not any(e["event"] == "claude.resume_guard.absorbed" for e in logs)
    completed = [e for e in events if isinstance(e, CompletedEvent)]
    assert completed and completed[0].usage["compaction"]["manual_success"] is True
