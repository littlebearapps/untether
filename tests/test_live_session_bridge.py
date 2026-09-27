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
            "\N{BELL} 2 background tasks finished",
        ),
        ("followup", {}, None),
    ],
)
def test_turn_headers(reason: str, detail: dict, expected: str | None) -> None:
    assert rb._turn_header(_turn("started", reason=reason, detail=detail)) == expected


@pytest.mark.parametrize(
    ("reason", "tasks", "expected"),
    [
        (
            "cancel",
            ["a"],
            "\N{BLACK SQUARE FOR STOP} Stopped 1 background task: a. Reply to continue.",
        ),
        (
            "drain",
            ["a", "b"],
            "\N{HOURGLASS WITH FLOWING SAND} Untether is restarting — stopping 2 background tasks: a, b. Reply to continue.",
        ),
        (
            "max_hold",
            ["a"],
            "\N{HOURGLASS WITH FLOWING SAND} Closing session — 1 background task still running at the background hold limit: a. Stopping it; reply to continue.",
        ),
    ],
)
def test_live_closing_notice_wording(
    reason: str, tasks: list[str], expected: str
) -> None:
    assert rb._live_closing_notice(reason, tasks) == expected


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
