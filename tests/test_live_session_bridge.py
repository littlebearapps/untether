"""#776: bridge-side behaviour for live Claude sessions."""

from __future__ import annotations

import anyio
import pytest

from tests.telegram_fakes import FakeTransport
from tests.test_exec_bridge import (
    _FakeClock,
    _KeyboardPresenter,
    _make_edits,
    _make_engine_state,
    _make_stream,
)

pytestmark = pytest.mark.anyio


async def test_stall_monitor_no_autocancel_or_warning_while_live_idle() -> None:
    """A live session idling between turns (holding for a background task)
    emits task/command_lifecycle lines, so last_event_type is not "result",
    and its ring buffer stays frozen for minutes. Neither may trip a stall
    warning, frozen-ring escalation or the max_warnings auto-cancel."""
    transport = FakeTransport()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, _KeyboardPresenter(), clock=clock)
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05
    edits._STALL_THRESHOLD_TOOL = 0.05
    edits._STALL_THRESHOLD_APPROVAL = 10.0
    edits._stall_repeat_seconds = 0.0  # every tick may warn
    edits._STALL_MAX_WARNINGS = 2
    cancel_event = anyio.Event()
    edits.cancel_event = cancel_event
    edits.stream = _make_stream(
        last_event_type="system",
        engine_state=_make_engine_state(
            result_received_at=None,
            live_mode=True,
            completed_turns=1,
            turn_open=False,
        ),
    )

    async with anyio.create_task_group() as tg:

        async def drive() -> None:
            for step in range(1, 30):
                clock.set(100.0 + step * 700.0)  # far past the 660 s limbo mark
                await anyio.sleep(0.02)
            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(drive)

    assert not cancel_event.is_set()
    assert edits._frozen_ring_count == 0
    stall_msgs = [c for c in transport.send_calls if "min" in c["message"].text]
    assert stall_msgs == []


@pytest.mark.parametrize("run_level", [True, False])
async def test_live_idle_hold_with_children_is_silent_and_not_peak_idle(
    run_level: bool,
) -> None:
    """#787: a live session held between turns — result delivered, several
    short wake turns done, only a pending ScheduleWakeup keeping it open,
    its MCP servers showing up as 8 CPU-ticking children — must not emit a
    "⏳ Waiting for child processes … /cancel to stop" warning, must not log
    ``progress_edits.stall_detected`` or count a stall warning, and must not
    report the hold as ``peak_idle`` (it goes to ``peak_live_idle`` instead)."""
    from unittest.mock import patch

    from structlog.testing import capture_logs

    from untether.utils.proc_diag import ProcessDiag

    transport = FakeTransport()
    clock = _FakeClock(start=100.0)
    edits = _make_edits(transport, _KeyboardPresenter(), clock=clock)
    edits.run_level = run_level
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05
    edits._STALL_THRESHOLD_SUBAGENT = 0.05
    edits._STALL_THRESHOLD_TOOL = 0.05
    edits._STALL_THRESHOLD_APPROVAL = 10.0
    edits._stall_repeat_seconds = 0.0
    edits.pid = 4242
    edits.event_seq = 123
    cancel_event = anyio.Event()
    edits.cancel_event = cancel_event
    edits.stream = _make_stream(
        last_event_type="user",
        engine_state=_make_engine_state(
            live_mode=True,
            completed_turns=6,
            turn_open=False,
            live_wakeups={"wk1": 0.0},
        ),
    )
    ticks = {"n": 0}

    def busy_children(pid: int) -> ProcessDiag:
        ticks["n"] += 1
        n = ticks["n"]
        return ProcessDiag(
            pid=pid,
            alive=True,
            state="S",
            cpu_utime=1000,
            cpu_stime=200,
            child_pids=[5001 + i for i in range(8)],
            tree_cpu_utime=3000 + n * 50,
            tree_cpu_stime=600 + n * 10,
        )

    with (
        patch("untether.utils.proc_diag.collect_proc_diag", side_effect=busy_children),
        capture_logs() as logs,
    ):
        async with anyio.create_task_group() as tg:

            async def drive() -> None:
                for step in range(1, 20):
                    clock.set(100.0 + step * 120.0)  # a 38-minute hold
                    await anyio.sleep(0.02)
                edits.signal_send.close()

            tg.start_soon(edits.run)
            tg.start_soon(drive)

    assert not cancel_event.is_set()
    stall_msgs = [c for c in transport.send_calls if "min" in c["message"].text]
    assert stall_msgs == []
    for noisy in (
        "progress_edits.stall_detected",
        "progress_edits.stall_threshold_selected",
    ):
        assert [e for e in logs if e.get("event") == noisy] == []
    suppressed = [
        e for e in logs if e.get("event") == "progress_edits.stall_live_idle_suppressed"
    ]
    assert len(suppressed) == 1  # once per live-idle episode, not per tick
    assert edits._total_stall_warn_count == 0
    assert edits._peak_idle == 0.0
    assert edits._peak_live_idle > 1000.0


async def test_live_turn_active_is_not_post_result_idle() -> None:
    transport = FakeTransport()
    edits = _make_edits(transport, _KeyboardPresenter(), clock=_FakeClock(start=0.0))
    edits.stream = _make_stream(
        last_event_type="assistant",
        engine_state=_make_engine_state(
            live_mode=True, completed_turns=1, turn_open=True
        ),
    )
    assert edits._is_live_session_idle() is False
    assert edits._is_post_result_idle() is False


# ── #811: peak_live_idle_seconds measures gaps between turns ───────────────


def _live_run_level_edits(
    transport: FakeTransport, clock: _FakeClock, *, turn_open: bool = False
):
    edits = _make_edits(transport, _KeyboardPresenter(), clock=clock)
    edits.run_level = True
    edits._stall_check_interval = 0.01
    edits._STALL_THRESHOLD_SECONDS = 0.05
    edits._STALL_THRESHOLD_TOOL = 0.05
    edits._STALL_THRESHOLD_SUBAGENT = 0.05
    edits._STALL_THRESHOLD_APPROVAL = 10.0
    edits._stall_repeat_seconds = 0.0
    edits._STALL_MAX_WARNINGS = 2
    edits.cancel_event = anyio.Event()
    edits.stream = _make_stream(
        last_event_type="system",
        engine_state=_make_engine_state(
            result_received_at=None,
            live_mode=True,
            completed_turns=1,
            turn_open=turn_open,
        ),
    )
    return edits


async def _drive_live_timeline(edits, clock: _FakeClock, steps: list) -> None:
    """Run the stall monitor over ``steps``: a float sets the fake clock and
    yields for a few monitor ticks; a str is a turn boundary phase."""
    async with anyio.create_task_group() as tg:

        async def drive() -> None:
            for step in steps:
                if isinstance(step, str):
                    edits.note_turn_boundary(step)
                else:
                    clock.set(step)
                await anyio.sleep(0.03)
            edits.signal_send.close()

        tg.start_soon(edits.run)
        tg.start_soon(drive)


async def test_811_peak_live_idle_excludes_followup_turn_time() -> None:
    """60 s idle, a 900 s follow-up turn, 60 s idle → peak ≈ 60, not ≥ 900."""
    transport = FakeTransport()
    clock = _FakeClock(start=100.0)
    edits = _live_run_level_edits(transport, clock)

    await _drive_live_timeline(
        edits,
        clock,
        [160.0, "started", 600.0, 1060.0, "completed", 1120.0],
    )

    assert 59.0 <= edits._peak_live_idle <= 61.0
    assert edits._peak_idle == 0.0
    assert edits._turn_active is False
    assert edits._live_idle_baseline == 1060.0


async def test_811_peak_live_idle_is_max_gap_across_turns() -> None:
    """Idle gaps of 45 / 61 / 30 s between turns → the peak is 61."""
    transport = FakeTransport()
    clock = _FakeClock(start=0.0)
    edits = _live_run_level_edits(transport, clock)

    await _drive_live_timeline(
        edits,
        clock,
        [
            45.0,
            "started",
            345.0,
            "completed",
            406.0,
            "started",
            1006.0,
            "completed",
            1036.0,
        ],
    )

    assert 60.0 <= edits._peak_live_idle <= 61.5


async def test_811_stall_suppression_unchanged_during_turn() -> None:
    """Regression: run-level edits still stand down while a turn is open —
    no stall warning, no auto-cancel — and a turn accrues no live-idle."""
    from structlog.testing import capture_logs

    transport = FakeTransport()
    clock = _FakeClock(start=100.0)
    edits = _live_run_level_edits(transport, clock, turn_open=True)
    edits.note_turn_boundary("started")

    with capture_logs() as logs:
        await _drive_live_timeline(
            edits, clock, [100.0 + step * 700.0 for step in range(1, 10)]
        )

    assert edits._is_live_session_idle() is True
    assert edits.cancel_event is not None and not edits.cancel_event.is_set()
    assert [c for c in transport.send_calls if "min" in c["message"].text] == []
    assert [e for e in logs if e.get("event") == "progress_edits.stall_detected"] == []
    assert edits._total_stall_warn_count == 0
    assert edits._peak_live_idle == 0.0
    assert edits._peak_idle == 0.0


