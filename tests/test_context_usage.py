"""#819 — context-window use (``N% ctx``) for Claude.

Schema decoding of the frames the feature reads (the Z10 / Z11 shapes from
``docs/findings/2026-09-30-claude-sdk-control-permissions-context.md`` Q4/Q5),
the tracker's ``telemetry`` branch, the runner's context maths and live-turn
re-emit, header rendering, the ``[progress] show_context_usage`` toggle, and
the Codex / OpenCode regression (Claude-only; the Codex half is #832).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import msgspec
import pytest
from structlog.testing import capture_logs

from untether.markdown import MarkdownFormatter, format_header
from untether.model import (
    Action,
    ActionEvent,
    CompletedEvent,
    StartedEvent,
    TurnEvent,
)
from untether.progress import ProgressTracker
from untether.runners import claude as claude_mod
from untether.runners.claude import (
    ClaudeStreamState,
    _context_pct,
    _context_window_for,
    _usage_context_tokens,
    translate_claude_event,
)
from untether.schemas import claude as claude_schema


def _decode(obj: dict) -> claude_schema.StreamJsonMessage:
    return claude_schema.decode_stream_json_line(json.dumps(obj))


def test_assistant_usage_decodes() -> None:
    evt = _decode(
        {
            "type": "assistant",
            "session_id": "s",
            "message": {
                "role": "assistant",
                "model": "claude-haiku-4-5",
                "content": [{"type": "text", "text": "hi"}],
                "usage": {
                    "input_tokens": 1234,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 185000,
                    "output_tokens": 5,
                },
            },
        }
    )
    assert isinstance(evt, claude_schema.StreamAssistantMessage)
    assert evt.message.usage["cache_read_input_tokens"] == 185000


def test_result_model_usage_decodes() -> None:
    evt = _decode(
        {
            "type": "result",
            "subtype": "success",
            "duration_ms": 10,
            "duration_api_ms": 5,
            "is_error": False,
            "num_turns": 1,
            "session_id": "s",
            "result": "ok",
            "modelUsage": {
                "claude-haiku-4-5": {"contextWindow": 200000, "maxOutputTokens": 64000}
            },
        }
    )
    assert isinstance(evt, claude_schema.StreamResultMessage)
    assert evt.modelUsage["claude-haiku-4-5"]["contextWindow"] == 200000


@pytest.mark.parametrize(
    "frame",
    [
        {"type": "system", "subtype": "status", "status": "compacting"},
        {
            "type": "system",
            "subtype": "status",
            "status": None,
            "compact_result": "failed",
            "compact_error": "boom",
        },
        {
            "type": "system",
            "subtype": "compact_boundary",
            "compact_metadata": {
                "trigger": "manual",
                "pre_tokens": 6336,
                "post_tokens": 277,
                "cumulative_dropped_tokens": 6059,
                "duration_ms": 47,
            },
            "logical_parent_uuid": "u-1",
        },
        # A clashing type must never drop the line (every field is Any).
        {
            "type": "system",
            "subtype": "compact_boundary",
            "compact_metadata": "odd",
            "compact_result": 3,
            "logical_parent_uuid": ["x"],
        },
    ],
)
def test_compaction_system_frames_decode(frame: dict) -> None:
    evt = _decode({**frame, "session_id": "s", "uuid": "u"})
    assert isinstance(evt, claude_schema.StreamSystemMessage)
    assert evt.subtype == frame["subtype"]
    for key in ("compact_metadata", "compact_result", "compact_error"):
        assert getattr(evt, key) == frame.get(key)


def test_user_replay_and_summary_flags_decode() -> None:
    evt = _decode(
        {
            "type": "user",
            "session_id": "s",
            "isReplay": True,
            "isCompactSummary": True,
            "message": {"role": "user", "content": "This session is being continued"},
        }
    )
    assert isinstance(evt, claude_schema.StreamUserMessage)
    assert evt.isReplay is True
    assert evt.isCompactSummary is True


def test_frames_without_new_fields_default_to_none() -> None:
    evt = _decode(
        {
            "type": "assistant",
            "session_id": "s",
            "message": {"role": "assistant", "model": "m", "content": []},
        }
    )
    assert isinstance(evt, claude_schema.StreamAssistantMessage)
    assert evt.message.usage is None


# ---------------------------------------------------------------------------
# C2 — tracker, runner maths and translation
# ---------------------------------------------------------------------------

SID = "sess-819"
HAIKU = "claude-haiku-4-5"
SONNET = "claude-sonnet-5-5"
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _clear_context_caches() -> None:
    claude_mod._CONTEXT_WINDOWS.clear()
    claude_mod._CONTEXT_WINDOW_MISSES.clear()
    claude_mod._CONTEXT_OVER_WINDOW_WARNED.clear()
    yield
    claude_mod._CONTEXT_WINDOWS.clear()
    claude_mod._CONTEXT_WINDOW_MISSES.clear()
    claude_mod._CONTEXT_OVER_WINDOW_WARNED.clear()


def _telemetry(pct: int | None, **extra: Any) -> ActionEvent:
    return ActionEvent(
        engine="claude",
        action=Action(
            id="claude.context",
            kind="telemetry",
            title="context",
            detail={"context_pct": pct, **extra},
        ),
        phase="updated",
    )


def _translate(state: ClaudeStreamState, payload: dict[str, Any]) -> list[Any]:
    event = msgspec.json.decode(
        msgspec.json.encode(payload), type=claude_schema.StreamJsonMessage
    )
    return translate_claude_event(
        event, title="claude", state=state, factory=state.factory
    )


def _init(model: str = HAIKU) -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "init",
        "session_id": SID,
        "model": model,
        "cwd": "/tmp",
        "tools": [],
    }


def _usage(inp: int, create: int = 0, read: int = 0) -> dict[str, Any]:
    return {
        "input_tokens": inp,
        "cache_creation_input_tokens": create,
        "cache_read_input_tokens": read,
        "output_tokens": 7,
    }


def _assistant(
    usage: Any,
    *,
    model: str = HAIKU,
    parent: str | None = None,
    text: str = "answer",
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "session_id": SID,
        "parent_tool_use_id": parent,
        "message": {
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
            "usage": usage,
        },
    }


def _result(model_usage: Any = None, *, num_turns: int = 1) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "duration_ms": 100,
        "duration_api_ms": 90,
        "is_error": False,
        "num_turns": num_turns,
        "session_id": SID,
        "result": "answer",
        "total_cost_usd": 0.01,
    }
    if model_usage is not None:
        payload["modelUsage"] = model_usage
    return payload


def _mu(model: str = HAIKU, window: Any = 200_000) -> dict[str, Any]:
    return {model: {"contextWindow": window, "maxOutputTokens": 64_000}}


def _state(model: str = HAIKU) -> ClaudeStreamState:
    state = ClaudeStreamState()
    _translate(state, _init(model))
    return state


def _ctx(events: list[Any]) -> list[ActionEvent]:
    return [
        e for e in events if isinstance(e, ActionEvent) and e.action.kind == "telemetry"
    ]


def _pcts(events: list[Any]) -> list[int | None]:
    return [e.action.detail["context_pct"] for e in _ctx(events)]


# ── tracker ─────────────────────────────────────────────────────────────────


def test_tracker_telemetry_sets_pct_without_step() -> None:
    tracker = ProgressTracker(engine="claude")
    assert tracker.note_event(_telemetry(42)) is True
    assert tracker.context_pct == 42
    assert tracker.action_count == 0
    snap = tracker.snapshot()
    assert snap.actions == ()
    assert snap.context_pct == 42


def test_tracker_telemetry_same_value_returns_false() -> None:
    tracker = ProgressTracker(engine="claude")
    assert tracker.note_event(_telemetry(42)) is True
    assert tracker.note_event(_telemetry(42)) is False


def test_tracker_telemetry_none_clears() -> None:
    tracker = ProgressTracker(engine="claude")
    tracker.note_event(_telemetry(42))
    assert tracker.note_event(_telemetry(None)) is True
    assert tracker.context_pct is None
    assert tracker.snapshot().context_pct is None


@pytest.mark.parametrize("bad", ["42", 42.0, True, -1, 101, [1]])
def test_tracker_telemetry_rejects_malformed_values(bad: Any) -> None:
    tracker = ProgressTracker(engine="claude")
    tracker.note_event(_telemetry(10))
    assert tracker.note_event(_telemetry(bad)) is False
    assert tracker.context_pct == 10


def test_tracker_telemetry_without_context_key_ignored() -> None:
    tracker = ProgressTracker(engine="claude")
    event = ActionEvent(
        engine="claude",
        action=Action(id="x", kind="telemetry", title="t", detail={"other": 1}),
        phase="updated",
    )
    assert tracker.note_event(event) is False
    assert tracker.action_count == 0


# ── maths ───────────────────────────────────────────────────────────────────


def test_context_pct_usage_part_matches_probe_z11() -> None:
    used = _usage_context_tokens(_usage(1234, 100, 185000))
    assert used == 186334
    assert _context_pct(used, 200_000) == 93


def test_context_pct_half_up_rounding() -> None:
    assert _context_pct(125, 1000) == 13  # banker's round() would give 12
    assert _context_pct(124, 1000) == 12
    assert _context_pct(0, 1000) == 0
    assert _context_pct(2500, 1000) == 250  # raw; the caller clamps


@pytest.mark.parametrize(
    "usage",
    [
        None,
        [],
        "x",
        {},
        {"input_tokens": "5"},
        {"input_tokens": True},
        {"input_tokens": None},
        {"input_tokens": -3},
    ],
)
def test_malformed_usage_ignored(usage: Any) -> None:
    assert _usage_context_tokens(usage) is None


def test_usage_partial_fields_summed() -> None:
    assert (
        _usage_context_tokens({"input_tokens": 5, "cache_read_input_tokens": 7}) == 12
    )


def test_one_m_suffix_rule() -> None:
    assert _context_window_for(f"{SONNET}[1m]", None) == 1_000_000
    assert _context_window_for(None, f"{SONNET}[1m]") == 1_000_000
    assert _context_window_for(SONNET, f"{SONNET}[1m]") == 1_000_000
    # A different model in a [1m] session is not the [1m] model.
    assert _context_window_for(HAIKU, f"{SONNET}[1m]") is None


def test_one_m_session_model_wins_over_stripped_cache_hit() -> None:
    claude_mod._CONTEXT_WINDOWS[SONNET] = 200_000
    assert _context_window_for(SONNET, f"{SONNET}[1m]") == 1_000_000
    assert _context_window_for(SONNET, SONNET) == 200_000


def test_dated_model_id_is_a_miss_not_a_fuzzy_match() -> None:
    claude_mod._CONTEXT_WINDOWS[HAIKU] = 200_000
    with capture_logs() as logs:
        assert _context_window_for(f"{HAIKU}-20251001", None) is None
        assert _context_window_for(f"{HAIKU}-20251001", None) is None
    misses = [log for log in logs if log["event"] == "claude.context.window_miss"]
    assert len(misses) == 1
    assert misses[0]["model"] == f"{HAIKU}-20251001"
    assert misses[0]["known"] == [HAIKU]


def test_session_model_cache_hit_used_when_message_model_unknown() -> None:
    claude_mod._CONTEXT_WINDOWS[HAIKU] = 200_000
    assert _context_window_for("claude-other", HAIKU) == 200_000


# ── translation ─────────────────────────────────────────────────────────────


def test_window_unknown_until_result_then_emitted_before_completed() -> None:
    state = _state()
    assert _ctx(_translate(state, _assistant(_usage(1234, 100, 185000)))) == []
    events = _translate(state, _result(_mu()))
    kinds = [type(e) for e in events]
    assert kinds == [ActionEvent, StartedEvent, CompletedEvent]
    assert _pcts(events) == [93]
    tel = _ctx(events)[0]
    assert tel.phase == "updated"
    assert tel.action.id == "claude.context"
    assert tel.action.detail == {
        "context_pct": 93,
        "context_used": 186334,
        "context_window": 200_000,
        "model": HAIKU,
    }


def test_usage_context_on_completed_event() -> None:
    state = _state()
    _translate(state, _assistant(_usage(50_000)))
    completed = next(
        e for e in _translate(state, _result(_mu())) if isinstance(e, CompletedEvent)
    )
    assert completed.usage["context"] == {
        "pct": 25,
        "used": 50_000,
        "window": 200_000,
        "model": HAIKU,
    }


def test_no_usage_context_when_unknown() -> None:
    state = _state()
    completed = next(
        e for e in _translate(state, _result(_mu())) if isinstance(e, CompletedEvent)
    )
    assert "context" not in (completed.usage or {})


def test_window_cache_shared_across_runs_per_model() -> None:
    first = _state()
    _translate(first, _assistant(_usage(10_000)))
    _translate(first, _result(_mu()))
    second = _state()
    assert _pcts(_translate(second, _assistant(_usage(20_000)))) == [10]


def test_window_learned_logged_once_per_model() -> None:
    with capture_logs() as logs:
        for _ in range(2):
            state = _state()
            _translate(state, _assistant(_usage(1)))
            _translate(state, _result(_mu()))
    learned = [log for log in logs if log["event"] == "claude.context.window_learned"]
    assert len(learned) == 1
    assert learned[0]["log_level"] == "info"
    assert (learned[0]["model"], learned[0]["context_window"]) == (HAIKU, 200_000)


@pytest.mark.parametrize("window", ["200000", 0, -5, None, True, 1.5])
def test_malformed_model_usage_ignored(window: Any) -> None:
    state = _state()
    _translate(state, _assistant(_usage(1000)))
    events = _translate(state, _result(_mu(window=window)))
    assert _ctx(events) == []
    assert claude_mod._CONTEXT_WINDOWS == {}


@pytest.mark.parametrize("model_usage", ["x", [], {"m": "x"}, {1: {}}])
def test_malformed_model_usage_shapes_ignored(model_usage: Any) -> None:
    state = _state()
    _translate(state, _assistant(_usage(1000)))
    assert _ctx(_translate(state, _result(model_usage))) == []


def test_dedupe_unchanged_pct_emits_nothing() -> None:
    claude_mod._CONTEXT_WINDOWS[HAIKU] = 200_000
    state = _state()
    assert _pcts(_translate(state, _assistant(_usage(20_000)))) == [10]
    # Same response, second frame; then a new response still at 10 %.
    assert _ctx(_translate(state, _assistant(_usage(20_000)))) == []
    assert _ctx(_translate(state, _assistant(_usage(20_400)))) == []
    assert _pcts(_translate(state, _assistant(_usage(30_000)))) == [15]
    # The result repeats the same value: nothing new.
    assert _ctx(_translate(state, _result(_mu()))) == []


def test_subagent_assistant_usage_ignored() -> None:
    claude_mod._CONTEXT_WINDOWS[HAIKU] = 200_000
    state = _state()
    assert _ctx(_translate(state, _assistant(_usage(90_000), parent="toolu_1"))) == []
    assert state.ctx_used is None


def test_synthetic_model_ignored() -> None:
    claude_mod._CONTEXT_WINDOWS[HAIKU] = 200_000
    state = _state()
    assert (
        _ctx(_translate(state, _assistant(_usage(90_000), model="<synthetic>"))) == []
    )


@pytest.mark.parametrize("usage", [None, "x", [1], {"input_tokens": "1"}])
def test_assistant_without_usable_usage_keeps_previous_value(usage: Any) -> None:
    claude_mod._CONTEXT_WINDOWS[HAIKU] = 200_000
    state = _state()
    _translate(state, _assistant(_usage(20_000)))
    assert _ctx(_translate(state, _assistant(usage))) == []
    assert state.ctx_used == 20_000


def test_plan_mode_model_switch_uses_message_model() -> None:
    claude_mod._CONTEXT_WINDOWS[HAIKU] = 200_000
    claude_mod._CONTEXT_WINDOWS[SONNET] = 1_000_000
    state = _state(HAIKU)
    assert _pcts(_translate(state, _assistant(_usage(100_000)))) == [50]
    assert _pcts(_translate(state, _assistant(_usage(100_000), model=SONNET))) == [10]


def test_context_pct_over_100_warns_once_and_displays_100() -> None:
    claude_mod._CONTEXT_WINDOWS[HAIKU] = 200_000
    state = _state()
    with capture_logs() as logs:
        assert _pcts(_translate(state, _assistant(_usage(250_000)))) == [100]
        assert _ctx(_translate(state, _assistant(_usage(260_000)))) == []
    warns = [log for log in logs if log["event"] == "claude.context.over_window"]
    assert len(warns) == 1
    assert warns[0]["log_level"] == "warning"
    assert (warns[0]["used"], warns[0]["context_window"], warns[0]["pct"]) == (
        250_000,
        200_000,
        125,
    )
    assert warns[0]["session_id"] == SID
    assert warns[0]["model"] == HAIKU


def test_compact_boundary_clears_context_pct() -> None:
    """D5: after a compaction the value is dropped until the next
    main-thread response."""
    claude_mod._CONTEXT_WINDOWS[HAIKU] = 200_000
    state = _state()
    _translate(state, _assistant(_usage(150_000)))
    boundary = {
        "type": "system",
        "subtype": "compact_boundary",
        "session_id": SID,
        "compact_metadata": {"trigger": "auto", "pre_tokens": 150_000},
    }
    assert _pcts(_translate(state, boundary)) == [None]
    assert state.ctx_used is None
    # Nothing more to clear; the result has no value to report either.
    assert _ctx(_translate(state, boundary)) == []
    assert _pcts(_translate(state, _assistant(_usage(40_000)))) == [20]


def test_compact_boundary_before_any_value_emits_nothing() -> None:
    state = _state()
    boundary = {"type": "system", "subtype": "compact_boundary", "session_id": SID}
    assert _translate(state, boundary) == []


def test_init_one_m_model_gives_window_before_first_result() -> None:
    state = _state(f"{SONNET}[1m]")
    assert _pcts(_translate(state, _assistant(_usage(250_000), model=SONNET))) == [25]


# ── live sessions ───────────────────────────────────────────────────────────


def _live_state() -> ClaudeStreamState:
    claude_mod._CONTEXT_WINDOWS[HAIKU] = 200_000
    state = _state()
    state.live_mode = True
    _translate(state, _assistant(_usage(20_000)))
    _translate(state, _result(_mu()))
    assert state.turn_open is False
    return state


def test_turn_open_re_emits_current_value() -> None:
    state = _live_state()
    events = _translate(state, _init())
    assert isinstance(events[0], TurnEvent)
    assert events[0].phase == "started"
    assert _pcts(events) == [10]
    assert events.index(_ctx(events)[0]) == 1


def test_turn_open_without_value_emits_no_telemetry() -> None:
    state = _state()
    state.live_mode = True
    _translate(state, _result(_mu()))
    events = _translate(state, _init())
    assert isinstance(events[0], TurnEvent)
    assert _ctx(events) == []


def test_live_result_forwards_context_before_turn_completed() -> None:
    state = _live_state()
    _translate(state, _init())
    _translate(state, _assistant(_usage(20_000), model="claude-new"))
    events = _translate(state, _result(_mu("claude-new", 100_000)))
    turn_done = [e for e in events if isinstance(e, TurnEvent)]
    assert turn_done and turn_done[-1].phase == "completed"
    assert _pcts(events) == [20]
    assert events.index(_ctx(events)[0]) < events.index(turn_done[-1])
    assert turn_done[-1].usage["context"]["pct"] == 20


def test_no_telemetry_between_live_turns() -> None:
    """A value change while no turn is open is held for the next turn's
    open instead of reaching the run's finalised message."""
    state = _live_state()
    boundary = {"type": "system", "subtype": "compact_boundary", "session_id": SID}
    assert _ctx(_translate(state, boundary)) == []
    assert state.ctx_used is None
    events = _translate(state, _init())
    assert _ctx(events) == []  # nothing known to re-emit
    assert _pcts(_translate(state, _assistant(_usage(60_000)))) == [30]


