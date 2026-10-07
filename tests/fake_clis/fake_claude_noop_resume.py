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
import signal
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
    a resume of this session (upstream dangling-tool_use bug, W3).

    Note: ``_register_background_handle`` in ``src/untether/runners/
    claude.py`` recognises ``Task`` (alongside ``Agent``) as a
    background-tracked tool name with bounded-keep deadline semantics
    (#374). This scenario reproduces the JSONL SHAPE that poisons a
    resume regardless of whether Untether's own background-task tracker
    recognises it."""
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


def _emit_tool_result(sid: str, *, tool_use_id: str = "bg1") -> None:
    """The ``user``-typed tool_result frame that answers a prior ``tool_use``.

    Shape matters twice over. ``runner.py::_handle_jsonl_line`` sets
    ``stream.last_event_type`` from the raw JSONL ``type`` field, so this
    frame is what makes the bridge's auto-continue gate see
    ``last_event_type == "user"`` -- the "tool results sent but never
    processed" signature (#322, upstream #34142/#30333). Without it, the
    gate short-circuits before it ever reaches the signal-death arm that
    #640 is about.
    """
    emit(
        {
            "type": "user",
            "session_id": sid,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": "background agent started",
                    }
                ],
            },
        }
    )


def _scenario_tool_result_then_sigterm(argv: list[str]) -> int:
    """#640: exit by SIGNAL with ``last_event_type == "user"``.

    Paired with :func:`_scenario_tool_result_then_clean_exit`, which emits
    the IDENTICAL frame sequence and differs ONLY in the exit path. That
    pairing is the discriminator: pre-fix, ``ClaudeRunner`` never wrote
    ``stream.proc_returncode`` back, so the bridge saw ``None``,
    ``_is_signal_death(None)`` returned False, and BOTH scenarios
    auto-continued. Post-fix only the clean-exit twin does.

    Emits no ``result`` frame, so ``final_delivery["sent"]`` stays False and
    the gate at ``runner_bridge.py`` is genuinely evaluated rather than
    skipped.
    """
    resume = _resume_arg(argv)
    if resume is not None:
        # Reached only if the auto-continue gate WRONGLY fired. The marker
        # text turns a silent miscount into a visible failure in the
        # transport, so the test fails loudly rather than by arithmetic.
        _emit_init(resume)
        _emit_real_result(
            resume, text="UNEXPECTED-AUTO-CONTINUE", num_turns=1, cost=0.01
        )
        return 0

    sid = f"S-ac-sig-{os.getpid()}"
    _emit_init(sid)
    _emit_dangling_tool_use(sid)
    _emit_tool_result(sid)
    sys.stdout.flush()
    # Restore default disposition first: the harness parent may have set a
    # handler, and SIG_DFL is what makes this a true signal death rather
    # than a normal exit. asyncio reports this as returncode -15.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGTERM)
    return 0  # unreachable


def _scenario_tool_result_then_clean_exit(argv: list[str]) -> int:
    """#640 positive control: identical frames to
    :func:`_scenario_tool_result_then_sigterm`, but rc=0.

    Proves the frame sequence alone DOES drive auto-continue, which is what
    rules out a false pass on the sigterm twin (a twin that suppressed for
    the wrong reason -- e.g. a falsy resume value or a non-``user``
    last_event_type -- would show zero auto-continues here too).

    The resume leg is the auto-continue re-entry and returns a real answer
    so the run terminates.
    """
    resume = _resume_arg(argv)
    if resume is not None:
        _emit_init(resume)
        _emit_real_result(
            resume, text="Continued after tool result.", num_turns=2, cost=0.02
        )
        return 0

    sid = f"S-ac-ok-{os.getpid()}"
    _emit_init(sid)
    _emit_dangling_tool_use(sid)
    _emit_tool_result(sid)
    return 0


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


def _scenario_resume_survives_sigterm(argv: list[str]) -> int:
    """#634 / #633 (W4): a resume leg that IGNORES SIGTERM.

    Models the worst case the W4 serialisation gate exists for — a prior
    subprocess that will not die on request, so the handoff wait must time out
    and the follow-up must divert to a fresh session rather than racing a
    session that still has a live owner.

    Non-resume invocations return a normal result immediately, so the fresh
    recovery leg still completes and the user gets a real answer.
    """
    resume = _resume_arg(argv)
    if resume is None:
        sid = f"S-fresh-{os.getpid()}"
        _emit_init(sid)
        _emit_real_result(sid, text="Fresh answer.", num_turns=2, cost=0.03)
        return 0

    # Deliberately unkillable-by-SIGTERM: this is the "will not hand off"
    # process. SIGKILL still works, so the test harness can always clean up.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    _emit_init(resume)
    _emit_real_result(resume, text="Done.", num_turns=1, cost=0.01)
    time.sleep(_linger_s())
    return 0


def _emit_tool_result(sid: str) -> None:
    """A ``user``-typed frame carrying a tool_result block — the shape a
    trailing tool result takes on the wire (#716)."""
    emit(
        {
            "type": "user",
            "session_id": sid,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_trailing",
                        "content": "ok",
                    }
                ],
            },
        }
    )


