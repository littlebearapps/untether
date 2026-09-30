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
        flush_withheld()  # #812: withheld async-hook responses land now
        if FOLLOWUP_DELAY_S > 0:
            time.sleep(FOLLOWUP_DELAY_S)
        else:
            init()
        text(f"ECHO: {user_text(obj)}")
        result(f"ECHO: {user_text(obj)}")
    shutdown()


def shutdown() -> None:
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


def scenario_followup(first: dict) -> None:
    init()
    text("FIRST")
    if RESULT_DELAY_S > 0:
        time.sleep(RESULT_DELAY_S)
    result("FIRST")
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
# The real CLI runs every command hook as a ``/bin/sh -c <command>`` child;
# Untether's hold reads that (#812): no shell child left → nothing can
# still rewake. So background hooks here run a real ``sh -c`` process.
_hook_procs: dict[str, subprocess.Popen] = {}
# Plain ``async: true`` hooks' responses: the CLI withholds them while the
# session is idle ("the response waits until the next user interaction")
# and flushes them at the next turn or at teardown (probed on CLI 2.1.285).
_withheld: list[tuple[str, str]] = []


def spawn_hook(hook_id: str, seconds: float) -> subprocess.Popen:
    proc = subprocess.Popen(  # the real CLI's hook shape: sh -c
        f"sleep {seconds}; true",
        shell=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
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


def kill_hooks() -> None:
    for proc in _hook_procs.values():
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
    hook_started("h-ups-2", "UserPromptSubmit")
    hook_response("h-ups-2", "UserPromptSubmit")
    init()
    text("HOOK: finding: key leak")
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
            "result": "HOOK: finding: key leak",
            "total_cost_usd": _cost,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "origin": {"kind": "task-notification", "producer": "session-task"},
        }
    )
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


# Seconds between a turn's UserPromptSubmit hooks and its Stop hooks.
TURN_S = float(os.environ.get("FAKE_CLAUDE_TURN_S", "0"))


def _mixed_hooks_turn(*, ups_s: float, stop_rewake: bool) -> None:
    """Live regression (CLI 2.1.285, @untether_dev_bot): the user-global
    plain ``async: true`` hook (``moshi-hook``) on UserPromptSubmit + Stop —
    its process exits at once and the CLI withholds its response while idle
    — next to a sync Stop hook and (``stop_rewake``) a project
    ``asyncRewake`` Stop hook still running (``sleep 120``). ``ups_s`` is
    how long the UserPromptSubmit hook's process lives (a long one is the
    still-running hook instead)."""
    fast = 0.01
    spawn_hook("h-ups", ups_s)
    hook_started("h-ups", "UserPromptSubmit")
    if ups_s == fast:
        _withheld.append(("h-ups", "UserPromptSubmit"))
    init()
    time.sleep(TURN_S)
    text("DONE")
    spawn_hook("h-stop-plain", fast)
    hook_started("h-stop-plain", "Stop")  # plain async, response withheld
    _withheld.append(("h-stop-plain", "Stop"))
    if stop_rewake:
        spawn_hook("h-stop-rewake", 600)
        hook_started("h-stop-rewake", "Stop")  # asyncRewake, still running
    hook_started("h-stop-sync", "Stop")
    hook_response("h-stop-sync", "Stop")
    result("DONE")
    while next_user(None) is not None:
        pass
    # EOF: the withheld plain responses flush; a hook still running is
    # killed with no response (§A1 P3/P5-B).
    _eof_with_pending_rewake()


def scenario_async_hook_mixed_live(first: dict) -> None:
    # Fast plain UPS + fast plain Stop; the asyncRewake Stop hook runs on.
    _mixed_hooks_turn(ups_s=0.01, stop_rewake=True)


def scenario_async_hook_old_hook_live(first: dict) -> None:
    # The OLDER hook is the running one: a long UserPromptSubmit hook started
    # ``TURN_S`` before the fast plain Stop hook.
    _mixed_hooks_turn(ups_s=600, stop_rewake=False)


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


_SCENARIOS = {
    "async_rewake_idle": scenario_async_rewake_idle,
    "async_hook_success": scenario_async_hook_success,
    "async_hook_post_result_response": scenario_async_hook_post_result_response,
    "async_hook_mixed_live": scenario_async_hook_mixed_live,
    "async_hook_old_hook_live": scenario_async_hook_old_hook_live,
    "async_hook_no_response": scenario_async_hook_no_response,
    "plain_async_cancelled_on_eof": scenario_plain_async_cancelled_on_eof,
    "hook_flood": scenario_hook_flood,
    "steer_mid_tool": scenario_steer_mid_tool,
    "steer_post_last_tool": scenario_steer_post_last_tool,
    "error_first": scenario_error_first,
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
    "followup": scenario_followup,
    "followup_launches_bg": scenario_followup_launches_bg,
    "followup_blocks": scenario_followup_blocks,
    "multi_agent_acks": scenario_multi_agent_acks,
    "five_agent_interleaved": scenario_five_agent_interleaved,
    "quiet_batch_report": scenario_quiet_batch_report,
    "acks_only_batch": scenario_acks_only_batch,
    "report_then_noop": scenario_report_then_noop,
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