# ── rendering ───────────────────────────────────────────────────────────────


def test_format_header_appends_context_pct_after_step() -> None:
    assert (
        format_header(96.0, 10, label="done", engine="claude", context_pct=62)
        == "done · claude · 1m 36s · step 10 · 62% ctx"
    )


def test_format_header_context_without_step() -> None:
    assert (
        format_header(12.0, None, label="done", engine="claude", context_pct=8)
        == "done · claude · 12s · 8% ctx"
    )


def test_format_header_omits_context_when_none() -> None:
    assert (
        format_header(12.0, 3, label="working", engine="claude", context_pct=None)
        == format_header(12.0, 3, label="working", engine="claude")
        == "working · claude · 12s · step 3"
    )


def _rendered(show: bool, pct: int | None) -> tuple[str, str, str | None]:
    tracker = ProgressTracker(engine="claude")
    tracker.meta = {"model": HAIKU}
    if pct is not None:
        tracker.note_event(_telemetry(pct))
    state = tracker.snapshot(
        context_line="dir: z80", meta_formatter=lambda m: "haiku 4.5"
    )
    formatter = MarkdownFormatter(show_context_usage=show)
    progress = formatter.render_progress_parts(state, elapsed_s=12.0)
    final = formatter.render_final_parts(
        state, elapsed_s=12.0, status="done", answer="ok"
    )
    assert progress.footer == final.footer
    return progress.header, final.header, final.footer


