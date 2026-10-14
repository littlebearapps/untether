"""Antigravity background rows from the stream only (#978, phase 10).

Pure translate tests over real agy captures: ``background_held`` and
``subagent`` (1.3.1) and the four 1.3.3 probes ``subagents_two`` (P-B1),
``schedule_timer`` (P-B2), ``daemon_task`` (P-B4) and ``foreground_sleeps``
(P-B6). Nothing here reads agy's transcript files.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import structlog.testing

from untether.background_status import live_shown, render_background_block
from untether.background_tasks import EngineBackgroundTask
from untether.markdown import HARD_BREAK, STOPPED_STATUS, format_action_line
from untether.model import ActionEvent, CompletedEvent
from untether.runners.antigravity import AntigravityRunner, AntigravityStreamState
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.schemas import antigravity as schema

FIXTURES = Path(__file__).parent / "fixtures" / "antigravity"
CID = "00000009-0000-4000-8000-000000000001"


def _lines(name: str) -> list[str]:
    return [
        line
        for line in (FIXTURES / f"{name}.jsonl").read_text().splitlines()
        if line.strip()
    ]


def _state(mode: str = "full") -> tuple[AntigravityRunner, AntigravityStreamState]:
    runner = AntigravityRunner(antigravity_cmd="agy")
    with apply_run_options(EngineRunOptions(permission_mode=mode)):
        state = runner.new_state("p", None)
    return runner, state


def _feed(
    runner: AntigravityRunner, state: AntigravityStreamState, lines: list[str]
) -> list[Any]:
    out: list[Any] = []
    for line in lines:
        out.extend(
            runner.translate(
                schema.decode_event(line), state=state, resume=None, found_session=None
            )
        )
    return out


def _step(idx: int, step_type: str, st: str, **extra: Any) -> str:
    payload = {
        "conversation_id": CID,
        "step_index": idx,
        "state": st,
        "step_type": step_type,
        **extra,
    }
    return json.dumps({"event": "step_update", "step_update": payload})


def _tool(idx: int, name: str, st: str, params: dict[str, Any], **extra: Any) -> str:
    info = {"name": name, "parameters": params, **extra}
    return _step(idx, "tool", st, tool_name=name, tool_info=info)


def _spawn(idx: int, *roles: str) -> str:
    return _tool(
        idx,
        "invoke_subagent",
        "ACTIVE",
        {"Subagents": [{"Role": r, "TypeName": "research"} for r in roles]},
    )


def _rows(state: AntigravityStreamState) -> list[str]:
    block = render_background_block(state.tasks.values(), now=time.monotonic())
    return block.split(HARD_BREAK) if block else []


def _statuses(state: AntigravityStreamState) -> dict[str, str]:
    return {tid: task.status for tid, task in state.tasks.items()}


# ── run_command ─────────────────────────────────────────────────────────────


def test_run_command_promoted_after_async_threshold() -> None:
    """P-A1: agy prints nothing while it holds its answer, so the row has to
    appear without a stream line — the task is registered at ACTIVE and
    counts as background once the wait has passed."""
    lines = _lines("background_held")
    runner, state = _state()
    _feed(runner, state, lines[:4])  # … run_command ACTIVE
    task = state.tasks["cmd:2"]
    assert task.task_type == "local_bash"
    assert task.description == "sleep 20; echo done > bg.txt"
    assert task.status == "running"
    assert task.is_backgrounded is False and _rows(state) == []
    # 5 s later, with no new line from agy:
    task.started_at -= 6.0
    task.background_from -= 6.0
    assert task.is_backgrounded is True
    assert _rows(state) == [
        "⏳ background (1)",
        "🐚 sleep 20; echo done \\> bg.txt · 6s",
    ]
    _feed(runner, state, lines[4:5])  # DONE
    assert task.status == "completed" and task.ended_at is not None
    assert _rows(state) == []
    events = _feed(runner, state, lines[5:])
    assert isinstance(events[-1], CompletedEvent) and events[-1].ok is True
    assert _statuses(state) == {"cmd:2": "completed"}


def test_short_command_not_promoted() -> None:
    """P-B6: two blocking foreground commands leave no task behind."""
    runner, state = _state()
    events = _feed(runner, state, _lines("foreground_sleeps"))
    assert isinstance(events[-1], CompletedEvent)
    assert state.tasks == {}


def test_foreground_command_past_the_default_wait_reads_as_background() -> None:
    """P-B6, the known limit: agy 1.3.3 chose ``WaitMsBeforeAsync`` 10000 for
    ``sleep 9`` but the stream doesn't carry it, so the 5 s default labels a
    blocking command background for its last seconds (display only)."""
    lines = _lines("foreground_sleeps")
    runner, state = _state()
    _feed(runner, state, lines[:7])  # … second run_command ACTIVE
    task = state.tasks["cmd:4"]
    task.background_from -= 6.0
    assert _rows(state)[0] == "⏳ background (1)"
    _feed(runner, state, lines[7:8])
    assert task.status == "completed"


def test_per_call_wait_from_the_stream_is_used() -> None:
    """Where a step's parameters carry agy's own ``WaitMsBeforeAsync`` (or
    ``IsDaemon``), that decides — not the 5 s default."""
    runner, state = _state()
    _feed(
        runner,
        state,
        [
            _tool(
                2,
                "run_command",
                "ACTIVE",
                {"CommandLine": "sleep 9", "WaitMsBeforeAsync": 10000},
            )
        ],
    )
    slow = state.tasks["cmd:2"]
    assert slow.background_from is not None
    assert round(slow.background_from - slow.started_at, 3) == 10.0
    slow.background_from -= 6.0  # 6 s in: still inside agy's own wait
    assert slow.is_backgrounded is False
    _feed(runner, state, [_tool(2, "run_command", "DONE", {}, output="ok")])
    assert "cmd:2" not in state.tasks

    for idx, params in (
        (3, {"CommandLine": "sleep 60", "WaitMsBeforeAsync": 0}),
        (4, {"CommandLine": "serve", "IsDaemon": True}),
    ):
        _feed(runner, state, [_tool(idx, "run_command", "ACTIVE", params)])
        assert state.tasks[f"cmd:{idx}"].is_backgrounded is True

    for bad in (-1, True, "5000", 10**9, None):
        _feed(
            runner,
            state,
            [
                _tool(
                    9,
                    "run_command",
                    "ACTIVE",
                    {"CommandLine": "x", "WaitMsBeforeAsync": bad},
                )
            ],
        )
        task = state.tasks.pop("cmd:9")
        state.bg_steps.pop(9)
        state.pending_actions.pop("step-9")
        assert round(task.background_from - task.started_at, 3) == 5.0, bad


def test_daemon_task_promoted_by_a_later_step_and_not_failed_at_result() -> None:
    """P-B4: a daemon ``run_command`` never reaches DONE and the result isn't
    held. A later step arriving while it is still ACTIVE is agy's own proof
    it went async (tool calls run one at a time, P-H15)."""
    lines = _lines("daemon_task")
    runner, state = _state()
    _feed(runner, state, lines[:4])  # … run_command ACTIVE
    task = state.tasks["cmd:2"]
    assert task.is_backgrounded is False
    _feed(runner, state, lines[4:5])  # agent_response step 3, command still ACTIVE
    assert task.is_backgrounded is True
    assert _rows(state) == ["⏳ background (1)", "🐚 sleep 241 · 0s"]
    with structlog.testing.capture_logs() as logs:
        events = _feed(runner, state, lines[5:])
    done = events[-1]
    assert isinstance(done, CompletedEvent) and done.ok is True
    closed = [e for e in events if isinstance(e, ActionEvent)]
    assert [(e.action.id, e.phase, e.ok) for e in closed] == [
        ("step-2", "completed", True)
    ]
    # Ended, not failed and not live: no row, and no post-result status
    # message (the bridge opens one only for live tasks).
    assert task.status == "ended" and task.ended_at is not None
    assert live_shown(state.tasks.values()) == []
    outlived = [e for e in logs if e["event"] == "antigravity.background.outlived"]
    assert len(outlived) == 1 and outlived[0]["tasks"] == ["cmd:2"]
    assert outlived[0]["log_level"] == "info"


# ── subagents ───────────────────────────────────────────────────────────────


def test_two_subagents_rows_and_fifo_completion() -> None:
    """P-B1 (1.3.3): the spawn is a ``tool`` step while ACTIVE and a
    ``subagent`` step at DONE; each child finishing is one content-less
    parent ``system_message``, so completion is first in, first out."""
    lines = _lines("subagents_two")
    runner, state = _state("workspace")
    with apply_run_options(EngineRunOptions(permission_mode="workspace")):
        events = _feed(runner, state, lines[:4])  # … invoke_subagent ACTIVE
        started = [e for e in events if isinstance(e, ActionEvent)][-1]
        assert started.phase == "started"
        assert started.action.kind == "subagent"
        assert started.action.title == "research: Alpha +1"
        assert _rows(state) == [
            "⏳ background (2)",
            "🤖 Alpha \\(research\\) · 0s",
            "🤖 Beta \\(research\\) · 0s",
        ]
        assert state.tasks["sub:2:0"].ref is None

        events = _feed(runner, state, lines[4:5])  # DONE as step_type subagent
        closed = [e for e in events if isinstance(e, ActionEvent)]
        assert [(e.action.id, e.phase, e.ok) for e in closed] == [
            ("step-2", "completed", True)
        ]
        assert closed[0].action.title == "research: Alpha +1"
        # The spawn returned; the children are still working.
        assert _statuses(state) == {"sub:2:0": "running", "sub:2:1": "running"}
        assert state.tasks["sub:2:0"].ref == "00000001-0000-4000-8000-000000000002"
        assert state.tasks["sub:2:1"].ref == "00000001-0000-4000-8000-000000000003"

        _feed(runner, state, lines[5:7])  # agent_response, system_message
        assert _statuses(state) == {"sub:2:0": "completed", "sub:2:1": "running"}
        assert _rows(state) == ["⏳ background (1)", "🤖 Beta \\(research\\) · 0s"]
        _feed(runner, state, lines[7:9])  # agent_response, system_message
        assert _statuses(state) == {"sub:2:0": "completed", "sub:2:1": "completed"}
        assert _rows(state) == []
        events = _feed(runner, state, lines[9:])
    assert isinstance(events[-1], CompletedEvent) and events[-1].ok is True


def test_subagent_row_from_the_1_3_1_shape() -> None:
    """agy 1.3.1 sent ``step_type: subagent`` already at ACTIVE."""
    lines = _lines("subagent")
    runner, state = _state()
    _feed(runner, state, lines[:4])
    assert _rows(state) == ["⏳ background (1)", "🤖 Secret Reader \\(research\\) · 0s"]
    _feed(runner, state, lines[4:5])
    task = state.tasks["sub:2:0"]
    assert task.status == "running"
    assert task.ref == "cc320b5c-7911-449e-b45f-013887cb5cfe"
    _feed(runner, state, lines[5:-1])
    assert task.status == "completed"


def test_subagents_never_mark_the_run_busy() -> None:
    """D33: only shell / timer steps excuse a silent run; a subagent row is
    display only."""
    runner, state = _state()
    _feed(runner, state, [_spawn(2, "Alpha")])
    assert live_shown(state.tasks.values())
    assert state.awaiting_background() is False
    assert state.has_live_background_work() is False
    assert state.background_count() == 0


def test_failed_spawn_leaves_no_running_subagent() -> None:
    runner, state = _state()
    _feed(runner, state, [_spawn(2, "Alpha", "Beta")])
    events = _feed(
        runner,
        state,
        [
            _tool(
                2,
                "invoke_subagent",
                "ERROR",
                {},
                error={"type": "TOOL_ERROR", "message": "denied by hook"},
            )
        ],
    )
    assert [e.ok for e in events if isinstance(e, ActionEvent)] == [False]
    assert _statuses(state) == {"sub:2:0": "failed", "sub:2:1": "failed"}
    assert _rows(state) == []


def test_subagent_spawn_without_roles_still_gets_a_row() -> None:
    runner, state = _state()
    events = _feed(
        runner, state, [_tool(2, "invoke_subagent", "ACTIVE", {"Subagents": [{}]})]
    )
    assert events[-1].action.title == "subagent"
    assert _rows(state) == ["⏳ background (1)", "🤖 subagent · 0s"]


# ── schedule ────────────────────────────────────────────────────────────────


def test_schedule_timer_row_for_the_timers_life() -> None:
    """P-B2: a ``schedule`` step is ACTIVE for the timer's whole life and
    agy holds its answer for it — a row from the first moment."""
    lines = _lines("schedule_timer")
    runner, state = _state("workspace")
    with apply_run_options(EngineRunOptions(permission_mode="workspace")):
        _feed(runner, state, lines[:4])  # … schedule ACTIVE
        task = state.tasks["timer:2"]
        assert task.is_backgrounded is True
        assert _rows(state) == ["⏳ background (1)", "🐚 timer \\(20s\\) · 0s"]
        assert state.awaiting_background() is True  # rc1's busy probe, unchanged
        _feed(runner, state, lines[4:5])  # DONE
        assert task.status == "completed" and _rows(state) == []
        events = _feed(runner, state, lines[5:])
    assert isinstance(events[-1], CompletedEvent) and events[-1].ok is True


def test_schedule_without_a_duration_is_a_plain_timer_row() -> None:
    runner, state = _state()
    _feed(runner, state, [_tool(2, "schedule", "ACTIVE", {"Prompt": "later"})])
    assert _rows(state) == ["⏳ background (1)", "🐚 timer · 0s"]


# ── the completion notice ───────────────────────────────────────────────────


def test_system_message_after_a_timer_is_not_a_subagent_finishing() -> None:
    """agy also sends a ``system_message`` when a timer or background command
    finishes (P-B2, P-A1); that one must not close a subagent's row."""
    runner, state = _state()
    _feed(
        runner,
        state,
        [
            _spawn(2, "Alpha"),
            _step(2, "subagent", "DONE", tool_name="invoke_subagent"),
            _tool(3, "schedule", "ACTIVE", {"DurationSeconds": 20}),
            _tool(3, "schedule", "DONE", {"DurationSeconds": 20}, output="done"),
            _step(4, "agent_response", "DONE", text_delta="timer set\n"),
            _step(5, "system_message", "DONE"),  # the timer's notice
        ],
    )
    assert _statuses(state) == {"sub:2:0": "running", "timer:3": "completed"}
    _feed(runner, state, [_step(6, "system_message", "DONE")])  # Alpha's
    assert state.tasks["sub:2:0"].status == "completed"


