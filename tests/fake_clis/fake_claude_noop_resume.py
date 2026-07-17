#!/usr/bin/env python3
"""Deterministic fake ``claude`` CLI for the no-op empty-resume fault
injection harness (#634, W6a).

Emits schema-accurate ``stream-json`` lines — matching
``src/untether/schemas/claude.py`` exactly (required fields such as
``duration_ms`` on the ``result`` event are always included) — so the REAL
``untether.runners.claude.ClaudeRunner`` and its msgspec decoder can parse
them unmodified. This lets tests drive the entire production pipeline
(subprocess spawn -> stream-json parse -> anomaly detection -> fresh
recovery) deterministically, without a real Anthropic API call.

See ``docs/plans/2026-07-16-noop-resume-remediation/04-test-strategy.md``,
"Layer 1 -- Fake-claude reproduction harness", for the scenario table this
implements.

Scenario selection: env var ``FAKE_CLAUDE_SCENARIO`` (one of the keys in
``_SCENARIOS`` below). ``FAKE_CLAUDE_LINGER_S`` (default ``"0"``) controls
the post-result sleep used by the scenarios that need to stay alive after
emitting their last line.

``ClaudeRunner`` in legacy (non permission-mode) invocation passes the
prompt as the final CLI argument after a bare ``--`` and never writes
anything to this process's stdin, so this script only needs to read argv
(specifically: whether ``--resume <value>`` is present) — stdin is ignored
entirely.

This file is test-only. Nothing under ``src/`` imports it.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any


def emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj), flush=True)


def _resume_arg(argv: list[str]) -> str | None:
    """Return the value following ``--resume``/``-r`` in argv, or None."""
    for flag in ("--resume", "-r"):
        if flag in argv:
            idx = argv.index(flag)
            if idx + 1 < len(argv):
                return argv[idx + 1]
    return None


def _linger_s() -> float:
    raw = os.environ.get("FAKE_CLAUDE_LINGER_S", "0")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


def _emit_init(sid: str) -> None:
    emit(
        {
            "type": "system",
            "subtype": "init",
            "session_id": sid,
            "model": "claude-fake-sonnet",
            "cwd": ".",
            "tools": [],
            "mcp_servers": [],
            "permissionMode": "default",
        }
    )


def _emit_empty_result(sid: str) -> None:
    """0-turn / $0 / no-API-time result -- the no-op empty-resume anomaly
    (#596/#631). ``duration_ms`` is a REQUIRED field on
    ``StreamResultMessage`` — omitting it (as the plan doc's illustrative
    sketch does) makes msgspec raise ValidationError and silently drops the
    line, so every result line here always sets it explicitly."""
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": 0,
            "total_cost_usd": 0.0,
            "duration_ms": 5,
            "duration_api_ms": 0,
            "session_id": sid,
            "result": "",
        }
    )


def _emit_real_result(
    sid: str, *, text: str, num_turns: int, cost: float, duration_api_ms: int = 8000
) -> None:
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": num_turns,
            "total_cost_usd": cost,
            "duration_ms": duration_api_ms + 1000,
            "duration_api_ms": duration_api_ms,
            "session_id": sid,
            "result": text,
        }
    )


def _emit_assistant_text(sid: str, text: str) -> None:
    emit(
        {
            "type": "assistant",
            "session_id": sid,
            "message": {
                "id": "msg_text",
                "type": "message",
                "role": "assistant",
                "model": "claude-fake-sonnet",
                "content": [{"type": "text", "text": text}],
            },
        }
    )


def _emit_dangling_tool_use(sid: str) -> None:
    """Assistant turn that ends on a background-Task ``tool_use`` with no
    matching ``tool_result`` in the stream -- the shape that later poisons
    a resume of this session (upstream dangling-tool_use bug, W3)."""
    emit(
        {
            "type": "assistant",
            "session_id": sid,
            "message": {
                "id": "msg_bg1",
                "type": "message",
                "role": "assistant",
                "model": "claude-fake-sonnet",
                "content": [
                    {"type": "text", "text": "spawning a background agent"},
                    {
                        "type": "tool_use",
                        "id": "bg1",
                        "name": "Task",
                        "input": {
                            "run_in_background": True,
                            "prompt": "watch the build",
                        },
                    },
                ],
            },
        }
    )


def _scenario_dangling_then_empty_resume(argv: list[str]) -> int:
    """#634 harness simplification (see 04-test-strategy.md "Layer 1"):
    keyed purely off the PRESENCE of ``--resume`` in argv, not the specific
    session id. A resume of ANY session under this scenario reproduces the
    poisoned-resume empty result; any non-resume invocation reproduces the
    dangling-tool_use turn that (in production) is what poisons the session
    in the first place, and also stands in for the post-quarantine "fresh"
    recovery leg -- both are "no --resume" invocations and both must return
    a real, non-empty answer so a single test can drive resume -> empty ->
    fresh-recovery in one ``handle_message`` call.
    """
    resume = _resume_arg(argv)
    if resume is not None:
        _emit_init(resume)
        _emit_empty_result(resume)
        return 0

    sid = f"S-fresh-{os.getpid()}"
    _emit_init(sid)
    _emit_dangling_tool_use(sid)
    _emit_real_result(sid, text="started", num_turns=4, cost=0.12)
    time.sleep(_linger_s())
    return 0


def _scenario_linger_then_sigterm_after_result(argv: list[str]) -> int:
    """Emits one real result, then sleeps past FAKE_CLAUDE_LINGER_S without
    exiting -- models the forced-teardown limbo case (W2). Callers that want
    to exercise the actual SIGTERM/watchdog path drive this scenario
    directly via subprocess and manage the process lifetime themselves
    (see test_harness_linger_scenario_emits_valid_result_and_outlives_it)."""
    resume = _resume_arg(argv)
    sid = resume or f"S-fresh-{os.getpid()}"
    _emit_init(sid)
    _emit_real_result(sid, text="Done.", num_turns=1, cost=0.01, duration_api_ms=500)
    time.sleep(_linger_s())
    return 0


def _scenario_healthy_resume(argv: list[str]) -> int:
    """Negative control: always a normal, non-empty answer -- no anomaly,
    no quarantine, no recovery run should ever be triggered by this."""
    resume = _resume_arg(argv)
    sid = resume or f"S-fresh-{os.getpid()}"
    _emit_init(sid)
    _emit_assistant_text(sid, "Continuing from where we left off.")
    _emit_real_result(
        sid,
        text="Here is the continued answer.",
        num_turns=2,
        cost=0.05,
        duration_api_ms=1200,
    )
    return 0


_SCENARIOS = {
    "dangling_then_empty_resume": _scenario_dangling_then_empty_resume,
    "linger_then_sigterm_after_result": _scenario_linger_then_sigterm_after_result,
    "healthy_resume": _scenario_healthy_resume,
}


def main() -> int:
    argv = sys.argv[1:]
    scenario = os.environ.get("FAKE_CLAUDE_SCENARIO")
    handler = _SCENARIOS.get(scenario or "")
    if handler is None:
        sys.stderr.write(
            "fake_claude_noop_resume: unknown or missing FAKE_CLAUDE_SCENARIO "
            f"{scenario!r}; expected one of {sorted(_SCENARIOS)}\n"
        )
        return 2
    return handler(argv)


if __name__ == "__main__":
    raise SystemExit(main())