def test_progress_and_final_headers_show_context() -> None:
    progress, final, _ = _rendered(True, 62)
    assert progress == "working · claude · 12s · 62% ctx"
    assert final == "done · claude · 12s · 62% ctx"


def test_render_final_footer_unchanged_with_context() -> None:
    assert _rendered(True, 62)[2] == _rendered(True, None)[2]
    assert "ctx" not in (_rendered(True, 62)[2] or "").replace("dir: z80", "")


def test_formatter_show_context_usage_false_hides_segment() -> None:
    progress, final, _ = _rendered(False, 62)
    assert "ctx" not in progress
    assert "ctx" not in final


def test_refresh_from_reads_show_context_usage() -> None:
    from untether.settings import ProgressSettings

    formatter = MarkdownFormatter()
    assert formatter.show_context_usage is True
    formatter.refresh_from(ProgressSettings(show_context_usage=False))
    assert formatter.show_context_usage is False
    formatter.refresh_from(ProgressSettings())
    assert formatter.show_context_usage is True


def test_show_context_usage_hot_reloads_through_settings_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The toggle is re-read per run through #506's content-keyed cache —
    an edit applies to the next run, never frozen by the cache."""
    from untether import runner_bridge

    cfg = tmp_path / "untether.toml"
    base = (
        'transport = "telegram"\n[transports.telegram]\nbot_token = "1:t"\n'
        "chat_id = 1\nallow_any_user = true\n[progress]\n"
    )
    monkeypatch.setenv("UNTETHER_CONFIG_PATH", str(cfg))
    formatter = MarkdownFormatter()
    cfg.write_text(base + "show_context_usage = false\n")
    formatter.refresh_from(runner_bridge._load_progress_settings())
    assert formatter.show_context_usage is False
    cfg.write_text(base + "show_context_usage = true\n")
    formatter.refresh_from(runner_bridge._load_progress_settings())
    assert formatter.show_context_usage is True
    cfg.write_text(base)
    formatter.refresh_from(runner_bridge._load_progress_settings())
    assert formatter.show_context_usage is True  # default on


def test_render_event_cli_skips_telemetry() -> None:
    from untether.markdown import render_event_cli

    assert render_event_cli(_telemetry(40)) == []


def test_resolve_presenter_copies_show_context_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from untether import runner_bridge
    from untether.telegram.bridge import TelegramPresenter

    default = TelegramPresenter(
        formatter=MarkdownFormatter(verbosity="compact", show_context_usage=False)
    )
    monkeypatch.setattr(
        "untether.telegram.commands.verbose.get_verbosity_override",
        lambda channel_id: "verbose",
    )
    resolved = runner_bridge._resolve_presenter(default, 123)
    assert resolved is not default
    assert resolved._formatter.verbosity == "verbose"
    assert resolved._formatter.show_context_usage is False


# ── other engines unchanged (#819 ships Claude-only; Codex half = #832) ─────


def _replay_header(events: list[Any], engine: str) -> str:
    tracker = ProgressTracker(engine=engine)
    for evt in events:
        tracker.note_event(evt)
    assert tracker.context_pct is None
    state = tracker.snapshot()
    parts = MarkdownFormatter().render_final_parts(
        state, elapsed_s=5.0, status="done", answer="ok"
    )
    return parts.header


def test_codex_output_has_no_context_segment() -> None:
    from untether.runners.codex import CodexRunner
    from untether.schemas import codex as codex_schema

    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    state = runner.new_state("hi", None)
    events: list[Any] = []
    for line in (FIXTURES / "codex_exec_json_0157.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        try:
            data = codex_schema.decode_event(line)
        except msgspec.DecodeError:
            continue
        events.extend(
            runner.translate(data, state=state, resume=None, found_session=None)
        )
    assert events
    assert not [
        e for e in events if isinstance(e, ActionEvent) and e.action.kind == "telemetry"
    ]
    header = _replay_header(events, "codex")
    assert header.startswith("done · codex · 5s")
    assert "ctx" not in header


def test_opencode_output_has_no_context_segment() -> None:
    from untether.runners.opencode import OpenCodeRunner
    from untether.schemas import opencode as opencode_schema

    runner = OpenCodeRunner(opencode_cmd="opencode")
    state = runner.new_state("hi", None)
    events: list[Any] = []
    for line in (FIXTURES / "opencode_run_json.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        try:
            data = opencode_schema.decode_event(line)
        except msgspec.DecodeError:
            continue
        events.extend(
            runner.translate(data, state=state, resume=None, found_session=None)
        )
    assert events
    header = _replay_header(events, "opencode")
    assert header.startswith("done · opencode · 5s")
    assert "ctx" not in header


# ── bridge end to end (real handle_message) ─────────────────────────────────


async def _run_once(
    presenter: Any, pct: int, usage: dict[str, Any] | None = None
) -> str:
    """One run whose engine reports ``pct``; returns the final's text."""
    from tests.telegram_fakes import FakeTransport
    from untether.model import ResumeToken
    from untether.runner_bridge import ExecBridgeConfig, IncomingMessage, handle_message
    from untether.runners.mock import Emit, Return, ScriptRunner

    token = ResumeToken(engine="claude", value="sess-819-e2e")
    transport = FakeTransport()
    runner = ScriptRunner(
        [
            Emit(StartedEvent(engine="claude", resume=token)),
            Emit(_telemetry(pct)),
            Emit(
                ActionEvent(
                    engine="claude",
                    action=Action(id="toolu_1", kind="command", title="ls"),
                    phase="started",
                )
            ),
            Return(answer="E2E-ANSWER", usage=usage or {}),
        ],
        engine="claude",
        resume_value=token.value,
    )
    cfg = ExecBridgeConfig(transport=transport, presenter=presenter, final_notify=True)
    await handle_message(
        cfg,
        runner=runner,
        incoming=IncomingMessage(channel_id=1, message_id=10, text="go"),
        resume_token=None,
    )
    calls = [*transport.send_calls, *transport.edit_calls]
    finals = [c["message"].text for c in calls if "E2E-ANSWER" in c["message"].text]
    assert len(finals) == 1
    return finals[0]


