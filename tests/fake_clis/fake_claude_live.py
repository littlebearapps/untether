#!/usr/bin/env python3
"""Deterministic fake Claude Code CLI speaking the control-channel protocol (#776).

Unlike ``fake_claude_noop_resume.py`` (legacy ``-p`` invocation, stdin
ignored), this double behaves like ``claude --input-format stream-json`` in
permission mode: it reads stream-json lines from stdin, runs one turn per
``user`` line, keeps running after a ``result`` while stdin is open, wakes
itself when a scripted background task finishes, and — on stdin EOF — stops
live background tasks (``task_updated{killed}`` + ``task_notification
{stopped}``) and exits 0, exactly as probed on CLI 2.1.283 (see
``docs/findings/2026-09-27-claude-live-session-probes.md``).

Scenario via ``FAKE_CLAUDE_SCENARIO``; timing via ``FAKE_CLAUDE_WAKE_S``
(default 0.3 s) and ``FAKE_CLAUDE_RESULT_DELAY_S`` (``followup`` only: delay
before the first result, default 0). Test-only.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time

SESSION_ID = os.environ.get("FAKE_CLAUDE_SESSION_ID", "fake-live-session")
WAKE_S = float(os.environ.get("FAKE_CLAUDE_WAKE_S", "0.3"))
# #510: hold the first turn's result so a concurrent spawn lands in between.
RESULT_DELAY_S = float(os.environ.get("FAKE_CLAUDE_RESULT_DELAY_S", "0"))

# #815: a follow-up's think time before its first frame. When set, the
# follow-up turn skips ``init`` (as CLI 2.1.28x does), so the turn's first
# frame is its answer — the tool-free shape whose header read ``0s``.
FOLLOWUP_DELAY_S = float(os.environ.get("FAKE_CLAUDE_FOLLOWUP_DELAY_S", "0"))

# #775: how long the steer scenarios wait for a steered user line.
STEER_WAIT_S = float(os.environ.get("FAKE_CLAUDE_STEER_WAIT_S", "5"))

# #383: the CLI's permission mode. Changed by an approved ExitPlanMode
# (-> "default", the `prePlanMode ?? "default"` path) and by a
# `set_permission_mode` control_request, which — like the real CLI — is
# handled inline by the stdin reader, not queued behind a turn.
_mode = os.environ.get("FAKE_CLAUDE_START_MODE", "bypassPermissions")
# Test knobs: refuse every set_permission_mode; emit no system/status frames.
REARM_ERROR = bool(os.environ.get("FAKE_CLAUDE_REARM_ERROR"))
NO_STATUS = bool(os.environ.get("FAKE_CLAUDE_NO_STATUS"))
# #751: report this in the FIRST system/init only (like `auto` on Haiku,
# which the real CLI silently runs as `default`); later inits report _mode.
_INIT_MODE_OVERRIDE = os.environ.get("FAKE_CLAUDE_INIT_PERMISSION_MODE")
_init_count = 0
# A queued wake turn starts this long after the result, without reading stdin.
WAKE_AFTER_RESULT_S = float(os.environ.get("FAKE_CLAUDE_WAKE_AFTER_RESULT_S", "0.05"))
STDIN_LOG = os.environ.get("FAKE_CLAUDE_STDIN_LOG")

_cost = 0.0
_lines: queue.Queue[dict | None] = queue.Queue()
_responses: queue.Queue[dict] = queue.Queue()
_live_tasks: dict[str, str] = {}  # task_id -> tool_use_id
_emit_lock = threading.Lock()


def emit(obj: dict) -> None:
    obj.setdefault("session_id", SESSION_ID)
    with _emit_lock:  # the stdin reader emits too (#383 acks)
        print(json.dumps(obj), flush=True)


def log_stdin(kind: str) -> None:
    """Append one ``<monotonic> <kind>`` line to FAKE_CLAUDE_STDIN_LOG."""
    if STDIN_LOG:
        with open(STDIN_LOG, "a") as fh:
            fh.write(f"{time.monotonic():.6f} {kind}\n")


def status_frame() -> None:
    if not NO_STATUS:
        emit(
            {
                "type": "system",
                "subtype": "status",
                "status": None,
                "permissionMode": _mode,
            }
        )


def _handle_control_request(obj: dict) -> None:
    global _mode
    request = obj.get("request") or {}
    if request.get("subtype") != "set_permission_mode":
        return
    request_id = obj.get("request_id")
    if REARM_ERROR:
        emit(
            {
                "type": "control_response",
                "response": {
                    "subtype": "error",
                    "request_id": request_id,
                    "error": "Cannot set permission mode (fake)",
                    "error_code": "invalid_mode",
                },
            }
        )
        return
    changed = request.get("mode") != _mode
    _mode = request.get("mode") or _mode
    emit(
        {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": {"mode": _mode},
            },
        }
    )
    if changed:
        status_frame()


def _reader() -> None:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = obj.get("type", "?")
        if kind == "control_request":
            kind = f"control_request:{(obj.get('request') or {}).get('subtype')}"
        log_stdin(kind)
        if obj.get("type") == "control_request":
            _handle_control_request(obj)
            continue
        if obj.get("type") == "control_response":
            _responses.put(obj)
            continue
        _lines.put(obj)
    _lines.put(None)  # EOF


def next_user(timeout: float | None) -> dict | None | str:
    """Return the next user line, None on EOF, or "timeout"."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        try:
            obj = _lines.get(timeout=remaining)
        except queue.Empty:
            return "timeout"
        if obj is None:
            return None
        if obj.get("type") == "user":
            return obj
        # control_request initialize / control_response: ignore


def init() -> None:
    global _init_count
    _init_count += 1
    mode = _mode
    if _INIT_MODE_OVERRIDE and _init_count == 1:
        mode = _INIT_MODE_OVERRIDE
    emit(
        {
            "type": "system",
            "subtype": "init",
            "cwd": os.getcwd(),
            "model": "claude-haiku-fake",
            "tools": ["Bash", "Agent", "Monitor", "ScheduleWakeup"],
            "permissionMode": mode,
        }
    )


FAKE_MODEL = "claude-haiku-fake"


def _usage(used: int | None) -> dict | None:
    """#819: an assistant ``usage`` whose input side totals ``used``."""
    if used is None:
        return None
    return {
        "input_tokens": 10,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": used - 10,
        "output_tokens": 5,
    }


def text(msg: str, *, usage: int | None = None, model: str = FAKE_MODEL) -> None:
    message = {
        "id": f"msg_{time.monotonic_ns()}",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": msg}],
    }
    if usage is not None:
        message["usage"] = _usage(usage)
    emit({"type": "assistant", "message": message})


def tool_use(
    name: str, tool_id: str, raw_input: dict, *, usage: int | None = None
) -> None:
    message = {
        "id": f"msg_{tool_id}",
        "role": "assistant",
        "model": FAKE_MODEL,
        "content": [
            {
                "type": "tool_use",
                "id": tool_id,
                "name": name,
                "input": raw_input,
            }
        ],
    }
    if usage is not None:
        message["usage"] = _usage(usage)
    emit({"type": "assistant", "message": message})


def tool_result(tool_id: str, content: str) -> None:
    emit(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_id, "content": content}
                ],
            },
        }
    )


def result(
    answer: str,
    *,
    turns: int = 1,
    delta: float = 0.01,
    api_ms: int = 900,
    model_usage: dict | None = None,
) -> None:
    global _cost
    _cost = round(_cost + delta, 6)
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 1000,
        "duration_api_ms": api_ms,
        "num_turns": turns,
        "result": answer,
        "total_cost_usd": _cost,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    if model_usage is not None:
        payload["modelUsage"] = model_usage
    emit(payload)


# #819: ``result.modelUsage`` naming the fake model's window.
MODEL_USAGE = {FAKE_MODEL: {"contextWindow": 200_000, "maxOutputTokens": 64_000}}


def compaction(
    trigger: str,
    pre: int,
    post: int | None,
    *,
    heartbeats: int = 0,
    failed: bool = False,
    init_between: bool = False,
) -> None:
    """#819: the compaction frames as captured on CLI 2.1.285
    (``tests/fixtures/claude_compaction_2.1.285.jsonl`` / ``…autocompact…``):
    ``status: compacting`` (re-sent every 30 s — ``heartbeats``), ``status:
    null`` + ``compact_result``, a fresh ``init`` (manual ``/compact`` only),
    then ``compact_boundary`` and the synthetic summary ``user`` frame. A
    failed compaction stops after its ``status: null``."""
    emit({"type": "system", "subtype": "status", "status": "compacting"})
    for _ in range(heartbeats):
        time.sleep(0.05)
        emit({"type": "system", "subtype": "status", "status": "compacting"})
    if failed:
        emit(
            {
                "type": "system",
                "subtype": "status",
                "status": None,
                "compact_result": "failed",
                "compact_error": "Conversation too long to compact",
            }
        )
        return
    emit(
        {
            "type": "system",
            "subtype": "status",
            "status": None,
            "compact_result": "success",
        }
    )
    if init_between:
        init()
    meta = {
        "trigger": trigger,
        "pre_tokens": pre,
        "cumulative_dropped_tokens": pre - (post or 0),
        "duration_ms": 50,
    }
    if post is not None:
        meta["post_tokens"] = post
    emit(
        {
            "type": "system",
            "subtype": "compact_boundary",
            "compact_metadata": meta,
            "logical_parent_uuid": "lp-fake",
        }
    )
    emit(
        {
            "type": "user",
            "isSynthetic": True,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "This session is being continued from a "
                        "previous conversation that ran out of context.",
                    }
                ],
            },
        }
    )


def compact_command_turn(cmd: str | None) -> None:
    """#819: a manual ``/compact`` written into the live session — Z10: no
    API turn, a replayed "Compacted" stdout and a 0-turn, 0-ms, empty
    result."""
    lifecycle(cmd, "queued")
    lifecycle(cmd, "started")
    compaction("manual", 60_000, 2_000, init_between=True)
    emit(
        {
            "type": "user",
            "isReplay": True,
            "message": {
                "role": "user",
                "content": "<local-command-stdout>Compacted </local-command-stdout>",
            },
        }
    )
    result("", turns=0, api_ms=0, delta=0.0, model_usage=MODEL_USAGE)


def snapshot() -> None:
    emit(
        {
            "type": "system",
            "subtype": "background_tasks_changed",
            "tasks": [
                {"task_id": tid, "task_type": "local_bash", "description": "bg"}
                for tid in _live_tasks
            ],
        }
    )


def start_bg(task_id: str, tool_id: str, *, task_type: str = "local_bash") -> None:
    _live_tasks[task_id] = tool_id
    snapshot()
    payload = {
        "type": "system",
        "subtype": "task_started",
        "task_id": task_id,
        "tool_use_id": tool_id,
        "description": f"bg {task_id}",
        "is_backgrounded": True,
        "task_type": task_type,
    }
    if task_type == "local_agent":
        payload["subagent_type"] = "general-purpose"
    emit(payload)


def end_bg(task_id: str, *, status: str = "completed") -> None:
    tool_id = _live_tasks.pop(task_id)
    snapshot()
    emit(
        {
            "type": "system",
            "subtype": "task_updated",
            "task_id": task_id,
            "patch": {"status": status, "end_time": int(time.time() * 1000)},
        }
    )
    emit(
        {
            "type": "system",
            "subtype": "task_notification",
            "task_id": task_id,
            "tool_use_id": tool_id,
            "status": "completed" if status == "completed" else "stopped",
            "output_file": "",
            "summary": f"bg {task_id} finished",
        }
    )


def lifecycle(command_uuid: str | None, state: str) -> None:
    emit({"type": "command_lifecycle", "command_uuid": command_uuid, "state": state})