def _scenario_trailing_user_after_result(argv: list[str]) -> int:
    """#716: emit a healthy run, then a trailing ``user`` (tool_result)
    frame AFTER the terminal ``result``.

    Establishes what the parser actually does with a post-result frame:
    whether it can still overwrite ``stream.last_event_type`` (the
    mechanism the issue asserts) or whether the reader has already stopped
    at the ``result`` (in which case a production ``session.summary`` line
    reading ``last_event_type=user`` on an ``ok=True`` run has a different
    cause). Either way ``saw_result`` must be True.
    """
    resume = _resume_arg(argv)
    sid = resume or f"S-fresh-{os.getpid()}"
    _emit_init(sid)
    _emit_assistant_text(sid, "Working.")
    _emit_real_result(sid, text="All done.", num_turns=3, cost=0.08)
    _emit_tool_result(sid)
    time.sleep(_linger_s())
    return 0


def _scenario_hang_before_result(argv: list[str]) -> int:
    """#667: emit init + a partial assistant chunk, then hang for
    FAKE_CLAUDE_LINGER_S WITHOUT ever emitting a result.

    Models a run cancelled mid-flight (/cancel, /new, drain) or dying messily
    before its first result — the path where ``run_impl`` never reaches its
    happy-path ``stream.proc_returncode = rc`` assignment. The harness cancels
    this scenario mid-hang; ``manage_subprocess``'s shielded terminate+reap
    SIGTERMs it, and the #667 ``finally`` capture must still record the reaped
    return code onto the stream (so the auto-continue signal-death guard sees a
    real rc instead of None). Uses DEFAULT SIGTERM handling — unlike
    ``resume_survives_sigterm`` — so the teardown kills it promptly."""
    resume = _resume_arg(argv)
    sid = resume or f"S-fresh-{os.getpid()}"
    _emit_init(sid)
    _emit_assistant_text(sid, "working on it")
    time.sleep(_linger_s())  # hang until cancelled/killed; no result ever emitted
    return 0


def _emit_compaction(sid: str, trigger: str, *, init_between: bool) -> None:
    """#819: the compaction frames as captured on CLI 2.1.285 (see
    ``tests/fixtures/claude_compaction_2.1.285.jsonl`` / ``…autocompact…``)."""
    emit(
        {
            "type": "system",
            "subtype": "status",
            "status": "compacting",
            "session_id": sid,
        }
    )
    emit(
        {
            "type": "system",
            "subtype": "status",
            "status": None,
            "compact_result": "success",
            "session_id": sid,
        }
    )
    if init_between:
        _emit_init(sid)
    emit(
        {
            "type": "system",
            "subtype": "compact_boundary",
            "session_id": sid,
            "compact_metadata": {
                "trigger": trigger,
                "pre_tokens": 180_000,
                "post_tokens": 20_000,
            },
        }
    )
    emit(
        {
            "type": "user",
            "session_id": sid,
            "isSynthetic": True,
            "message": {
                "role": "user",
                "content": "This session is being continued from a previous "
                "conversation that ran out of context.",
            },
        }
    )


def _scenario_resume_autocompact_noop(argv: list[str]) -> int:
    """#819: the poisoned-session case the manual-only exemption protects —
    a resume auto-compacts and *then* returns the #596 0-turn empty result.
    Non-resume invocations (the fresh recovery leg) answer normally."""
    resume = _resume_arg(argv)
    if resume is not None:
        _emit_init(resume)
        _emit_compaction(resume, "auto", init_between=False)
        _emit_empty_result(resume)
        return 0
    sid = f"S-fresh-{os.getpid()}"
    _emit_init(sid)
    _emit_real_result(sid, text="fresh answer", num_turns=2, cost=0.02)
    return 0


def _scenario_manual_compact_result(argv: list[str]) -> int:
    """#819: a successful manual ``/compact`` (Z10): no API turn, a replayed
    "Compacted" stdout and a 0-turn, 0-ms, empty result."""
    resume = _resume_arg(argv)
    sid = resume or f"S-fresh-{os.getpid()}"
    _emit_init(sid)
    _emit_compaction(sid, "manual", init_between=True)
    emit(
        {
            "type": "user",
            "session_id": sid,
            "isReplay": True,
            "message": {
                "role": "user",
                "content": "<local-command-stdout>Compacted </local-command-stdout>",
            },
        }
    )
    _emit_empty_result(sid)
    return 0


_SCENARIOS = {
    "resume_autocompact_noop": _scenario_resume_autocompact_noop,
    "manual_compact_result": _scenario_manual_compact_result,
    "dangling_then_empty_resume": _scenario_dangling_then_empty_resume,
    "linger_then_sigterm_after_result": _scenario_linger_then_sigterm_after_result,
    "healthy_resume": _scenario_healthy_resume,
    "resume_survives_sigterm": _scenario_resume_survives_sigterm,
    "hang_before_result": _scenario_hang_before_result,
    "trailing_user_after_result": _scenario_trailing_user_after_result,
    "tool_result_then_sigterm": _scenario_tool_result_then_sigterm,
    "tool_result_then_clean_exit": _scenario_tool_result_then_clean_exit,
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