async def test_811_run_runner_with_cancel_feeds_turn_boundaries() -> None:
    """The bridge feeds each TurnEvent phase to the run-level edits."""
    from untether.model import ResumeToken as _RT
    from untether.runner_bridge import run_runner_with_cancel
    from untether.runners.mock import Emit as _Emit
    from untether.runners.mock import Return as _Return
    from untether.runners.mock import ScriptRunner as _ScriptRunner

    token = _RT(engine="claude", value="sess-811")
    clock = _FakeClock(start=50.0)
    edits = _make_edits(FakeTransport(), _KeyboardPresenter(), clock=clock)
    runner = _ScriptRunner(
        [
            _Emit(TurnEvent(engine="claude", phase="started", turn=2)),
            _Emit(TurnEvent(engine="claude", phase="completed", turn=2)),
            _Emit(TurnEvent(engine="claude", phase="started", turn=3)),
            _Return(answer="ok"),
        ],
        engine="claude",
        resume_value=token.value,
    )
    with anyio.fail_after(5):
        await run_runner_with_cancel(
            runner,
            prompt="go",
            resume_token=None,
            edits=edits,
            running_task=None,
            on_thread_known=None,
        )

    assert edits._live_idle_baseline == 50.0
    assert edits._turn_active is True  # turn 3 never closed


# ── FollowupTurnRouter (phase 04) ───────────────────────────────────────────

from untether import runner_bridge as rb  # noqa: E402
from untether.model import Action, ActionEvent, TurnEvent  # noqa: E402
from untether.progress import ProgressTracker  # noqa: E402
from untether.transport import MessageRef  # noqa: E402

USER_REF = MessageRef(channel_id=1, message_id=10)


class _Recorder:
    def __init__(self) -> None:
        self.created: list[int] = []
        self.closed: list[int] = []
        self.delivered: list[tuple[int, bool, str, str | None, bool, int]] = []

    async def create(self, ctx: rb._TurnCtx) -> None:
        self.created.append(ctx.turn)

        class _Edits:
            def __init__(self) -> None:
                self.events: list = []

            async def on_event(self, evt) -> None:
                self.events.append(evt)

        ctx.edits = _Edits()  # type: ignore[assignment]
        ctx.progress_ref = MessageRef(channel_id=1, message_id=100 + ctx.turn)

    async def close(self, ctx: rb._TurnCtx) -> None:
        self.closed.append(ctx.turn)

    async def deliver(self, completed, ctx: rb._TurnCtx) -> None:
        ctx.delivery["sent"] = True
        self.delivered.append(
            (
                ctx.turn,
                completed.ok,
                completed.answer,
                ctx.header,
                ctx.notify,
                ctx.reply_to.message_id,
            )
        )


def _router(
    rec: _Recorder,
    anchors: dict[str, tuple[MessageRef, MessageRef | None]] | None = None,
):
    return rb.FollowupTurnRouter(
        new_tracker=lambda: ProgressTracker(engine="claude"),
        create_progress=rec.create,
        close_progress=rec.close,
        deliver=rec.deliver,
        default_reply_to=USER_REF,
        followup_notify=False,
        anchor_for=(anchors or {}).get,
    )


def _turn(phase: str, turn: int = 2, reason: str = "task_finished", **kw):
    return TurnEvent(engine="claude", phase=phase, turn=turn, reason=reason, **kw)


def _action() -> ActionEvent:
    return ActionEvent(
        engine="claude",
        action=Action(id="toolu_1", kind="command", title="ls"),
        phase="started",
    )


async def test_router_creates_progress_on_first_action_and_routes_events() -> None:
    """Approvals raised inside a wake turn are ActionEvents: the first one
    forces the turn's progress message into existence, so the keyboard has
    somewhere to render."""
    rec = _Recorder()
    router = _router(rec)
    await router.on_turn(_turn("started", detail={"tasks": ["build"]}))
    assert rec.created == []
    await router.on_event(_action())
    assert rec.created == [2]
    assert router.current is not None and len(router.current.edits.events) == 1
    await router.on_turn(_turn("completed", ok=True, answer="done"))
    assert rec.delivered == [
        (2, True, "done", "\N{BELL} Background task finished — build", True, 10)
    ]
    assert rec.closed == [2]
    assert router.active is False


async def test_router_retro_attributed_turn_gets_the_task_header() -> None:
    """#785: a wake turn that opened ``unknown`` and completed attributed to
    the task that ended during it is delivered with that task's header."""
    rec = _Recorder()
    router = _router(rec)
    await router.on_turn(_turn("started", reason="unknown"))
    assert router.current is not None
    assert router.current.header == "\N{BELL} Claude continued"
    await router.on_turn(
        _turn(
            "completed",
            reason="task_finished",
            ok=True,
            answer="report",
            detail={"tasks": ["Stale sweep: Trello"], "retro_attributed": True},
        )
    )
    assert rec.delivered == [
        (
            2,
            True,
            "report",
            "\N{BELL} Background task finished — Stale sweep: Trello",
            True,
            10,
        )
    ]


async def test_router_already_announced_turn_is_not_pushed() -> None:
    """#785: the second wake turn for one background-task finish arrives
    without a push (still delivered, with its header)."""
    rec = _Recorder()
    router = _router(rec)
    detail = {"tasks": ["Stale sweep: Trello"], "already_announced": True}
    await router.on_turn(_turn("started", detail=detail))
    await router.on_turn(_turn("completed", ok=True, answer="again", detail=detail))
    _turn_no, _ok, _answer, header, notify, _reply = rec.delivered[0]
    assert header == "\N{BELL} Background task finished — Stale sweep: Trello"
    assert notify is False


async def test_825_router_reheads_task_finished_turn_with_late_tasks() -> None:
    """#825: a wake turn that opened as an already-announced repeat for task
    A, during which task B finished, is delivered naming both — and pushed,
    because B's finish is news. Its reply anchor is kept."""
    rec = _Recorder()
    router = _router(rec)
    opened = {"tasks": ["job A"], "task_ids": ["a1"], "already_announced": True}
    await router.on_turn(_turn("started", detail=opened))
    assert router.current is not None and router.current.notify is False
    reply_before = router.current.reply_to
    await router.on_turn(
        _turn(
            "completed",
            ok=True,
            answer="both done",
            detail={
                "tasks": ["job A", "job B"],
                "task_ids": ["a1", "a2"],
                "late_tasks": ["job B"],
            },
        )
    )
    _turn_no, _ok, _answer, header, notify, reply = rec.delivered[0]
    assert header == (
        "\N{BELL} 2 background tasks finished \N{EM DASH} job A \N{MIDDLE DOT} job B"
    )
    assert notify is True
    assert reply == reply_before.message_id


async def test_825_router_keeps_header_without_late_tasks() -> None:
    rec = _Recorder()
    router = _router(rec)
    detail = {"tasks": ["job A"], "task_ids": ["a1"]}
    await router.on_turn(_turn("started", detail=detail))
    await router.on_turn(
        _turn(
            "completed",
            ok=True,
            answer="done",
            detail={"tasks": ["job A", "job B"], "task_ids": ["a1", "a2"]},
        )
    )
    assert rec.delivered[0][3] == "\N{BELL} Background task finished \N{EM DASH} job A"


@pytest.mark.parametrize(
    ("answer", "actions", "expected"),
    [
        ("B's report: " + "findings. " * 40, 0, "long_answer"),
        ("checked B", 1, "tools"),
        ("B finished (again).", 0, "fold"),  # the accepted #785 trade-off
    ],
)
def test_825_turn_after_late_attribution_is_delivered_when_substantive(
    answer: str, actions: int, expected: str
) -> None:
    """#825 review: B was named in A's turn, so B's own CLI turn arrives
    ``already_announced``. A substantive B turn still breaks out as its own
    message; only a short restatement folds into the status message."""
    from untether.background_status import wake_fold_decision

    assert (
        wake_fold_decision(
            reason="task_finished",
            ok=True,
            answer=answer,
            substantive_actions=actions,
            already_announced=True,
            live_tasks_remaining=0,
            batch_announced=True,
        )
        == expected
    )


async def test_router_completed_without_detail_keeps_open_header() -> None:
    rec = _Recorder()
    router = _router(rec)
    await router.on_turn(_turn("started", detail={"tasks": ["build"]}))
    await router.on_turn(_turn("completed", ok=True, answer="done"))
    assert rec.delivered[0][3] == "\N{BELL} Background task finished — build"


async def test_router_short_turn_without_actions_never_creates_progress() -> None:
    rec = _Recorder()
    router = _router(rec)
    await router.on_turn(_turn("started", reason="monitor_event"))
    await router.on_turn(
        _turn("completed", reason="monitor_event", ok=True, answer="t")
    )
    assert rec.created == []
    assert rec.delivered[0][4] is False  # Monitor ticks are silent


