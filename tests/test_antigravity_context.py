"""Antigravity ``~N% ctx`` (#979): the seeded window table and the runner's
context value.

agy reports no context window, so the window comes from a small table of
published figures; the numerator is the usage of the turn's latest model
call (``step_update.usage`` on an ``agent_response`` step), never the summed
``result.usage``. Fixtures ``context_*`` are the agy 1.3.3 probe streams.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import structlog.testing

from untether.model import ActionEvent, CompletedEvent, ResumeToken
from untether.progress import ProgressTracker
from untether.runners.antigravity import ENGINE, AntigravityRunner
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.schemas import antigravity as schema
from untether.utils import antigravity_context as ctx

FIXTURES = Path(__file__).parent / "fixtures" / "antigravity"
CID = "7a2d9c4e-1b3f-4e6a-8c5d-2f1e0a9b8c7d"


def _lines(name: str, *, model: str | None = None) -> list[str]:
    """The fixture's lines; *model* is written into ``init.model`` the way
    agy echoes ``--model`` (the plan-turn captures ran without the flag)."""
    out: list[str] = []
    for raw in (FIXTURES / f"{name}.jsonl").read_text().splitlines():
        if not raw.strip():
            continue
        obj = json.loads(raw)
        if model is not None and obj.get("event") == "init":
            obj["init"]["model"] = model
        out.append(json.dumps(obj))
    return out


def _replay(
    lines: list[str],
    *,
    resume: ResumeToken | None = None,
    runner: AntigravityRunner | None = None,
    options: EngineRunOptions | None = None,
) -> list[Any]:
    runner = runner or AntigravityRunner(antigravity_cmd="agy")
    events: list[Any] = []
    with apply_run_options(options):
        state = runner.new_state("prompt", resume)
        runner.start_run("prompt", resume, state=state)
        for line in lines:
            events.extend(
                runner.translate(
                    schema.decode_event(line),
                    state=state,
                    resume=resume,
                    found_session=None,
                )
            )
    return events


def _context(events: list[Any]) -> list[dict[str, Any]]:
    return [
        e.action.detail
        for e in events
        if isinstance(e, ActionEvent) and e.action.kind == "telemetry"
    ]


def _usage(events: list[Any]) -> dict[str, Any]:
    (done,) = [e for e in events if isinstance(e, CompletedEvent)]
    return done.usage or {}


def _step(index: int, usage: dict[str, int]) -> str:
    return json.dumps(
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": CID,
                "step_index": index,
                "state": "DONE",
                "step_type": "agent_response",
                "usage": usage,
            },
        }
    )


def _init(model: str | None) -> str:
    init: dict[str, Any] = {"cwd": "/p", "permission_mode": "request-review"}
    if model is not None:
        init["model"] = model
    return json.dumps({"event": "init", "conversation_id": CID, "init": init})


# ── the window table ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("model", "window"),
    [
        ("gemini-3.8-flash", 1_048_576),
        ("gemini-3.8-flash-low", 1_048_576),
        ("gemini-3.8-flash-high", 1_048_576),
        ("gemini-3.7-flash-medium", 1_048_576),
        ("gemini-3.6-flash-low", 1_048_576),
        ("gemini-3.1-pro-high", 1_048_576),
        ("claude-opus-5-5-high", 1_000_000),
        ("claude-sonnet-5-5-medium", 1_000_000),
        ("gpt-oss-120b-medium", 131_072),
    ],
)
def test_seeded_windows_by_agy_model_id(model: str, window: int) -> None:
    assert ctx.context_window_for(model) == window


def test_seed_holds_only_the_verified_models() -> None:
    assert set(ctx.AGY_CONTEXT_WINDOWS) == {
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.1-pro",
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "gpt-oss-120b",
    }


@pytest.mark.parametrize(
    "model",
    [
        None,
        "",
        "gemini-4.0-flash",
        "gemini-3.8-flash-preview",
        "gemini-3.1-pro-preview",
        "claude-opus-4-6",
        "Gemini 3.8 Flash (Low)",
        "GEMINI-3.8-FLASH",
        "gemini-3.8-flash-low-low",
        "x" * 300,
    ],
)
def test_unknown_model_has_no_window(model: str | None) -> None:
    """Never a guess and never a default: no fuzzy match, no label match."""
    assert ctx.context_window_for(model) is None


def test_context_pct_rounds_half_up_and_clamps() -> None:
    assert ctx.context_pct(7102, 1_048_576) == 1
    assert ctx.context_pct(5000, 1_048_576) == 0
    assert ctx.context_pct(5243, 1_048_576) == 1  # 0.50002 %
    assert ctx.context_pct(7102, 131_072) == 5
    assert ctx.context_pct(131_072, 131_072) == 100
    assert ctx.context_pct(200_000, 131_072) == 100
    assert ctx.context_pct(0, 131_072) == 0


# ── the runner: when a percentage is shown ──────────────────────────────────


def test_model_flag_capture_emits_context() -> None:
    """``--model`` passed → agy echoes it in ``init.model`` → 7,102 of
    1,048,576."""
    events = _replay(_lines("context_model_flag"))
    assert _context(events) == [
        {
            "context_pct": 1,
            "context_used": 7102,
            "context_window": 1_048_576,
            "model": "gemini-3.8-flash-low",
            "context_estimated": True,
        }
    ]
    assert _usage(events)["context"] == {
        "pct": 1,
        "used": 7102,
        "window": 1_048_576,
        "model": "gemini-3.8-flash-low",
        "estimated": True,
    }


def test_no_model_in_the_stream_shows_nothing() -> None:
    """No ``--model`` → no line names the model → no percentage."""
    events = _replay(_lines("context_turn"))
    assert _context(events) == []
    assert "context" not in _usage(events)


def test_configured_model_without_init_model_shows_nothing() -> None:
    """The model Untether would pass is not proof of what agy ran: only
    agy's own ``init.model`` counts."""
    runner = AntigravityRunner(antigravity_cmd="agy", model="gemini-3.8-flash")
    events = _replay(_lines("context_turn"), runner=runner)
    assert _context(events) == []
    events = _replay(
        _lines("context_turn"), options=EngineRunOptions(model="gemini-3.8-flash")
    )
    assert _context(events) == []
    assert "context" not in _usage(events)