def user_text(obj: dict) -> str:
    content = obj.get("message", {}).get("content")
    return content if isinstance(content, str) else json.dumps(content)


def serve_followups() -> None:
    """Answer each further user line as its own turn until stdin EOF."""
    while _deferred:
        _lines.put(_deferred.pop(0))
    while True:
        obj = next_user(None)
        if obj is None or obj == "timeout":
            break
        cmd = obj.get("uuid")
        lifecycle(cmd, "queued")
        lifecycle(cmd, "started")
        flush_withheld()  # #812: withheld async-hook responses land now
        if FOLLOWUP_DELAY_S > 0:
            time.sleep(FOLLOWUP_DELAY_S)
        else:
            init()
        text(f"ECHO: {user_text(obj)}")
        result(f"ECHO: {user_text(obj)}")
    shutdown()


def _wait_for_sigint() -> None:
    """#829: a real background agent ignores stdin EOF (probe G6); the CLI
    exits ``FAKE_CLAUDE_SIGINT_RC`` (default 0, probe G7) on the SIGINT
    Untether sends after the close grace."""
    rc = int(os.environ.get("FAKE_CLAUDE_SIGINT_RC", "0"))

    def _on_sigint(*_: object) -> None:
        sys.stdout.flush()
        os._exit(rc)

    signal.signal(signal.SIGINT, _on_sigint)
    _maybe_ignore_sigint()
    time.sleep(60)
    os._exit(0)


def shutdown() -> None:
    # #829: FAKE_CLAUDE_EOF_MODE=until_sigint models live background agents,
    # which keep working after EOF — only a SIGINT ends the process.
    if os.environ.get("FAKE_CLAUDE_EOF_MODE") == "until_sigint":
        _wait_for_sigint()
    # Stdin closed: stop live background work, as the real CLI does (F3).
    flush_withheld()  # #812: plain async hooks report at teardown
    kill_hooks()
    for task_id in list(_live_tasks):
        end_bg(task_id, status="killed")
    for task_id in list(_orphans):  # a subagent's bg task dies too (#801)
        _orphans.discard(task_id)
        emit(
            {
                "type": "system",
                "subtype": "task_updated",
                "task_id": task_id,
                "patch": {"status": "killed", "end_time": int(time.time() * 1000)},
            }
        )
    sys.exit(0)


_deferred: list[dict] = []


def wait_idle_or_eof(seconds: float) -> dict | None | str:
    """Sleep while idle but notice EOF (the CLI would kill bg work). A user
    line arriving meanwhile is queued, as the real CLI queues it (F7) — it
    runs as its own turn once the scripted wake is done."""
    deadline = time.monotonic() + seconds
    while True:
        got = next_user(max(0.0, deadline - time.monotonic()))
        if got is None:
            return None
        if got == "timeout":
            return "timeout"
        _deferred.append(got)


def scenario_bg_bash_wake(first: dict) -> None:
    init()
    bash_input: dict = {"command": "sleep 20", "run_in_background": True}
    # #872: the budget Claude declares with run_in_background.
    if os.environ.get("FAKE_CLAUDE_BASH_TIMEOUT_MS"):
        bash_input["timeout"] = int(os.environ["FAKE_CLAUDE_BASH_TIMEOUT_MS"])
    tool_use("Bash", "toolu_bg", bash_input)
    start_bg("b1", "toolu_bg")
    tool_result("toolu_bg", "Command running in background with ID: b1.")
    text("waiting")
    result("waiting", turns=2)
    got = wait_idle_or_eof(WAKE_S)
    if got is None:
        shutdown()
    end_bg("b1")
    wake_hook_s = float(os.environ.get("FAKE_CLAUDE_WAKE_HOOK_S", "0") or 0)
    if wake_hook_s:
        # #872 R17-01a: the task-notification prompt fires a global async
        # UserPromptSubmit hook, and the wake turn opens a moment later.
        spawn_hook("h-wake-ups", wake_hook_s)
        hook_started("h-wake-ups", "UserPromptSubmit")
        _withheld.append(("h-wake-ups", "UserPromptSubmit"))
        delay = float(os.environ.get("FAKE_CLAUDE_WAKE_DELAY_S", "0.4"))
        if wait_idle_or_eof(delay) is None:
            shutdown()
    init()
    flush_withheld()
    text("GOT: BG-FINISHED")
    result("GOT: BG-FINISHED")
    serve_followups()


def scenario_bg_agent_wake(first: dict) -> None:
    init()
    tool_use("Agent", "toolu_ag", {"description": "research", "prompt": "go"})
    start_bg("a1", "toolu_ag", task_type="local_agent")
    tool_result("toolu_ag", "Async agent launched successfully.")
    result("agent started", turns=2)
    # The subagent works while the parent is idle: its events carry
    # parent_tool_use_id and must not open a parent turn (F2).
    emit(
        {
            "type": "assistant",
            "parent_tool_use_id": "toolu_ag",
            "message": {
                "id": "msg_sub",
                "role": "assistant",
                "model": "claude-haiku-fake",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_sub",
                        "name": "Bash",
                        "input": {"command": "sleep 1"},
                    }
                ],
            },
        }
    )
    emit(
        {
            "type": "user",
            "parent_tool_use_id": "toolu_ag",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_sub", "content": ""}
                ],
            },
        }
    )
    got = wait_idle_or_eof(WAKE_S)
    if got is None:
        shutdown()
    end_bg("a1")
    init()
    tool_use("Read", "toolu_read", {"file_path": "/tmp/report.md"})
    tool_result("toolu_read", "report")
    text("REPORT: all good")
    result("REPORT: all good", turns=2)
    serve_followups()


def _end_quietly(task_id: str) -> None:
    # The task ends (snapshot + task_updated) without its notification yet.
    _live_tasks.pop(task_id)
    snapshot()
    emit(
        {
            "type": "system",
            "subtype": "task_updated",
            "task_id": task_id,
            "patch": {"status": "completed", "end_time": int(time.time() * 1000)},
        }
    )


def scenario_agent_wake_unknown_first(first: dict) -> None:
    """#785: the CLI opens a wake turn on a background agent's result BEFORE
    any task event names it, then a second turn once its task_notification
    lands. ``FAKE_CLAUDE_TASK_END=mid`` ends the task inside the first turn;
    ``after`` (default) just after it. A subagent-owned foreground task's
    notification arrives while the parent idles (it must not label a turn)."""
    mode = os.environ.get("FAKE_CLAUDE_TASK_END", "after")
    init()
    tool_use("Agent", "toolu_ag", {"description": "sweep", "prompt": "go"})
    start_bg("a1", "toolu_ag", task_type="local_agent")
    tool_result("toolu_ag", "Async agent launched successfully.")
    result("agent started", turns=2)
    emit(
        {
            "type": "system",
            "subtype": "task_started",
            "task_id": "n1",
            "tool_use_id": "toolu_nested",
            "description": "Inspect nested agents",
            "owned_by_subagent": True,
            "is_backgrounded": False,
            "task_type": "local_agent",
        }
    )
    emit(
        {
            "type": "system",
            "subtype": "task_notification",
            "task_id": "n1",
            "tool_use_id": "toolu_nested",
            "status": "completed",
            "output_file": "",
            "summary": "Inspect nested agents",
        }
    )
    if wait_idle_or_eof(WAKE_S) is None:
        shutdown()
    init()
    text("The sweep is back")
    if mode == "mid":
        _end_quietly("a1")
    result("The sweep is back")
    if mode != "mid":
        _end_quietly("a1")
    emit(
        {
            "type": "system",
            "subtype": "task_notification",
            "task_id": "a1",
            "tool_use_id": "toolu_ag",
            "status": "completed",
            "output_file": "",
            "summary": "sweep finished",
        }
    )
    init()
    text("The sweep has finished")
    result("The sweep has finished")
    serve_followups()


def scenario_agent_resumed(first: dict) -> None:
    """#801: a background agent finishes, the wake turn sends it back to
    re-check (SendMessage) — the CLI reuses the SAME task_id with a fresh
    snapshot + task_started — and the parent goes idle while it works. The
    re-check finishes ``FAKE_CLAUDE_WAKE_S`` later and wakes a third turn."""
    init()
    tool_use("Agent", "toolu_ag", {"description": "verify", "prompt": "go"})
    start_bg("a1", "toolu_ag", task_type="local_agent")
    tool_result("toolu_ag", "Async agent launched successfully.")
    result("agent started", turns=2)
    if wait_idle_or_eof(WAKE_S) is None:
        shutdown()
    end_bg("a1")
    init()
    tool_use("SendMessage", "toolu_sm", {"to": "a1", "message": "re-check 5b"})
    start_bg("a1", "toolu_ag", task_type="local_agent")
    tool_result("toolu_sm", "Message sent; agent a1 resumed in the background.")
    text("I've sent it back to re-check, it's running now")
    result("I've sent it back to re-check, it's running now", turns=2)
    if wait_idle_or_eof(WAKE_S) is None:
        shutdown()
    end_bg("a1")
    init()
    text("RECHECK DONE")
    result("RECHECK DONE")
    serve_followups()


_orphans: set[str] = set()  # subagent-owned bg tasks (not in the snapshot)


def scenario_agent_orphans_bg_task(first: dict) -> None:
    """#801 follow-up (dev bot 2026-09-29): a background agent backgrounds a
    ``sleep`` of its own (``owned_by_subagent`` + ``is_backgrounded``) and
    ends; the parent's wake turn says the re-check is still running and goes
    idle. The orphaned task finishes ``FAKE_CLAUDE_WAKE_S`` later — only if
    stdin is still open; on EOF it is killed like any live task (F3)."""
    init()
    tool_use("Agent", "toolu_ag", {"description": "recheck", "prompt": "go"})
    start_bg("a1", "toolu_ag", task_type="local_agent")
    tool_result("toolu_ag", "Async agent launched successfully.")
    result("agent started", turns=2)
    _orphans.add("bz1")
    emit(
        {
            "type": "system",
            "subtype": "task_started",
            "task_id": "bz1",
            "tool_use_id": "toolu_sub_sleep",
            "description": 'Wait 75 seconds then print "recheck"',
            "owned_by_subagent": True,
            "is_backgrounded": True,
            "task_type": "local_bash",
        }
    )
    end_bg("a1")  # the agent ends; its backgrounded sleep carries on
    init()
    text("The re-check is still running, I'll report when it finishes")
    result("The re-check is still running, I'll report when it finishes")
    if wait_idle_or_eof(WAKE_S) is None:
        shutdown()
    _orphans.discard("bz1")
    emit(
        {
            "type": "system",
            "subtype": "task_updated",
            "task_id": "bz1",
            "patch": {"status": "completed", "end_time": int(time.time() * 1000)},
        }
    )
    emit(
        {
            "type": "system",
            "subtype": "task_notification",
            "task_id": "bz1",
            "tool_use_id": "toolu_sub_sleep",
            "status": "completed",
            "output_file": "",
            "summary": "recheck",
        }
    )
    init()
    text("RECHECK: recheck")
    result("RECHECK: recheck")
    serve_followups()


def scenario_monitor_ticks(first: dict) -> None:
    init()
    tool_use("Monitor", "toolu_mon", {"command": "tick", "timeout_ms": 30000})
    start_bg("m1", "toolu_mon")
    tool_result("toolu_mon", "Monitor started (task m1).")
    result("watching", turns=2)
    for tick in (1, 2):
        if wait_idle_or_eof(WAKE_S) is None:
            shutdown()
        init()
        text(f"TICK {tick}")
        result(f"TICK {tick}")
    if wait_idle_or_eof(WAKE_S) is None:
        shutdown()
    end_bg("m1")
    init()
    text("TICK 3")
    result("TICK 3")
    serve_followups()


