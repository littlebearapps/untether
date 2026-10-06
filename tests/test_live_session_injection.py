"""#776 phase 06: follow-ups go into a live Claude session (queue semantics)."""

from __future__ import annotations

import functools
import os
import time
from typing import Any

import anyio
import pytest
from structlog.testing import capture_logs

from tests.test_live_session_harness import _drive, _watchdog
from untether import runner_bridge as rb
from untether.live_followup import inject_live_followup
from untether.model import TURN_COMPLETE_MARKER, ResumeToken
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
    rb._FOLLOWUP_IN_FLIGHT.clear()


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
    # #798: the injected follow-up's final shows "✓ turn complete" too.
    assert TURN_COMPLETE_MARKER in edits[-1]["message"].text
    assert rb._FOLLOWUP_ANCHORS == {}
    os.environ.pop("FAKE_CLAUDE_SCENARIO", None)


async def test_795_wake_turn_replies_to_the_followup_that_launched_the_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#795: a follow-up injected into the live session launches a background
    task; the task's 🔔 wake message replies to that follow-up (msg 20), not
    to the run's first prompt (msg 10)."""
    _watchdog(monkeypatch, post_result_limbo_grace=1.0)

    async def run_job(job: ThreadJob) -> None:  # pragma: no cover
        raise AssertionError("follow-up should be injected, not resumed")

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
            await sched.enqueue(_job(text="launch it"))

        async def drive() -> None:
            holder["transport"] = await _drive("followup_launches_bg", wake_s=0.5)

        tg.start_soon(follow_up)
        tg.start_soon(drive)

    transport = holder["transport"]
    wake = next(c for c in transport.send_calls if "GOT: B2" in c["message"].text)
    assert "Background task finished — bg b2" in wake["message"].text
    assert wake["options"].reply_to.message_id == 20
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
        # Poll rather than sleep a fixed 0.1s: under full-suite load two pump
        # ticks may not fit in a fixed window.
        with anyio.fail_after(5):
            while attempts < 2:
                await anyio.sleep(0.01)
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


def _retire_claude_level(monkeypatch) -> None:
    """Simulate a future retired Claude reasoning level (live sessions are
    Claude-only, so a stale Codex level can't reach them) (#416)."""
    from untether.telegram import engine_overrides

    monkeypatch.setitem(
        engine_overrides._ENGINE_REASONING_LEVELS, "claude", ("low", "medium", "high")
    )


async def test_stale_reasoning_does_not_fake_options_changed(
    cleanup, monkeypatch
) -> None:
    """#416: spawn options and follow-up options both come from the
    sanitising resolver, so a retired level compares equal."""
    from untether.runners.run_options import EngineRunOptions
    from untether.telegram.engine_overrides import drop_unsupported_reasoning

    _retire_claude_level(monkeypatch)
    live, pipe = _install("sid-inj", idle=True)
    live.state.spawn_run_options = drop_unsupported_reasoning(
        "claude", EngineRunOptions(reasoning="retired-level", permission_mode="plan")
    )

    async def resolver_equivalent(job: ThreadJob) -> EngineRunOptions | None:
        return drop_unsupported_reasoning(
            "claude",
            EngineRunOptions(reasoning="retired-level", permission_mode="plan"),
        )

    assert (
        await inject_live_followup(_job("sid-inj"), options_for=resolver_equivalent)
        is True
    )
    assert len(pipe.sent) == 1
    assert live.state.live_close_reason != "options_changed"


async def test_raw_vs_sanitised_options_would_mismatch(cleanup, monkeypatch) -> None:
    """#416 negative control: raw (unsanitised) follow-up options WOULD close
    the session — why sanitising must happen in the producer, not only in the
    executor."""
    from untether.runners.run_options import EngineRunOptions
    from untether.telegram.engine_overrides import drop_unsupported_reasoning

    _retire_claude_level(monkeypatch)
    live, pipe = _install("sid-inj", idle=True)
    live.state.spawn_run_options = drop_unsupported_reasoning(
        "claude", EngineRunOptions(reasoning="retired-level", permission_mode="plan")
    )

    async def aclose() -> None:
        return None

    pipe.aclose = aclose  # type: ignore[method-assign]

    async def raw(job: ThreadJob) -> EngineRunOptions:
        return EngineRunOptions(reasoning="retired-level", permission_mode="plan")

    assert await inject_live_followup(_job("sid-inj"), options_for=raw) is False
    assert live.state.live_close_reason == "options_changed"