def test_unknown_model_shows_nothing() -> None:
    with structlog.testing.capture_logs() as logs:
        events = _replay(_lines("context_turn", model="gemini-9-ultra-high"))
    assert _context(events) == []
    assert "context" not in _usage(events)
    misses = [e for e in logs if e["event"] == "antigravity.context.window_miss"]
    assert len(misses) == 1  # once per run, not once per model call
    assert misses[0]["model"] == "gemini-9-ultra-high"


def test_numerator_is_the_last_model_call_not_the_result_sum() -> None:
    """Five model calls of 7,902 → 9,501 input tokens; ``result.usage`` adds
    them up to 42,593. The context is 9,501."""
    events = _replay(_lines("context_turn", model="gemini-3.8-flash-low"))
    usage = _usage(events)
    assert usage["input_tokens"] == 42593  # the summed figure, left alone
    assert usage["context"]["used"] == 9501
    assert usage["context"]["pct"] == 1
    # 7,902 … 9,501 are all 1 % of 1,048,576: emitted once, on the change
    assert [d["context_used"] for d in _context(events)] == [7902]


def test_small_window_emits_each_change() -> None:
    events = _replay(_lines("context_turn", model="gpt-oss-120b-medium"))
    assert [(d["context_used"], d["context_pct"]) for d in _context(events)] == [
        (7902, 6),
        (8656, 7),
    ]
    assert _usage(events)["context"] == {
        "pct": 7,
        "used": 9501,
        "window": 131_072,
        "model": "gpt-oss-120b-medium",
        "estimated": True,
    }


def test_resume_uses_the_last_call_not_the_conversation_sum() -> None:
    """The resumed turn's ``result.usage`` is 84,958 (both turns, every
    call); its last model call read 11,290."""
    resume = ResumeToken(engine=ENGINE, value=CID)
    events = _replay(
        _lines("context_resume", model="gpt-oss-120b-medium"), resume=resume
    )
    usage = _usage(events)
    assert usage["input_tokens"] == 84958
    assert usage["context"]["used"] == 11290
    assert usage["context"]["pct"] == 9
    assert _context(events)[0]["context_used"] == 9931


def test_cache_read_tokens_are_added_to_input() -> None:
    """agy reports cache reads apart from ``input_tokens`` (1.3.1 capture
    ``r7-bypass-hook-timeout``: 14,580 → 7,312 + 8,127 cached → 15,860)."""
    lines = [
        _init("gpt-oss-120b-medium"),
        _step(5, {"input_tokens": 14580, "cache_read_tokens": 0}),
        _step(7, {"input_tokens": 7312, "cache_read_tokens": 8127}),
    ]
    assert [d["context_used"] for d in _context(_replay(lines))] == [14580, 15439]


def test_step_without_usable_usage_changes_nothing() -> None:
    lines = [
        _init("gpt-oss-120b-medium"),
        _step(1, {"input_tokens": 14580}),
        _step(3, {"output_tokens": 5}),
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": CID,
                    "step_index": 4,
                    "state": "DONE",
                    "step_type": "tool",
                    "tool_name": "view_file",
                    "usage": {"input_tokens": 99999},
                },
            }
        ),
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": CID,
                    "step_index": 5,
                    "state": "ACTIVE",
                    "step_type": "agent_response",
                    "usage": {"input_tokens": 99999},
                },
            }
        ),
    ]
    assert [d["context_used"] for d in _context(_replay(lines))] == [14580]


def test_over_window_clamped_and_warned_once() -> None:
    lines = [
        _init("gpt-oss-120b-medium"),
        _step(1, {"input_tokens": 140_000}),
        _step(3, {"input_tokens": 150_000}),
    ]
    with structlog.testing.capture_logs() as logs:
        events = _replay(lines)
    assert [d["context_pct"] for d in _context(events)] == [100]
    warned = [e for e in logs if e["event"] == "antigravity.context.over_window"]
    assert len(warned) == 1
    assert warned[0]["log_level"] == "warning"


def test_context_event_reaches_the_shared_tracker() -> None:
    """The same ``telemetry`` path Claude's ``N% ctx`` uses (#819)."""
    tracker = ProgressTracker(engine=ENGINE)
    for event in _replay(_lines("context_model_flag")):
        tracker.note_event(event)
    assert tracker.context_pct == 1
    assert tracker.context_estimated is True
    # telemetry is a value for the header, never a step
    assert tracker.action_count == 0
