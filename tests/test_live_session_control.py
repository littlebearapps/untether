"""#776 phase 07: control paths (cancel, drain, busy) with live sessions."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from tests.telegram_fakes import FakeTransport
from tests.test_live_session_harness import _drive, _watchdog
from untether import runner_bridge as rb
from untether.model import ResumeToken
from untether.runner_bridge import (
    RunningTask,
    close_idle_live_sessions,
    running_task_is_live_idle,
    unique_running_tasks,
)
from untether.runners import claude as claude_mod
from untether.runners.claude import ClaudeStreamState, LiveSession
from untether.transport import MessageRef

pytestmark = pytest.mark.anyio


def _task(*, live: bool, idle: bool, sid: str = "s-ctl") -> RunningTask:
    state = ClaudeStreamState()
    state.live_mode = live
    state.completed_turns = 1
    state.turn_open = not idle
    task = RunningTask(
        edits=SimpleNamespace(stream=SimpleNamespace(engine_state=state))  # type: ignore[arg-type]
    )
    task.resume = ResumeToken(engine="claude", value=sid)
    return task


def test_unique_running_tasks_dedupes_turn_aliases() -> None:
    task = _task(live=True, idle=False)
    other = _task(live=False, idle=False, sid="s2")
    running = {
        MessageRef(channel_id=1, message_id=1): task,
        MessageRef(channel_id=1, message_id=7): task,  # a turn's alias
        MessageRef(channel_id=1, message_id=9): other,
    }
    assert [t for _, t in unique_running_tasks(running)] == [task, other]


@pytest.mark.parametrize(
    ("live", "idle", "expected"),
    [(True, True, True), (True, False, False), (False, True, False)],
)
def test_running_task_is_live_idle(live: bool, idle: bool, expected: bool) -> None:
    assert running_task_is_live_idle(_task(live=live, idle=idle)) is expected


async def test_close_idle_live_sessions_closes_only_idle() -> None:
    class _Pipe:
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    idle_pipe, busy_pipe = _Pipe(), _Pipe()
    idle_task = _task(live=True, idle=True, sid="s-idle")
    busy_task = _task(live=True, idle=False, sid="s-busy")
    for sid, task, pipe in (
        ("s-idle", idle_task, idle_pipe),
        ("s-busy", busy_task, busy_pipe),
    ):
        claude_mod._LIVE_SESSIONS[sid] = LiveSession(
            session_id=sid, state=task.edits.stream.engine_state, stdin=pipe
        )
    try:
        running = {
            MessageRef(channel_id=1, message_id=1): idle_task,
            MessageRef(channel_id=1, message_id=2): busy_task,
        }
        assert await close_idle_live_sessions(running, "drain") == 1
        assert idle_pipe.closed and not busy_pipe.closed
        assert idle_task.edits.stream.engine_state.live_close_reason == "drain"
    finally:
        claude_mod._LIVE_SESSIONS.pop("s-idle", None)
        claude_mod._LIVE_SESSIONS.pop("s-busy", None)


async def test_cancel_idle_live_session_closes_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/cancel while the session holds for a background task: stdin closes,
    the CLI stops the task and exits cleanly — no SIGTERM, no quarantine —
    and the user is told which task was stopped."""
    from untether.session_quarantine import get_quarantine_store

    _watchdog(monkeypatch)
    running_tasks: dict[MessageRef, RunningTask] = {}
    streams: list[Any] = []

    async def cancel_when_idle() -> None:
        with anyio.fail_after(20):
            while True:
                tasks = [t for _, t in unique_running_tasks(running_tasks)]
                live = claude_mod.get_live_session("fake-live-session")
                # A human /cancel arrives after the bridge has delivered turn 1
                # and subscribed to lifecycle notices.
                if (
                    tasks
                    and running_task_is_live_idle(tasks[0])
                    and live is not None
                    and live.listeners
                ):
                    streams.append(tasks[0].edits.stream)
                    tasks[0].cancel_requested.set()
                    return
                await anyio.sleep(0.02)

    holder: dict[str, Any] = {}

    async def drive() -> None:
        holder["t"] = await _drive(
            "bg_bash_wake", wake_s=30, running_tasks=running_tasks
        )

    async with anyio.create_task_group() as tg:
        tg.start_soon(cancel_when_idle)
        tg.start_soon(drive)

    stream = streams[0]
    assert stream.engine_state.live_close_reason == "cancel"
    assert stream.sigterm_sent is False
    assert stream.proc_returncode == 0
    assert not get_quarantine_store().is_quarantined("claude", "fake-live-session")
    texts = [c["message"].text for c in holder["t"].send_calls]
    assert any("stopped by /cancel" in t and "bg b1" in t for t in texts)
    assert running_tasks == {}


async def test_handle_cancel_fallback_counts_a_live_run_once() -> None:
    from tests.test_telegram_bridge import make_cfg
    from untether.telegram.commands.cancel import handle_cancel
    from untether.telegram.types import TelegramIncomingMessage

    transport = FakeTransport()
    task = _task(live=True, idle=False)
    running = {
        MessageRef(channel_id=123, message_id=1): task,
        MessageRef(channel_id=123, message_id=5): task,  # follow-up turn alias
    }
    msg = TelegramIncomingMessage(
        transport="telegram",
        chat_id=123,
        message_id=10,
        text="/cancel",
        reply_to_message_id=None,
        reply_to_text=None,
        sender_id=123,
    )
    await handle_cancel(make_cfg(transport), msg, running)
    assert task.cancel_requested.is_set()


def test_rb_exports() -> None:
    assert callable(rb.close_idle_live_sessions)