def test_unclaimed_notice_is_dropped_when_the_next_tool_starts() -> None:
    """A foreground command mislabelled background (P-B6) gets no notice from
    agy; the one Untether expected for it must not swallow a subagent's."""
    runner, state = _state()
    _feed(runner, state, [_tool(2, "run_command", "ACTIVE", {"CommandLine": "x"})])
    state.tasks["cmd:2"].background_from -= 6.0
    _feed(runner, state, [_tool(2, "run_command", "DONE", {}, output="ok")])
    assert state.bg_notices_owed == 1
    _feed(runner, state, [_spawn(3, "Alpha")])
    assert state.bg_notices_owed == 0
    _feed(
        runner,
        state,
        [
            _step(3, "subagent", "DONE", tool_name="invoke_subagent"),
            _step(4, "system_message", "DONE"),
        ],
    )
    assert state.tasks["sub:3:0"].status == "completed"


def test_system_message_with_nothing_running_is_ignored() -> None:
    runner, state = _state()
    _feed(runner, state, [_step(0, "user_input", "DONE")])
    assert _feed(runner, state, [_step(1, "system_message", "DONE")]) == []
    assert state.tasks == {} and state.bg_notices_owed == 0


# ── #1086: a timer cut short by a message; subagents agy killed ────────────