async def test_router_lazy_progress_after_delay(monkeypatch) -> None:
    monkeypatch.setattr(rb, "_TURN_LAZY_PROGRESS_S", 0.05)
    rec = _Recorder()
    router = _router(rec)
    async with anyio.create_task_group() as tg:
        router.bind_task_group(tg)
        await router.on_turn(_turn("started"))
        await anyio.sleep(0.2)
        assert rec.created == [2]
        await router.on_turn(_turn("completed", ok=True, answer="x"))


async def test_router_interrupted_turn_delivers_error_on_aclose() -> None:
    rec = _Recorder()
    router = _router(rec)
    await router.on_turn(_turn("started"))
    await router.on_event(_action())
    await router.aclose()
    assert len(rec.delivered) == 1
    turn, ok, _answer, _header, _notify, _reply = rec.delivered[0]
    assert (turn, ok) == (2, False)
    assert rec.closed == [2]


class _CancelRecorder(_Recorder):
    def __init__(self) -> None:
        super().__init__()
        self.cancelled: list[int] = []

    async def deliver_cancelled(self, ctx: rb._TurnCtx) -> None:
        ctx.delivery["sent"] = True
        self.cancelled.append(ctx.turn)


def _cancel_router(rec: _CancelRecorder):
    return rb.FollowupTurnRouter(
        new_tracker=lambda: ProgressTracker(engine="claude"),
        create_progress=rec.create,
        close_progress=rec.close,
        deliver=rec.deliver,
        default_reply_to=USER_REF,
        followup_notify=False,
        anchor_for={}.get,
        deliver_cancelled=rec.deliver_cancelled,
    )


async def test_806_router_cancelled_turn_renders_cancelled_on_aclose() -> None:
    """#806: /cancel of an in-flight follow-up turn renders as cancelled —
    no "the session ended before this turn finished" error final."""
    from structlog.testing import capture_logs

    rec = _CancelRecorder()
    router = _cancel_router(rec)
    await router.on_turn(_turn("started", reason="followup"))
    await router.on_event(_action())
    with capture_logs() as logs:
        await router.aclose("cancel")
    assert rec.cancelled == [2]
    assert rec.delivered == []
    assert rec.closed == [2]
    assert router.active is False
    events = [e for e in logs if e["event"].startswith("live_turn.")]
    assert [(e["event"], e["turn"], e["reason"]) for e in events] == [
        ("live_turn.cancelled", 2, "cancel")
    ]


async def test_806_router_new_reason_renders_cancelled() -> None:
    """/new cancels through the same ``cancel_requested`` event as /cancel,
    so the bridge's close reason for it is "cancel" too — and renders the
    in-flight turn as cancelled."""
    from untether.telegram.commands.topics import _cancel_chat_tasks

    task = rb.RunningTask()
    assert _cancel_chat_tasks(1, {USER_REF: task}) == 1
    assert task.cancel_requested.is_set()

    rec = _CancelRecorder()
    router = _cancel_router(rec)
    await router.on_turn(_turn("started", reason="task_finished"))
    await router.aclose("cancel")
    assert rec.cancelled == [2]
    assert rec.delivered == []


@pytest.mark.parametrize("reason", [None, "abs_cap", "error", "idle_no_tasks"])
async def test_806_abs_cap_mid_turn_still_renders_error(reason: str | None) -> None:
    """Genuine process loss (abs_cap, a crash, a close the lifecycle made
    itself) keeps the error final."""
    rec = _CancelRecorder()
    router = _cancel_router(rec)
    await router.on_turn(_turn("started", reason="followup"))
    await router.on_event(_action())
    await router.aclose(reason)
    assert rec.cancelled == []
    assert [(d[0], d[1]) for d in rec.delivered] == [(2, False)]
    assert rec.closed == [2]


async def test_router_followup_turn_anchors_to_its_message() -> None:
    rec = _Recorder()
    anchor = MessageRef(channel_id=1, message_id=55)
    router = _router(rec, anchors={"cmd-1": (anchor, None)})
    await router.on_turn(_turn("started", reason="followup", command_uuid="cmd-1"))
    await router.on_turn(
        _turn("completed", reason="followup", command_uuid="cmd-1", ok=True, answer="a")
    )
    turn, ok, answer, header, notify, reply = rec.delivered[0]
    assert header is None and reply == 55 and notify is False


@pytest.mark.parametrize(
    ("reason", "detail", "expected"),
    [
        ("scheduled_wakeup", {}, "\N{ALARM CLOCK} Scheduled wake-up"),
        (
            "monitor_event",
            {"tasks": ["deploy log"]},
            "\N{SATELLITE ANTENNA} Monitor — deploy log",
        ),
        ("monitor_event", {}, "\N{SATELLITE ANTENNA} Monitor"),
        ("unknown", {}, "\N{BELL} Claude continued"),
        (
            "task_finished",
            {"tasks": ["a", "b"]},
            "\N{BELL} 2 background tasks finished \N{EM DASH} a \N{MIDDLE DOT} b",
        ),
        (
            # #825: first three names, then a count of the rest.
            "task_finished",
            {"tasks": ["a", "b", "c", "d", "e"]},
            "\N{BELL} 5 background tasks finished \N{EM DASH} a \N{MIDDLE DOT} b"
            " \N{MIDDLE DOT} c (+2 more)",
        ),
        (
            # #825: each name is cut to 40 characters.
            "task_finished",
            {"tasks": ["x" * 60, "job B"]},
            "\N{BELL} 2 background tasks finished \N{EM DASH} "
            + "x" * 39
            + "\N{HORIZONTAL ELLIPSIS} \N{MIDDLE DOT} job B",
        ),
        (
            # #892: a continued agent's mark survives the name cut.
            "task_finished",
            {"tasks": ["x" * 60 + " (continued)", "job B"]},
            "\N{BELL} 2 background tasks finished \N{EM DASH} "
            + "x" * 39
            + "\N{HORIZONTAL ELLIPSIS} (continued) \N{MIDDLE DOT} job B",
        ),
        (
            "task_finished",
            {"tasks": ["y" * 90 + " (continued)"]},
            "\N{BELL} Background task finished \N{EM DASH} "
            + "y" * 80
            + " (continued)",
        ),
        ("followup", {}, None),
    ],
)
def test_turn_headers(reason: str, detail: dict, expected: str | None) -> None:
    assert rb._turn_header(_turn("started", reason=reason, detail=detail)) == expected


@pytest.mark.parametrize(
    ("reason", "tasks", "expected"),
    [
        # #829: no closing text promises "reply to continue" any more — the
        # "closed" follow-up says whether the session continues.
        (
            "cancel",
            ["a"],
            "\N{BLACK SQUARE FOR STOP} Stopped 1 background task: a.",
        ),
        (
            "drain",
            ["a", "b"],
            "\N{HOURGLASS WITH FLOWING SAND} Untether is restarting — stopping 2 background tasks: a, b.",
        ),
        (
            "max_hold",
            ["a"],
            "\N{HOURGLASS WITH FLOWING SAND} Closing session — 1 background task still running at the background hold limit: a. Stopping it.",
        ),
        (
            "abs_cap",
            ["a", "b"],
            "\N{HOURGLASS WITH FLOWING SAND} Closing session — 2 background tasks still running at the session time limit: a, b. Stopping them.",
        ),
    ],
)
def test_live_closing_notice_wording(
    reason: str, tasks: list[str], expected: str
) -> None:
    assert rb._live_closing_notice(reason, tasks) == expected


@pytest.mark.parametrize(
    ("tasks", "max_hold_s", "rearm", "expected"),
    [
        (
            ["A", "B"],
            1800.0,
            True,
            "\N{HOURGLASS WITH FLOWING SAND} Closing session — 2 background tasks still running with no progress for 30 min: A, B. Stopping them.",
        ),
        (
            ["A"],
            60.0,
            True,
            "\N{HOURGLASS WITH FLOWING SAND} Closing session — 1 background task still running with no progress for 1 min: A. Stopping it.",
        ),
        (
            ["A"],
            45.0,
            True,
            "\N{HOURGLASS WITH FLOWING SAND} Closing session — 1 background task still running with no progress for 45 s: A. Stopping it.",
        ),
        (
            ["A"],
            1800.0,
            False,  # kill switch: the hold counted from the turn, not activity
            "\N{HOURGLASS WITH FLOWING SAND} Closing session — 1 background task still running at the background hold limit: A. Stopping it.",
        ),
    ],
)
def test_829_max_hold_notice_names_the_quiet_time(
    tasks: list[str], max_hold_s: float, rearm: bool, expected: str
) -> None:
    text = rb._live_closing_notice(
        "max_hold", tasks, max_hold_s=max_hold_s, rearm_on_progress=rearm
    )
    assert text == expected
    assert "reply to continue" not in text.lower()


def test_829_closed_notice_wording() -> None:
    assert rb._live_closed_notice(False) == (
        "\N{LEFTWARDS ARROW WITH HOOK}\N{VARIATION SELECTOR-16} Reply to "
        "continue in the same session."
    )
    warning = rb._live_closed_notice(True)
    assert warning.startswith("\N{WARNING SIGN}")
    assert "fresh session" in warning and "Partial work" in warning