def test_options_changed_notice_wording() -> None:
    assert rb._live_closing_notice("options_changed", ["x"]) == (
        "\N{GEAR}\N{VARIATION SELECTOR-16} Settings changed — stopping 1 "
        "background task: x. Your message starts with the new settings."
    )


async def test_838_followup_injection_never_checks_guard(cleanup, monkeypatch) -> None:
    """#838: a live follow-up writes into an existing process — no spawn, so
    the pre-spawn guard is never consulted (even at the ceiling)."""
    from untether.runner import JsonlSubprocessRunner

    def _boom(self, resume):
        raise AssertionError("pre-spawn guard consulted for a live follow-up")

    monkeypatch.setattr(JsonlSubprocessRunner, "_check_prespawn_ram_guard", _boom)
    _live, pipe = _install("sid-inj", idle=True)
    assert await inject_live_followup(_job("sid-inj")) is True
    assert len(pipe.sent) == 1


async def test_835_human_followup_into_unattended_session_resumes_fresh(
    cleanup,
) -> None:
    """#835: a human reply into a still-live cron process is not written into
    it (its approvals would be denied); the options differ by the unattended
    marker, so the session closes and the reply resumes attended."""
    from untether.runners.run_options import EngineRunOptions

    live, pipe = _install("sid-inj", idle=True)
    live.state.spawn_run_options = EngineRunOptions(
        permission_mode="default", unattended_trigger="cron:c1"
    )
    closed: list[bool] = []

    async def aclose() -> None:
        closed.append(True)

    pipe.aclose = aclose  # type: ignore[method-assign]

    async def human(job: ThreadJob) -> EngineRunOptions:
        return EngineRunOptions(permission_mode="default")

    assert await inject_live_followup(_job("sid-inj"), options_for=human) is False
    assert pipe.sent == []
    assert closed == [True]
    assert live.state.live_close_reason == "options_changed"


async def test_835_busy_steer_keeps_the_turn_unattended(cleanup) -> None:
    """#835 (documented): a /steer into a *busy* cron turn is folded into the
    unattended process — that turn's approvals stay denied (fail closed);
    only an idle session is isolated (options_changed)."""
    from untether.runners.claude import steer_into_session
    from untether.runners.run_options import EngineRunOptions

    live, pipe = _install("sid-inj", idle=False)
    live.state.spawn_run_options = EngineRunOptions(unattended_trigger="cron:c1")
    live.state.unattended_trigger = "cron:c1"
    out = await steer_into_session(
        "sid-inj", "also do X", command_uuid="u835", run_options=EngineRunOptions()
    )
    assert out == "steered"
    assert len(pipe.sent) == 1
    assert live.state.unattended_trigger == "cron:c1"

    live.state.turn_open = False  # idle now → isolated
    out = await steer_into_session(
        "sid-inj", "again", command_uuid="u836", run_options=EngineRunOptions()
    )
    assert out == "options_changed"


async def test_743_reply_to_cron_session_with_model_override_closes_options_changed(
    cleanup,
) -> None:
    """#743 D2: a reply to a cron run uses the chat's model, so a live session
    spawned with the cron's model closes (options_changed) and resumes."""
    from untether.runners.run_options import EngineRunOptions

    live, pipe = _install("sid-inj", idle=True)
    live.state.spawn_run_options = EngineRunOptions(
        model="haiku", permission_mode="plan-auto"
    )
    closed: list[bool] = []

    async def aclose() -> None:
        closed.append(True)

    pipe.aclose = aclose  # type: ignore[method-assign]

    async def chat(job: ThreadJob) -> EngineRunOptions:
        return EngineRunOptions()

    assert await inject_live_followup(_job("sid-inj"), options_for=chat) is False
    assert closed == [True]
    assert live.state.live_close_reason == "options_changed"