_CHILD_A = "00001086-0000-4000-8000-000000000002"
_CHILD_B = "00001086-0000-4000-8000-000000000003"


def _spawned(idx: int, *children: tuple[str, str]) -> list[str]:
    """An ``invoke_subagent`` step ACTIVE then DONE with each child's id."""
    subs = [
        {"type_name": "research", "role": role, "conversation_id": child}
        for role, child in children
    ]
    return [
        _spawn(idx, *(role for role, _ in children)),
        _step(
            idx,
            "subagent",
            "DONE",
            tool_name="invoke_subagent",
            subagent_info={"subagents": subs},
        ),
    ]


def test_timer_cut_short_by_a_subagent_closes_that_subagent() -> None:
    """agy 1.3.5 capture (the #1086 shape): a timer the model gave
    ``TimerCondition`` "any" (not in the stream) ends as soon as a subagent's
    message arrives, and its DONE output names the sender. That message is
    the timer's wakeup — agy sends no other notice for the timer — so it
    closes the named subagent (here the second one), not the oldest."""
    lines = _lines("subagents_timer_any")
    runner, state = _state("workspace")
    with apply_run_options(EngineRunOptions(permission_mode="workspace")):
        _feed(runner, state, lines[:6])  # … schedule ACTIVE (step 3)
        assert _statuses(state) == {
            "sub:2:0": "running",
            "sub:2:1": "running",
            "timer:3": "running",
        }
        assert state.tasks["sub:2:1"].ref == _CHILD_B
        with structlog.testing.capture_logs() as logs:
            _feed(runner, state, lines[6:7])  # schedule DONE, cut short by B
        finished = [e for e in logs if e["event"] == "antigravity.background.finished"]
        assert [(e["task_id"], e["status"]) for e in finished] == [
            ("timer:3", "completed"),
            ("sub:2:1", "completed"),
        ]
        assert finished[1]["attribution"] == "sender"
        assert _statuses(state) == {
            "sub:2:0": "running",
            "sub:2:1": "completed",
            "timer:3": "completed",
        }
        _feed(runner, state, lines[7:9])  # reply, system_message (B's result)
        assert state.tasks["sub:2:0"].status == "running"
        assert state.bg_notices_owed == 0
        _feed(runner, state, lines[9:14])  # manage_subagents list, A's result
        assert state.tasks["sub:2:0"].status == "completed"
        assert _rows(state) == []
        with structlog.testing.capture_logs() as logs:
            events = _feed(runner, state, lines[14:])  # kill_all, reply, result
        # kill_all found nothing still open: both had reported.
        assert not [e for e in logs if e.get("source") == "manage_subagents"]
    assert isinstance(events[-1], CompletedEvent) and events[-1].ok is True
    assert set(_statuses(state).values()) == {"completed"}