async def test_router_tracks_last_reply_anchor_for_notices() -> None:
    rec = _Recorder()
    anchor = MessageRef(channel_id=1, message_id=77)
    router = _router(rec, anchors={"cmd-9": (anchor, None)})
    assert router.last_reply_to == USER_REF
    await router.on_turn(_turn("started", reason="followup", command_uuid="cmd-9"))
    assert router.last_reply_to == anchor


async def test_router_creates_progress_once_under_concurrent_requests(
    monkeypatch,
) -> None:
    """Review finding: the lazy timer and the first action could both create
    a progress message (the first orphaned, its alias leaked)."""
    monkeypatch.setattr(rb, "_TURN_LAZY_PROGRESS_S", 0.0)
    rec = _Recorder()
    real_create = rec.create

    async def slow_create(ctx):
        await anyio.sleep(0.05)  # a send in flight
        await real_create(ctx)

    rec.create = slow_create  # type: ignore[method-assign]
    router = rb.FollowupTurnRouter(
        new_tracker=lambda: ProgressTracker(engine="claude"),
        create_progress=slow_create,
        close_progress=rec.close,
        deliver=rec.deliver,
        default_reply_to=USER_REF,
        followup_notify=False,
    )
    async with anyio.create_task_group() as tg:
        router.bind_task_group(tg)
        await router.on_turn(_turn("started"))
        await anyio.sleep(0.01)  # lazy task now mid-create
        await router.on_event(_action())
        await router.on_turn(_turn("completed", ok=True, answer="x"))
    assert rec.created == [2]
    assert len(rec.delivered) == 1


def test_run_level_edits_stand_down_during_followup_turns() -> None:
    """Review finding: the run's own stall monitor saw a wake turn as a stall
    (it no longer receives the turn's events) and could auto-cancel it."""
    transport = FakeTransport()
    edits = _make_edits(transport, _KeyboardPresenter(), clock=_FakeClock(start=0.0))
    edits.stream = _make_stream(
        last_event_type="assistant",
        engine_state=_make_engine_state(
            live_mode=True, completed_turns=1, turn_open=True
        ),
    )
    assert edits._is_live_session_idle() is False  # a turn's own edits
    edits.run_level = True
    assert edits._is_live_session_idle() is True  # the run's edits stand down


# ── #798: "✓ turn complete" on live follow-up / wake turn finals ────────────
#
# Driven through the real handle_message → FollowupTurnRouter → _deliver_final
# path: ScriptRunner's TurnEvents reach the router exactly like a live
# ClaudeRunner's do.

from untether.markdown import MarkdownPresenter  # noqa: E402
from untether.model import (  # noqa: E402
    TURN_COMPLETE_MARKER,
    CompletedEvent,
    ResumeToken,
    StartedEvent,
)
from untether.runner_bridge import (  # noqa: E402
    ExecBridgeConfig,
    IncomingMessage,
    handle_message,
)
from untether.runners.mock import Emit, Return, ScriptRunner  # noqa: E402

_TOKEN = ResumeToken(engine="claude", value="sess-798")


async def _run_with_turn(
    *turn_steps: Emit, end_mid_turn: bool = False
) -> tuple[FakeTransport, MessageRef]:
    """A run whose first result carried the #333 marker (as the Claude runner
    sends it), then one live turn. ``end_mid_turn``: the run's result comes
    first (the real live order) and the stream ends with the turn still open.
    Returns the transport and the run's own progress ref."""
    first = CompletedEvent(engine="claude", resume=_TOKEN, ok=True, answer="FIRST")
    transport = FakeTransport()
    runner = ScriptRunner(
        [
            Emit(StartedEvent(engine="claude", resume=_TOKEN, meta={"model": "opus"})),
            Emit(
                StartedEvent(
                    engine="claude",
                    resume=_TOKEN,
                    meta={"complete": TURN_COMPLETE_MARKER},
                )
            ),
            *([Emit(first)] if end_mid_turn else []),
            *turn_steps,
            # (end_mid_turn: ScriptRunner's closing CompletedEvent lands in
            # the still-open turn, as any late event would.)
            *([] if end_mid_turn else [Return(answer="FIRST")]),
        ],
        engine="claude",
        resume_value=_TOKEN.value,
    )
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=True
    )
    await handle_message(
        cfg,
        runner=runner,
        incoming=IncomingMessage(channel_id=1, message_id=10, text="go"),
        resume_token=None,
    )
    return transport, transport.send_calls[0]["ref"]


def _turn_texts(
    transport: FakeTransport, run_progress: MessageRef, answer: str
) -> tuple[str, list[str]]:
    """(the turn's final text, every text its progress message showed before
    it). A pushed final is a new message; a silent one (Monitor tick) edits
    the progress message in place."""
    calls = [*transport.send_calls, *transport.edit_calls]
    finals = [c["message"].text for c in calls if answer in c["message"].text]
    assert len(finals) == 1
    progress_refs = {
        c["ref"]
        for c in transport.send_calls
        if c["ref"] != run_progress
        and answer not in c["message"].text
        and "FIRST" not in c["message"].text
    }
    assert progress_refs, "the turn never showed a progress message"
    progress = [
        c["message"].text
        for c in calls
        if c["ref"] in progress_refs and answer not in c["message"].text
    ]
    return finals[0], progress


@pytest.mark.parametrize(
    "reason",
    ["followup", "task_finished", "scheduled_wakeup", "monitor_event", "unknown"],
)
async def test_turn_final_carries_turn_complete_marker(reason: str) -> None:
    """#798: every successful live turn final shows the #333 marker, whatever
    started the turn; the turn's in-flight progress never does (the run's
    marker is stripped from the turn tracker and the final adds its own to
    the snapshot only)."""
    transport, run_progress = await _run_with_turn(
        Emit(_turn("started", reason=reason)),
        Emit(_action()),
        Emit(_turn("completed", reason=reason, ok=True, answer="TURN-2-ANSWER")),
    )
    final, progress = _turn_texts(transport, run_progress, "TURN-2-ANSWER")
    assert final.count(TURN_COMPLETE_MARKER) == 1
    assert "opus" in final  # the run's meta is kept alongside the marker
    assert all(TURN_COMPLETE_MARKER not in t for t in progress)


async def test_failed_turn_final_has_no_turn_complete_marker() -> None:
    transport, run_progress = await _run_with_turn(
        Emit(_turn("started", reason="followup")),
        Emit(_action()),
        Emit(
            _turn(
                "completed",
                reason="followup",
                ok=False,
                answer="TURN-2-ANSWER",
                error="API Error: overloaded",
            )
        ),
    )
    final, progress = _turn_texts(transport, run_progress, "TURN-2-ANSWER")
    assert TURN_COMPLETE_MARKER not in final
    assert all(TURN_COMPLETE_MARKER not in t for t in progress)


async def test_interrupted_turn_final_has_no_turn_complete_marker() -> None:
    """A turn the session ended under (router.aclose) is not "complete"."""
    transport, run_progress = await _run_with_turn(
        Emit(_turn("started", reason="task_finished", detail={"tasks": ["b1"]})),
        Emit(_action()),
        end_mid_turn=True,
    )
    final, progress = _turn_texts(
        transport, run_progress, "the session ended before this turn finished"
    )
    assert TURN_COMPLETE_MARKER not in final
    assert all(TURN_COMPLETE_MARKER not in t for t in progress)


async def test_806_cancelled_turn_without_progress_replies_cancelled() -> None:
    """#806: /cancel of a follow-up turn that never grew a progress message
    (no tool yet, under the lazy-progress delay) replies ``cancelled`` to the
    turn's message — not the error final."""
    from untether.runners.mock import Wait

    first = CompletedEvent(engine="claude", resume=_TOKEN, ok=True, answer="FIRST")
    never = anyio.Event()
    transport = FakeTransport()
    runner = ScriptRunner(
        [
            Emit(StartedEvent(engine="claude", resume=_TOKEN)),
            Emit(first),
            Emit(_turn("started", reason="followup")),
            Wait(never),  # the turn is still running when /cancel lands
        ],
        engine="claude",
        resume_value=_TOKEN.value,
    )
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=True
    )
    running: dict[MessageRef, rb.RunningTask] = {}

    async def cancel_when_turn_open() -> None:
        with anyio.fail_after(5):
            while not any("FIRST" in c["message"].text for c in transport.send_calls):
                await anyio.sleep(0.01)
            await anyio.sleep(0.05)
        (_, task), *_ = rb.unique_running_tasks(running)
        task.cancel_requested.set()

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            tg.start_soon(cancel_when_turn_open)
            await handle_message(
                cfg,
                runner=runner,
                incoming=IncomingMessage(channel_id=1, message_id=10, text="go"),
                resume_token=None,
                running_tasks=running,
            )

    texts = [c["message"].text for c in (*transport.send_calls, *transport.edit_calls)]
    assert not any("session ended before this turn finished" in t for t in texts)
    cancelled = [c for c in transport.send_calls if "cancelled" in c["message"].text]
    assert len(cancelled) == 1
    assert cancelled[0]["options"].reply_to.message_id == 10
    # The run's own (already delivered) final is left alone.
    assert any("FIRST" in t for t in texts)


