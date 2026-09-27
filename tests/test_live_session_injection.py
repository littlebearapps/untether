"""#776 phase 06: follow-ups go into a live Claude session (queue semantics)."""

from __future__ import annotations

import os
import time
from typing import Any

import anyio
import pytest

from tests.test_live_session_harness import _drive, _watchdog
from untether import runner_bridge as rb
from untether.live_followup import inject_live_followup
from untether.model import ResumeToken
from untether.runners import claude as claude_mod
from untether.runners.claude import (
    ClaudeStreamState,
    LiveSession,
    inject_when_idle,
)
from untether.scheduler import ThreadJob, ThreadScheduler
from untether.transport import MessageRef

pytestmark = pytest.mark.anyio

SID = "fake-live-session"


def _job(sid: str = SID, *, engine: str = "claude", text: str = "again") -> ThreadJob:
    return ThreadJob(
        chat_id=123,
        user_msg_id=20,
        text=text,
        resume_token=ResumeToken(engine=engine, value=sid),
        progress_ref=MessageRef(channel_id=123, message_id=99),
    )


# ── scheduler hook ──────────────────────────────────────────────────────────


async def test_scheduler_skips_resume_when_injected() -> None:
    ran: list[ThreadJob] = []

    async def run_job(job: ThreadJob) -> None:
        ran.append(job)

    async def inject(job: ThreadJob) -> bool:
        return True

    async with anyio.create_task_group() as tg:
        sched = ThreadScheduler(task_group=tg, run_job=run_job, inject_job=inject)
        busy = anyio.Event()  # never set: a live owner
        await sched.note_thread_known(_job().resume_token, busy)
        await sched.enqueue(_job())
        await anyio.sleep(0.05)
        tg.cancel_scope.cancel()
    assert ran == []


@pytest.mark.parametrize("outcome", ["false", "raises"])
async def test_scheduler_falls_back_to_resume(outcome: str) -> None:
    ran: list[ThreadJob] = []

    async def run_job(job: ThreadJob) -> None:
        ran.append(job)

    async def inject(job: ThreadJob) -> bool:
        if outcome == "raises":
            raise RuntimeError("boom")
        return False

    async with anyio.create_task_group() as tg:
        sched = ThreadScheduler(task_group=tg, run_job=run_job, inject_job=inject)
        await sched.enqueue(_job())
        await anyio.sleep(0.05)
    assert len(ran) == 1


# ── inject_when_idle ────────────────────────────────────────────────────────


class _Pipe:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def aclose(self) -> None:
        pass


def _install(sid: str, *, idle: bool) -> tuple[LiveSession, _Pipe]:
    pipe = _Pipe()
    state = ClaudeStreamState()
    state.completed_turns = 1
    state.turn_open = not idle
    live = LiveSession(session_id=sid, state=state, stdin=pipe)
    claude_mod._LIVE_SESSIONS[sid] = live
    claude_mod._SESSION_STDIN[sid] = pipe
    claude_mod._SESSION_BG_STATE[sid] = state
    return live, pipe


@pytest.fixture
def cleanup():
    yield
    for reg in (
        claude_mod._LIVE_SESSIONS,
        claude_mod._SESSION_STDIN,
        claude_mod._SESSION_BG_STATE,
    ):
        reg.pop("sid-inj", None)
    rb._FOLLOWUP_ANCHORS.clear()


async def test_followup_during_active_turn_is_held_then_written(cleanup) -> None:
    live, pipe = _install("sid-inj", idle=False)
    result: dict[str, Any] = {}

    async def go() -> None:
        result["ok"] = await inject_when_idle(
            "sid-inj", "hi", command_uuid="u1", poll_s=0.01
        )

    async with anyio.create_task_group() as tg:
        tg.start_soon(go)
        await anyio.sleep(0.1)
        assert pipe.sent == []  # held: the turn is still running (F5)
        live.state.turn_open = False
    assert result["ok"] is True
    assert b'"uuid": "u1"' in pipe.sent[0]
    assert "u1" in live.state.awaiting_injected