@pytest.mark.anyio
async def test_final_header_shows_context_and_toggle_hot_reloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R15-19a/f offline: the final's header ends with ``N% ctx`` (footer
    untouched), and flipping ``[progress] show_context_usage`` applies to the
    very next run through the per-run refresh (#506 cache included)."""
    from untether.telegram.bridge import TelegramPresenter

    cfg_path = tmp_path / "untether.toml"
    base = (
        'transport = "telegram"\n[transports.telegram]\nbot_token = "1:t"\n'
        "chat_id = 1\nallow_any_user = true\n[progress]\n"
    )
    monkeypatch.setenv("UNTETHER_CONFIG_PATH", str(cfg_path))
    presenter = TelegramPresenter(formatter=MarkdownFormatter())

    cfg_path.write_text(base + "show_context_usage = true\n")
    final = await _run_once(presenter, 62)
    header, rest = final.split("\n", 1)
    assert header.startswith("done · claude · ")
    assert header.endswith("· step 1 · 62% ctx")
    assert "ctx" not in rest

    cfg_path.write_text(base + "show_context_usage = false\n")
    final = await _run_once(presenter, 62)
    assert "ctx" not in final
    assert final.split("\n", 1)[0].endswith("· step 1")


@pytest.mark.anyio
async def test_runner_completed_logs_context_pct() -> None:
    from untether.markdown import MarkdownPresenter

    ctx = {"pct": 62, "used": 124_000, "window": 200_000, "model": HAIKU}
    with capture_logs() as logs:
        await _run_once(MarkdownPresenter(), 62, usage={"context": ctx})
        await _run_once(MarkdownPresenter(), 62, usage={"context": "odd"})
    done = [log for log in logs if log["event"] == "runner.completed"]
    assert len(done) == 2
    assert done[0]["context_pct"] == 62
    assert "context_pct" not in done[1]