def scenario_scheduled_wakeup(first: dict) -> None:
    init()
    tool_use("ScheduleWakeup", "toolu_wk", {"delaySeconds": 60, "prompt": "WAKE"})
    tool_result("toolu_wk", "Next wakeup scheduled for 19:15:00 (in 60s).")
    result("scheduled", turns=2)
    if wait_idle_or_eof(WAKE_S) is None:
        shutdown()
    lifecycle("wake-cmd-unknown", "started")
    init()
    text("WOKE")
    result("WOKE")
    serve_followups()


def scenario_wake_chain(first: dict) -> None:
    """#925 §14.4: a dynamic /loop — every wake turn schedules the next
    wake-up, until stdin closes (at most 5 wake turns)."""
    init()
    tool_use("ScheduleWakeup", "toolu_wk0", {"delaySeconds": 60, "prompt": "WAKE"})
    tool_result("toolu_wk0", "Next wakeup scheduled for 19:15:00 (in 60s).")
    result("scheduled", turns=2)
    for n in range(1, 6):
        if wait_idle_or_eof(WAKE_S) is None:
            shutdown()
        lifecycle(f"wake-cmd-{n}", "started")
        init()
        tool_use(
            "ScheduleWakeup", f"toolu_wk{n}", {"delaySeconds": 60, "prompt": "WAKE"}
        )
        tool_result(f"toolu_wk{n}", "Next wakeup scheduled for 19:16:00 (in 60s).")
        result(f"WOKE {n}", turns=2)
    serve_followups()


def scenario_native_cron_fire(first: dict) -> None:
    """#925 §14.3: a CLI cron job (no ScheduleWakeup in this process) fires
    between turns — the native-fire detector's case."""
    init()
    text("FIRST")
    result("FIRST")
    if wait_idle_or_eof(WAKE_S) is None:
        shutdown()
    lifecycle("cron-fire-unknown", "started")
    init()
    text("TICK")
    result("TICK")
    serve_followups()


def scenario_followup(first: dict) -> None:
    init()
    text("FIRST")
    if RESULT_DELAY_S > 0:
        time.sleep(RESULT_DELAY_S)
    result("FIRST")
    serve_followups()


def scenario_compact_followup(first: dict) -> None:
    """#819: the run answers with a known context size; a ``/compact``
    follow-up compacts in the same process (its own turn), and the next
    follow-up answers with a much lower context."""
    init()
    text("FIRST", usage=60_000)
    result("FIRST", model_usage=MODEL_USAGE)
    while True:
        obj = next_user(None)
        if obj is None or obj == "timeout":
            break
        cmd = obj.get("uuid")
        if user_text(obj).strip() == "/compact":
            compact_command_turn(cmd)
            continue
        lifecycle(cmd, "queued")
        lifecycle(cmd, "started")
        init()
        text(f"ECHO: {user_text(obj)}", usage=20_000)
        result(f"ECHO: {user_text(obj)}", model_usage=MODEL_USAGE)
    shutdown()


def scenario_auto_compact_mid_turn(first: dict) -> None:
    """#819 (the shape captured in ``claude_autocompact_2.1.285.jsonl``): a
    tool loop fills the context, the CLI auto-compacts between the
    ``tool_result`` and the next request — no fresh ``init`` — and the turn
    carries on with a lower context."""
    init()
    tool_use("Read", "toolu_big", {"file_path": "big.txt"}, usage=150_000)
    tool_result("toolu_big", "lots of text")
    compaction("auto", 170_000, 30_000, heartbeats=2)
    text("done reading", usage=40_000)
    result("done reading", turns=2, model_usage=MODEL_USAGE)
    serve_followups()


def scenario_context_usage_growth(first: dict) -> None:
    """#819: three main-thread responses with rising usage in one turn."""
    init()
    tool_use("Read", "toolu_a", {"file_path": "a.txt"}, usage=20_000)
    time.sleep(WAKE_S)
    tool_result("toolu_a", "a")
    tool_use("Read", "toolu_b", {"file_path": "b.txt"}, usage=60_000)
    time.sleep(WAKE_S)
    tool_result("toolu_b", "b")
    text("GROWN", usage=124_000)
    result("GROWN", turns=3, model_usage=MODEL_USAGE)
    serve_followups()


def scenario_followup_blocks(first: dict) -> None:
    """#806: the run answers; a follow-up's turn starts a (foreground) tool
    and is still running it when the user cancels — no result ever comes."""
    init()
    text("FIRST")
    result("FIRST")
    obj = next_user(None)
    if not isinstance(obj, dict):
        shutdown()
    cmd = obj.get("uuid")
    lifecycle(cmd, "queued")
    lifecycle(cmd, "started")
    init()
    tool_use("Bash", "toolu_block", {"command": "sleep 60"})
    while next_user(None) is not None:  # the tool runs until killed / EOF
        pass
    shutdown()


def _notify(task_id: str, tool_id: str) -> None:
    emit(
        {
            "type": "system",
            "subtype": "task_notification",
            "task_id": task_id,
            "tool_use_id": tool_id,
            "status": "completed",
            "output_file": "",
            "summary": f"bg {task_id} finished",
        }
    )


def scenario_multi_agent_acks(first: dict) -> None:
    """#785 part 2 (the nsd blogs shape): two background agents. Each finish
    produces the CLI's pair of wake turns — an ``unknown`` one opened before
    the task's end lands (the end arrives mid-turn), then the task's own
    notification turn restating it. The first finish is a short ack while the
    other agent still runs; the second finish's first turn is the compiled
    report (long). ``FAKE_CLAUDE_ACK_TOOL=1`` makes the first ack run a shell
    command (a read-only ``Read`` would still fold, #813)."""
    init()
    tool_use("Agent", "toolu_a1", {"description": "sweep one", "prompt": "go"})
    start_bg("a1", "toolu_a1", task_type="local_agent")
    tool_result("toolu_a1", "Async agent launched successfully.")
    tool_use("Agent", "toolu_a2", {"description": "sweep two", "prompt": "go"})
    start_bg("a2", "toolu_a2", task_type="local_agent")
    tool_result("toolu_a2", "Async agent launched successfully.")
    text("Two sweeps running in the background; I'll report back.")
    result("Two sweeps running in the background; I'll report back.", turns=3)
    for task_id, tool_id, first_answer in (
        ("a1", "toolu_a1", "Sweep one is back; waiting on sweep two."),
        ("a2", "toolu_a2", "REPORT: " + "all findings compiled. " * 30),
    ):
        if wait_idle_or_eof(WAKE_S) is None:
            shutdown()
        init()
        if task_id == "a1" and os.environ.get("FAKE_CLAUDE_ACK_TOOL") == "1":
            tool_use("Bash", "toolu_sh", {"command": "ls /tmp/out"})
            tool_result("toolu_sh", "notes")
        text(first_answer)
        _end_quietly(task_id)
        result(first_answer)
        _notify(task_id, tool_id)
        init()
        text(f"{task_id} finished (again).")
        result(f"{task_id} finished (again).")
    serve_followups()


def scenario_two_tasks_one_wake_turn(first: dict) -> None:
    """#825 (lba-1, rc14): two background tasks; the second ends while the
    first one's wake turn is open, and the CLI folds its notification into
    that turn. ``FAKE_CLAUDE_EXTRA_TURN=1``: the second notification lands
    after the turn instead, and the CLI gives it its own (repeat) turn."""
    extra = os.environ.get("FAKE_CLAUDE_EXTRA_TURN") == "1"
    # R17-821: the second task ends after the turn's final message began
    # (the model never saw it); the CLI then runs an empty turn and wakes
    # Claude for it in a turn no task event names.
    unseen = os.environ.get("FAKE_CLAUDE_LATE_UNSEEN") == "1"
    init()
    for task_id, tool_id in (("a1", "toolu_a1"), ("a2", "toolu_a2")):
        tool_use("Bash", tool_id, {"command": "sleep", "run_in_background": True})
        start_bg(task_id, tool_id)
        tool_result(tool_id, f"Command running in background with ID: {task_id}.")
    text("Two jobs running.")
    result("Two jobs running.", turns=3)
    if wait_idle_or_eof(WAKE_S) is None:
        shutdown()
    end_bg("a1")
    init()
    text("Job one is done; checking the other.")
    if unseen:
        text("A is done; B is still running.")
        _end_quietly("a2")
        result("A is done; B is still running.", turns=2)
        init()
        _no_query_result()  # #928: the CLI's empty no-query result
        init()
        text("B printed its report.")
        result("B printed its report.")
        serve_followups()
        return
    _end_quietly("a2")
    if not extra:
        _notify("a2", "toolu_a2")
    text("Both jobs finished: B printed its report.")
    result("Both jobs finished: B printed its report.", turns=2)
    if extra:
        _notify("a2", "toolu_a2")
        init()
        text("a2 finished (again).")
        result("a2 finished (again).")
    serve_followups()


def scenario_fg_task_backgrounded(first: dict) -> None:
    """#825/#876 (nsd, rc16): the parent runs a FOREGROUND Bash; past its
    timeout the CLI moves it to the background (``task_updated{patch:
    {is_backgrounded: true}}``) and the turn ends. The task finishes later
    and wakes the parent. ``FAKE_CLAUDE_BG_PATCH=0`` omits the patch (only
    the idle notification shows the move)."""
    init()
    tool_use("Bash", "toolu_fg", {"command": "cp -r big dest", "timeout": 10000})
    _live_tasks["f1"] = "toolu_fg"
    emit(
        {
            "type": "system",
            "subtype": "task_started",
            "task_id": "f1",
            "tool_use_id": "toolu_fg",
            "description": "copy attempt",
            "is_backgrounded": False,
            "task_type": "local_bash",
        }
    )
    if os.environ.get("FAKE_CLAUDE_BG_PATCH", "1") != "0":
        emit(
            {
                "type": "system",
                "subtype": "task_updated",
                "task_id": "f1",
                "patch": {"is_backgrounded": True},
            }
        )
    tool_result(
        "toolu_fg",
        "Command is still running after 10s. It was moved to the background as "
        "task f1 and keeps running; you'll receive a notification with the "
        "result when it completes.",
    )
    text("The copy is still running in the background.")
    result("The copy is still running in the background.", turns=2)
    if wait_idle_or_eof(WAKE_S) is None:
        shutdown()
    end_bg("f1")
    init()
    text("COPY DONE")
    result("COPY DONE")
    serve_followups()


def _read_ack(turn: int, task_id: str, answer: str) -> None:
    """#813: a wake turn that collects a finished task's result with one
    ``Read`` of its output file (CLI >= 2.1.277) and then acks it."""
    tool_id = f"toolu_read_{turn}"
    tool_use("Read", tool_id, {"file_path": f"/tmp/tasks/{task_id}.output"})
    tool_result(tool_id, f"{task_id} output")
    text(answer)