async def test_743_cron_without_overrides_still_injects(cleanup) -> None:
    from untether.runners.run_options import EngineRunOptions

    live, pipe = _install("sid-inj", idle=True)
    live.state.spawn_run_options = EngineRunOptions(model="opus")

    async def chat(job: ThreadJob) -> EngineRunOptions:
        return EngineRunOptions(model="opus")

    assert await inject_live_followup(_job("sid-inj"), options_for=chat) is True
    assert len(pipe.sent) == 1


# ── #835: a human reply to a running cron turn resumes attended ──────────────


def _cron_context():
    from untether.context import RunContext

    return RunContext(
        project="proj",
        branch="feat",
        trigger_source="cron:nightly",
        permission_mode="bypassPermissions",
        model="claude-cron-model",
        reasoning="high",
    )


async def _reply_to_running_cron_turn(running_task) -> ThreadJob:
    """Drive ``send_with_resume`` (the ResumeResolver's reply-to-running-task
    path) and return the ThreadJob it enqueues."""
    from tests.telegram_fakes import FakeTransport, make_cfg
    from untether.telegram.loop import send_with_resume

    jobs: list[ThreadJob] = []

    async def enqueue(
        chat_id, user_msg_id, text, resume, context, thread_id, session_key, ref
    ) -> None:
        jobs.append(
            ThreadJob(
                chat_id=chat_id,
                user_msg_id=user_msg_id,
                text=text,
                resume_token=resume,
                context=context,
                thread_id=thread_id,
                session_key=session_key,
                progress_ref=ref,
            )
        )

    await send_with_resume(
        make_cfg(FakeTransport()), enqueue, running_task, 123, 20, None, None, "hi"
    )
    assert len(jobs) == 1
    return jobs[0]


def _chat_options_for(job: ThreadJob):
    """Mirror of loop.run_main_loop's ``_job_run_options``: the chat's own
    options (here: plan mode) with the job context's trigger overrides."""
    from untether.runners.run_options import EngineRunOptions
    from untether.telegram.loop import _apply_trigger_overrides

    async def options_for(j: ThreadJob):
        return _apply_trigger_overrides(
            EngineRunOptions(permission_mode="plan"),
            j.context,
            engine=j.resume_token.engine,
            log=False,
        )

    return options_for


async def test_835_reply_to_running_cron_turn_strips_trigger_context() -> None:
    from untether.context import RunContext

    running = rb.RunningTask(context=_cron_context())
    running.resume = ResumeToken(engine="claude", value="sid-inj")
    job = await _reply_to_running_cron_turn(running)
    assert job.context == RunContext(project="proj", branch="feat")
    options = await _chat_options_for(job)(job)
    assert options is not None
    assert options.unattended_trigger is None
    # The cron's own overrides don't leak into the human's run.
    assert options.permission_mode == "plan"
    assert options.model is None
    assert options.reasoning is None


async def test_835_reply_to_running_cron_turn_closes_live_process(cleanup) -> None:
    """A human reply never goes into the cron's unattended live process: the
    options differ (``unattended_trigger`` + the cron overrides), so the
    process is closed once idle and the reply resumes attended."""
    from untether.runners.run_options import EngineRunOptions
    from untether.telegram.loop import _apply_trigger_overrides

    live, pipe = _install("sid-inj", idle=True)
    live.state.spawn_run_options = _apply_trigger_overrides(
        EngineRunOptions(permission_mode="plan"),
        _cron_context(),
        engine="claude",
        log=False,
    )
    assert live.state.spawn_run_options.unattended_trigger == "cron:nightly"

    async def aclose() -> None:
        return None

    pipe.aclose = aclose  # type: ignore[method-assign]
    running = rb.RunningTask(context=_cron_context())
    running.resume = ResumeToken(engine="claude", value="sid-inj")
    job = await _reply_to_running_cron_turn(running)

    injected = await inject_live_followup(job, options_for=_chat_options_for(job))
    assert injected is False
    assert pipe.sent == []
    assert live.state.live_close_reason == "options_changed"