async def test_806_aborted_terminal_reason_turn_renders_cancelled() -> None:
    """#806: a turn the CLI reports as interrupted (``terminal_reason``
    ``aborted_*`` → ``usage["terminal_reason"]``) renders as cancelled, not
    as its partial answer."""
    transport, _run_progress = await _run_with_turn(
        Emit(_turn("started", reason="followup")),
        Emit(_action()),
        Emit(
            _turn(
                "completed",
                reason="followup",
                ok=True,
                answer="PARTIAL-ANSWER",
                usage={"terminal_reason": "aborted_tools"},
            )
        ),
    )
    texts = [c["message"].text for c in (*transport.send_calls, *transport.edit_calls)]
    assert not any("PARTIAL-ANSWER" in t for t in texts)
    assert any("cancelled" in t for t in texts)
    assert not any(TURN_COMPLETE_MARKER in t and "cancelled" in t for t in texts)


async def test_806_aborted_turn_still_accounts_its_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#806 review: an aborted turn skips ``_deliver_final`` for its render,
    but its spend must still reach the #778 ledger, the daily total and the
    ``runner.completed`` log — otherwise it is lost if the session closes."""
    from structlog.testing import capture_logs

    from untether import cost_tracker
    from untether.session_costs import get_session_cost_ledger

    monkeypatch.setattr(cost_tracker, "_daily_cost", ("", 0.0))
    first = CompletedEvent(
        engine="claude",
        resume=_TOKEN,
        ok=True,
        answer="FIRST",
        usage={"total_cost_usd": 1.0, "num_turns": 1},
    )
    transport = FakeTransport()
    runner = ScriptRunner(
        [
            Emit(StartedEvent(engine="claude", resume=_TOKEN)),
            Emit(first),
            Emit(_turn("started", reason="followup")),
            Emit(_action()),
            Emit(
                _turn(
                    "completed",
                    reason="followup",
                    ok=True,
                    answer="PARTIAL-ANSWER",
                    resume=_TOKEN,
                    usage={
                        "terminal_reason": "aborted_tools",
                        "total_cost_usd": 1.5,
                        "num_turns": 2,
                    },
                )
            ),
        ],
        engine="claude",
        resume_value=_TOKEN.value,
    )
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=True
    )
    with capture_logs() as logs:
        await handle_message(
            cfg,
            runner=runner,
            incoming=IncomingMessage(channel_id=1, message_id=10, text="go"),
            resume_token=None,
        )

    # Rendering is unchanged: cancelled, never the partial answer.
    texts = [c["message"].text for c in (*transport.send_calls, *transport.edit_calls)]
    assert not any("PARTIAL-ANSWER" in t for t in texts)
    assert any("cancelled" in t for t in texts)
    # Accounting: first result $1.00 + the aborted turn's $0.50 delta.
    assert get_session_cost_ledger().last("claude", _TOKEN.value) == pytest.approx(1.5)
    assert cost_tracker.get_daily_cost() == pytest.approx(1.5)
    completed_logs = [e for e in logs if e["event"] == "runner.completed"]
    assert [e.get("turn_cost_usd") for e in completed_logs] == [
        pytest.approx(1.0),
        pytest.approx(0.5),
    ]


async def test_806_aborted_turn_is_accounted_once_when_its_render_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#806 review: an aborted turn is accounted before its ``cancelled``
    render. When /cancel interrupts that render, ``aclose`` retries it and —
    if the retry fails too — delivers a synthetic final through
    ``_deliver_final``, which used to account the turn a second time
    (``runner.completed`` and /stats double-recorded)."""
    from structlog.testing import capture_logs

    from untether import cost_tracker, session_stats

    monkeypatch.setattr(cost_tracker, "_daily_cost", ("", 0.0))
    stats_runs: list[dict] = []
    monkeypatch.setattr(session_stats, "record_run", lambda **kw: stats_runs.append(kw))
    running: dict[MessageRef, rb.RunningTask] = {}
    cancelled_renders = {"n": 0}

    class _Transport(FakeTransport):
        async def _maybe_fail(self, message) -> None:
            if "cancelled" not in message.text:
                return
            cancelled_renders["n"] += 1
            if cancelled_renders["n"] == 1:
                # /cancel lands while the cancelled render is in flight.
                (_, task), *_ = rb.unique_running_tasks(running)
                task.cancel_requested.set()
                await anyio.sleep(5)
            raise RuntimeError("telegram down")

        async def send(self, *, channel_id, message, options=None):
            await self._maybe_fail(message)
            return await super().send(
                channel_id=channel_id, message=message, options=options
            )

        async def edit(self, *, ref, message, wait=True):
            await self._maybe_fail(message)
            return await super().edit(ref=ref, message=message, wait=wait)

    first = CompletedEvent(
        engine="claude",
        resume=_TOKEN,
        ok=True,
        answer="FIRST",
        usage={"total_cost_usd": 1.0, "num_turns": 1},
    )
    transport = _Transport()
    runner = ScriptRunner(
        [
            Emit(StartedEvent(engine="claude", resume=_TOKEN)),
            Emit(first),
            Emit(_turn("started", reason="followup")),
            Emit(_action()),
            Emit(
                _turn(
                    "completed",
                    reason="followup",
                    ok=True,
                    answer="PARTIAL-ANSWER",
                    resume=_TOKEN,
                    usage={
                        "terminal_reason": "aborted_tools",
                        "total_cost_usd": 1.5,
                        "num_turns": 2,
                    },
                )
            ),
        ],
        engine="claude",
        resume_value=_TOKEN.value,
    )
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=True
    )
    with capture_logs() as logs, anyio.fail_after(20):
        await handle_message(
            cfg,
            runner=runner,
            incoming=IncomingMessage(channel_id=1, message_id=10, text="go"),
            resume_token=None,
            running_tasks=running,
        )

    assert cancelled_renders["n"] == 2  # interrupted, then retried by aclose
    completed_logs = [e for e in logs if e["event"] == "runner.completed"]
    assert len(completed_logs) == 2  # the run's result + the aborted turn
    assert len(stats_runs) == 2
    assert cost_tracker.get_daily_cost() == pytest.approx(1.5)


# ── #795 wake-turn reply anchor ──────────────────────────────────────────────

FOLLOWUP_REF = MessageRef(channel_id=1, message_id=20)


async def _followup_turn(router, turn: int = 2) -> None:
    await router.on_turn(
        _turn("started", turn=turn, reason="followup", command_uuid="u1")
    )
    await router.on_turn(
        _turn(
            "completed",
            turn=turn,
            reason="followup",
            ok=True,
            answer="ok",
            command_uuid="u1",
        )
    )


async def test_795_wake_turn_replies_to_the_turn_that_launched_its_task() -> None:
    rec = _Recorder()
    router = _router(rec, anchors={"u1": (FOLLOWUP_REF, None)})
    await _followup_turn(router)
    detail = {"tasks": ["bg b2"], "task_ids": ["b2"], "origin_turn": 2}
    await router.on_turn(_turn("started", turn=3, detail=detail))
    await router.on_turn(
        _turn("completed", turn=3, ok=True, answer="done", detail=detail)
    )
    assert rec.delivered[-1][5] == 20
    assert router.anchor_for_turn(3) == FOLLOWUP_REF
    assert router.last_reply_to == FOLLOWUP_REF


async def test_795_task_from_the_run_itself_replies_to_the_run_prompt() -> None:
    rec = _Recorder()
    router = _router(rec, anchors={"u1": (FOLLOWUP_REF, None)})
    await _followup_turn(router)
    detail = {"tasks": ["bg b1"], "task_ids": ["b1"], "origin_turn": 1}
    await router.on_turn(_turn("started", turn=3, detail=detail))
    await router.on_turn(
        _turn("completed", turn=3, ok=True, answer="done", detail=detail)
    )
    assert rec.delivered[-1][5] == 10


async def test_795_retro_attributed_turn_moves_its_reply_to_the_origin() -> None:
    """An ``unknown`` wake turn only learns its task at completion; its final
    (not yet sent) then replies to the launching follow-up."""
    rec = _Recorder()
    router = _router(rec, anchors={"u1": (FOLLOWUP_REF, None)})
    await _followup_turn(router)
    await router.on_turn(_turn("started", turn=3, reason="unknown"))
    assert router.current is not None and router.current.reply_to == USER_REF
    await router.on_turn(
        _turn(
            "completed",
            turn=3,
            reason="task_finished",
            ok=True,
            answer="report",
            detail={
                "tasks": ["bg b2"],
                "task_ids": ["b2"],
                "origin_turn": 2,
                "retro_attributed": True,
            },
        )
    )
    assert rec.delivered[-1][5] == 20