async def test_second_followup_waits_for_first_turn_to_start(cleanup) -> None:
    live, pipe = _install("sid-inj", idle=True)
    assert await inject_when_idle("sid-inj", "one", command_uuid="u1", poll_s=0.01)
    done = anyio.Event()

    async def second() -> None:
        await inject_when_idle("sid-inj", "two", command_uuid="u2", poll_s=0.01)
        done.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(second)
        await anyio.sleep(0.1)
        assert len(pipe.sent) == 1  # FIFO: one per turn
        live.state.awaiting_injected.pop("u1")  # u1's turn started and ended
    assert done.is_set() and len(pipe.sent) == 2


async def test_injection_after_closing_falls_back(cleanup) -> None:
    live, pipe = _install("sid-inj", idle=True)
    live.closing = True
    assert (
        await inject_when_idle("sid-inj", "x", command_uuid="u1", poll_s=0.01) is False
    )
    assert pipe.sent == []


async def test_injection_unavailable_without_live_session(cleanup) -> None:
    assert await inject_live_followup(_job("sid-none")) is False
    assert rb._FOLLOWUP_ANCHORS == {}


async def test_non_claude_jobs_never_injected(cleanup) -> None:
    _install("sid-inj", idle=True)
    assert await inject_live_followup(_job("sid-inj", engine="codex")) is False


async def test_awaiting_injected_expires(cleanup) -> None:
    state = ClaudeStreamState()
    state.awaiting_injected["old"] = time.monotonic() - 999
    assert claude_mod._awaiting_injected(state) is False


def test_drain_followup_anchors_returns_leftovers_for_session() -> None:
    ref = MessageRef(channel_id=1, message_id=5)
    ph = MessageRef(channel_id=1, message_id=6)
    rb.register_followup_anchor("a", session_id="s1", reply_to=ref, placeholder=ph)
    rb.register_followup_anchor("b", session_id="s2", reply_to=ref, placeholder=None)
    assert rb.drain_followup_anchors("s1") == [(ref, ph)]
    assert list(rb._FOLLOWUP_ANCHORS) == ["b"]
    rb._FOLLOWUP_ANCHORS.clear()


# ── end to end ──────────────────────────────────────────────────────────────


