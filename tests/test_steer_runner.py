"""#775: steer mode against a REAL ``ClaudeRunner`` and the protocol fake.

A steer is written into the live session immediately (``steer_into_session``)
instead of being held until the turn ends. Written while a tool runs, the CLI
folds it into the same turn (probe F5) — one result, plus a "steer received"
row; written after the turn's last tool call it becomes the next turn (F6),
which #776 delivers as a follow-up turn.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import anyio
import pytest
from structlog.testing import capture_logs

from tests.test_live_session_runner import (  # noqa: F401 — fixtures
    SID,
    _clean_env,
    _collect,
    _turns,
)
from untether.model import ActionEvent, CompletedEvent
from untether.runners import claude as claude_mod
from untether.runners.claude import ClaudeRunner, steer_into_session

pytestmark = pytest.mark.anyio


def _absorbed(events: list[Any]) -> list[ActionEvent]:
    return [
        e
        for e in events
        if isinstance(e, ActionEvent)
        and e.phase == "completed"
        and (e.action.detail or {}).get("absorbed_command_uuid")
    ]


async def test_mid_tool_steer_folds_into_the_same_turn() -> None:
    cmd = str(uuid.uuid4())
    outcomes: list[str] = []

    async def steer(evt: Any, runner: ClaudeRunner) -> None:
        if (
            isinstance(evt, ActionEvent)
            and evt.phase == "started"
            and evt.action.id == "toolu_sleep"
        ):
            outcomes.append(
                await steer_into_session(SID, "also the hostname", command_uuid=cmd)
            )

    with capture_logs() as logs:
        events = await _collect("steer_mid_tool", until=1, on_event=steer)
    assert outcomes == ["steered"]
    completed = [e for e in events if isinstance(e, CompletedEvent)]
    assert len(completed) == 1
    assert completed[0].answer == "DONE + also the hostname"
    # Folded, not a second turn.
    assert _turns(events) == []
    absorbed = _absorbed(events)
    assert len(absorbed) == 1
    assert absorbed[0].action.detail["absorbed_command_uuid"] == cmd
    assert absorbed[0].action.detail["steer"] is True
    assert "steer received: also the hostname" in absorbed[0].action.title
    # The steer row lands inside the turn, before its result.
    assert events.index(absorbed[0]) < events.index(completed[0])
    assert any(e["event"] == "claude.live_session.steered" for e in logs)
    assert any(e["event"] == "claude.live_session.injected_absorbed" for e in logs)


async def test_mid_tool_steer_clears_awaiting_marker() -> None:
    """A folded steer must not hold the idle close / next follow-up for the
    120 s injected-turn timeout (its turn never opens)."""
    cmd = str(uuid.uuid4())
    seen: dict[str, Any] = {}

    async def steer(evt: Any, runner: ClaudeRunner) -> None:
        if (
            isinstance(evt, ActionEvent)
            and evt.action.id == "toolu_sleep"
            and evt.phase == "started"
        ):
            await steer_into_session(SID, "x", command_uuid=cmd)
            seen["after_write"] = dict(
                claude_mod._SESSION_BG_STATE[SID].awaiting_injected
            )
        if isinstance(evt, CompletedEvent):
            seen["at_result"] = dict(
                claude_mod._SESSION_BG_STATE[SID].awaiting_injected
            )

    await _collect("steer_mid_tool", until=1, on_event=steer)
    assert cmd in seen["after_write"]
    assert seen["at_result"] == {}


async def test_several_mid_tool_steers_keep_their_order() -> None:
    ids = [str(uuid.uuid4()) for _ in range(3)]

    async def steer(evt: Any, runner: ClaudeRunner) -> None:
        if (
            isinstance(evt, ActionEvent)
            and evt.phase == "started"
            and evt.action.id == "toolu_sleep"
        ):
            for n, cmd in enumerate(ids, start=1):
                assert (
                    await steer_into_session(SID, f"s{n}", command_uuid=cmd)
                    == "steered"
                )

    events = await _collect("steer_mid_tool", until=1, on_event=steer)
    completed = [e for e in events if isinstance(e, CompletedEvent)]
    assert completed[0].answer == "DONE + s1 | s2 | s3"
    assert [a.action.detail["absorbed_command_uuid"] for a in _absorbed(events)] == ids


async def test_post_last_tool_steer_becomes_a_followup_turn() -> None:
    cmd = str(uuid.uuid4())

    async def steer(evt: Any, runner: ClaudeRunner) -> None:
        if (
            isinstance(evt, ActionEvent)
            and evt.phase == "completed"
            and evt.action.id == "toolu_echo"
        ):
            assert (
                await steer_into_session(SID, "and a haiku", command_uuid=cmd)
                == "steered"
            )

    events = await _collect("steer_post_last_tool", until=2, on_event=steer)
    completed = [e for e in events if isinstance(e, CompletedEvent)]
    assert len(completed) == 1 and completed[0].answer == "FIRST"
    start, end = _turns(events)
    assert start.reason == "followup" and start.command_uuid == cmd
    assert end.answer == "ECHO: and a haiku" and end.ok is True
    # Not absorbed: it got a turn (and so its own reply) of its own.
    assert _absorbed(events) == []


async def test_steer_window_closed_falls_back() -> None:
    cmd = str(uuid.uuid4())
    outcomes: list[str] = []

    async def steer(evt: Any, runner: ClaudeRunner) -> None:
        if (
            isinstance(evt, ActionEvent)
            and evt.phase == "started"
            and evt.action.id == "toolu_sleep"
        ):
            assert await claude_mod.close_steer_window(SID, "cancel")
            outcomes.append(await steer_into_session(SID, "late", command_uuid=cmd))

    os.environ["FAKE_CLAUDE_STEER_WAIT_S"] = "0.2"
    try:
        events = await _collect("steer_mid_tool", until=1, on_event=steer)
    finally:
        os.environ.pop("FAKE_CLAUDE_STEER_WAIT_S", None)
    assert outcomes == ["window_closed"]
    completed = [e for e in events if isinstance(e, CompletedEvent)]
    assert completed[0].answer == "DONE"


async def test_steer_without_live_session() -> None:
    assert (
        await steer_into_session("no-such-session", "x", command_uuid="u")
        == "no_live_session"
    )


async def test_close_steer_window_without_live_session() -> None:
    assert await claude_mod.close_steer_window("no-such-session", "cancel") is False


async def test_steer_cannot_race_a_closing_session() -> None:
    """The window check runs under ``LiveSession.lock``: once
    ``close_live_session`` has flipped ``closing`` the steer never writes."""
    from untether.runners.claude import ClaudeStreamState, LiveSession

    class _Pipe:
        def __init__(self) -> None:
            self.sent: list[bytes] = []

        async def send(self, data: bytes) -> None:
            self.sent.append(data)

        async def aclose(self) -> None:
            pass

    pipe = _Pipe()
    state = ClaudeStreamState()
    live = LiveSession(session_id="sid-race", state=state, stdin=pipe)
    claude_mod._LIVE_SESSIONS["sid-race"] = live
    claude_mod._SESSION_STDIN["sid-race"] = pipe
    claude_mod._SESSION_BG_STATE["sid-race"] = state
    try:
        outcomes: list[str] = []
        async with anyio.create_task_group() as tg:
            await live.lock.acquire()  # a close is mid-flight
            tg.start_soon(lambda: _steer_into(outcomes, "sid-race", "late", "u-race"))
            await anyio.sleep(0.05)
            live.closing = True
            live.lock.release()
        assert outcomes == ["window_closed"]
        assert pipe.sent == []
        assert state.steered_commands == {}
    finally:
        for reg in (
            claude_mod._LIVE_SESSIONS,
            claude_mod._SESSION_STDIN,
            claude_mod._SESSION_BG_STATE,
        ):
            reg.pop("sid-race", None)


async def _steer_into(out: list[str], sid: str, text: str, cmd: str) -> None:
    out.append(await steer_into_session(sid, text, command_uuid=cmd))