@pytest.mark.parametrize("detail", [{}, {"origin_turn": 9}, {"origin_turn": True}])
async def test_795_unknown_origin_falls_back_to_the_default(detail: dict) -> None:
    rec = _Recorder()
    router = _router(rec)
    await router.on_turn(_turn("started", turn=2, detail=detail))
    await router.on_turn(_turn("completed", turn=2, ok=True, answer="x", detail=detail))
    assert rec.delivered[-1][5] == 10


async def test_785_thinking_note_alone_opens_no_progress_when_filtered() -> None:
    """#785 part 2: with consolidation on, a wake turn's thinking note must
    not open a progress message (it may fold into the status message); a
    tool call still does."""
    rec = _Recorder()
    router = rb.FollowupTurnRouter(
        new_tracker=lambda: ProgressTracker(engine="claude"),
        create_progress=rec.create,
        close_progress=rec.close,
        deliver=rec.deliver,
        default_reply_to=USER_REF,
        followup_notify=False,
        anchor_for=None,
        progress_for=lambda evt: evt.action.kind != "note",
    )
    await router.on_turn(_turn("started", detail={"tasks": ["build"]}))
    note = ActionEvent(
        engine="claude",
        action=Action(id="claude.thinking.1", kind="note", title="hmm"),
        phase="completed",
        ok=True,
    )
    await router.on_event(note)
    assert rec.created == []
    assert router.current is not None
    assert router.current.tracker.action_count == 1  # still tracked
    await router.on_event(_action())
    assert rec.created == [2]


# ── #812: hook feedback turns ──────────────────────────────────────────────


async def test_812_hook_rewake_header_pushes_never_folds() -> None:
    """An asyncRewake hook's findings (security reviews) are always their own
    pushed message with a 🪝 header — never folded into a panel line (D-4)."""
    from untether.background_status import FOLDABLE_REASONS, wake_fold_decision

    assert "hook_rewake" not in FOLDABLE_REASONS
    assert "hook_rewake" in rb._TURN_PUSH_REASONS
    # Even a short, tool-free, ok answer — the exact shape that folds for
    # task_finished — does not fold.
    decision = wake_fold_decision(
        reason="hook_rewake",
        ok=True,
        answer="ok",
        substantive_actions=0,
        already_announced=False,
        live_tasks_remaining=0,
        batch_announced=True,
    )
    assert decision != "fold"

    rec = _Recorder()
    router = _router(rec)
    detail = {"hook": "Stop", "hook_event": "Stop"}
    await router.on_turn(_turn("started", reason="hook_rewake", detail=detail))
    await router.on_turn(
        _turn(
            "completed", reason="hook_rewake", ok=True, answer="finding", detail=detail
        )
    )
    _turn_no, ok, answer, header, notify, _reply = rec.delivered[0]
    assert (ok, answer) == (True, "finding")
    assert header == "\N{HOOK} Hook feedback — Stop"
    assert notify is True


async def test_812_retro_attributed_hook_rewake_gets_its_header() -> None:
    rec = _Recorder()
    router = _router(rec)
    await router.on_turn(_turn("started", reason="unknown"))
    await router.on_turn(
        _turn(
            "completed",
            reason="hook_rewake",
            ok=True,
            answer="finding",
            detail={"hook": "Stop", "hook_event": "Stop", "retro_attributed": True},
        )
    )
    _turn_no, _ok, _answer, header, notify, _reply = rec.delivered[0]
    assert header == "\N{HOOK} Hook feedback — Stop"
    assert notify is True


def test_812_hook_rewake_header_without_event() -> None:
    assert rb._turn_header(_turn("started", reason="hook_rewake")) == (
        "\N{HOOK} Hook feedback"
    )


_NOT_REPLANNED = (
    "\N{WARNING SIGN}\N{VARIATION SELECTOR-16} Not re-planned: the approved"
    " plan's background agents are still running. Plan mode resumes when they"
    " finish."
)


@pytest.mark.parametrize(
    ("reason", "detail", "expected"),
    [
        # A follow-up has no header of its own: the line is the header.
        ("followup", {"plan_deferred": {"agents": 1}}, _NOT_REPLANNED),
        (
            "task_finished",
            {"tasks": ["lint"], "plan_deferred": {"agents": 2}},
            "\N{BELL} Background task finished — lint\n" + _NOT_REPLANNED,
        ),
        (
            "monitor_event",
            {"plan_deferred": {"agents": 1}},
            "\N{SATELLITE ANTENNA} Monitor\n" + _NOT_REPLANNED,
        ),
        # Without the flag, headers are unchanged.
        ("followup", {}, None),
        (
            "task_finished",
            {"tasks": ["lint"]},
            "\N{BELL} Background task finished — lint",
        ),
    ],
)
def test_383_turn_header_shows_not_replanned_line(
    reason: str, detail: dict, expected: str | None
) -> None:
    """#383 C4: a turn that runs unplanned because the approved plan's
    agents are still working says so under its header."""
    assert rb._turn_header(_turn("started", reason=reason, detail=detail)) == expected


async def test_383_deferred_followup_final_carries_the_line() -> None:
    rec = _Recorder()
    anchor = MessageRef(channel_id=1, message_id=55)
    router = _router(rec, anchors={"cmd-1": (anchor, None)})
    detail = {"plan_deferred": {"agents": 1}}
    await router.on_turn(
        _turn("started", reason="followup", command_uuid="cmd-1", detail=detail)
    )
    await router.on_turn(
        _turn(
            "completed",
            reason="followup",
            command_uuid="cmd-1",
            ok=True,
            answer="12:04",
            detail=detail,
        )
    )
    _turn_no, _ok, _answer, header, _notify, reply = rec.delivered[0]
    assert header == _NOT_REPLANNED
    assert reply == 55


_HG = "\N{HOURGLASS WITH FLOWING SAND} Closing session — "


@pytest.mark.parametrize(
    ("hooks", "count", "expected"),
    [
        (
            ["Stop"],
            None,
            f"{_HG}a background hook (Stop) was still running; its feedback "
            "wasn't delivered.",
        ),
        (
            ["Stop", "PostToolUse"],
            None,
            f"{_HG}2 background hooks (Stop, PostToolUse) were still running; "
            "their feedback wasn't delivered.",
        ),
        # #812: one live hook process among candidates of two events — one
        # hook that could be either, never "2 hooks".
        (
            ["Stop", "UserPromptSubmit"],
            1,
            f"{_HG}a background hook (Stop or UserPromptSubmit) was still "
            "running; its feedback wasn't delivered.",
        ),
        (
            ["Stop"],
            1,
            f"{_HG}a background hook (Stop) was still running; its feedback "
            "wasn't delivered.",
        ),
        (
            ["PostToolUse", "Stop", "UserPromptSubmit"],
            1,
            f"{_HG}a background hook (PostToolUse, Stop or UserPromptSubmit) "
            "was still running; its feedback wasn't delivered.",
        ),
        (
            ["Stop"],
            2,
            f"{_HG}2 background hooks (Stop) were still running; their "
            "feedback wasn't delivered.",
        ),
    ],
)
def test_812_closing_notice_hooks_variant(
    hooks: list[str], count: int | None, expected: str
) -> None:
    assert rb._live_closing_notice("idle_no_tasks", [], hooks, count) == expected


def test_812_closing_notice_hooks_and_tasks_both_named() -> None:
    text = rb._live_closing_notice("max_hold", ["a"], ["Stop"])
    first, second = text.split("\n")
    assert "background hook (Stop)" in first
    assert second == rb._live_closing_notice("max_hold", ["a"])


# ── #815: a turn's final header times it from when the CLI started it ─────


async def _timed_turn_final(*turn_steps: Emit, answer: str) -> str:
    """Run a live session whose first result lands at t=100 on a fake clock,
    then ``turn_steps`` (which move the clock via ``Emit(at=…)``); return the
    turn's final text."""
    clock = _FakeClock(start=100.0)
    first = CompletedEvent(engine="claude", resume=_TOKEN, ok=True, answer="FIRST")
    transport = FakeTransport()
    runner = ScriptRunner(
        [
            Emit(StartedEvent(engine="claude", resume=_TOKEN), at=100.0),
            Emit(first, at=100.0),
            *turn_steps,
        ],
        engine="claude",
        resume_value=_TOKEN.value,
        advance=clock.set,
    )
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=True
    )
    await handle_message(
        cfg,
        runner=runner,
        incoming=IncomingMessage(channel_id=1, message_id=10, text="go"),
        resume_token=None,
        clock=clock,
    )
    calls = [*transport.send_calls, *transport.edit_calls]
    finals = [c["message"].text for c in calls if answer in c["message"].text]
    assert len(finals) == 1
    return finals[0]


async def test_815_tool_free_followup_turn_header_shows_its_real_elapsed() -> None:
    """The CLI announced the follow-up 7 s before its first frame (the
    answer itself, for a tool-free turn): the header must say 7s, not 0s."""
    final = await _timed_turn_final(
        Emit(_turn("started", reason="followup", started_ago_s=7.0), at=200.0),
        Emit(
            _turn("completed", reason="followup", ok=True, answer="TOOL-FREE"),
            at=200.0,
        ),
        answer="TOOL-FREE",
    )
    assert "· 7s" in final
    assert "· 0s" not in final
    assert final.count(TURN_COMPLETE_MARKER) == 1