def scenario_five_agent_interleaved(first: dict) -> None:
    """#813 (the nsd 5-task / 9-turn batch): five background agents; every
    wake turn collects a result with one ``Read`` and acks it. Ends are
    interleaved the way the CLI delivers them — a1 lands just after its
    unnamed ack (paired) then restates it; a2 ends inside its ack
    (retro-attributed); a3 and a4 each end just after their own unnamed ack
    (both paired) and are only restated afterwards, so a3's restatement
    arrives while the *latest* unattributed ack is a4's; a5 ends inside the
    report turn, then restates it."""
    init()
    for n in range(1, 6):
        tool_use("Agent", f"toolu_a{n}", {"description": f"agent {n}", "prompt": "go"})
        start_bg(f"a{n}", f"toolu_a{n}", task_type="local_agent")
        tool_result(f"toolu_a{n}", "Async agent launched successfully.")
    text("Five agents running in the background; I'll report back.")
    result("Five agents running in the background; I'll report back.", turns=6)

    def wake() -> None:
        if wait_idle_or_eof(WAKE_S) is None:
            shutdown()
        init()

    # T2 unnamed ack; a1 ends just after it (paired with T2).
    wake()
    _read_ack(2, "a1", "a1 is back; four still running.")
    result("a1 is back; four still running.")
    _end_quietly("a1")
    # T3: a1's own notification turn restates it.
    _notify("a1", "toolu_a1")
    init()
    _read_ack(3, "a1", "a1 findings filed.")
    result("a1 findings filed.")
    # T4: a2 ends inside its ack turn (retro-attributed).
    wake()
    _read_ack(4, "a2", "a2 is back.")
    _end_quietly("a2")
    result("a2 is back.")
    # T5 / T6: unnamed acks, each followed by its task's end (paired).
    wake()
    _read_ack(5, "a3", "a3 is back.")
    result("a3 is back.")
    _end_quietly("a3")
    wake()
    _read_ack(6, "a4", "a4 is back.")
    result("a4 is back.")
    _end_quietly("a4")
    # T7 / T8: the restatements, a3's first (latest unattributed ack = a4's).
    for turn, task_id in ((7, "a3"), (8, "a4")):
        _notify(task_id, f"toolu_{task_id}")
        init()
        _read_ack(turn, task_id, f"{task_id} findings filed.")
        result(f"{task_id} findings filed.")
    # T9: the report; a5 ends inside it. T10: a5's restatement.
    wake()
    report = "REPORT: " + "all five agents finished; findings compiled. " * 12
    _read_ack(9, "a5", report)
    _end_quietly("a5")
    result(report)
    _notify("a5", "toolu_a5")
    init()
    _read_ack(10, "a5", "a5 finished (again).")
    result("a5 finished (again).")
    serve_followups()


def scenario_quiet_batch_report(first: dict) -> None:
    """#785 dev-bot regression (session 09ab089b): three background tasks;
    two short acks fold; an ``unknown`` ack turn completes while the shell
    task still runs; the shell task then ends moments later (the runner pairs
    that end with the ack turn, so the task counts as announced); its own
    notification turn is the batch's only real content — a long report."""
    init()
    for task_id, tool_id, kind in (
        ("a1", "toolu_a1", "local_agent"),
        ("a2", "toolu_a2", "local_agent"),
        ("b3", "toolu_b3", "local_bash"),
    ):
        tool_use("Agent" if kind == "local_agent" else "Bash", tool_id, {})
        start_bg(task_id, tool_id, task_type=kind)
        tool_result(tool_id, "launched")
    result("Three jobs running; I'll report back.", turns=4)
    for task_id in ("a1", "a2"):
        if wait_idle_or_eof(WAKE_S) is None:
            shutdown()
        init()
        text(f"{task_id} is back.")
        _end_quietly(task_id)
        result(f"{task_id} is back.")
    if wait_idle_or_eof(WAKE_S) is None:
        shutdown()
    init()
    text("Still waiting on the shell job.")
    result("Still waiting on the shell job.")
    _end_quietly("b3")
    _notify("b3", "toolu_b3")
    init()
    report = "REPORT: " + "all three jobs finished cleanly. " * 20
    text(report)
    result(report)
    serve_followups()


def scenario_acks_only_batch(first: dict) -> None:
    """#785: every wake turn of the batch is a short ack that folds, and the
    last task ends with no turn after it — the batch still needs one push."""
    init()
    for task_id, tool_id in (("b1", "toolu_b1"), ("b2", "toolu_b2")):
        tool_use("Bash", tool_id, {"command": "sleep 20", "run_in_background": True})
        start_bg(task_id, tool_id)
        tool_result(tool_id, "running")
    result("Two jobs running.", turns=3)
    if wait_idle_or_eof(WAKE_S) is None:
        shutdown()
    end_bg("b1")
    init()
    text("b1 done; waiting on b2.")
    result("b1 done; waiting on b2.")
    if wait_idle_or_eof(WAKE_S) is None:
        shutdown()
    _end_quietly("b2")
    serve_followups()


def scenario_report_then_noop(first: dict) -> None:
    """#785 dev bot (session 4b3a2ab8): the task's report pushes; two seconds
    later the CLI runs an unnamed ``unknown`` no-op turn ("nothing new"), and
    later a ScheduleWakeup fires with nothing new either."""
    init()
    tool_use("Bash", "toolu_b1", {"command": "sleep 20", "run_in_background": True})
    start_bg("b1", "toolu_b1")
    tool_result("toolu_b1", "running")
    result("One job running.", turns=2)
    if wait_idle_or_eof(WAKE_S) is None:
        shutdown()
    end_bg("b1")
    init()
    report = "REPORT: " + "the job finished cleanly. " * 20
    text(report)
    result(report)
    init()
    text("I already posted its output above; nothing new.")
    result("I already posted its output above; nothing new.")
    lifecycle("wake-cmd-late", "started")
    init()
    text("Wake-up: still nothing new.")
    result("Wake-up: still nothing new.")
    serve_followups()


def scenario_followup_launches_bg(first: dict) -> None:
    """#795: the run answers; a follow-up (injected user line) launches a
    background Bash; that task's wake turn must reply to the follow-up."""
    init()
    text("FIRST")
    result("FIRST")
    obj = next_user(None)
    if not isinstance(obj, dict):
        shutdown()
    cmd = obj.get("uuid")
    lifecycle(cmd, "queued")
    lifecycle(cmd, "started")
    init()
    tool_use("Bash", "toolu_bg2", {"command": "sleep 20", "run_in_background": True})
    start_bg("b2", "toolu_bg2")
    tool_result("toolu_bg2", "Command running in background with ID: b2.")
    text("launched")
    result("launched", turns=2)
    if wait_idle_or_eof(WAKE_S) is None:
        shutdown()
    end_bg("b2")
    init()
    text("GOT: B2")
    result("GOT: B2")
    serve_followups()


def scenario_resume_after_killed_task(first: dict) -> None:
    resumed = "--resume" in sys.argv or "-r" in sys.argv
    if resumed:
        emit(
            {
                "type": "system",
                "subtype": "task_notification",
                "task_id": "b-old",
                "tool_use_id": "toolu_old",
                "status": "stopped",
                "output_file": "",
                "summary": "Background shell command didn't finish before exit",
            }
        )
        init()
        global _cost
        _cost = 0.05  # previous process's session total (F12)
        result("", turns=0, delta=0.0, api_ms=0)
    init()
    text("4")
    result("4")
    serve_followups()


def scenario_inherited_fd_after_exit(first: dict) -> None:
    # A grandchild inherits stdout and outlives us (#505): the reader must
    # not block on it forever.
    subprocess.Popen(["sleep", "30"], stdout=sys.stdout, stderr=subprocess.DEVNULL)
    init()
    text("done")
    result("done")
    sys.stdout.flush()
    os._exit(0)


def scenario_exit_after_result(first: dict) -> None:
    # #820: the CLI ends on its own right after its result (no close by
    # Untether, no grandchild holding stdout — unlike the scenario above).
    init()
    text("done")
    result("done")
    sys.stdout.flush()
    os._exit(0)


def _maybe_ignore_sigint() -> None:
    # #791: FAKE_CLAUDE_IGNORE_SIGINT=1 models a CLI that is also deaf to
    # the Ctrl-C path, so the close escalates on to SIGTERM.
    if os.environ.get("FAKE_CLAUDE_IGNORE_SIGINT") == "1":
        signal.signal(signal.SIGINT, signal.SIG_IGN)


def scenario_ignore_eof(first: dict) -> None:
    # A wedged CLI: answers, then ignores stdin EOF (forces the signal path).
    _maybe_ignore_sigint()
    init()
    text("stuck")
    result("stuck")
    while next_user(None) is not None:
        pass
    time.sleep(60)


def scenario_ignore_eof_with_task(first: dict) -> None:
    # #791: wedged with a background task still live — the transcript has a
    # dangling background tool_use, so a forced teardown must quarantine.
    _maybe_ignore_sigint()
    init()
    tool_use("Bash", "toolu_bg", {"command": "sleep 60", "run_in_background": True})
    start_bg("b1", "toolu_bg")
    tool_result("toolu_bg", "Command running in background with ID: b1.")
    result("waiting", turns=2)
    while next_user(None) is not None:
        pass
    time.sleep(60)


def scenario_error_first(first: dict) -> None:
    # The first result is an error (usage limit / API error) — the session
    # must not be kept live. ``FAKE_CLAUDE_ERROR_TEXT`` overrides the error
    # (#900: a Type-A stall for the #572 retry).
    init()
    emit(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "duration_ms": 500,
            "duration_api_ms": 400,
            "num_turns": 1,
            "result": os.environ.get("FAKE_CLAUDE_ERROR_TEXT")
            or "API Error: overloaded",
            "total_cost_usd": 0.001,
        }
    )
    serve_followups()


def scenario_error_first_agent_wake(first: dict) -> None:
    """#900 (mac 2026-10-02): the run's own result is an error while a
    background agent is still running. Untether closes stdin on the errored
    result; the agent ignores EOF (#829 G6), finishes, and the CLI runs a
    wake turn for it before exiting. ``FAKE_CLAUDE_ERROR_TEXT`` sets the
    run's error (default ``API Error: RUN-FAILED``); ``FAKE_CLAUDE_WAKE_OK``
    makes the wake turn succeed."""
    global _cost
    init()
    tool_use("Agent", "toolu_ag", {"description": "research", "prompt": "go"})
    start_bg("a1", "toolu_ag", task_type="local_agent")
    tool_result("toolu_ag", "Async agent launched successfully.")
    _cost = round(_cost + 0.5, 6)
    emit(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "duration_ms": 900,
            "duration_api_ms": 800,
            "num_turns": 2,
            "result": os.environ.get("FAKE_CLAUDE_ERROR_TEXT")
            or "API Error: RUN-FAILED",
            "total_cost_usd": _cost,
        }
    )
    time.sleep(WAKE_S)  # stdin EOF is ignored while the agent works
    end_bg("a1")
    init()
    if os.environ.get("FAKE_CLAUDE_WAKE_OK"):
        text("WAKE-REPORT")
        result("WAKE-REPORT")
    else:
        _cost = round(_cost + 0.06, 6)
        emit(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "duration_ms": 300,
                "duration_api_ms": 200,
                "num_turns": 1,
                "result": "API Error: WAKE-FAILED",
                "total_cost_usd": _cost,
            }
        )
    sys.exit(0)


def scenario_safeguard_refusal_retry(first: dict) -> None:
    # #814: the nsd transcript shape — the model's first response is
    # stopped by Anthropic's safeguards (assistant ``stop_reason:"refusal"``
    # with ``stop_details``), the CLI prints its informational notice and
    # re-runs once on the same model, and the turn completes normally.
    init()
    emit(
        {
            "type": "assistant",
            "message": {
                "id": "msg_refused",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": "Looking at the exploit"}],
                "stop_reason": "refusal",
                "stop_details": {
                    "type": "refusal",
                    "category": "cyber",
                    "explanation": "flagged",
                },
            },
        }
    )
    emit(
        {
            "type": "system",
            "subtype": "informational",
            "content": (
                "Opus 5.5's safeguards stopped the response above \u00b7 "
                "continuing once with that noted"
            ),
            "level": "notice",
            "uuid": "info-1",
        }
    )
    text("Here is the defensive summary.")
    result("Here is the defensive summary.", turns=2)
    serve_followups()