def test_timer_cut_short_by_another_sender_closes_no_subagent() -> None:
    """A condition met by a task's message (``<cid>/task-N``) or by a child
    this run never listed: no row is the sender's, and the message still
    comes, so it must not close the oldest subagent either."""
    runner, state = _state()
    _feed(runner, state, _spawned(2, ("Alpha", _CHILD_A)))
    _feed(runner, state, [_tool(3, "schedule", "ACTIVE", {"DurationSeconds": 60})])
    for sender in (f"{CID}/task-9", "00001086-0000-4000-8000-0000000000ff"):
        output = (
            "Timer cancelled early: its early-termination condition (any) was "
            f"met by a message from {sender}."
        )
        state.tasks["timer:3"].status = "running"
        _feed(
            runner,
            state,
            [_tool(3, "schedule", "DONE", {"DurationSeconds": 60}, output=output)],
        )
        assert state.tasks["timer:3"].status == "completed"
        assert state.tasks["sub:2:0"].status == "running"
        assert state.bg_notices_owed == 1
        _feed(runner, state, [_step(4, "system_message", "DONE")])
        assert state.tasks["sub:2:0"].status == "running", sender
        assert state.bg_notices_owed == 0


def test_manage_subagents_kill_all_stops_the_open_subagent_rows() -> None:
    """agy's ``manage_subagents`` ``kill_all`` ends every subagent still at
    work: their rows end stopped (as a ``manage_task`` kill does, #1062); one
    that already reported keeps ``completed``. agy sends no notice for it."""
    runner, state = _state()
    _feed(runner, state, _spawned(2, ("Alpha", _CHILD_A), ("Beta", _CHILD_B)))
    _feed(runner, state, [_step(3, "system_message", "DONE")])  # Alpha's
    assert _statuses(state) == {"sub:2:0": "completed", "sub:2:1": "running"}
    kill_all = {"Action": "kill_all"}
    with structlog.testing.capture_logs() as logs:
        _feed(runner, state, [_tool(4, "manage_subagents", "ACTIVE", kill_all)])
    finished = [e for e in logs if e["event"] == "antigravity.background.finished"]
    assert [(e["task_id"], e["status"], e["source"]) for e in finished] == [
        ("sub:2:1", "stopped", "manage_subagents")
    ]
    assert _statuses(state) == {"sub:2:0": "completed", "sub:2:1": "stopped"}
    assert state.tasks["sub:2:1"].ended_at is not None
    assert _rows(state) == [] and state.bg_notices_owed == 0
    output = "Successfully killed 2 subagent(s) and their descendants."
    _feed(
        runner,
        state,
        [_tool(4, "manage_subagents", "DONE", kill_all, output=output)],
    )
    assert _statuses(state) == {"sub:2:0": "completed", "sub:2:1": "stopped"}


