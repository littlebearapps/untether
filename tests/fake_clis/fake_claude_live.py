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

_cost = 0.0
_lines: queue.Queue[dict | None] = queue.Queue()
_live_tasks: dict[str, str] = {}  # task_id -> tool_use_id


def emit(obj: dict) -> None:
    obj.setdefault("session_id", SESSION_ID)
    print(json.dumps(obj), flush=True)


def _reader() -> None:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            _lines.put(json.loads(raw))
        except json.JSONDecodeError:
            continue
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
    emit(
        {
            "type": "system",
            "subtype": "init",
            "cwd": os.getcwd(),
            "model": "claude-haiku-fake",
            "tools": ["Bash", "Agent", "Monitor", "ScheduleWakeup"],
            "permissionMode": "bypassPermissions",
        }
    )


def text(msg: str) -> None:
    emit(
        {
            "type": "assistant",
            "message": {
                "id": f"msg_{time.monotonic_ns()}",
                "role": "assistant",
                "model": "claude-haiku-fake",
                "content": [{"type": "text", "text": msg}],
            },
        }
    )


def tool_use(name: str, tool_id: str, raw_input: dict) -> None:
    emit(
        {
            "type": "assistant",
            "message": {
                "id": f"msg_{tool_id}",
                "role": "assistant",
                "model": "claude-haiku-fake",
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": name,
                        "input": raw_input,
                    }
                ],
            },
        }
    )


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
    answer: str, *, turns: int = 1, delta: float = 0.01, api_ms: int = 900
) -> None:
    global _cost
    _cost = round(_cost + delta, 6)
    emit(
        {
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
    )


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
        init()
        text(f"ECHO: {user_text(obj)}")
        result(f"ECHO: {user_text(obj)}")
    shutdown()


def shutdown() -> None:
    # Stdin closed: stop live background work, as the real CLI does (F3).
    for task_id in list(_live_tasks):
        end_bg(task_id, status="killed")
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
    tool_use("Bash", "toolu_bg", {"command": "sleep 20", "run_in_background": True})
    start_bg("b1", "toolu_bg")
    tool_result("toolu_bg", "Command running in background with ID: b1.")
    text("waiting")
    result("waiting", turns=2)
    got = wait_idle_or_eof(WAKE_S)
    if got is None:
        shutdown()
    end_bg("b1")
    init()
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


def scenario_followup(first: dict) -> None:
    init()
    text("FIRST")
    if RESULT_DELAY_S > 0:
        time.sleep(RESULT_DELAY_S)
    result("FIRST")
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
    # must not be kept live.
    init()
    emit(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "duration_ms": 500,
            "duration_api_ms": 400,
            "num_turns": 1,
            "result": "API Error: overloaded",
            "total_cost_usd": 0.001,
        }
    )
    serve_followups()


_SCENARIOS = {
    "error_first": scenario_error_first,
    "ignore_eof": scenario_ignore_eof,
    "ignore_eof_with_task": scenario_ignore_eof_with_task,
    "bg_bash_wake": scenario_bg_bash_wake,
    "bg_agent_wake": scenario_bg_agent_wake,
    "agent_wake_unknown_first": scenario_agent_wake_unknown_first,
    "agent_resumed": scenario_agent_resumed,
    "monitor_ticks": scenario_monitor_ticks,
    "scheduled_wakeup": scenario_scheduled_wakeup,
    "followup": scenario_followup,
    "followup_launches_bg": scenario_followup_launches_bg,
    "resume_after_killed_task": scenario_resume_after_killed_task,
    "inherited_fd_after_exit": scenario_inherited_fd_after_exit,
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