async def test_815_tool_using_turn_elapsed_unchanged() -> None:
    final = await _timed_turn_final(
        Emit(_turn("started", reason="followup"), at=200.0),
        Emit(_action(), at=201.0),
        Emit(
            _turn("completed", reason="followup", ok=True, answer="WITH-TOOL"),
            at=205.0,
        ),
        answer="WITH-TOOL",
    )
    assert "· 5s" in final
    assert final.count(TURN_COMPLETE_MARKER) == 1


# ── #819: context-window use in turn headers ───────────────────────────────


def _telemetry(pct: int | None) -> ActionEvent:
    return ActionEvent(
        engine="claude",
        action=Action(
            id="claude.context",
            kind="telemetry",
            title="context",
            detail={"context_pct": pct},
        ),
        phase="updated",
    )


async def test_819_telemetry_does_not_force_turn_progress() -> None:
    """A status-line value never opens a wake turn's progress message (the
    lazy progress and #785 folding stay intact) but is still noted."""
    from untether.background_status import count_substantive_actions

    rec = _Recorder()
    router = _router(rec)
    await router.on_turn(_turn("started", detail={"tasks": ["build"]}))
    await router.on_event(_telemetry(30))
    assert rec.created == []
    assert router.current is not None
    tracker = router.current.tracker
    assert tracker.context_pct == 30
    assert tracker.action_count == 0
    assert count_substantive_actions(a.action for a in tracker.snapshot().actions) == 0
    await router.on_event(_action())
    assert rec.created == [2]


async def test_819_turn_final_header_shows_context_pct() -> None:
    transport, run_progress = await _run_with_turn(
        Emit(_turn("started", reason="followup")),
        Emit(_telemetry(30)),
        Emit(_action()),
        Emit(_telemetry(34)),
        Emit(_turn("completed", reason="followup", ok=True, answer="TURN-2-ANSWER")),
    )
    final, progress = _turn_texts(transport, run_progress, "TURN-2-ANSWER")
    header = final.splitlines()[0]
    assert header.endswith("· 34% ctx")
    assert "ctx" not in final.split("\n", 1)[1].replace("TURN-2-ANSWER", "")
    assert any("% ctx" in t.splitlines()[0] for t in progress)


def test_819_export_skips_telemetry_keeps_other_actions(monkeypatch) -> None:
    recorded: list[dict] = []
    monkeypatch.setattr(
        "untether.telegram.commands.export.record_session_event",
        lambda session_id, event, channel_id=0: recorded.append(event),
    )
    rb._record_export_event(_telemetry(40), _TOKEN)
    assert recorded == []
    rb._record_export_event(_action(), _TOKEN)
    assert [e["action"]["kind"] for e in recorded] == ["command"]


async def test_router_refreshes_progress_settings_at_each_turn_start() -> None:
    """rc15 integration finding (R15-19f): a hot-reloaded ``[progress]``
    toggle reached only the next spawned run; follow-up turns of a live
    session kept the old value. The router now fires a hook per turn start."""
    rec = _CancelRecorder()
    starts: list[int] = []
    router = rb.FollowupTurnRouter(
        new_tracker=lambda: ProgressTracker(engine="claude"),
        create_progress=rec.create,
        close_progress=rec.close,
        deliver=rec.deliver,
        default_reply_to=USER_REF,
        followup_notify=False,
        anchor_for={}.get,
        on_turn_started=lambda: starts.append(1),
    )
    await router.on_turn(_turn("started", turn=2, reason="followup"))
    await router.on_turn(_turn("completed", turn=2, reason="followup"))
    await router.on_turn(_turn("started", turn=3, reason="followup"))
    assert len(starts) == 2


def test_refresh_progress_settings_reaches_a_verbose_override(monkeypatch) -> None:
    from types import SimpleNamespace

    from untether.markdown import MarkdownFormatter
    from untether.telegram.bridge import TelegramPresenter

    default = TelegramPresenter(formatter=MarkdownFormatter(show_context_usage=True))
    override = TelegramPresenter(
        formatter=MarkdownFormatter(verbosity="verbose", show_context_usage=True)
    )
    monkeypatch.setattr(
        rb,
        "_load_progress_settings",
        lambda: SimpleNamespace(
            max_actions=3, verbosity="compact", show_context_usage=False
        ),
    )
    rb._refresh_progress_settings(default, override)
    assert default._formatter.show_context_usage is False
    assert override._formatter.show_context_usage is False
    assert override._formatter.max_actions == 3
    assert override._formatter.verbosity == "verbose"  # the override's own


# ── #418: /export records every turn of a live session ──────────────────────


def _turn_action(turn: int) -> ActionEvent:
    return ActionEvent(
        engine="claude",
        action=Action(id=f"toolu_t{turn}", kind="command", title=f"echo turn{turn}"),
        phase="completed",
        ok=True,
    )


async def test_418_export_records_every_live_turn() -> None:
    """Live finding: after one run plus three injected follow-ups (four
    turns), /export held only the first run's events — the follow-up and
    wake turns' events went to the turn router before the export recorder."""
    from untether.telegram.commands import export as export_mod

    export_mod._SESSION_HISTORY.clear()
    usage = {"total_cost_usd": 0.42, "num_turns": 9}
    steps: list[Emit] = [Emit(_turn_action(1))]
    for turn, reason in ((2, "followup"), (3, "followup"), (4, "task_finished")):
        steps += [
            Emit(_turn("started", turn=turn, reason=reason)),
            Emit(_turn_action(turn)),
            Emit(
                _turn(
                    "completed",
                    turn=turn,
                    reason=reason,
                    ok=True,
                    answer=f"ANSWER-{turn}",
                    resume=_TOKEN,
                    **({"usage": usage} if turn == 4 else {}),
                )
            ),
        ]
    await _run_with_turn(*steps, end_mid_turn=True)

    _ts, events, recorded_usage = export_mod._SESSION_HISTORY[(1, _TOKEN.value)]
    action_ids = [e["action"]["id"] for e in events if e["type"] == "action"]
    # Every turn's action, each exactly once (no double recording).
    assert action_ids == ["toolu_t1", "toolu_t2", "toolu_t3", "toolu_t4"]
    answers = [e["answer"] for e in events if e["type"] == "completed"]
    assert answers[:1] == ["FIRST"]
    assert [a for a in answers if a.startswith("ANSWER-")] == [
        "ANSWER-2",
        "ANSWER-3",
        "ANSWER-4",
    ]
    turns = [(e["turn"], e["reason"]) for e in events if e["type"] == "turn"]
    assert turns == [(2, "followup"), (3, "followup"), (4, "task_finished")]
    # The latest result's usage (session-cumulative for the live process).
    assert recorded_usage == usage

    md = export_mod._format_export_markdown(_TOKEN.value, events, recorded_usage)
    for needle in (
        "FIRST",
        "ANSWER-2",
        "ANSWER-3",
        "ANSWER-4",
        "echo turn4",
        "## Turn 2 (follow-up)",
        "## Turn 4 (background task finished)",
    ):
        assert needle in md
    export_mod._SESSION_HISTORY.clear()


# ── #890: repeat usage-limit errors from wake turns coalesce ────────────────

_CAP = "You've hit your session limit · resets 5:30pm (Australia/Melbourne)"


def _capped_turn(
    turn: int,
    *,
    reason: str = "task_finished",
    head: str = _CAP,
    latched: bool = True,
) -> list[Emit]:
    usage: dict = {"num_turns": 1, "total_cost_usd": 45.25}
    if latched:
        usage["usage_limit_latched"] = True
    return [
        Emit(
            _turn("started", turn=turn, reason=reason, detail={"tasks": [f"t{turn}"]})
        ),
        Emit(
            _turn(
                "completed",
                turn=turn,
                reason=reason,
                ok=False,
                answer="",
                error=f"{head}\nsession: 681bd6d5 · live turn {turn} · turns: 1",
                resume=_TOKEN,
                usage=usage,
            )
        ),
    ]


def _cap_messages(transport: FakeTransport, needle: str = "hit your session limit"):
    return [c for c in transport.send_calls if needle in c["message"].text]


async def test_890_repeat_capped_wake_errors_fold_into_the_first() -> None:
    """#890: once the limit is latched, each later background wake fails at
    once with the same message — one error final, edited with a counter, not
    one push per wake."""
    transport, _ = await _run_with_turn(
        *_capped_turn(2), *_capped_turn(3), *_capped_turn(4), end_mid_turn=True
    )
    sent = _cap_messages(transport)
    assert len(sent) == 1
    ref = sent[0]["ref"]
    edits = [c["message"].text for c in transport.edit_calls if c["ref"] == ref]
    assert edits, "the first error final was never updated"
    assert "+1 more background wake-up hit the same limit" in edits[0]
    assert "+2 more background wake-ups hit the same limit" in edits[-1]
    # The first error's own text survives the edits.
    assert "hit your session limit" in edits[-1]
    assert "Background task finished" in edits[-1]