def test_manage_subagents_kill_stops_only_the_named_subagents() -> None:
    runner, state = _state()
    _feed(runner, state, _spawned(2, ("Alpha", _CHILD_A), ("Beta", _CHILD_B)))
    _feed(
        runner,
        state,
        [_tool(3, "manage_subagents", "ACTIVE", {"Action": "list"})],
    )
    assert _statuses(state) == {"sub:2:0": "running", "sub:2:1": "running"}
    kill = {"Action": "kill", "ConversationIds": [_CHILD_B]}
    _feed(runner, state, [_tool(4, "manage_subagents", "ACTIVE", kill)])
    assert _statuses(state) == {"sub:2:0": "running", "sub:2:1": "stopped"}
    # A kill with no usable ids stops nothing.
    _feed(
        runner,
        state,
        [_tool(5, "manage_subagents", "ACTIVE", {"Action": "kill"})],
    )
    assert _statuses(state) == {"sub:2:0": "running", "sub:2:1": "stopped"}


# ── end of run ──────────────────────────────────────────────────────────────


def test_every_end_of_run_path_leaves_no_live_task() -> None:
    for end in ("result", "stream_end", "process_error"):
        runner, state = _state()
        _feed(
            runner,
            state,
            [
                _step(1, "agent_response", "DONE"),
                _spawn(2, "Alpha"),
                _tool(3, "schedule", "ACTIVE", {"DurationSeconds": 600}),
            ],
        )
        assert len(live_shown(state.tasks.values())) == 2, end
        if end == "result":
            result = {"conversation_id": CID, "status": "ERROR", "error": "boom"}
            _feed(runner, state, [json.dumps({"event": "result", "result": result})])
        elif end == "stream_end":
            runner.stream_end_events(resume=None, found_session=None, state=state)
        else:
            runner.process_error_events(1, resume=None, found_session=None, state=state)
        assert live_shown(state.tasks.values()) == [], end
        assert set(_statuses(state).values()) == {"ended"}, end
        assert state.bg_steps == {}, end