async def test_835_raw_cron_context_would_inject_unattended(cleanup) -> None:
    """Negative control (the bug): reusing the running task's raw context
    makes the follow-up's options equal the cron process's spawn options, so
    the human turn would be written into the unattended process."""
    from untether.runners.run_options import EngineRunOptions
    from untether.telegram.loop import _apply_trigger_overrides

    live, pipe = _install("sid-inj", idle=True)
    live.state.spawn_run_options = _apply_trigger_overrides(
        EngineRunOptions(permission_mode="plan"),
        _cron_context(),
        engine="claude",
        log=False,
    )
    raw = ThreadJob(
        chat_id=123,
        user_msg_id=20,
        text="hi",
        resume_token=ResumeToken(engine="claude", value="sid-inj"),
        context=_cron_context(),
    )
    assert await inject_live_followup(raw, options_for=_chat_options_for(raw)) is True
    assert len(pipe.sent) == 1


async def test_835_reply_to_attended_run_keeps_context_identity() -> None:
    from untether.context import RunContext

    ctx = RunContext(project="proj", branch="feat")
    running = rb.RunningTask(context=ctx)
    running.resume = ResumeToken(engine="claude", value="sid-x")
    job = await _reply_to_running_cron_turn(running)
    assert job.context is ctx


# ── #921 in-flight anchor ownership ─────────────────────────────────────────

_RESEND = "please send it again"


def _ref(msg: int = 5) -> MessageRef:
    return MessageRef(channel_id=1, message_id=msg)


def test_921_drain_skips_in_flight_anchor(cleanup) -> None:
    rb.register_followup_anchor(
        "a", session_id="s1", reply_to=_ref(5), placeholder=None, in_flight=True
    )
    rb.register_followup_anchor(
        "b", session_id="s1", reply_to=_ref(6), placeholder=None
    )
    assert rb.drain_followup_anchors("s1") == [(_ref(6), None)]
    assert list(rb._FOLLOWUP_ANCHORS) == ["a"]


def test_921_settle_not_written_pops_and_signals(cleanup) -> None:
    rb.register_followup_anchor(
        "a", session_id="s1", reply_to=_ref(), placeholder=None, in_flight=True
    )
    flight = rb._FOLLOWUP_IN_FLIGHT["a"]
    rb.settle_followup_anchor("a", written=False)
    assert rb._FOLLOWUP_ANCHORS == {}
    assert rb._FOLLOWUP_IN_FLIGHT == {}
    assert flight.settled.is_set() and flight.written is False


def test_921_settle_written_keeps_anchor(cleanup) -> None:
    rb.register_followup_anchor(
        "a", session_id="s1", reply_to=_ref(), placeholder=None, in_flight=True
    )
    flight = rb._FOLLOWUP_IN_FLIGHT["a"]
    rb.settle_followup_anchor("a", written=True)
    assert flight.settled.is_set() and flight.written is True
    assert rb._FOLLOWUP_IN_FLIGHT == {}
    # Written but never ran: the run-end sweep still owns telling the user.
    assert rb.drain_followup_anchors("s1") == [(_ref(), None)]


def test_921_settle_unknown_uuid_is_noop(cleanup) -> None:
    rb.register_followup_anchor("b", session_id="s1", reply_to=_ref(), placeholder=None)
    rb.settle_followup_anchor("missing", written=False)
    rb.settle_followup_anchor("missing", written=True)
    assert list(rb._FOLLOWUP_ANCHORS) == ["b"]
    assert rb._FOLLOWUP_IN_FLIGHT == {}


async def test_921_inject_failure_settles_and_requeues(cleanup) -> None:
    """The session goes away while the follow-up waits for the turn to end:
    inject returns False and leaves nothing behind in either registry."""
    _install("sid-inj", idle=False)
    result: dict[str, Any] = {}

    async def go() -> None:
        result["ok"] = await inject_live_followup(_job("sid-inj"))

    async with anyio.create_task_group() as tg:
        tg.start_soon(go)
        with anyio.fail_after(2):
            while not rb._FOLLOWUP_IN_FLIGHT:
                await anyio.sleep(0.01)
        claude_mod._LIVE_SESSIONS.pop("sid-inj", None)
    assert result["ok"] is False
    assert rb._FOLLOWUP_ANCHORS == {}
    assert rb._FOLLOWUP_IN_FLIGHT == {}