def _collect_steers() -> tuple[list[dict], bool]:
    """Wait for steered user lines (#775): the first within STEER_WAIT_S,
    then any more until a short quiet gap. Returns (steers, eof)."""
    steers: list[dict] = []
    got = next_user(STEER_WAIT_S)
    while isinstance(got, dict):
        steers.append(got)
        lifecycle(got.get("uuid"), "queued")
        got = next_user(0.3)
    return steers, got is None


def scenario_steer_mid_tool(first: dict) -> None:
    """#775 / probe F5: user lines written while a tool runs are folded into
    the SAME turn at the next tool boundary — one result that honours them.
    The CLI reports each as ``command_lifecycle{started}`` after the tool's
    result, while the turn is still open (probed on CLI 2.1.284)."""
    init()
    tool_use("Bash", "toolu_sleep", {"command": "sleep 8"})
    steers, eof = _collect_steers()
    tool_result("toolu_sleep", "slept")
    for steer in steers:
        lifecycle(steer.get("uuid"), "started")
    answer = "DONE"
    if steers:
        answer += " + " + " | ".join(user_text(s) for s in steers)
    text(answer)
    result(answer, turns=2)
    for steer in steers:
        lifecycle(steer.get("uuid"), "completed")
    if eof:
        shutdown()
    serve_followups()


def scenario_steer_post_last_tool(first: dict) -> None:
    """#775 / probe F6: a user line written after the turn's last tool call
    (during text generation) becomes the NEXT turn in the same process — a
    second result, started only after the first one."""
    init()
    tool_use("Bash", "toolu_echo", {"command": "echo hi"})
    tool_result("toolu_echo", "hi")
    steers, eof = _collect_steers()
    text("FIRST")
    result("FIRST", turns=2)
    for steer in steers:
        lifecycle(steer.get("uuid"), "started")
        init()
        text(f"ECHO: {user_text(steer)}")
        result(f"ECHO: {user_text(steer)}")
    if eof:
        shutdown()
    serve_followups()


# ── #812 hooks (``--include-hook-events`` frames, CLI 2.1.284 shapes) ──────
# How long the CLI keeps running after stdin EOF while an asyncRewake hook
# is still pending (the real CLI: up to 30 s, ``Rxo``). A rewake that fires
# in that window is dropped — no turn, no hook_response (§A1 P5-B).
REWAKE_WAIT_S = float(os.environ.get("FAKE_CLAUDE_REWAKE_WAIT_S", "0.2"))


def hook_started(hook_id: str, event: str, name: str | None = None) -> None:
    emit(
        {
            "type": "system",
            "subtype": "hook_started",
            "hook_id": hook_id,
            "hook_name": name or event,
            "hook_event": event,
            "uuid": f"u-{hook_id}-s",
        }
    )


def hook_response(
    hook_id: str,
    event: str,
    *,
    outcome: str = "success",
    exit_code: int | None = 0,
    stderr: str = "",
    name: str | None = None,
) -> None:
    payload = {
        "type": "system",
        "subtype": "hook_response",
        "hook_id": hook_id,
        "hook_name": name or event,
        "hook_event": event,
        "output": stderr,
        "stdout": "",
        "stderr": stderr,
        "outcome": outcome,
        "uuid": f"u-{hook_id}-r",
    }
    if exit_code is not None:
        payload["exit_code"] = exit_code
    emit(payload)


# ── hook processes ──
# The real CLI runs every command hook as a ``/bin/sh -c <command>`` child,
# spawned detached (its own session / process group; CLI 2.1.285), right
# after emitting ``hook_started``. Untether's hold reads the process table
# (#812): no hook process left → nothing can still rewake. So background
# hooks here run a real, detached process; MCP-like services do not.
_hook_procs: dict[str, subprocess.Popen] = {}
# Plain ``async: true`` hooks' responses: the CLI withholds them while the
# session is idle ("the response waits until the next user interaction")
# and flushes them at the next turn or at teardown (probed on CLI 2.1.285).
_withheld: list[tuple[str, str]] = []


def spawn_hook(hook_id: str, seconds: float) -> subprocess.Popen:
    proc = subprocess.Popen(  # the real CLI's hook shape: sh -c, detached
        f"sleep {seconds}; true",
        shell=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _hook_procs[hook_id] = proc
    return proc


def wait_hook(hook_id: str) -> None:
    proc = _hook_procs.pop(hook_id, None)
    if proc is not None:
        proc.wait()


def flush_withheld() -> None:
    while _withheld:
        hook_id, event = _withheld.pop(0)
        hook_response(hook_id, event)


def _kill_children(pid: int) -> None:
    with contextlib.suppress(OSError):
        for tid in os.listdir(f"/proc/{pid}/task"):
            with open(f"/proc/{pid}/task/{tid}/children") as f:
                for tok in f.read().split():
                    with contextlib.suppress(OSError, ValueError):
                        os.kill(int(tok), signal.SIGKILL)


def kill_hooks() -> None:
    for proc in _hook_procs.values():
        with contextlib.suppress(OSError):
            # A detached hook leads its own group: take its children too.
            if os.getpgid(proc.pid) == proc.pid:
                os.killpg(proc.pid, signal.SIGKILL)
        _kill_children(proc.pid)  # e.g. a service shell's command
        with contextlib.suppress(OSError):
            proc.kill()
        proc.wait()
    _hook_procs.clear()


def _eof_with_pending_rewake() -> None:
    # P5-B: the CLI waits for the pending asyncRewake hook, then exits
    # without running (or even reporting) it.
    time.sleep(REWAKE_WAIT_S)
    shutdown()


def _stop_turn(answer: str, *hooks: tuple[str, str], hook_s: float = WAKE_S) -> None:
    """A turn whose background Stop hook(s) start before the result (their
    ``sh -c`` process outlives it; the response lands after the result)."""
    init()
    hook_started("h-ups-1", "UserPromptSubmit")
    hook_response("h-ups-1", "UserPromptSubmit")
    text(answer)
    for hook_id, event in hooks:
        spawn_hook(hook_id, hook_s)
        hook_started(hook_id, event)
    result(answer)


def scenario_async_rewake_idle(first: dict) -> None:
    _stop_turn("DONE", ("h-stop", "Stop"))
    got = wait_idle_or_eof(WAKE_S)
    if got is None:
        _eof_with_pending_rewake()
    wait_hook("h-stop")
    # stdin still open: the rewake exits 2 and the CLI wakes itself (P5-A).
    hook_response(
        "h-stop", "Stop", outcome="error", exit_code=2, stderr="finding: key leak\n"
    )
    _rewake_turn("HOOK: finding: key leak")
    serve_followups()


def _rewake_turn(answer: str) -> None:
    """The turn the CLI starts itself after an asyncRewake hook exits 2
    (P5-A); withheld plain-async responses flush as it opens."""
    hook_started("h-ups-2", "UserPromptSubmit")
    hook_response("h-ups-2", "UserPromptSubmit")
    flush_withheld()
    init()
    text(answer)
    global _cost
    _cost = round(_cost + 0.01, 6)
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 1000,
            "duration_api_ms": 900,
            "num_turns": 1,
            "result": answer,
            "total_cost_usd": _cost,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "origin": {"kind": "task-notification", "producer": "session-task"},
        }
    )


def _task_notification_result(answer: str) -> None:
    """A result of a turn the CLI started itself (``origin`` =
    ``task-notification``), as every background-task wake turn has."""
    global _cost
    _cost = round(_cost + 0.01, 6)
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 1000,
            "duration_api_ms": 900,
            "num_turns": 1,
            "result": answer,
            "total_cost_usd": _cost,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "origin": {"kind": "task-notification", "producer": "session-task"},
        }
    )


def _no_query_result() -> None:
    """#928: the CLI's documented no-query result — a notification answered
    together with others: no model call, empty, ``num_turns: 0``,
    ``origin`` task-notification (SDK docs, CLI 2.1.289)."""
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 2,
            "duration_api_ms": 0,
            "num_turns": 0,
            "result": "",
            "total_cost_usd": _cost,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "origin": {"kind": "task-notification"},
        }
    )


def scenario_agent_wake_no_query_tail(first: dict) -> None:
    """#928 (channelo/sl/lba-1/nsd, rc19): two background agents. a1's wake
    turn is followed ~0.2 s later by the CLI's empty no-query result —
    with its own ``init`` when ``FAKE_CLAUDE_NO_QUERY_INIT=1`` (case I),
    bare otherwise (case R). Then a2 finishes and gets its own wake turn."""
    init()
    for task_id, tool_id in (("a1", "toolu_a1"), ("a2", "toolu_a2")):
        tool_use("Agent", tool_id, {"description": f"sweep {task_id}", "prompt": "go"})
        start_bg(task_id, tool_id, task_type="local_agent")
        tool_result(tool_id, "Async agent launched successfully.")
    text("Two sweeps running.")
    result("Two sweeps running.", turns=3)
    if wait_idle_or_eof(WAKE_S) is None:
        shutdown()
    end_bg("a1")
    init()
    text("a1 done")
    _task_notification_result("a1 done")
    time.sleep(0.2)
    if os.environ.get("FAKE_CLAUDE_NO_QUERY_INIT") == "1":
        init()
    _no_query_result()
    if wait_idle_or_eof(1.0) is None:
        shutdown()
    end_bg("a2")
    init()
    text("a2 done")
    _task_notification_result("a2 done")
    serve_followups()


def scenario_bg_agent_pretooluse_denial(first: dict) -> None:
    """#828 (channelo, rc14): while the parent idles, a background agent's
    Bash call is denied by a sync ``PreToolUse`` hook (exit 2, started and
    answered while idle). The agent then finishes and the CLI opens its wake
    turn well inside the 10 s rewake TTL; the agent's end lands mid-turn."""
    init()
    tool_use("Agent", "toolu_ag", {"description": "builder", "prompt": "go"})
    start_bg("a1", "toolu_ag", task_type="local_agent")
    tool_result("toolu_ag", "Async agent launched successfully.")
    text("Builder running in the background.")
    result("Builder running in the background.", turns=2)
    if wait_idle_or_eof(WAKE_S) is None:
        shutdown()
    hook_started("h-pre", "PreToolUse", name="PreToolUse:Bash")
    hook_response(
        "h-pre",
        "PreToolUse",
        outcome="error",
        exit_code=2,
        stderr="blocked",
        name="PreToolUse:Bash",
    )
    if wait_idle_or_eof(WAKE_S) is None:
        shutdown()
    init()
    text("agent done")
    _end_quietly("a1")
    _task_notification_result("agent done")
    serve_followups()


def scenario_bg_agent_async_rewake_idle(first: dict) -> None:
    """#923 (lba-1, rc17+): while the parent idles, a background agent's
    ``git commit`` fires an asyncRewake ``PostToolUse:Bash`` hook (e.g.
    security-guidance's review). It starts AND exits 2 in the idle gap after
    running a while, and the CLI opens the parent's rewake turn at once.
    Later the agent finishes and gets its own wake turn."""
    init()
    tool_use("Agent", "toolu_ag", {"description": "committer", "prompt": "go"})
    start_bg("a1", "toolu_ag", task_type="local_agent")
    tool_result("toolu_ag", "Async agent launched successfully.")
    text("Committer running in the background.")
    result("Committer running in the background.", turns=2)
    if wait_idle_or_eof(WAKE_S) is None:
        shutdown()
    spawn_hook("h-sub", 1.5)
    hook_started("h-sub", "PostToolUse", name="PostToolUse:Bash")
    wait_hook("h-sub")
    hook_response(
        "h-sub",
        "PostToolUse",
        outcome="error",
        exit_code=2,
        stderr="R20 finding\n",
        name="PostToolUse:Bash",
    )
    _rewake_turn("HOOK: R20 finding")
    if wait_idle_or_eof(WAKE_S) is None:
        shutdown()
    end_bg("a1")
    init()
    text("agent done")
    _task_notification_result("agent done")
    serve_followups()