def test_finished_tasks_are_pruned_so_the_map_stays_bounded() -> None:
    runner, state = _state()
    for idx in range(2, 202):
        _feed(runner, state, [_tool(idx, "schedule", "ACTIVE", {})])
        _feed(runner, state, [_tool(idx, "schedule", "DONE", {}, output="ok")])
    assert len(state.tasks) <= 64
    _feed(runner, state, [_tool(900, "schedule", "ACTIVE", {})])
    assert state.tasks["timer:900"].status == "running"


def test_task_map_is_what_the_bridge_duck_types() -> None:
    """``runner_bridge._bg_tasks`` reads ``engine_state.tasks`` as a dict;
    ``running_task_is_idle_after_result`` reads ``holds_session``."""
    runner, state = _state()
    _feed(runner, state, [_spawn(2, "Alpha")])
    tasks = getattr(state, "tasks", None)
    assert isinstance(tasks, dict)
    assert not any(getattr(t, "holds_session", False) for t in tasks.values())
    # No Claude live-session attribute came along with it.
    for name in ("live_mode", "completed_turns", "turn_open", "result_received_at"):
        assert not hasattr(state, name), name


# ── #1062: a task agy killed itself ────────────────────────────────────────


def test_task_killed_by_manage_task_ends_stopped_not_completed() -> None:
    """agy 1.3.3 capture: ``manage_task`` kill ends the ``run_command`` step
    DONE with ``tool_info.error`` "context canceled". The task reads as
    stopped (⏹️), the log says so, and the row is not a ✓."""
    lines = _lines("task_killed")
    runner, state = _state()
    _feed(runner, state, lines[:4])  # … run_command ACTIVE (step 2)
    task = state.tasks["cmd:2"]
    task.started_at -= 6.0
    task.background_from -= 6.0
    with structlog.testing.capture_logs() as logs:
        events = _feed(runner, state, lines[4:5])  # step 2 DONE + error
    assert task.status == "stopped" and task.ended_at is not None
    finished = [e for e in logs if e["event"] == "antigravity.background.finished"]
    assert [(e["task_id"], e["status"]) for e in finished] == [("cmd:2", "stopped")]
    closed = [e for e in events if isinstance(e, ActionEvent)]
    assert [(e.action.id, e.phase, e.ok) for e in closed] == [
        ("step-2", "completed", False)
    ]
    assert closed[0].message == "context canceled"
    # rc4 QA: the command's own row read "✗ sleep 300" (failed); it is ⏹️.
    assert closed[0].action.detail.get("stopped") is True
    row = format_action_line(closed[0].action, "completed", False, command_width=None)
    assert row.startswith(STOPPED_STATUS + " ") and "✗" not in row
    # The ⏳ block lists live tasks only, as for Claude: a stopped one leaves.
    assert render_background_block(state.tasks.values(), now=time.monotonic()) is None
    # The kill owes the notice, not the cancelled step (it comes next).
    assert state.bg_notices_owed == 0
    events = _feed(runner, state, lines[5:])
    assert isinstance(events[-1], CompletedEvent) and events[-1].ok is True
    assert _statuses(state) == {"cmd:2": "stopped"}