async def test_followup_to_live_session_is_injected_not_resumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The acceptance case: a follow-up while the session is live lands in
    the same process — one spawn, no resume, the reply under the follow-up's
    own message, the queued placeholder replaced by the answer."""
    _watchdog(monkeypatch, post_result_limbo_grace=1.0)
    resumed: list[ThreadJob] = []

    async def run_job(job: ThreadJob) -> None:
        resumed.append(job)

    spawns = 0
    real_run_impl = claude_mod.ClaudeRunner.run_impl

    def counting_run_impl(self, prompt, resume):
        nonlocal spawns
        spawns += 1
        return real_run_impl(self, prompt, resume)

    monkeypatch.setattr(claude_mod.ClaudeRunner, "run_impl", counting_run_impl)
    holder: dict[str, Any] = {}

    async with anyio.create_task_group() as tg:
        sched = ThreadScheduler(
            task_group=tg, run_job=run_job, inject_job=inject_live_followup
        )

        async def follow_up() -> None:
            with anyio.fail_after(20):
                while True:
                    live = claude_mod.get_live_session(SID)
                    if live is not None and live.idle:
                        break
                    await anyio.sleep(0.02)
            await sched.enqueue(_job())

        async def drive() -> None:
            holder["transport"] = await _drive("followup")

        tg.start_soon(follow_up)
        tg.start_soon(drive)

    transport = holder["transport"]
    assert resumed == []
    assert spawns == 1
    # With final_notify off, the answer edits the "⏳ queued" placeholder in
    # place (the follow-up's own message thread), exactly like a normal run's
    # final edits its progress message.
    placeholder = MessageRef(channel_id=123, message_id=99)
    edits = [c for c in transport.edit_calls if "ECHO: again" in c["message"].text]
    assert edits and edits[-1]["ref"] == placeholder
    assert rb._FOLLOWUP_ANCHORS == {}
    os.environ.pop("FAKE_CLAUDE_SCENARIO", None)


async def test_scheduler_offers_next_job_while_a_live_run_is_in_flight() -> None:
    """Regression (found on @untether_dev_bot): the worker awaited run_job for
    the whole live run, so a follow-up for the same session never reached the
    injector until the session closed and then resumed in a new process."""
    ran: list[str] = []
    injected: list[str] = []
    release = anyio.Event()

    async def run_job(job: ThreadJob) -> None:
        ran.append(job.text)
        if job.text == "first":
            await release.wait()  # a live run: lasts until its session closes

    async def inject(job: ThreadJob) -> bool:
        if job.text == "first":
            return False
        injected.append(job.text)
        return True

    async with anyio.create_task_group() as tg:
        sched = ThreadScheduler(task_group=tg, run_job=run_job, inject_job=inject)
        sched._live_pump_interval_s = 0.01
        await sched.enqueue(_job(text="first"))
        await anyio.sleep(0.05)
        await sched.enqueue(_job(text="second"))
        with anyio.fail_after(2):
            while not injected:
                await anyio.sleep(0.01)
        release.set()
    assert ran == ["first"]
    assert injected == ["second"]


async def test_scheduler_pump_keeps_job_queued_when_not_injectable() -> None:
    ran: list[str] = []
    release = anyio.Event()
    attempts = 0

    async def run_job(job: ThreadJob) -> None:
        ran.append(job.text)
        if job.text == "first":
            await release.wait()

    async def inject(job: ThreadJob) -> bool:
        nonlocal attempts
        if job.text == "second":
            attempts += 1
        return False

    async with anyio.create_task_group() as tg:
        sched = ThreadScheduler(task_group=tg, run_job=run_job, inject_job=inject)
        sched._live_pump_interval_s = 0.01
        await sched.enqueue(_job(text="first"))
        await anyio.sleep(0.03)
        await sched.enqueue(_job(text="second"))
        await anyio.sleep(0.1)
        assert ran == ["first"] and attempts >= 2
        assert sched.queued_for_chat(123)  # still cancellable while queued
        release.set()
    assert ran == ["first", "second"]


async def test_injects_when_chat_options_unchanged(cleanup) -> None:
    from untether.runners.run_options import EngineRunOptions

    live, pipe = _install("sid-inj", idle=True)
    live.state.spawn_run_options = EngineRunOptions(permission_mode="plan")

    async def same(job: ThreadJob) -> EngineRunOptions:
        return EngineRunOptions(permission_mode="plan")

    assert await inject_live_followup(_job("sid-inj"), options_for=same) is True
    assert len(pipe.sent) == 1


async def test_changed_chat_options_close_session_instead_of_injecting(
    cleanup,
) -> None:
    """Found on the dev bot: after /planmode off, a follow-up injected into
    the still-live plan-mode process ran under the old mode."""
    from untether.runners.run_options import EngineRunOptions

    live, pipe = _install("sid-inj", idle=True)
    live.state.spawn_run_options = EngineRunOptions(permission_mode="plan")
    closed: list[bool] = []

    async def aclose() -> None:
        closed.append(True)

    pipe.aclose = aclose  # type: ignore[method-assign]

    async def changed(job: ThreadJob) -> EngineRunOptions:
        return EngineRunOptions(permission_mode="acceptEdits")

    assert await inject_live_followup(_job("sid-inj"), options_for=changed) is False
    assert pipe.sent == []
    assert closed == [True]
    assert live.state.live_close_reason == "options_changed"


def test_options_changed_notice_wording() -> None:
    assert rb._live_closing_notice("options_changed", ["x"]) == (
        "\N{GEAR}\N{VARIATION SELECTOR-16} Settings changed — stopping 1 "
        "background task: x. Your message starts with the new settings."
    )