def scenario_async_hook_success(first: dict) -> None:
    _stop_turn("DONE", ("h-stop", "Stop"))
    got = wait_idle_or_eof(WAKE_S)
    if got is None:
        _eof_with_pending_rewake()
    wait_hook("h-stop")
    hook_response("h-stop", "Stop", outcome="success", exit_code=0)
    serve_followups()


def scenario_async_hook_post_result_response(first: dict) -> None:
    """Live regression (CLI 2.1.285): plain ``async: true`` hooks (e.g.
    ``moshi-hook claude-hook``) on UserPromptSubmit + Stop next to sync
    hooks and one ``asyncRewake`` Stop hook, in the probe's frame order.
    The sync hooks answer before the result; the rewake hook answers
    ``WAKE_S`` after it, while idle; the plain async hooks' processes exit
    at once but their responses are withheld until stdin closes."""
    hook_started("h-ss", "SessionStart", name="SessionStart:startup")
    hook_response("h-ss", "SessionStart", name="SessionStart:startup")
    hook_started("h-ups-a", "UserPromptSubmit")
    spawn_hook("h-ups-b", 0.01)
    hook_started("h-ups-b", "UserPromptSubmit")  # plain async
    _withheld.append(("h-ups-b", "UserPromptSubmit"))
    hook_started("h-ups-c", "UserPromptSubmit")
    hook_response("h-ups-a", "UserPromptSubmit")
    hook_response("h-ups-c", "UserPromptSubmit")
    init()
    text("DONE")
    hook_started("h-stop-a", "Stop")
    spawn_hook("h-stop-b", 0.01)
    hook_started("h-stop-b", "Stop")  # plain async
    _withheld.append(("h-stop-b", "Stop"))
    spawn_hook("h-stop-c", WAKE_S)
    hook_started("h-stop-c", "Stop")  # asyncRewake, exits 0
    hook_started("h-stop-d", "Stop")
    hook_response("h-stop-a", "Stop")
    hook_response("h-stop-d", "Stop")
    result("DONE")
    wait_hook("h-ups-b")
    wait_hook("h-stop-b")
    if wait_idle_or_eof(WAKE_S) is None:
        shutdown()
    wait_hook("h-stop-c")
    hook_response("h-stop-c", "Stop")
    serve_followups()


# How long the live mix's synchronous Stop hook runs after the result.
SYNC_HOOK_S = float(os.environ.get("FAKE_CLAUDE_SYNC_HOOK_S", "1.5"))


def spawn_exec_hook(hook_id: str, seconds: float) -> subprocess.Popen:
    """A hook whose shell exec'd its command (bash — macOS ``/bin/sh`` —
    does that for a single simple command): no ``<shell> -c`` process is
    left to see."""
    proc = subprocess.Popen(
        ["sleep", str(seconds)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _hook_procs[hook_id] = proc
    return proc


def _live_mix_turn(rewake_s: float) -> None:
    """Live regression (CLI 2.1.285, @untether_dev_bot, 06:24): the
    user-global plain ``async: true`` hook (moshi-hook) on UserPromptSubmit +
    Stop — its process exits at once and the CLI withholds its response
    while idle — a user-global sync Stop hook (no ``<shell> -c`` visible,
    responds ``SYNC_HOOK_S`` after the result), and a project
    ``asyncRewake`` Stop hook whose inline command (no ``/hooks/`` path)
    runs ``rewake_s``. All four ``hook_started`` land within a few ms."""
    fast = 0.01
    spawn_hook("h-ups", fast)
    hook_started("h-ups", "UserPromptSubmit")  # plain async, withheld
    _withheld.append(("h-ups", "UserPromptSubmit"))
    init()
    text("DONE")
    spawn_hook("h-stop-plain", fast)
    hook_started("h-stop-plain", "Stop")  # plain async, withheld
    _withheld.append(("h-stop-plain", "Stop"))
    spawn_hook("h-stop-rewake", rewake_s)
    hook_started("h-stop-rewake", "Stop")  # asyncRewake, still running
    spawn_exec_hook("h-stop-sync", SYNC_HOOK_S)
    hook_started("h-stop-sync", "Stop")  # sync
    result("DONE")
    wait_hook("h-stop-sync")
    hook_response("h-stop-sync", "Stop")


def scenario_async_hook_live_mix_rewake(first: dict) -> None:
    # The rewake fires (exit 2) ``WAKE_S`` after spawn — while idle, if
    # stdin is still open; after EOF the CLI drops it (P5-B).
    _live_mix_turn(WAKE_S)
    proc = _hook_procs["h-stop-rewake"]
    while proc.poll() is None:
        got = next_user(0.05)
        if got is None:
            _eof_with_pending_rewake()
        if isinstance(got, dict):
            _deferred.append(got)
    wait_hook("h-stop-rewake")
    hook_response(
        "h-stop-rewake",
        "Stop",
        outcome="error",
        exit_code=2,
        stderr="finding: key leak\n",
    )
    _rewake_turn("HOOK: finding: key leak")
    serve_followups()


def scenario_async_hook_live_mix_running(first: dict) -> None:
    # The rewake hook outlives the hold bound (``sleep 120`` vs 45 s live).
    _live_mix_turn(600)
    while next_user(None) is not None:
        pass
    # EOF: the withheld plain responses flush; the running hook is killed
    # with no response (§A1 P3/P5-B).
    _eof_with_pending_rewake()


# The model's part of a turn — real Stop hooks start a model call after
# ``system/init``, well after Untether records its baseline there.
MODEL_S = 0.5


def spawn_execd_hook(hook_id: str, seconds: float) -> subprocess.Popen:
    """A hook whose shell execs its single command — bash (macOS
    ``/bin/sh``) and zsh do this for ``sh -c '<one command>'``, e.g. the
    security-guidance plugin's ``bash …/sg-python.sh …`` asyncRewake hook.
    ``exec`` makes dash do the same: the live process is the command, a
    direct child of the CLI, with no ``<shell> -c`` left."""
    proc = subprocess.Popen(
        f"exec sleep {seconds}",
        shell=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _hook_procs[hook_id] = proc
    return proc


def spawn_service(name: str, *argv: str) -> None:
    """A long-lived non-hook child of the CLI (an MCP server)."""
    _hook_procs[name] = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)", *argv],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def scenario_async_hook_exec_rewake(first: dict) -> None:
    """The macOS shape of #812's target: plain async UserPromptSubmit + Stop
    hooks (response withheld) and an asyncRewake Stop hook whose shell
    exec'd its command. Rewakes ``WAKE_S`` after spawn if stdin is open."""
    fast = 0.01
    spawn_hook("h-ups", fast)
    hook_started("h-ups", "UserPromptSubmit")
    _withheld.append(("h-ups", "UserPromptSubmit"))
    init()
    time.sleep(MODEL_S)  # the model's turn: Stop hooks come after it
    text("DONE")
    spawn_hook("h-stop-plain", fast)
    hook_started("h-stop-plain", "Stop")
    _withheld.append(("h-stop-plain", "Stop"))
    proc = spawn_execd_hook("h-stop-rewake", WAKE_S)
    hook_started("h-stop-rewake", "Stop")
    result("DONE")
    _rewake_when_done(proc, "h-stop-rewake", "Stop")


def _rewake_when_done(proc: subprocess.Popen, hook_id: str, event: str) -> None:
    """While idle, wait for an asyncRewake hook's process; it exits 2 and
    the CLI wakes itself (P5-A) — or, after stdin EOF, drops it (P5-B)."""
    while proc.poll() is None:
        got = next_user(0.05)
        if got is None:
            _eof_with_pending_rewake()
        if isinstance(got, dict):
            _deferred.append(got)
    wait_hook(hook_id)
    hook_response(
        hook_id,
        event,
        outcome="error",
        exit_code=2,
        stderr="finding: key leak\n",
    )
    _rewake_turn("HOOK: finding: key leak")
    serve_followups()


def scenario_async_hook_ups_rewake(first: dict) -> None:
    """#812 review: an asyncRewake UserPromptSubmit hook (its shell exec'd
    the command) is still running at ``system/init``, where Untether takes
    its baseline — ``hook_started`` for UserPromptSubmit precedes init. It
    must not be baselined: held until its rewake ``WAKE_S`` after spawn."""
    hook_started("h-ups-rewake", "UserPromptSubmit")  # frame, then spawn
    proc = spawn_execd_hook("h-ups-rewake", WAKE_S)
    time.sleep(0.2)  # let the exec land before init
    init()
    time.sleep(MODEL_S)
    text("DONE")
    result("DONE")
    _rewake_when_done(proc, "h-ups-rewake", "UserPromptSubmit")


def scenario_async_hook_service_named_rewake(first: dict) -> None:
    """#812 review: an asyncRewake Stop hook whose argv happens to look like
    an MCP server (``uvx mcp-scan``, a script in an ``acme-mcp/`` repo) —
    still a hook (detached), so held until its rewake."""
    init()
    time.sleep(MODEL_S)
    text("DONE")
    hook_started("h-stop-rewake", "Stop")
    proc = subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({WAKE_S})", "mcp-scan"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _hook_procs["h-stop-rewake"] = proc
    result("DONE")
    _rewake_when_done(proc, "h-stop-rewake", "Stop")


def scenario_async_hook_with_services(first: dict) -> None:
    """MCP servers up before ``system/init`` (the baseline — one wrapped in
    a non-exec'ing ``sh -c``) and one started later (a reconnect) must never
    read as a running hook: the plain async hooks are released once their
    own processes are gone."""
    spawn_service("svc-baseline", "trello-server")
    # ``"command": "sh", "args": ["-c", "cd srv && node x.js"]``: the shell
    # stays as the CLI's (non-detached) child.
    _hook_procs["svc-shell"] = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 600; true", "wrapped-server"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    spawn_hook("h-ups", 0.01)
    hook_started("h-ups", "UserPromptSubmit")
    _withheld.append(("h-ups", "UserPromptSubmit"))
    init()
    text("DONE")
    spawn_hook("h-stop-plain", 0.01)
    hook_started("h-stop-plain", "Stop")
    _withheld.append(("h-stop-plain", "Stop"))
    result("DONE")
    spawn_service("svc-late", "npm", "exec", "firecrawl-mcp")
    while next_user(None) is not None:
        pass
    shutdown()


def scenario_async_hook_no_response(first: dict) -> None:
    # A hook that never reports back (exercises the hold bound).
    _stop_turn("DONE", ("h-stop", "Stop"), hook_s=600)
    while next_user(None) is not None:
        pass
    _eof_with_pending_rewake()


def scenario_plain_async_cancelled_on_eof(first: dict) -> None:
    # A plain `async` hook: the CLI kills it at stdin close and reports it
    # cancelled (§A1 P3), then exits.
    _stop_turn("DONE", ("h-async", "PostToolUse"), hook_s=600)
    while next_user(None) is not None:
        pass
    hook_response("h-async", "PostToolUse", outcome="cancelled", exit_code=1)
    shutdown()


def scenario_hook_flood(first: dict) -> None:
    # Every configured hook on every tool call emits a started/response
    # pair; none of it may reach progress rows.
    init()
    for i in range(40):
        tool_id = f"toolu_f{i}"
        hook_started(f"h-pre-{i}", "PreToolUse")
        hook_response(f"h-pre-{i}", "PreToolUse")
        if i == 0:
            tool_use("Bash", tool_id, {"command": "echo hi"})
            tool_result(tool_id, "hi")
        hook_started(f"h-post-{i}", "PostToolUse")
        hook_response(f"h-post-{i}", "PostToolUse")
    text("FLOOD DONE")
    result("FLOOD DONE", turns=2)
    serve_followups()


# ── #829: background activity and the live-session hold ───────────────────

PROGRESS_S = float(os.environ.get("FAKE_CLAUDE_PROGRESS_S", "0.1"))
PROGRESS_FOR_S = float(os.environ.get("FAKE_CLAUDE_PROGRESS_FOR_S", "1.0"))
TOOL_S = float(os.environ.get("FAKE_CLAUDE_TOOL_S", "1.0"))


def _mark_progress() -> None:
    """Write the wall-clock time of the latest activity where the test can
    time the close from (``FAKE_CLAUDE_MARKER_FILE``)."""
    path = os.environ.get("FAKE_CLAUDE_MARKER_FILE")
    if path:
        with open(path, "w") as fh:
            fh.write(repr(time.time()))


def _launch_agent() -> None:
    init()
    tool_use("Agent", "toolu_ag", {"description": "build", "prompt": "go"})
    start_bg("a1", "toolu_ag", task_type="local_agent")
    tool_result("toolu_ag", "Async agent launched successfully.")
    result("agent started", turns=2)


def _task_progress(task_id: str, n: int, tool_use_id: str = "toolu_ag") -> None:
    emit(
        {
            "type": "system",
            "subtype": "task_progress",
            "task_id": task_id,
            "tool_use_id": tool_use_id,
            "description": f"Running step {n}",
            "subagent_type": "general-purpose",
            "usage": {"total_tokens": 1000 + 100 * n, "tool_uses": n, "duration_ms": n},
            "last_tool_name": "Read",
        }
    )


def _wait_or_shutdown(seconds: float) -> None:
    if wait_idle_or_eof(seconds) is None:
        shutdown()


def _silent_until_eof() -> None:
    while True:
        _wait_or_shutdown(3600)


def scenario_bg_agent_progressing(first: dict) -> None:
    """#829: a background agent emitting ``task_progress`` (rising usage,
    one frame per tool call, P0 G1) every ``FAKE_CLAUDE_PROGRESS_S`` for
    ``FAKE_CLAUDE_PROGRESS_FOR_S``; then it finishes and wakes the parent
    (``FAKE_CLAUDE_PROGRESS_THEN=finish``, default) or goes silent."""
    _launch_agent()
    deadline = time.monotonic() + PROGRESS_FOR_S
    n = 0
    while time.monotonic() < deadline:
        n += 1
        _task_progress("a1", n)
        _mark_progress()
        _wait_or_shutdown(PROGRESS_S)
    if os.environ.get("FAKE_CLAUDE_PROGRESS_THEN", "finish") != "finish":
        _silent_until_eof()
    end_bg("a1")
    init()
    text("GOT: AGENT-DONE")
    result("GOT: AGENT-DONE")
    serve_followups()


def scenario_bg_agent_silent(first: dict) -> None:
    """#829: a background agent that never reports progress."""
    _launch_agent()
    _silent_until_eof()


def scenario_bg_agent_long_tool(first: dict) -> None:
    """#829 A.2: the agent enters one long foreground tool — the CLI sends
    no ``task_progress`` meanwhile (P0 G2), only the subagent-owned
    foreground task's start and ``task_notification`` (P0 G3)."""
    _launch_agent()
    emit(
        {
            "type": "assistant",
            "parent_tool_use_id": "toolu_ag",
            "message": {
                "id": "msg_sub_long",
                "role": "assistant",
                "model": "claude-haiku-fake",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_sub_long",
                        "name": "Bash",
                        "input": {"command": "make test"},
                    }
                ],
            },
        }
    )
    _task_progress("a1", 1)
    emit(
        {
            "type": "system",
            "subtype": "task_started",
            "task_id": "bfg1",
            "tool_use_id": "toolu_sub_long",
            "description": "make test",
            "owned_by_subagent": True,
            "is_backgrounded": False,
            "task_type": "local_bash",
        }
    )
    _wait_or_shutdown(TOOL_S)
    emit(
        {
            "type": "system",
            "subtype": "task_notification",
            "task_id": "bfg1",
            "tool_use_id": "toolu_sub_long",
            "status": "completed",
            "output_file": "",
            "summary": "make test",
        }
    )
    _mark_progress()
    _silent_until_eof()