def test_kill_before_the_step_closes_still_reads_stopped() -> None:
    """Order-independent: if the ``manage_task`` kill arrives while the
    command's step is still open, its task ends stopped at once."""
    runner, state = _state()
    _feed(
        runner,
        state,
        [
            _step(1, "agent_response", "DONE"),
            _tool(2, "run_command", "ACTIVE", {"CommandLine": "sleep 300"}),
        ],
    )
    task = state.tasks["cmd:2"]
    task.background_from = None
    kill = {"Action": "kill", "TaskId": f"{CID}/task-2"}
    _feed(runner, state, [_tool(4, "manage_task", "ACTIVE", kill)])
    assert task.status == "stopped"
    # The step's own DONE later changes nothing.
    _feed(runner, state, [_tool(2, "run_command", "DONE", {"CommandLine": "x"})])
    assert task.status == "stopped"


def test_fast_kill_still_reads_stopped_1_3_4() -> None:
    """agy 1.3.4 capture, replayed with no delay — the rc4 QA failure: the
    model picked ``WaitMsBeforeAsync`` 500 (not in the stream) and killed
    the task about 4 s in, so the step's cancelled DONE arrived before the
    default 5 s background threshold. The kill proves the command was in the
    background: it ends stopped, not dropped as a foreground step."""
    lines = _lines("task_killed_fast")
    runner, state = _state()
    _feed(runner, state, lines[:4])  # … run_command ACTIVE (step 2)
    assert state.tasks["cmd:2"].is_backgrounded is False
    # A subagent of an earlier step is still at work: the kill's notice
    # must not be read as its end.
    alpha = state.tasks["sub:1:0"] = EngineBackgroundTask(
        task_id="sub:1:0", task_type="local_agent", description="Alpha"
    )
    with structlog.testing.capture_logs() as logs:
        # step 2 DONE (cancelled), reply, kill ACTIVE + DONE, system_message
        _feed(runner, state, lines[4:9])
    finished = [e for e in logs if e["event"] == "antigravity.background.finished"]
    assert [(e["task_id"], e["status"]) for e in finished] == [("cmd:2", "stopped")]
    assert finished[0]["source"] == "manage_task"
    task = state.tasks["cmd:2"]
    assert task.status == "stopped" and task.is_backgrounded is True
    assert task.ended_at is not None
    # 1.3.4 sends the kill's ``system_message`` (step 5) at once; it is the
    # killed task's, not Alpha's.
    assert alpha.status == "running"
    assert state.bg_notices_owed == 0
    events = _feed(runner, state, lines[9:])
    assert isinstance(events[-1], CompletedEvent) and events[-1].ok is True
    assert _statuses(state) == {"cmd:2": "stopped", "sub:1:0": "ended"}