async def test_823_capped_repeat_counter_edit_names_final_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#823: the #890 counter edit rewrites an error *final*, so a failed
    edit (e.g. the user deleted that message) must log ``kind=final``, not
    ``kind=None``."""
    from tests import telegram_fakes
    from untether.transport import current_message_kind

    kinds: list[tuple[MessageRef, str | None]] = []
    base_edit = telegram_fakes.FakeTransport.edit

    async def edit(self, *, ref, message, wait=True):  # type: ignore[no-untyped-def]
        kinds.append((ref, current_message_kind()))
        return await base_edit(self, ref=ref, message=message, wait=wait)

    monkeypatch.setattr(telegram_fakes.FakeTransport, "edit", edit)
    transport, _ = await _run_with_turn(
        *_capped_turn(2), *_capped_turn(3), end_mid_turn=True
    )
    ref = _cap_messages(transport)[0]["ref"]
    counter_kinds = [kind for edited, kind in kinds if edited == ref]
    assert counter_kinds, "the first error final was never updated"
    assert set(counter_kinds) == {"final"}


async def test_890_consolidation_off_keeps_one_message_per_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``consolidate_wake_turns = false`` (the #785 kill switch) keeps the
    rc12 delivery: every capped wake error is its own message."""
    import untether.runner_bridge as bridge_mod
    from untether.settings import ProgressSettings

    settings = ProgressSettings(consolidate_wake_turns=False)
    monkeypatch.setattr(bridge_mod, "_load_progress_settings", lambda: settings)
    transport, _ = await _run_with_turn(
        *_capped_turn(2), *_capped_turn(3), end_mid_turn=True
    )
    assert len(_cap_messages(transport)) == 2


async def test_890_different_or_unlatched_errors_get_their_own_message() -> None:
    """Only the same latched limit message coalesces: another error, an
    unlatched error and a user's follow-up all keep their own final."""
    other = "You've hit your weekly limit · resets 9:00am (Australia/Melbourne)"
    transport, _ = await _run_with_turn(
        *_capped_turn(2),
        *_capped_turn(3, head=other),
        *_capped_turn(4, latched=False),
        *_capped_turn(5, reason="followup"),
        end_mid_turn=True,
    )
    assert len(_cap_messages(transport)) == 3
    assert len(_cap_messages(transport, "weekly limit")) == 1
    assert not any(
        "more background wake-up" in c["message"].text for c in transport.edit_calls
    )


def _ok_wake_turn(turn: int, *, reason: str = "monitor_event") -> list[Emit]:
    return [
        Emit(_turn("started", turn=turn, reason=reason)),
        Emit(
            _turn(
                "completed",
                turn=turn,
                reason=reason,
                ok=True,
                answer="Still waiting.",
                resume=_TOKEN,
                usage={"num_turns": 1},
            )
        ),
    ]


async def test_890_successful_wake_that_folds_starts_afresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wake turn that got through means the limit lifted, even when its
    short ack folds into the background panel: a later cap is a fresh
    (pushed) error, not a counter edited into the hours-old one."""
    from types import SimpleNamespace

    import untether.runner_bridge as bridge_mod

    target = SimpleNamespace(breakouts=0)
    folded: list[str] = []

    async def fake_fold(self, text: str, **_kw) -> bool:
        folded.append(text)
        return True

    monkeypatch.setattr(
        bridge_mod.BackgroundStatusManager,
        "fold_target",
        property(lambda self: target),
    )
    monkeypatch.setattr(bridge_mod.BackgroundStatusManager, "fold", fake_fold)
    transport, _ = await _run_with_turn(
        *_capped_turn(2), *_ok_wake_turn(3), *_capped_turn(4), end_mid_turn=True
    )
    assert folded == ["Still waiting."]
    assert len(_cap_messages(transport)) == 2
    assert not any(
        "more background wake-up" in c["message"].text for c in transport.edit_calls
    )


async def test_890_cancelled_wake_turn_starts_afresh() -> None:
    """A cancelled turn also ends the run of repeats: the next cap gets its
    own message."""
    transport, _ = await _run_with_turn(
        *_capped_turn(2),
        Emit(_turn("started", turn=3, reason="task_finished")),
        Emit(
            _turn(
                "completed",
                turn=3,
                reason="task_finished",
                ok=True,
                answer="PARTIAL",
                resume=_TOKEN,
                usage={"terminal_reason": "aborted_tools"},
            )
        ),
        *_capped_turn(4),
        end_mid_turn=True,
    )
    assert len(_cap_messages(transport)) == 2
    assert not any(
        "more background wake-up" in c["message"].text for c in transport.edit_calls
    )


# ── #928: the CLI's no-query results never reach Telegram ──────────────────

from structlog.testing import capture_logs  # noqa: E402

from untether.model import ResumeToken  # noqa: E402


async def test_928_router_drops_no_query_turn_silently() -> None:
    rec = _Recorder()
    router = _router(rec)
    await router.on_turn(_turn("started", 3, "unknown"))
    assert router.current is not None
    with capture_logs() as logs:
        await router.on_turn(_turn("completed", 3, "no_query", ok=True, answer=""))
    assert rec.delivered == []
    assert rec.created == []
    assert rec.closed == [3]
    assert router.current is None
    assert router.turns_delivered == 0
    (dropped,) = [e for e in logs if e["event"] == "live_turn.no_query_dropped"]
    assert dropped["log_level"] == "debug"
    assert dropped["matched"] is True and dropped["had_progress"] is False


async def test_928_router_no_query_without_open_turn_is_ignored() -> None:
    rec = _Recorder()
    router = _router(rec)
    await router.on_turn(_turn("completed", 4, "no_query", ok=True, answer=""))
    assert rec.delivered == []
    assert rec.closed == []
    assert router.current is None
    assert router.turns_delivered == 0


async def test_928_router_no_query_closes_created_progress() -> None:
    rec = _Recorder()
    router = _router(rec)
    await router.on_turn(_turn("started", 3, "unknown"))
    await router.on_event(_action())  # forces the progress message
    assert rec.created == [3]
    with capture_logs() as logs:
        await router.on_turn(_turn("completed", 3, "no_query", ok=True, answer=""))
    assert rec.closed == [3]
    assert rec.delivered == []
    (dropped,) = [e for e in logs if e["event"] == "live_turn.no_query_dropped"]
    assert dropped["had_progress"] is True


@pytest.mark.parametrize("opened_as", ["hook_rewake", "unknown"])
async def test_928_no_empty_hook_rewake_reaches_telegram(opened_as: str) -> None:
    """Review amendment 1: a no-query tail that ``init`` opened on a hook
    hint must never become an empty pushed ``🪝 Hook feedback``."""
    rec = _Recorder()
    router = _router(rec)
    await router.on_turn(
        _turn("started", 3, opened_as, detail={"hook_event": "PostToolUse"})
    )
    await router.on_turn(_turn("completed", 3, "no_query", ok=True, answer=""))
    assert rec.delivered == []
    # The real rewake turn that follows still pushes.
    await router.on_turn(
        _turn("started", 4, "hook_rewake", detail={"hook_event": "Stop"})
    )
    await router.on_turn(
        _turn("completed", 4, "hook_rewake", ok=True, answer="finding")
    )
    assert len(rec.delivered) == 1
    turn, ok, answer, header, notify, _ = rec.delivered[0]
    assert (turn, ok, answer, notify) == (4, True, "finding", True)
    assert header is not None and "Hook feedback" in header


def test_928_export_drops_the_no_query_turn() -> None:
    from untether.telegram.commands.export import _format_export_markdown

    events = [
        {"type": "started", "engine": "claude", "title": "m"},
        {"type": "completed", "ok": True, "answer": "first"},
        {"type": "turn", "phase": "started", "turn": 2, "reason": "unknown"},
        {"type": "turn_dropped", "turn": 2, "reason": "no_query"},
        {"type": "turn", "phase": "started", "turn": 3, "reason": "task_finished"},
        {"type": "completed", "ok": True, "answer": "a2 done", "turn": 3},
    ]
    text = _format_export_markdown("sid", events, None)
    assert "## Turn 2" not in text
    assert "## Turn 3 (background task finished)" in text
    assert text.count("✓ Completed") == 2


def test_928_record_export_event_marks_no_query_dropped(monkeypatch) -> None:
    recorded: list[dict] = []
    from untether.telegram.commands import export as export_mod

    monkeypatch.setattr(
        export_mod,
        "record_session_event",
        lambda sid, evt, **kw: recorded.append(evt),
    )
    resume = ResumeToken(engine="claude", value="sid")
    rb._record_export_event(
        _turn("completed", 3, "no_query", ok=True, answer=""), resume
    )
    assert recorded == [{"type": "turn_dropped", "turn": 3, "reason": "no_query"}]