def scenario_nonholding_progress(first: dict) -> None:
    """#829 negative: only a subagent-owned *foreground* task (no known
    owner) shows progress while a silent background Bash holds the session
    — that progress must not re-arm the hold."""
    init()
    tool_use("Bash", "toolu_bg", {"command": "sleep 600", "run_in_background": True})
    start_bg("b1", "toolu_bg")
    tool_result("toolu_bg", "Command running in background with ID: b1.")
    result("waiting", turns=2)
    emit(
        {
            "type": "system",
            "subtype": "task_started",
            "task_id": "bfg9",
            "tool_use_id": "toolu_unknown",
            "description": "fg",
            "owned_by_subagent": True,
            "is_backgrounded": False,
            "task_type": "local_bash",
        }
    )
    n = 0
    while True:
        n += 1
        _task_progress("bfg9", n, tool_use_id="toolu_unknown")
        _wait_or_shutdown(PROGRESS_S)


def scenario_bg_bash_printing(first: dict) -> None:
    """#829 fallback: a background Bash whose output file (named in its
    tool_result, P0 G4) grows every ``FAKE_CLAUDE_PROGRESS_S`` for
    ``FAKE_CLAUDE_PROGRESS_FOR_S`` — or never (``FAKE_CLAUDE_PROGRESS_FOR_S=0``,
    a silent ``sleep``)."""
    path = os.environ["FAKE_CLAUDE_OUTPUT_FILE"]
    with open(path, "w"):
        pass
    init()
    tool_use("Bash", "toolu_bg", {"command": "ticker", "run_in_background": True})
    start_bg("b1", "toolu_bg")
    tool_result(
        "toolu_bg",
        "Command running in background with ID: b1. Output is being written to: "
        f"{path}. You will be notified when it completes.",
    )
    result("waiting", turns=2)
    deadline = time.monotonic() + PROGRESS_FOR_S
    n = 0
    while time.monotonic() < deadline:
        n += 1
        with open(path, "a") as fh:
            fh.write(f"tick {n}\n")
        _mark_progress()
        _wait_or_shutdown(PROGRESS_S)
    _silent_until_eof()


# ── #383: plan approval and the plan re-arm ─────────────────────────────────


def ask_exit_plan_mode(req_id: str, *, timeout: float = 10.0) -> bool:
    """ExitPlanMode round trip: tool_use, can_use_tool control_request, wait
    for the host's answer. An allow moves the mode to ``default`` (a session
    started in plan has no prePlanMode) and emits the status frame BEFORE the
    tool_result, as probed on CLI 2.1.285."""
    global _mode
    tool_id = f"toolu_{req_id}"
    tool_use("ExitPlanMode", tool_id, {"plan": "# Plan\n1. do it"})
    emit(
        {
            "type": "control_request",
            "request_id": req_id,
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "ExitPlanMode",
                "input": {"plan": "# Plan\n1. do it"},
                "tool_use_id": tool_id,
            },
        }
    )
    log_stdin(f"can_use_tool:{req_id}")
    deadline = time.monotonic() + timeout
    while True:
        try:
            resp = _responses.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            tool_result(tool_id, "timed out")
            return False
        inner = resp.get("response") or {}
        if inner.get("request_id") != req_id:
            continue
        allowed = (inner.get("response") or {}).get("behavior") == "allow"
        break
    if allowed:
        _mode = "default"
        status_frame()
        tool_result(tool_id, "User has approved your plan. You can now start coding.")
    else:
        tool_result(tool_id, "User denied")
    return allowed


def mode_turn(*, reply: str | None = None, sample: str | None = None) -> None:
    """One turn answering ``MODE: <mode>`` — the mode the turn STARTED in."""
    mode = sample if sample is not None else _mode
    log_stdin(f"turn_start:{mode}")
    init()
    text(reply or f"MODE: {mode}")
    result(reply or f"MODE: {mode}")


def serve_mode_followups() -> None:
    """Like serve_followups, but each follow-up answers ``MODE: <mode>``."""
    while _deferred:
        _lines.put(_deferred.pop(0))
    while True:
        obj = next_user(None)
        if obj is None or obj == "timeout":
            break
        cmd = obj.get("uuid")
        lifecycle(cmd, "queued")
        lifecycle(cmd, "started")
        mode_turn()
    shutdown()


def _plan_first_turn() -> bool:
    init()
    allowed = ask_exit_plan_mode("req-epm-1")
    text("PLANNED")
    return allowed


def scenario_plan_approve_followup(first: dict) -> None:
    _plan_first_turn()
    result("PLANNED", turns=2)
    serve_mode_followups()


def scenario_plan_approve_error_turn(first: dict) -> None:
    """#383: a live follow-up turn approves a plan and then ends in an error
    — it left plan mode all the same."""
    init()
    text("FIRST")
    result("FIRST")
    obj = next_user(None)
    if not isinstance(obj, dict):
        shutdown()
    cmd = obj.get("uuid")
    lifecycle(cmd, "queued")
    lifecycle(cmd, "started")
    init()
    ask_exit_plan_mode("req-epm-err")
    emit(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "duration_ms": 1000,
            "duration_api_ms": 900,
            "num_turns": 2,
            "result": "",
            "total_cost_usd": 0.02,
        }
    )
    serve_mode_followups()


def scenario_plan_approve_bg_bash_wake(first: dict) -> None:
    _plan_first_turn()
    tool_use("Bash", "toolu_bg", {"command": "sleep 20", "run_in_background": True})
    start_bg("b1", "toolu_bg")
    tool_result("toolu_bg", "Command running in background with ID: b1.")
    result("PLANNED", turns=3)
    if wait_idle_or_eof(WAKE_S) is None:
        shutdown()
    end_bg("b1")
    mode_turn()
    serve_mode_followups()


def scenario_plan_approve_queued_wake(first: dict) -> None:
    """#383 wake-turn race: the bg task's notification is already queued when
    the result is emitted, so the CLI starts the wake turn by itself after
    WAKE_AFTER_RESULT_S without waiting for any stdin line. The stdin reader
    keeps applying set_permission_mode meanwhile — the turn samples the mode
    only when it starts, so it answers ``MODE: plan`` only if the host's
    re-arm beat it."""
    _plan_first_turn()
    tool_use("Bash", "toolu_bg", {"command": "true", "run_in_background": True})
    start_bg("b1", "toolu_bg")
    tool_result("toolu_bg", "Command running in background with ID: b1.")
    result("PLANNED", turns=3)
    time.sleep(WAKE_AFTER_RESULT_S)
    mode = _mode
    end_bg("b1")
    mode_turn(sample=mode)
    serve_mode_followups()


def scenario_plan_approve_monitor_ticks(first: dict) -> None:
    """#383: Monitor ticks after an approval. An acting tick that starts in
    plan mode calls ExitPlanMode first (and waits for the host)."""
    _plan_first_turn()
    tool_use("Monitor", "toolu_mon", {"command": "tick", "timeout_ms": 30000})
    start_bg("m1", "toolu_mon")
    tool_result("toolu_mon", "Monitor started (task m1).")
    result("PLANNED", turns=3)
    for tick in (1, 2, 3):
        if wait_idle_or_eof(WAKE_S) is None:
            shutdown()
        started_in = _mode
        log_stdin(f"turn_start:{started_in}")
        init()
        if started_in == "plan":
            ask_exit_plan_mode(f"req-tick-{tick}")
        if tick == 3:
            end_bg("m1")
        text(f"TICK {tick} MODE: {started_in}")
        result(f"TICK {tick} MODE: {started_in}")
    serve_mode_followups()