def test_cancelled_step_without_a_kill_is_never_a_row() -> None:
    """A foreground step that ends cancelled with no ``manage_task`` kill
    after it (the run itself was stopped) stays out of the list."""
    runner, state = _state()
    _feed(runner, state, [_tool(2, "run_command", "ACTIVE", {"CommandLine": "x"})])
    error = {"type": "TOOL_ERROR", "message": "context canceled"}
    _feed(runner, state, [_tool(2, "run_command", "DONE", {}, error=error)])
    assert state.tasks == {} and _rows(state) == []
    # A kill naming another task changes nothing.
    kill = {"Action": "kill", "TaskId": f"{CID}/task-7"}
    _feed(runner, state, [_tool(3, "manage_task", "ACTIVE", kill)])
    assert state.tasks == {} and state.bg_notices_owed == 0


def test_failed_step_with_a_non_cancel_error_is_failed() -> None:
    runner, state = _state()
    _feed(runner, state, [_tool(2, "run_command", "ACTIVE", {"CommandLine": "x"})])
    state.tasks["cmd:2"].background_from = None
    error = {"type": "TOOL_ERROR", "message": "exec failed"}
    events = _feed(runner, state, [_tool(2, "run_command", "DONE", {}, error=error)])
    assert state.tasks["cmd:2"].status == "failed"
    # #1062: only a cancellation reads ⏹️; a real failure keeps its ✗.
    (closed,) = [e for e in events if isinstance(e, ActionEvent)]
    assert "stopped" not in closed.action.detail
    row = format_action_line(closed.action, "completed", False, command_width=None)
    assert row.startswith("✗ ")


# ── #1080: a step waiting on an Ask me approval card ───────────────────────


def _age(state: AntigravityStreamState, key: str, seconds: float) -> None:
    task = state.tasks[key]
    task.started_at -= seconds
    if task.background_from is not None:
        task.background_from -= seconds


def test_command_waiting_on_an_approval_card_is_not_background() -> None:
    """rc4 QA: while an Ask me card waited, the progress message said
    ``⏳ background (1) 🐚 git status · 1m51s``. agy hasn't run it yet."""
    runner, state = _state("ask")
    _feed(runner, state, [_tool(2, "run_command", "ACTIVE", {"CommandLine": "x"})])
    state.note_approval_wait(2, True)
    _age(state, "cmd:2", 120.0)
    assert _rows(state) == []
    state.note_approval_wait(2, False)  # approved: the command runs from now
    assert _rows(state) == []
    task = state.tasks["cmd:2"]
    assert time.monotonic() - task.started_at < 1.0
    _age(state, "cmd:2", 6.0)  # past the 5 s guess, still running
    assert len(_rows(state)) == 2 and "x" in _rows(state)[1]


def test_card_before_the_step_line_still_hides_it() -> None:
    runner, state = _state("ask")
    state.note_approval_wait(3, True)
    _feed(runner, state, [_spawn(3, "Alpha")])
    assert _rows(state) == []
    state.note_approval_wait(3, False)
    assert len(_rows(state)) == 2  # a subagent counts from the start


# ── #1061: the held answer ──────────────────────────────────────────────────


def test_hold_notes_follow_the_listed_tasks() -> None:
    """The ``⏳ background`` block ends with the engine's note; the cancel
    note is offered only while a listed task is still running."""
    runner, state = _state()
    assert state.cancelled_hold_note() is None
    _feed(runner, state, [_tool(2, "schedule", "ACTIVE", {"DurationSeconds": 60})])
    block = render_background_block(
        state.tasks.values(), now=time.monotonic(), note=state.background_hold_note()
    )
    assert block is not None
    assert block.split(HARD_BREAK)[-1] == (
        "agy sends its answer when this work finishes \\(up to 30 min\\)"
    )
    assert state.cancelled_hold_note() == (
        "⏳ Cancelled while agy was holding its answer for background work, so"
        " it wasn't shown. The conversation kept it: send a message such as"
        ' "repeat your last answer" to see it.'
    )
    # A command still inside its foreground window isn't listed: no note.
    _feed(runner, state, [_tool(2, "schedule", "DONE", {}, output="ok")])
    _feed(runner, state, [_tool(3, "run_command", "ACTIVE", {"CommandLine": "x"})])
    assert state.cancelled_hold_note() is None


def test_background_block_without_a_note_is_unchanged() -> None:
    runner, state = _state()
    _feed(runner, state, [_tool(2, "schedule", "ACTIVE", {"DurationSeconds": 60})])
    rows = _rows(state)
    assert len(rows) == 2 and rows[-1].startswith("🐚 timer")
