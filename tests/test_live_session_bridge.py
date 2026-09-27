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