def _launch_bg_agent(task_id: str) -> None:
    tool_id = f"toolu_{task_id}"
    tool_use("Agent", tool_id, {"description": f"work {task_id}", "prompt": "go"})
    start_bg(task_id, tool_id, task_type="local_agent")
    tool_result(tool_id, "Async agent launched successfully.")


def _answer_while_agents_run(seconds: float, *, launch: str | None = None) -> None:
    """#383 C4: the approved plan's agents work for ``seconds``; each user
    line meanwhile runs at once as its own turn answering ``MODE: <mode>``
    (the real CLI does not hold a follow-up behind a background agent). The
    first such turn launches agent ``launch`` when given."""
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        obj = next_user(remaining)
        if obj is None:
            shutdown()
        if obj == "timeout":
            return
        cmd = obj.get("uuid")
        lifecycle(cmd, "queued")
        lifecycle(cmd, "started")
        mode = _mode
        log_stdin(f"turn_start:{mode}")
        init()
        if launch is not None:
            _launch_bg_agent(launch)
            launch = None
        text(f"MODE: {mode}")
        result(f"MODE: {mode}")


def _agent_finishes(task_id: str) -> None:
    """The agent ends (recording the mode it ran its last step in) and the
    CLI starts its wake turn by itself after WAKE_AFTER_RESULT_S, reading no
    stdin line first — so the turn answers ``MODE: plan`` only if the host
    re-armed on the task's end frames."""
    log_stdin(f"agent_end:{task_id}:{_mode}")
    end_bg(task_id)
    time.sleep(WAKE_AFTER_RESULT_S)
    mode_turn(sample=_mode)


def scenario_plan_approve_agent_wake(first: dict) -> None:
    """#383 C4: the approved turn launches a background agent. Follow-ups
    while it works run unplanned; its wake turn and later follow-ups are
    planned again."""
    _plan_first_turn()
    _launch_bg_agent("a1")
    result("PLANNED", turns=3)
    _answer_while_agents_run(WAKE_S)
    _agent_finishes("a1")
    serve_mode_followups()


def scenario_plan_approve_agent_chain(first: dict) -> None:
    """#383 C4: a deferred (unplanned) follow-up launches a second agent; it
    never extends the deferral — the first agent's end re-arms plan while
    the second still runs."""
    _plan_first_turn()
    _launch_bg_agent("a1")
    result("PLANNED", turns=3)
    _answer_while_agents_run(WAKE_S, launch="a2")
    _agent_finishes("a1")
    if wait_idle_or_eof(WAKE_S) is None:
        shutdown()
    _agent_finishes("a2")
    serve_mode_followups()


def scenario_plan_approve_agent_before(first: dict) -> None:
    """#383 C4: an agent launched in an earlier (planning) turn doesn't hold
    the re-arm back — it ran under plan mode anyway."""
    init()
    _launch_bg_agent("a0")
    text("PLANNING")
    result("PLANNING", turns=2)
    obj = next_user(None)
    if not isinstance(obj, dict):
        shutdown()
    cmd = obj.get("uuid")
    lifecycle(cmd, "queued")
    lifecycle(cmd, "started")
    init()
    ask_exit_plan_mode("req-epm-2")
    text("PLANNED")
    result("PLANNED", turns=2)
    _answer_while_agents_run(WAKE_S)
    _agent_finishes("a0")
    serve_mode_followups()


# ── #684: a control request the CLI withdraws / never gets answered ─────────

# How long ``control_cancel`` waits for a host answer before withdrawing.
CANCEL_AFTER_S = float(os.environ.get("FAKE_CLAUDE_CANCEL_AFTER_S", "0.3"))
# ``control_unanswered``: emit a result this long after the request (a
# background agent's request pending across the turn's end); unset = never.
UNANSWERED_RESULT_S = os.environ.get("FAKE_CLAUDE_UNANSWERED_RESULT_S")
# ``control_cancel``: pause between the cancel frame and the tool_result, so
# the host re-renders its progress message in between.
AFTER_CANCEL_S = float(os.environ.get("FAKE_CLAUDE_AFTER_CANCEL_S", "0"))


def _raise_can_use_tool(req_id: str, tool_id: str) -> None:
    tool_use("Bash", tool_id, {"command": "touch x"})
    emit(
        {
            "type": "control_request",
            "request_id": req_id,
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "input": {"command": "touch x"},
                "tool_use_id": tool_id,
            },
        }
    )
    log_stdin(f"can_use_tool:{req_id}")


def _cancel_turn(req_id: str = "req-cancel-1") -> None:
    """A Bash permission request the CLI withdraws (interrupt / turn abort):
    ``control_cancel_request`` then the synthetic rejection tool_result, as
    probed on CLI 2.1.285 (findings 2026-09-30 Z4). Any host answer is
    recorded in FAKE_CLAUDE_STDIN_LOG (``control_response``) and ignored."""
    tool_id = "toolu_c"
    _raise_can_use_tool(req_id, tool_id)
    with contextlib.suppress(queue.Empty):
        _responses.get(timeout=CANCEL_AFTER_S)
        log_stdin("answered_before_cancel")
    emit({"type": "control_cancel_request", "request_id": req_id})
    log_stdin(f"cancel_sent:{req_id}")
    if AFTER_CANCEL_S > 0:
        time.sleep(AFTER_CANCEL_S)
    tool_result(
        tool_id,
        "The user doesn't want to proceed with this tool use. The tool use was "
        "rejected (eg. if it was a file edit, the new_string was NOT written to "
        "the file). STOP what you are doing and wait for the user to tell you "
        "how to proceed.",
    )
    text("Stopped.")
    result("Stopped.")


def scenario_control_cancel(first: dict) -> None:
    init()
    _cancel_turn()
    serve_followups()


def scenario_control_cancel_followup(first: dict) -> None:
    """Turn 1 answers; the injected follow-up's turn raises and withdraws the
    request (a follow-up turn is translated by the run's reader tasks)."""
    init()
    text("ready")
    result("ready")
    obj = next_user(None)
    if isinstance(obj, dict):
        cmd = obj.get("uuid")
        lifecycle(cmd, "queued")
        lifecycle(cmd, "started")
        init()
        _cancel_turn()
    serve_followups()


def scenario_control_unanswered(first: dict) -> None:
    """A Bash permission request nobody answers: never cancelled. With
    FAKE_CLAUDE_UNANSWERED_RESULT_S the turn still ends (the request stays
    pending across the result); otherwise the CLI waits until stdin EOF."""
    init()
    _raise_can_use_tool("req-unanswered-1", "toolu_u")
    if UNANSWERED_RESULT_S is not None:
        time.sleep(float(UNANSWERED_RESULT_S))
        text("waiting on approval")
        result("waiting on approval")
    while True:
        obj = next_user(None)
        if obj is None or obj == "timeout":
            break


_SCENARIOS = {
    "plan_approve_agent_wake": scenario_plan_approve_agent_wake,
    "plan_approve_agent_chain": scenario_plan_approve_agent_chain,
    "plan_approve_agent_before": scenario_plan_approve_agent_before,
    "control_cancel": scenario_control_cancel,
    "control_cancel_followup": scenario_control_cancel_followup,
    "control_unanswered": scenario_control_unanswered,
    "bg_agent_progressing": scenario_bg_agent_progressing,
    "bg_agent_silent": scenario_bg_agent_silent,
    "bg_agent_long_tool": scenario_bg_agent_long_tool,
    "nonholding_progress": scenario_nonholding_progress,
    "bg_bash_printing": scenario_bg_bash_printing,
    "plan_approve_followup": scenario_plan_approve_followup,
    "plan_deny_followup": scenario_plan_approve_followup,  # the host denies
    "plan_approve_error_turn": scenario_plan_approve_error_turn,
    "plan_approve_bg_bash_wake": scenario_plan_approve_bg_bash_wake,
    "plan_approve_queued_wake": scenario_plan_approve_queued_wake,
    "plan_approve_monitor_ticks": scenario_plan_approve_monitor_ticks,
    "async_rewake_idle": scenario_async_rewake_idle,
    "bg_agent_pretooluse_denial": scenario_bg_agent_pretooluse_denial,
    "bg_agent_async_rewake_idle": scenario_bg_agent_async_rewake_idle,
    "agent_wake_no_query_tail": scenario_agent_wake_no_query_tail,
    "async_hook_success": scenario_async_hook_success,
    "async_hook_post_result_response": scenario_async_hook_post_result_response,
    "async_hook_live_mix_rewake": scenario_async_hook_live_mix_rewake,
    "async_hook_live_mix_running": scenario_async_hook_live_mix_running,
    "async_hook_exec_rewake": scenario_async_hook_exec_rewake,
    "async_hook_with_services": scenario_async_hook_with_services,
    "async_hook_ups_rewake": scenario_async_hook_ups_rewake,
    "async_hook_service_named_rewake": scenario_async_hook_service_named_rewake,
    "async_hook_no_response": scenario_async_hook_no_response,
    "plain_async_cancelled_on_eof": scenario_plain_async_cancelled_on_eof,
    "hook_flood": scenario_hook_flood,
    "steer_mid_tool": scenario_steer_mid_tool,
    "steer_post_last_tool": scenario_steer_post_last_tool,
    "error_first": scenario_error_first,
    "error_first_agent_wake": scenario_error_first_agent_wake,
    "safeguard_refusal_retry": scenario_safeguard_refusal_retry,
    "ignore_eof": scenario_ignore_eof,
    "ignore_eof_with_task": scenario_ignore_eof_with_task,
    "bg_bash_wake": scenario_bg_bash_wake,
    "bg_agent_wake": scenario_bg_agent_wake,
    "agent_wake_unknown_first": scenario_agent_wake_unknown_first,
    "agent_resumed": scenario_agent_resumed,
    "agent_orphans_bg_task": scenario_agent_orphans_bg_task,
    "monitor_ticks": scenario_monitor_ticks,
    "scheduled_wakeup": scenario_scheduled_wakeup,
    "wake_chain": scenario_wake_chain,  # #925
    "native_cron_fire": scenario_native_cron_fire,  # #925
    "followup": scenario_followup,
    "compact_followup": scenario_compact_followup,
    "auto_compact_mid_turn": scenario_auto_compact_mid_turn,
    "context_usage_growth": scenario_context_usage_growth,
    "followup_launches_bg": scenario_followup_launches_bg,
    "followup_blocks": scenario_followup_blocks,
    "multi_agent_acks": scenario_multi_agent_acks,
    "two_tasks_one_wake_turn": scenario_two_tasks_one_wake_turn,
    "fg_task_backgrounded": scenario_fg_task_backgrounded,
    "five_agent_interleaved": scenario_five_agent_interleaved,
    "quiet_batch_report": scenario_quiet_batch_report,
    "acks_only_batch": scenario_acks_only_batch,
    "report_then_noop": scenario_report_then_noop,
    "resume_after_killed_task": scenario_resume_after_killed_task,
    "inherited_fd_after_exit": scenario_inherited_fd_after_exit,
    "exit_after_result": scenario_exit_after_result,
}


def main() -> None:
    scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "followup")
    fn = _SCENARIOS.get(scenario)
    if fn is None:
        print(f"unknown scenario {scenario!r}", file=sys.stderr)
        sys.exit(2)
    threading.Thread(target=_reader, daemon=True).start()
    first = next_user(30.0)
    if not isinstance(first, dict):
        sys.exit(0)
    fn(first)
    shutdown()


if __name__ == "__main__":
    main()