async def test_921_inject_exception_still_settles(
    cleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install("sid-inj", idle=True)

    async def boom(*_a: Any, **_k: Any) -> bool:
        assert rb._FOLLOWUP_IN_FLIGHT  # registered as in flight
        raise RuntimeError("boom")

    monkeypatch.setattr(claude_mod, "inject_when_idle", boom)
    with pytest.raises(RuntimeError, match="boom"):
        await inject_live_followup(_job("sid-inj"))
    assert rb._FOLLOWUP_ANCHORS == {}
    assert rb._FOLLOWUP_IN_FLIGHT == {}


async def test_921_written_inject_keeps_anchor_not_in_flight(
    cleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install("sid-inj", idle=True)
    seen: list[bool] = []

    async def wrote(*_a: Any, **_k: Any) -> bool:
        seen.append(bool(rb._FOLLOWUP_IN_FLIGHT))
        return True

    monkeypatch.setattr(claude_mod, "inject_when_idle", wrote)
    assert await inject_live_followup(_job("sid-inj")) is True
    assert seen == [True]
    assert len(rb._FOLLOWUP_ANCHORS) == 1
    assert rb._FOLLOWUP_IN_FLIGHT == {}


async def test_921_settle_wait_logs_requeued_when_writer_falls_back(cleanup) -> None:
    rb.register_followup_anchor(
        "a", session_id="s1", reply_to=_ref(), placeholder=None, in_flight=True
    )

    async def writer() -> None:
        await anyio.sleep(0.05)
        rb.settle_followup_anchor("a", written=False)

    with capture_logs() as logs:
        async with anyio.create_task_group() as tg:
            tg.start_soon(writer)
            await rb._await_in_flight_followups("s1")
    events = [e["event"] for e in logs]
    assert "claude.live_session.followup_requeued" in events
    assert "claude.live_session.followup_settle_timeout" not in events
    assert rb.drain_followup_anchors("s1") == []


async def test_921_settle_wait_silent_when_writer_wrote(cleanup) -> None:
    rb.register_followup_anchor(
        "a", session_id="s1", reply_to=_ref(), placeholder=None, in_flight=True
    )

    async def writer() -> None:
        await anyio.sleep(0.05)
        rb.settle_followup_anchor("a", written=True)

    with capture_logs() as logs:
        async with anyio.create_task_group() as tg:
            tg.start_soon(writer)
            await rb._await_in_flight_followups("s1")
    events = {e["event"] for e in logs}
    assert not events & {
        "claude.live_session.followup_requeued",
        "claude.live_session.followup_settle_timeout",
    }
    # Written and the session is gone: the sweep's drain now reports it.
    assert rb.drain_followup_anchors("s1") == [(_ref(), None)]


async def test_921_settle_wait_ignores_other_sessions(cleanup) -> None:
    rb.register_followup_anchor(
        "a", session_id="other", reply_to=_ref(), placeholder=None, in_flight=True
    )
    with anyio.fail_after(0.5), capture_logs() as logs:
        await rb._await_in_flight_followups("s1")
    assert logs == []
    assert "a" in rb._FOLLOWUP_IN_FLIGHT


@pytest.mark.parametrize("live_owner", [False, True])
async def test_921_settle_timeout_leaves_anchor_for_writer(
    cleanup, monkeypatch: pytest.MonkeyPatch, live_owner: bool
) -> None:
    """A writer still in flight after the bound keeps its anchor (no notice).
    With a newer live owner of the same session id (#816) that is expected:
    the line goes into the new process, whose router consumes the anchor."""
    monkeypatch.setattr(rb, "_FOLLOWUP_SETTLE_S", 0.05)
    if live_owner:
        _install("sid-inj", idle=False)
    rb.register_followup_anchor(
        "a", session_id="sid-inj", reply_to=_ref(), placeholder=None, in_flight=True
    )
    with capture_logs() as logs:
        await rb._await_in_flight_followups("sid-inj")
    timeouts = [
        e for e in logs if e["event"] == "claude.live_session.followup_settle_timeout"
    ]
    assert len(timeouts) == 1
    assert timeouts[0]["live_owner_present"] is live_owner
    assert timeouts[0]["log_level"] == "info"
    assert rb.drain_followup_anchors("sid-inj") == []  # left for the writer
    assert "a" in rb._FOLLOWUP_ANCHORS
    rb.settle_followup_anchor("a", written=False)
    assert rb._FOLLOWUP_ANCHORS == {} and rb._FOLLOWUP_IN_FLIGHT == {}


def _all_texts(transport: Any) -> list[str]:
    return [c["message"].text for c in transport.send_calls] + [
        c["message"].text for c in transport.edit_calls
    ]


async def _cancel_once_in_flight(
    running_tasks: dict[MessageRef, rb.RunningTask], *, enqueue: Any
) -> None:
    """Wait for the first turn to be running, queue a follow-up, wait until
    its inject has registered the anchor, then /cancel the run."""
    with anyio.fail_after(20):
        while True:
            live = claude_mod.get_live_session(SID)
            if live is not None and live.state.turn_open:
                break
            await anyio.sleep(0.02)
        await enqueue()
        while not rb._FOLLOWUP_ANCHORS:
            await anyio.sleep(0.01)
        while not running_tasks:
            await anyio.sleep(0.01)
    (_, task), *_ = rb.unique_running_tasks(running_tasks)
    task.cancel_requested.set()


async def test_921_cancel_while_followup_waits_does_not_ask_to_resend(
    cleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance case: /cancel lands while a queued follow-up waits for
    the running turn to end. The follow-up is re-dispatched (resume path), so
    the user must NOT be told to send it again, and nothing WARNs."""
    _watchdog(monkeypatch)
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "followup")
    monkeypatch.setenv("FAKE_CLAUDE_RESULT_DELAY_S", "3")
    resumed: list[ThreadJob] = []

    async def run_job(job: ThreadJob) -> None:
        resumed.append(job)

    running_tasks: dict[MessageRef, rb.RunningTask] = {}
    holder: dict[str, Any] = {}
    with capture_logs() as logs:
        async with anyio.create_task_group() as tg:
            sched = ThreadScheduler(
                task_group=tg, run_job=run_job, inject_job=inject_live_followup
            )

            async def drive() -> None:
                holder["transport"] = await _drive(
                    "followup", running_tasks=running_tasks
                )

            tg.start_soon(drive)
            await _cancel_once_in_flight(
                running_tasks, enqueue=lambda: sched.enqueue(_job())
            )

    texts = _all_texts(holder["transport"])
    assert not [t for t in texts if _RESEND in t]
    assert [j.user_msg_id for j in resumed] == [20]  # the resume fallback, once
    assert rb._FOLLOWUP_ANCHORS == {}
    assert rb._FOLLOWUP_IN_FLIGHT == {}
    events = [e["event"] for e in logs]
    assert "claude.live_session.inject_unavailable" in events
    assert "claude.live_session.followup_requeued" in events
    assert "claude.live_session.followup_not_run" not in events


async def test_921_written_followup_whose_session_dies_still_warns(
    cleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: a follow-up that WAS written into the session but
    never got a turn (the run was cancelled first) still gets the notice and
    the WARN — it really didn't run."""
    _watchdog(monkeypatch)
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "followup")
    monkeypatch.setenv("FAKE_CLAUDE_RESULT_DELAY_S", "3")

    async def wrote_nothing(*_a: Any, **_k: Any) -> bool:
        return True  # "written" — but nothing reaches the fake CLI

    monkeypatch.setattr(claude_mod, "inject_when_idle", wrote_nothing)

    async def run_job(job: ThreadJob) -> None:  # pragma: no cover
        raise AssertionError("an injected follow-up must not resume")

    running_tasks: dict[MessageRef, rb.RunningTask] = {}
    holder: dict[str, Any] = {}
    with capture_logs() as logs:
        async with anyio.create_task_group() as tg:
            sched = ThreadScheduler(
                task_group=tg, run_job=run_job, inject_job=inject_live_followup
            )

            async def drive() -> None:
                holder["transport"] = await _drive(
                    "followup", running_tasks=running_tasks
                )

            tg.start_soon(drive)
            await _cancel_once_in_flight(
                running_tasks, enqueue=lambda: sched.enqueue(_job())
            )

    transport = holder["transport"]
    placeholder = MessageRef(channel_id=123, message_id=99)
    notices = [c for c in transport.edit_calls if _RESEND in c["message"].text]
    assert notices and notices[-1]["ref"] == placeholder
    assert notices[-1]["message"].text == (
        "\N{WARNING SIGN} The session ended before this message ran "
        "— please send it again."
    )
    warns = [e for e in logs if e["event"] == "claude.live_session.followup_not_run"]
    assert len(warns) == 1 and warns[0]["log_level"] == "warning"
    assert rb._FOLLOWUP_ANCHORS == {}
    assert rb._FOLLOWUP_IN_FLIGHT == {}


# ── #996: a context change (/ctx set, /ctx clear) never injects ─────────────


def _closing_pipe(pipe: _Pipe) -> list[bool]:
    closed: list[bool] = []

    async def aclose() -> None:
        closed.append(True)

    pipe.aclose = aclose  # type: ignore[method-assign]
    return closed


def _cwd(path: Any):
    async def cwd_for(job: ThreadJob) -> Any:
        if isinstance(path, Exception):
            raise path
        return path

    return cwd_for


async def test_996_same_cwd_still_injects(cleanup, tmp_path) -> None:
    live, pipe = _install("sid-inj", idle=True)
    live.state.spawn_cwd = tmp_path / "proj-a"
    ok = await inject_live_followup(_job("sid-inj"), cwd_for=_cwd(tmp_path / "proj-a"))
    assert ok is True
    assert len(pipe.sent) == 1


async def test_996_no_project_on_both_sides_still_injects(cleanup) -> None:
    """No /ctx binding: spawned in Untether's own cwd (None) and the chat
    still resolves to None — the ordinary follow-up keeps injecting."""
    _live, pipe = _install("sid-inj", idle=True)
    assert await inject_live_followup(_job("sid-inj"), cwd_for=_cwd(None)) is True
    assert len(pipe.sent) == 1


@pytest.mark.parametrize(
    ("spawned", "wanted"),
    [
        pytest.param(None, "proj-b", id="ctx-set-from-no-project"),
        pytest.param("proj-a", "proj-b", id="ctx-set-other-project"),
        pytest.param("proj-a", None, id="ctx-clear"),
    ],
)
async def test_996_changed_context_closes_instead_of_injecting(
    cleanup, tmp_path, spawned: str | None, wanted: str | None
) -> None:
    """The bug: after /ctx set the next prompt was written into the idle live
    process and ran in the old cwd. It must close (options_changed) so the
    message resumes the session in the new directory."""
    live, pipe = _install("sid-inj", idle=True)
    live.state.spawn_cwd = tmp_path / spawned if spawned else None
    closed = _closing_pipe(pipe)
    want = tmp_path / wanted if wanted else None
    with capture_logs() as logs:
        ok = await inject_live_followup(_job("sid-inj"), cwd_for=_cwd(want))
    assert ok is False
    assert pipe.sent == []
    assert closed == [True]
    assert live.state.live_close_reason == "options_changed"
    (event,) = [e for e in logs if e["event"] == "claude.live_session.context_changed"]
    assert event["log_level"] == "info"
    assert event["closed"] is True
    assert event["spawn_cwd"] == (str(tmp_path / spawned) if spawned else None)
    assert event["cwd"] == (str(want) if want else None)


async def test_996_unresolvable_context_closes_instead_of_injecting(
    cleanup, tmp_path
) -> None:
    """A context that no longer resolves (project removed from the config,
    worktree error) must not fall through to a write into the old cwd: the
    session closes and the resume path reports the error."""
    live, pipe = _install("sid-inj", idle=True)
    live.state.spawn_cwd = tmp_path / "proj-a"
    closed = _closing_pipe(pipe)
    with capture_logs() as logs:
        ok = await inject_live_followup(
            _job("sid-inj"), cwd_for=_cwd(RuntimeError("unknown project"))
        )
    assert ok is False
    assert pipe.sent == [] and closed == [True]
    (event,) = [e for e in logs if e["event"] == "claude.live_session.context_changed"]
    assert event["resolve_error"] == "RuntimeError"


async def test_996_mid_turn_context_change_waits_then_closes(cleanup, tmp_path) -> None:
    """Mid-turn the close is refused (only_if_idle) and nothing is written —
    the scheduler re-offers the job; once the turn ends the session closes.
    The refused attempts don't spam INFO."""
    live, pipe = _install("sid-inj", idle=False)
    live.state.spawn_cwd = tmp_path / "proj-a"
    closed = _closing_pipe(pipe)
    cwd_for = _cwd(tmp_path / "proj-b")
    with capture_logs() as logs:
        assert await inject_live_followup(_job("sid-inj"), cwd_for=cwd_for) is False
    assert pipe.sent == [] and closed == []
    assert not live.closing
    (event,) = [e for e in logs if e["event"] == "claude.live_session.context_changed"]
    assert event["log_level"] == "debug" and event["closed"] is False

    live.state.turn_open = False  # the turn ended
    assert await inject_live_followup(_job("sid-inj"), cwd_for=cwd_for) is False
    assert pipe.sent == [] and closed == [True]
    assert live.state.live_close_reason == "options_changed"


async def test_996_live_process_records_its_spawn_cwd_and_ctx_change_resumes(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """End to end (real ClaudeRunner + fake CLI): the live process records
    the cwd it was spawned in; a follow-up whose context resolves elsewhere
    is not written into it — the session closes and the job takes the
    resume path (one resume, no injection)."""
    from untether.utils.paths import reset_run_base_dir, set_run_base_dir

    _watchdog(monkeypatch, post_result_limbo_grace=1.0)
    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    proj_a.mkdir()
    proj_b.mkdir()
    resumed: list[ThreadJob] = []
    seen_spawn_cwd: list[Any] = []

    async def run_job(job: ThreadJob) -> None:
        resumed.append(job)

    async def follow_up(sched: ThreadScheduler) -> None:
        with anyio.fail_after(20):
            while True:
                live = claude_mod.get_live_session(SID)
                if live is not None and live.idle:
                    break
                await anyio.sleep(0.02)
        seen_spawn_cwd.append(live.state.spawn_cwd)
        await sched.enqueue(_job())

    async def drive() -> None:
        token = set_run_base_dir(proj_a)
        try:
            await _drive("followup")
        finally:
            reset_run_base_dir(token)

    with capture_logs() as logs:
        async with anyio.create_task_group() as tg:
            sched = ThreadScheduler(
                task_group=tg,
                run_job=run_job,
                inject_job=functools.partial(
                    inject_live_followup, cwd_for=_cwd(proj_b)
                ),
            )
            tg.start_soon(follow_up, sched)
            tg.start_soon(drive)

    assert seen_spawn_cwd == [proj_a]
    assert [j.text for j in resumed] == ["again"]
    assert not any(e["event"] == "claude.live_session.injected" for e in logs)
    changed = [e for e in logs if e["event"] == "claude.live_session.context_changed"]
    assert changed and changed[-1]["closed"] is True
    assert rb._FOLLOWUP_ANCHORS == {}
    os.environ.pop("FAKE_CLAUDE_SCENARIO", None)


async def test_996_steer_into_idle_session_in_other_cwd_falls_back(
    cleanup, tmp_path
) -> None:
    """Steer mode shares the root cause: an idle steer is the next turn, so a
    changed cwd returns options_changed (the queue path closes + resumes); a
    mid-turn steer still folds into the running turn, like changed options."""
    from untether.runners.claude import steer_into_session

    live, pipe = _install("sid-inj", idle=True)
    live.state.spawn_cwd = tmp_path / "proj-a"
    with capture_logs() as logs:
        out = await steer_into_session(
            "sid-inj", "pwd", command_uuid="u996", cwd=tmp_path / "proj-b"
        )
    assert out == "options_changed"
    assert pipe.sent == []
    assert any(
        e["event"] == "claude.live_session.context_changed" and e["source"] == "steer"
        for e in logs
    )
    out = await steer_into_session(
        "sid-inj", "pwd", command_uuid="u997", cwd=tmp_path / "proj-a"
    )
    assert out == "written_idle"

    live.state.turn_open = True  # busy: the steer joins the running turn
    out = await steer_into_session(
        "sid-inj", "also", command_uuid="u998", cwd=tmp_path / "proj-b"
    )
    assert out == "steered"
