"""Tests for the loop_scheduler module (#289).

Covers registration, persistence, cancellation, the fire path, the
do-not-resume sentinel, and restart resilience.  Mirrors the shape of
``test_at_command.py``: ``FakeTransport`` + ``RunJobRecorder`` + an
optional ``runtime`` stand-in for tests that exercise the chat→engine
freeze.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import pytest

from untether import loop_scheduler
from untether.context import RunContext
from untether.transport import MessageRef

pytestmark = pytest.mark.anyio


# ── Fakes ────────────────────────────────────────────────────────────────


@dataclass
class FakeTransport:
    sent: list[Any] = None  # type: ignore[assignment]

    def __post_init__(self):
        self.sent = []

    async def send(self, *, channel_id, message, options=None, **_):
        self.sent.append((channel_id, message.text, options))
        return MessageRef(channel_id=channel_id, message_id=9999)

    async def edit(self, *, ref, message, **_):
        return ref

    async def delete(self, ref):
        return None


class RunJobRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def __call__(self, *args, **kwargs):
        self.calls.append(args)


async def _noop_run_job(*args, **kwargs):
    return None


# ── Helpers ──────────────────────────────────────────────────────────────


def _register_simple_cron(
    chat_id: int = 100,
    *,
    session_id: str = "sess-abc",
    tool_use_id: str | None = None,
    prompt: str = "check deploy",
    cron_expression: str = "*/5 * * * *",
    recurring: bool = True,
) -> str:
    if tool_use_id is None:
        tool_use_id = f"tu-{chat_id}-{prompt[:8]}"
    return loop_scheduler.register_pending_cron(
        session_id=session_id,
        tool_use_id=tool_use_id,
        cron_expression=cron_expression,
        prompt=prompt,
        recurring=recurring,
        chat_id=chat_id,
    )


# ── Install / uninstall lifecycle ───────────────────────────────────────


class TestInstallUninstall:
    @pytest.fixture(autouse=True)
    def _cleanup(self):
        loop_scheduler.uninstall()
        yield
        loop_scheduler.uninstall()

    async def test_register_when_not_installed_raises(self):
        with pytest.raises(loop_scheduler.LoopSchedulerError):
            loop_scheduler.register_pending_cron(
                session_id="sess",
                tool_use_id="tu1",
                cron_expression="* * * * *",
                prompt="x",
                recurring=True,
                chat_id=1,
            )

    async def test_install_then_uninstall_clears_state(self):
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                _register_simple_cron(chat_id=42)
                assert loop_scheduler.active_count() == 1
            finally:
                tg.cancel_scope.cancel()
        loop_scheduler.uninstall()
        assert loop_scheduler.active_count() == 0

    async def test_uninstall_clears_do_not_resume(self):
        loop_scheduler.mark_do_not_resume("sess-xyz")
        assert loop_scheduler.is_do_not_resume("sess-xyz")
        loop_scheduler.uninstall()
        assert not loop_scheduler.is_do_not_resume("sess-xyz")


# ── Registration ────────────────────────────────────────────────────────


class TestRegisterCron:
    @pytest.fixture(autouse=True)
    def _cleanup(self):
        loop_scheduler.uninstall()
        yield
        loop_scheduler.uninstall()

    async def test_register_recurring_cron(self):
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                token = _register_simple_cron(chat_id=7, prompt="ping")
                assert token.startswith("ut_loop_")
                pending = loop_scheduler.pending_for_chat(7)
                assert len(pending) == 1
                assert pending[0].kind == "cron"
                assert pending[0].cron_expression == "*/5 * * * *"
                assert pending[0].prompt == "ping"
                assert pending[0].recurring is True
                assert pending[0].fire_at_monotonic > 0
            finally:
                tg.cancel_scope.cancel()

    async def test_register_one_shot_cron(self):
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                token = _register_simple_cron(
                    chat_id=8, recurring=False, prompt="reminder"
                )
                pending = loop_scheduler.pending_for_chat(8)
                assert len(pending) == 1
                assert pending[0].recurring is False
                assert pending[0].token == token
            finally:
                tg.cancel_scope.cancel()

    async def test_register_invalid_cron_raises(self):
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                with pytest.raises(loop_scheduler.LoopSchedulerError):
                    loop_scheduler.register_pending_cron(
                        session_id="s",
                        tool_use_id="t",
                        cron_expression="not-a-cron",
                        prompt="p",
                        recurring=True,
                        chat_id=9,
                    )
            finally:
                tg.cancel_scope.cancel()

    async def test_register_stamps_trigger_source_loop(self):
        """Trigger source ``loop:<token>`` shows in the run footer."""
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                token = _register_simple_cron(chat_id=10)
                entry = loop_scheduler.pending_for_chat(10)[0]
                assert entry.context is not None
                assert entry.context.trigger_source == f"loop:{token}"
            finally:
                tg.cancel_scope.cancel()

    async def test_register_preserves_project_in_context(self):
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                ctx = RunContext(project="acme", branch=None)
                loop_scheduler.register_pending_cron(
                    session_id="s",
                    tool_use_id="t",
                    cron_expression="* * * * *",
                    prompt="p",
                    recurring=True,
                    chat_id=11,
                    context=ctx,
                    engine_override="claude",
                )
                entry = loop_scheduler.pending_for_chat(11)[0]
                assert entry.context.project == "acme"
                assert entry.engine_override == "claude"
            finally:
                tg.cancel_scope.cancel()


class TestRegisterWakeup:
    @pytest.fixture(autouse=True)
    def _cleanup(self):
        loop_scheduler.uninstall()
        yield
        loop_scheduler.uninstall()

    async def test_register_wakeup(self):
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                token = loop_scheduler.register_pending_wakeup(
                    session_id="s",
                    tool_use_id="t",
                    delay_seconds=600.0,
                    prompt="check",
                    chat_id=20,
                )
                pending = loop_scheduler.pending_for_chat(20)
                assert len(pending) == 1
                assert pending[0].kind == "wakeup"
                assert pending[0].delay_seconds == 600.0
                assert pending[0].recurring is False
                assert pending[0].token == token
            finally:
                tg.cancel_scope.cancel()

    async def test_register_wakeup_zero_delay_raises(self):
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                with pytest.raises(loop_scheduler.LoopSchedulerError):
                    loop_scheduler.register_pending_wakeup(
                        session_id="s",
                        tool_use_id="t",
                        delay_seconds=0,
                        prompt="x",
                        chat_id=21,
                    )
            finally:
                tg.cancel_scope.cancel()

    async def test_register_wakeup_with_fallback(self):
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                loop_scheduler.register_pending_wakeup(
                    session_id="s",
                    tool_use_id="t",
                    delay_seconds=120,
                    prompt="<<autonomous-loop-dynamic>>",
                    fallback_first_user_message="poll the build",
                    chat_id=22,
                )
                entry = loop_scheduler.pending_for_chat(22)[0]
                assert entry.fallback_first_user_message == "poll the build"
            finally:
                tg.cancel_scope.cancel()


# ── Upstream-ID binding ─────────────────────────────────────────────────


class TestBindUpstreamId:
    @pytest.fixture(autouse=True)
    def _cleanup(self):
        loop_scheduler.uninstall()
        yield
        loop_scheduler.uninstall()

    async def test_bind_then_cancel_by_upstream_id(self):
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                _register_simple_cron(chat_id=30, tool_use_id="tu-bind-1")
                loop_scheduler.bind_upstream_id("tu-bind-1", "abcdef12")
                assert loop_scheduler.cancel_by_upstream_id("abcdef12") is True
                assert loop_scheduler.active_count() == 0
            finally:
                tg.cancel_scope.cancel()

    async def test_bind_unknown_tool_use_id_is_noop(self):
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                # Should not raise.
                loop_scheduler.bind_upstream_id("nonexistent", "deadbeef")
            finally:
                tg.cancel_scope.cancel()

    async def test_cancel_by_unknown_upstream_id_returns_false(self):
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                assert loop_scheduler.cancel_by_upstream_id("nope") is False
            finally:
                tg.cancel_scope.cancel()


# ── Cancellation ────────────────────────────────────────────────────────


class TestCancellation:
    @pytest.fixture(autouse=True)
    def _cleanup(self):
        loop_scheduler.uninstall()
        yield
        loop_scheduler.uninstall()

    async def test_cancel_by_token_marks_do_not_resume(self):
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                token = _register_simple_cron(chat_id=40, session_id="sess-40")
                assert loop_scheduler.cancel_by_token(token) is True
                assert loop_scheduler.is_do_not_resume("sess-40") is True
                assert loop_scheduler.active_count() == 0
            finally:
                tg.cancel_scope.cancel()

    async def test_cancel_by_unknown_token_returns_false(self):
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                assert loop_scheduler.cancel_by_token("nope") is False
            finally:
                tg.cancel_scope.cancel()

    async def test_cancel_pending_for_chat_only_drops_chat(self):
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                _register_simple_cron(chat_id=50, prompt="a", tool_use_id="tu-50a")
                _register_simple_cron(chat_id=50, prompt="b", tool_use_id="tu-50b")
                _register_simple_cron(chat_id=51, prompt="c", tool_use_id="tu-51c")
                cancelled = loop_scheduler.cancel_pending_for_chat(50)
                assert cancelled == 2
                assert loop_scheduler.active_count() == 1
                assert loop_scheduler.pending_for_chat(51)[0].prompt == "c"
            finally:
                tg.cancel_scope.cancel()

    async def test_826_cancel_pending_for_chat_thread_filter(self):
        """#826: a thread filter drops only the matching topic's loops."""
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                for thread_id, tu in ((6, "tu-6"), (10, "tu-10"), (None, "tu-g")):
                    loop_scheduler.register_pending_cron(
                        session_id=f"sess-{tu}",
                        tool_use_id=tu,
                        cron_expression="*/5 * * * *",
                        prompt=tu,
                        recurring=True,
                        chat_id=52,
                        thread_id=thread_id,
                    )
                cancelled = loop_scheduler.cancel_pending_for_chat(
                    52, thread_filter=lambda t: t == 6
                )
                assert cancelled == 1
                remaining = {e.thread_id for e in loop_scheduler.pending_for_chat(52)}
                assert remaining == {10, None}
                # Default (no filter) still drops the whole chat.
                assert loop_scheduler.cancel_pending_for_chat(52) == 2
                assert loop_scheduler.pending_for_chat(52) == []
            finally:
                tg.cancel_scope.cancel()


# ── Inspection ──────────────────────────────────────────────────────────


class TestInspection:
    @pytest.fixture(autouse=True)
    def _cleanup(self):
        loop_scheduler.uninstall()
        yield
        loop_scheduler.uninstall()

    async def test_active_count_excludes_cancelled(self):
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                t1 = _register_simple_cron(chat_id=60, prompt="a", tool_use_id="t60a")
                _register_simple_cron(chat_id=60, prompt="b", tool_use_id="t60b")
                assert loop_scheduler.active_count() == 2
                loop_scheduler.cancel_by_token(t1)
                assert loop_scheduler.active_count() == 1
            finally:
                tg.cancel_scope.cancel()

    async def test_next_fire_for_session_returns_min(self):
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                loop_scheduler.register_pending_wakeup(
                    session_id="sess-shared",
                    tool_use_id="t1",
                    delay_seconds=600,
                    prompt="x",
                    chat_id=70,
                )
                loop_scheduler.register_pending_wakeup(
                    session_id="sess-shared",
                    tool_use_id="t2",
                    delay_seconds=120,
                    prompt="y",
                    chat_id=70,
                )
                next_fire = loop_scheduler.next_fire_for_session("sess-shared")
                assert next_fire is not None
            finally:
                tg.cancel_scope.cancel()

    async def test_next_fire_for_unknown_session_is_none(self):
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                assert loop_scheduler.next_fire_for_session("nope") is None
            finally:
                tg.cancel_scope.cancel()


# ── Cron next-fire computation ──────────────────────────────────────────


class TestNextCronFire:
    def test_simple_every_minute(self):
        result = loop_scheduler._next_cron_fire("* * * * *")
        assert result is not None
        assert result > 0

    def test_malformed_returns_none(self):
        assert loop_scheduler._next_cron_fire("not-a-cron") is None
        assert loop_scheduler._next_cron_fire("") is None
        assert loop_scheduler._next_cron_fire("* * *") is None

    def test_every_5_minutes(self):
        result = loop_scheduler._next_cron_fire("*/5 * * * *")
        assert result is not None


# ── Fire path ───────────────────────────────────────────────────────────


class TestFirePath:
    @pytest.fixture(autouse=True)
    def _cleanup(self):
        loop_scheduler.uninstall()
        yield
        loop_scheduler.uninstall()

    async def test_fire_skips_cancelled_entry(self):
        recorder = RunJobRecorder()
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, recorder, FakeTransport(), 1)
            try:
                token = _register_simple_cron(chat_id=80)
                loop_scheduler.cancel_by_token(token)
                # Should be a no-op even though the token still has an entry
                # in the cancelled state.
                await loop_scheduler._fire(token)
                assert recorder.calls == []
            finally:
                tg.cancel_scope.cancel()

    async def test_826_fire_notice_and_run_go_to_entry_thread(self):
        """#826: a topic's loop fire posts its notice in that topic and runs
        there (the run replies to the notice)."""
        recorder = RunJobRecorder()
        transport = FakeTransport()
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, recorder, transport, 1)
            try:
                token = loop_scheduler.register_pending_cron(
                    session_id="sess-t6",
                    tool_use_id="tu-t6",
                    cron_expression="*/5 * * * *",
                    prompt="p",
                    recurring=True,
                    chat_id=86,
                    thread_id=6,
                )
                entry = loop_scheduler._PENDING_BY_TOKEN[token]
                await loop_scheduler._spawn_loop_iteration(entry)
                assert transport.sent[0][2].thread_id == 6
                assert recorder.calls[0][5] == 6
            finally:
                tg.cancel_scope.cancel()

    async def test_fire_skips_unknown_token(self):
        recorder = RunJobRecorder()
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, recorder, FakeTransport(), 1)
            try:
                await loop_scheduler._fire("ut_loop_deadbeef")
                assert recorder.calls == []
            finally:
                tg.cancel_scope.cancel()

    async def test_fire_skips_when_max_iterations_reached(self):
        recorder = RunJobRecorder()
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, recorder, FakeTransport(), 1)
            try:
                token = loop_scheduler.register_pending_cron(
                    session_id="s",
                    tool_use_id="t",
                    cron_expression="* * * * *",
                    prompt="p",
                    recurring=True,
                    chat_id=81,
                    max_iterations=1,
                )
                entry = loop_scheduler._PENDING_BY_TOKEN[token]
                entry.iteration_count = 1  # already at cap
                await loop_scheduler._fire(token)
                assert recorder.calls == []
                assert entry.cancelled is True
            finally:
                tg.cancel_scope.cancel()

    async def test_fire_skips_when_do_not_resume_set(self):
        recorder = RunJobRecorder()
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, recorder, FakeTransport(), 1)
            try:
                token = _register_simple_cron(chat_id=82, session_id="sess-blocked")
                loop_scheduler.mark_do_not_resume("sess-blocked")
                await loop_scheduler._fire(token)
                assert recorder.calls == []
                # Entry should be expired (not just skipped).
                assert token not in loop_scheduler._PENDING_BY_TOKEN
            finally:
                tg.cancel_scope.cancel()

    async def test_fire_drops_when_chat_busy(self):
        recorder = RunJobRecorder()
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(
                tg,
                recorder,
                FakeTransport(),
                1,
                is_chat_busy=lambda _chat_id: True,
            )
            try:
                token = _register_simple_cron(chat_id=83)
                await loop_scheduler._fire(token)
                # No run dispatched.
                assert recorder.calls == []
                # Entry preserved (rearm scheduled in task group, not awaited here).
                assert token in loop_scheduler._PENDING_BY_TOKEN
            finally:
                tg.cancel_scope.cancel()

    async def test_fire_defers_while_session_alive_not_closeable(self, monkeypatch):
        """#925: an alive session that can't be closed (not live-idle) defers
        the fire; a cancel ends the retry without a run."""
        recorder = RunJobRecorder()
        from untether.runners import claude as claude_mod

        monkeypatch.setattr(
            claude_mod,
            "is_session_alive",
            lambda sid: sid == "sess-alive",
        )
        monkeypatch.setattr(loop_scheduler, "_redundancy_check_interval", lambda: 0.01)
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, recorder, FakeTransport(), 1)
            try:
                token = _register_simple_cron(chat_id=84, session_id="sess-alive")
                tg.start_soon(loop_scheduler._fire, token)
                await anyio.sleep(0.05)
                assert recorder.calls == []
                assert token in loop_scheduler._PENDING_BY_TOKEN
                loop_scheduler.cancel_by_token(token)
                await anyio.sleep(0.03)
                assert recorder.calls == []
            finally:
                tg.cancel_scope.cancel()

    async def test_fire_dispatches_run_with_wrapped_prompt(self, monkeypatch):
        recorder = RunJobRecorder()
        from untether.runners import claude as claude_mod

        monkeypatch.setattr(claude_mod, "is_session_alive", lambda _sid: False)
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, recorder, FakeTransport(), 1)
            try:
                token = loop_scheduler.register_pending_wakeup(
                    session_id="sess-fire",
                    tool_use_id="t",
                    delay_seconds=120,
                    prompt="check the deploy",
                    chat_id=85,
                )
                await loop_scheduler._fire(token)
                assert len(recorder.calls) == 1
                args = recorder.calls[0]
                # Layout per at_scheduler._run_delayed (run_job 11-arg):
                # (chat_id, message_id, prompt, resume_token, context,
                #  thread_id, chat_session_key, reply_ref, on_thread_known,
                #  engine_override, progress_ref)
                assert args[0] == 85
                assert "Loop iteration 1" in args[2]
                assert "check the deploy" in args[2]
                assert args[3] is not None
                assert args[3].engine == "claude"
                assert args[3].value == "sess-fire"
            finally:
                tg.cancel_scope.cancel()

    async def test_fire_uses_fallback_for_sentinel_prompt(self, monkeypatch):
        recorder = RunJobRecorder()
        from untether.runners import claude as claude_mod

        monkeypatch.setattr(claude_mod, "is_session_alive", lambda _sid: False)
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, recorder, FakeTransport(), 1)
            try:
                token = loop_scheduler.register_pending_wakeup(
                    session_id="s",
                    tool_use_id="t",
                    delay_seconds=120,
                    prompt="<<autonomous-loop-dynamic>>",
                    fallback_first_user_message="poll the build",
                    chat_id=86,
                )
                await loop_scheduler._fire(token)
                assert len(recorder.calls) == 1
                wrapped = recorder.calls[0][2]
                assert "poll the build" in wrapped
                assert "<<autonomous-loop-dynamic>>" not in wrapped
            finally:
                tg.cancel_scope.cancel()

    async def test_fire_one_shot_wakeup_expires_after_fire(self, monkeypatch):
        recorder = RunJobRecorder()
        from untether.runners import claude as claude_mod

        monkeypatch.setattr(claude_mod, "is_session_alive", lambda _sid: False)
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, recorder, FakeTransport(), 1)
            try:
                token = loop_scheduler.register_pending_wakeup(
                    session_id="s",
                    tool_use_id="t",
                    delay_seconds=120,
                    prompt="x",
                    chat_id=87,
                )
                await loop_scheduler._fire(token)
                # One-shot — expired after firing.
                assert token not in loop_scheduler._PENDING_BY_TOKEN
            finally:
                tg.cancel_scope.cancel()


# ── Persistence ─────────────────────────────────────────────────────────


class TestPersistence:
    @pytest.fixture(autouse=True)
    def _cleanup(self):
        loop_scheduler.uninstall()
        yield
        loop_scheduler.uninstall()

    async def test_register_writes_state_file(self, tmp_path: Path):
        state_path = tmp_path / "active_loops.json"
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(
                tg, _noop_run_job, FakeTransport(), 1, state_path=state_path
            )
            try:
                _register_simple_cron(chat_id=200)
                assert state_path.exists()
                assert b'"entries"' in state_path.read_bytes()
            finally:
                tg.cancel_scope.cancel()

    async def test_restart_restores_pending_entries(self, tmp_path: Path):
        state_path = tmp_path / "active_loops.json"
        # First install: register one entry, persist.
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(
                tg, _noop_run_job, FakeTransport(), 1, state_path=state_path
            )
            try:
                token = loop_scheduler.register_pending_wakeup(
                    session_id="sess-persisted",
                    tool_use_id="t",
                    delay_seconds=3600,
                    prompt="long delay",
                    chat_id=201,
                )
            finally:
                tg.cancel_scope.cancel()
        loop_scheduler.uninstall()

        # Second install — must restore the entry from disk.
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(
                tg, _noop_run_job, FakeTransport(), 1, state_path=state_path
            )
            try:
                pending = loop_scheduler.pending_for_chat(201)
                assert len(pending) == 1
                assert pending[0].token == token
                assert pending[0].prompt == "long delay"
                assert pending[0].resume_token == "sess-persisted"
            finally:
                tg.cancel_scope.cancel()

    async def test_restart_skips_cancelled_entries(self, tmp_path: Path):
        state_path = tmp_path / "active_loops.json"
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(
                tg, _noop_run_job, FakeTransport(), 1, state_path=state_path
            )
            try:
                token = _register_simple_cron(chat_id=202)
                loop_scheduler.cancel_by_token(token)
            finally:
                tg.cancel_scope.cancel()
        loop_scheduler.uninstall()

        # Restart — cancelled entry should not be restored.
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(
                tg, _noop_run_job, FakeTransport(), 1, state_path=state_path
            )
            try:
                assert loop_scheduler.active_count() == 0
            finally:
                tg.cancel_scope.cancel()

    async def test_do_not_resume_persists_across_restart(self, tmp_path: Path):
        state_path = tmp_path / "active_loops.json"
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(
                tg, _noop_run_job, FakeTransport(), 1, state_path=state_path
            )
            try:
                loop_scheduler.mark_do_not_resume("sess-blocked")
            finally:
                tg.cancel_scope.cancel()
        loop_scheduler.uninstall()

        async with anyio.create_task_group() as tg:
            loop_scheduler.install(
                tg, _noop_run_job, FakeTransport(), 1, state_path=state_path
            )
            try:
                assert loop_scheduler.is_do_not_resume("sess-blocked")
            finally:
                tg.cancel_scope.cancel()

    async def test_corrupt_state_file_is_ignored(self, tmp_path: Path):
        state_path = tmp_path / "active_loops.json"
        state_path.write_text("not valid json{{")
        async with anyio.create_task_group() as tg:
            # Should not raise.
            loop_scheduler.install(
                tg, _noop_run_job, FakeTransport(), 1, state_path=state_path
            )
            try:
                assert loop_scheduler.active_count() == 0
            finally:
                tg.cancel_scope.cancel()

    async def test_persistence_disabled_when_no_path(self):
        """install(state_path=None) skips persistence — used by tests."""
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                _register_simple_cron(chat_id=203)
                # No file created anywhere; nothing to assert directly,
                # but the call must not have raised on persist.
                assert loop_scheduler.active_count() == 1
            finally:
                tg.cancel_scope.cancel()


# ── do-not-resume sentinel ──────────────────────────────────────────────


class TestDoNotResume:
    @pytest.fixture(autouse=True)
    def _cleanup(self):
        loop_scheduler.uninstall()
        yield
        loop_scheduler.uninstall()

    def test_mark_then_check(self):
        loop_scheduler.mark_do_not_resume("sess-x")
        assert loop_scheduler.is_do_not_resume("sess-x")

    def test_unknown_session_returns_false(self):
        assert not loop_scheduler.is_do_not_resume("never-marked")

    def test_mark_is_idempotent(self):
        loop_scheduler.mark_do_not_resume("sess-y")
        loop_scheduler.mark_do_not_resume("sess-y")
        assert loop_scheduler.is_do_not_resume("sess-y")


# ── #925: Untether owns the schedule (rc20) ─────────────────────────────


def _patch_live(
    monkeypatch,
    *,
    alive=lambda sid: True,
    accepting=lambda sid: False,
    closeable=lambda sid: False,
    close_result=True,
    closes: list | None = None,
    interval: float = 0.01,
):
    """Stub the live-session probes the fire path consults."""
    monkeypatch.setattr(loop_scheduler, "_is_session_alive_safe", alive)
    monkeypatch.setattr(loop_scheduler, "_is_session_accepting_safe", accepting)
    monkeypatch.setattr(loop_scheduler, "_live_session_loop_closeable_safe", closeable)

    async def _close(sid):
        if closes is not None:
            closes.append(sid)
        return close_result(sid) if callable(close_result) else close_result

    monkeypatch.setattr(loop_scheduler, "_close_live_session_for_fire", _close)
    monkeypatch.setattr(loop_scheduler, "_redundancy_check_interval", lambda: interval)


class TestRegisterIdempotent:
    @pytest.fixture(autouse=True)
    def _cleanup(self):
        loop_scheduler.uninstall()
        yield
        loop_scheduler.uninstall()

    async def test_register_is_idempotent_per_tool_use_id(self):
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                first = _register_simple_cron(chat_id=300, tool_use_id="tu-dup")
                second = _register_simple_cron(chat_id=300, tool_use_id="tu-dup")
                assert first == second
                assert loop_scheduler.active_count() == 1
                assert loop_scheduler.token_for_tool_use("tu-dup") == first
            finally:
                tg.cancel_scope.cancel()

    async def test_cancelled_tool_use_id_is_not_reused(self):
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                first = _register_simple_cron(chat_id=301, tool_use_id="tu-again")
                loop_scheduler.cancel_by_token(first)
                assert loop_scheduler.token_for_tool_use("tu-again") is None
            finally:
                tg.cancel_scope.cancel()

    async def test_entry_summary(self):
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                token = _register_simple_cron(chat_id=302, prompt="tick")
                summary = loop_scheduler.entry_summary(token)
                assert summary is not None
                assert summary["prompt"] == "tick"
                assert summary["cron_expression"] == "*/5 * * * *"
                assert summary["max_iterations"] == 20
                assert loop_scheduler.entry_summary("ut_loop_nope") is None
            finally:
                tg.cancel_scope.cancel()


class TestLiveFirePath:
    """#925 D-D: firing while a process still owns the session."""

    @pytest.fixture(autouse=True)
    def _cleanup(self):
        loop_scheduler.uninstall()
        yield
        loop_scheduler.uninstall()

    async def test_cron_alive_closeable_closes_session_then_fires(self, monkeypatch):
        from structlog.testing import capture_logs

        recorder = RunJobRecorder()
        closes: list[str] = []
        _patch_live(monkeypatch, closeable=lambda sid: True, closes=closes)
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, recorder, FakeTransport(), 1)
            try:
                token = _register_simple_cron(chat_id=310, session_id="sess-live")
                entry = loop_scheduler._PENDING_BY_TOKEN[token]
                with capture_logs() as logs:
                    await loop_scheduler._fire(token)
                assert closes == ["sess-live"]
                assert len(recorder.calls) == 1
                assert entry.iteration_count == 1
                assert any(
                    e["event"] == "loop.live_session_closed_for_fire" for e in logs
                )
            finally:
                tg.cancel_scope.cancel()

    async def test_cron_alive_busy_defers_then_skips_at_next_fire(self, monkeypatch):
        from structlog.testing import capture_logs

        recorder = RunJobRecorder()
        _patch_live(monkeypatch)
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, recorder, FakeTransport(), 1)
            try:
                token = _register_simple_cron(chat_id=311, session_id="sess-busy")
                entry = loop_scheduler._PENDING_BY_TOKEN[token]
                generation = entry.generation
                # The next cron fire lands 50 ms from now.
                monkeypatch.setattr(
                    loop_scheduler,
                    "_next_cron_fire",
                    lambda _expr: time.monotonic() + 0.05,
                )
                with capture_logs() as logs:
                    await loop_scheduler._fire(token)
                assert recorder.calls == []
                assert entry.iteration_count == 0
                assert entry.generation == generation + 1  # re-armed
                assert token in loop_scheduler._PENDING_BY_TOKEN
                events = [e["event"] for e in logs]
                assert "loop.iteration_skipped_session_busy" in events
                assert "loop.fire_deferred_session_busy" in events
                assert "loop.fire_skipped_subprocess_alive" not in events
            finally:
                tg.cancel_scope.cancel()

    async def test_cron_alive_busy_then_idle_fires_once(self, monkeypatch):
        recorder = RunJobRecorder()
        probes: list[str] = []

        def closeable(sid):
            probes.append(sid)
            return len(probes) >= 2  # busy on the first probe, idle after

        _patch_live(monkeypatch, closeable=closeable)
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, recorder, FakeTransport(), 1)
            try:
                token = _register_simple_cron(chat_id=312, session_id="s")
                entry = loop_scheduler._PENDING_BY_TOKEN[token]
                await loop_scheduler._fire(token)
                assert len(recorder.calls) == 1
                assert entry.iteration_count == 1
            finally:
                tg.cancel_scope.cancel()

    async def test_cron_close_refused_falls_back_to_busy_retry(self, monkeypatch):
        """§13 amendment 2: a refused close (a follow-up got in first) is
        busy — never spawn the iteration then."""
        recorder = RunJobRecorder()
        closes: list[str] = []
        _patch_live(
            monkeypatch,
            closeable=lambda sid: True,
            close_result=False,
            closes=closes,
        )
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, recorder, FakeTransport(), 1)
            try:
                token = _register_simple_cron(chat_id=313, session_id="s")
                monkeypatch.setattr(
                    loop_scheduler,
                    "_next_cron_fire",
                    lambda _expr: time.monotonic() + 0.05,
                )
                await loop_scheduler._fire(token)
                assert recorder.calls == []
                assert len(closes) >= 1
            finally:
                tg.cancel_scope.cancel()

    async def test_wakeup_alive_expires_cli_fired_live(self, monkeypatch):
        from structlog.testing import capture_logs

        recorder = RunJobRecorder()
        _patch_live(monkeypatch, accepting=lambda sid: True)
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, recorder, FakeTransport(), 1)
            try:
                token = loop_scheduler.register_pending_wakeup(
                    session_id="s-wake",
                    tool_use_id="tw",
                    delay_seconds=600,
                    prompt="check",
                    chat_id=314,
                )
                with capture_logs() as logs:
                    await loop_scheduler._fire(token)
                assert recorder.calls == []
                assert token not in loop_scheduler._PENDING_BY_TOKEN
                expired = [e for e in logs if e["event"] == "loop.expired"]
                assert expired and expired[0]["reason"] == "cli_fired_live"
            finally:
                tg.cancel_scope.cancel()

    async def test_wakeup_dead_still_fires(self, monkeypatch):
        recorder = RunJobRecorder()
        _patch_live(monkeypatch, alive=lambda sid: False)
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, recorder, FakeTransport(), 1)
            try:
                token = loop_scheduler.register_pending_wakeup(
                    session_id="s-dead",
                    tool_use_id="tw2",
                    delay_seconds=600,
                    prompt="check",
                    chat_id=315,
                )
                await loop_scheduler._fire(token)
                assert len(recorder.calls) == 1
            finally:
                tg.cancel_scope.cancel()

    async def test_wakeup_alive_but_not_accepting_still_fires_after_exit(
        self, monkeypatch
    ):
        """§13 amendment 3: a process in limbo / closing can't fire the wake
        itself — retry and fire once it has exited."""
        recorder = RunJobRecorder()
        alive_probes: list[str] = []

        def alive(sid):
            alive_probes.append(sid)
            return len(alive_probes) < 3

        _patch_live(monkeypatch, alive=alive, accepting=lambda sid: False)
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, recorder, FakeTransport(), 1)
            try:
                token = loop_scheduler.register_pending_wakeup(
                    session_id="s-limbo",
                    tool_use_id="tw3",
                    delay_seconds=600,
                    prompt="check",
                    chat_id=316,
                )
                await loop_scheduler._fire(token)
                assert len(recorder.calls) == 1
                assert token not in loop_scheduler._PENDING_BY_TOKEN  # one-shot
            finally:
                tg.cancel_scope.cancel()

    async def test_max_iterations_enforced_across_live_closes(self, monkeypatch):
        from structlog.testing import capture_logs

        recorder = RunJobRecorder()
        _patch_live(monkeypatch, closeable=lambda sid: True)
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, recorder, FakeTransport(), 1)
            try:
                token = loop_scheduler.register_pending_cron(
                    session_id="s-cap",
                    tool_use_id="tcap",
                    cron_expression="* * * * *",
                    prompt="tick",
                    recurring=True,
                    chat_id=317,
                    max_iterations=2,
                )
                with capture_logs() as logs:
                    for _ in range(3):
                        await loop_scheduler._fire(token)
                assert len(recorder.calls) == 2
                expired = [e for e in logs if e["event"] == "loop.expired"]
                assert expired[0]["reason"] == "max_iterations"
                assert expired[0]["iterations_completed"] == 2
            finally:
                tg.cancel_scope.cancel()

    async def test_duration_cap_enforced_while_session_busy(self, monkeypatch):
        recorder = RunJobRecorder()
        _patch_live(monkeypatch)
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, recorder, FakeTransport(), 1)
            try:
                token = _register_simple_cron(chat_id=318, session_id="s")
                entry = loop_scheduler._PENDING_BY_TOKEN[token]
                done = anyio.Event()

                async def run_fire():
                    await loop_scheduler._fire(token)
                    done.set()

                tg.start_soon(run_fire)
                await anyio.sleep(0.03)
                # The loop has now run past max_total_duration_hours.
                entry.created_at_wallclock -= 5 * 3600
                with anyio.fail_after(2):
                    await done.wait()
                assert recorder.calls == []
                assert token not in loop_scheduler._PENDING_BY_TOKEN
            finally:
                tg.cancel_scope.cancel()

    async def test_busy_retry_never_double_fires_after_rearm(self, monkeypatch):
        """§13 amendment 4: a retry still asleep when the entry is re-armed
        (generation bump) bails instead of firing an extra iteration."""
        recorder = RunJobRecorder()
        idle = {"value": False}
        _patch_live(monkeypatch, closeable=lambda sid: idle["value"])
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, recorder, FakeTransport(), 1)
            try:
                token = _register_simple_cron(chat_id=319, session_id="s")
                entry = loop_scheduler._PENDING_BY_TOKEN[token]
                done = anyio.Event()

                async def run_fire():
                    await loop_scheduler._fire(token)
                    done.set()

                tg.start_soon(run_fire)
                await anyio.sleep(0.03)
                entry.generation += 1  # re-armed elsewhere
                idle["value"] = True
                with anyio.fail_after(2):
                    await done.wait()
                assert recorder.calls == []
            finally:
                tg.cancel_scope.cancel()

    async def test_chat_busy_skips_before_live_check(self, monkeypatch):
        """§13 amendment 1: an open turn makes the chat busy, so the fire is
        skipped (``iteration_skipped_previous_running``) before D-D runs."""
        from structlog.testing import capture_logs

        recorder = RunJobRecorder()
        probes: list[str] = []
        _patch_live(monkeypatch, closeable=lambda sid: probes.append(sid) or True)
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(
                tg, recorder, FakeTransport(), 1, is_chat_busy=lambda _c: True
            )
            try:
                token = _register_simple_cron(chat_id=320, session_id="s")
                with capture_logs() as logs:
                    await loop_scheduler._fire(token)
                assert recorder.calls == []
                assert probes == []
                assert any(
                    e["event"] == "loop.iteration_skipped_previous_running"
                    for e in logs
                )
            finally:
                tg.cancel_scope.cancel()


class TestCancelReason:
    @pytest.fixture(autouse=True)
    def _cleanup(self):
        loop_scheduler.uninstall()
        yield
        loop_scheduler.uninstall()

    async def test_cancel_by_token_logs_reason(self):
        from structlog.testing import capture_logs

        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                token = _register_simple_cron(chat_id=330)
                other = _register_simple_cron(chat_id=331)
                with capture_logs() as logs:
                    loop_scheduler.cancel_by_token(token, reason="cron_delete")
                    loop_scheduler.cancel_by_token(other)
                reasons = [e["reason"] for e in logs if e["event"] == "loop.cancelled"]
                assert reasons == ["cron_delete", "user_cancel"]
            finally:
                tg.cancel_scope.cancel()


class TestBindSuppression:
    """#925 D-E: a CLI-accepted job marks its session cron-suppressed."""

    @pytest.fixture(autouse=True)
    def _cleanup(self):
        loop_scheduler.uninstall()
        yield
        loop_scheduler.uninstall()

    async def test_bind_upstream_id_marks_cron_suppressed(self):
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                _register_simple_cron(
                    chat_id=340, session_id="sess-bind", tool_use_id="tu-bind"
                )
                assert loop_scheduler.cron_suppressed_until("sess-bind") is None
                loop_scheduler.bind_upstream_id("tu-bind", "abcd1234")
                until = loop_scheduler.cron_suppressed_until("sess-bind")
                assert until is not None
                assert until - time.time() == pytest.approx(
                    loop_scheduler.CLI_CRON_MAX_AGE_S, abs=60
                )
            finally:
                tg.cancel_scope.cancel()

    async def test_bind_upstream_id_no_suppression_when_own_schedule_false(
        self, monkeypatch
    ):
        monkeypatch.setattr(loop_scheduler, "own_schedule_enabled", lambda: False)
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                _register_simple_cron(
                    chat_id=341, session_id="sess-ks", tool_use_id="tu-ks"
                )
                loop_scheduler.bind_upstream_id("tu-ks", "abcd1234")
                assert loop_scheduler.cron_suppressed_until("sess-ks") is None
                entry = loop_scheduler.pending_for_chat(341)[0]
                assert entry.upstream_cron_id == "abcd1234"
            finally:
                tg.cancel_scope.cancel()


# ── #926: per-loop cancel sentinel + cron suppression (rc20) ───────────


class TestScopedCancelSentinel:
    @pytest.fixture(autouse=True)
    def _cleanup(self):
        loop_scheduler.uninstall()
        yield
        loop_scheduler.uninstall()

    async def test_loop_registered_after_cancel_fires(self, monkeypatch):
        """The 04:27:00 evidence: a loop created after /cancel in the same
        session must fire, not expire ``do_not_resume``."""
        from structlog.testing import capture_logs

        recorder = RunJobRecorder()
        monkeypatch.setattr(loop_scheduler, "_is_session_alive_safe", lambda s: False)
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, recorder, FakeTransport(), 1)
            try:
                first = _register_simple_cron(
                    chat_id=400, session_id="S", tool_use_id="tu-a"
                )
                loop_scheduler.cancel_by_token(first)
                await anyio.sleep(0.01)  # a later wall-clock instant
                second = _register_simple_cron(
                    chat_id=400, session_id="S", tool_use_id="tu-b"
                )
                with capture_logs() as logs:
                    await loop_scheduler._fire(second)
                assert len(recorder.calls) == 1
                assert any(e["event"] == "loop.fired_ok" for e in logs)
                assert not any(
                    e["event"] == "loop.expired" and e["reason"] == "do_not_resume"
                    for e in logs
                )
            finally:
                tg.cancel_scope.cancel()

    async def test_entry_created_before_cancel_is_blocked(self):
        recorder = RunJobRecorder()
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, recorder, FakeTransport(), 1)
            try:
                token = _register_simple_cron(chat_id=401, session_id="S2")
                entry = loop_scheduler._PENDING_BY_TOKEN[token]
                loop_scheduler._DO_NOT_RESUME_AT["S2"] = entry.created_at_wallclock + 1
                await loop_scheduler._fire(token)
                assert recorder.calls == []
                assert token not in loop_scheduler._PENDING_BY_TOKEN
            finally:
                tg.cancel_scope.cancel()

    def test_sentinel_pruned_after_7_days(self):
        old = time.time() - loop_scheduler.CLI_CRON_MAX_AGE_S - 10
        loop_scheduler._DO_NOT_RESUME_AT["old"] = old
        loop_scheduler._DO_NOT_RESUME_AT["new"] = time.time()
        loop_scheduler._prune_sentinels()
        assert not loop_scheduler.is_do_not_resume("old")
        assert loop_scheduler.is_do_not_resume("new")

    def test_is_do_not_resume_truthiness_unchanged(self):
        assert not loop_scheduler.is_do_not_resume("s")
        loop_scheduler.mark_do_not_resume("s")
        assert loop_scheduler.is_do_not_resume("s")
        at = loop_scheduler._DO_NOT_RESUME_AT["s"]
        loop_scheduler.mark_do_not_resume("s")  # idempotent: time kept
        assert loop_scheduler._DO_NOT_RESUME_AT["s"] == at


class TestCronSuppression:
    @pytest.fixture(autouse=True)
    def _cleanup(self):
        loop_scheduler.uninstall()
        yield
        loop_scheduler.uninstall()

    async def test_cancel_without_upstream_id_does_not_suppress(self):
        """The post-#925 normal case: the CronCreate was declined, so the
        CLI holds nothing and the resumed session keeps scheduling."""
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                token = _register_simple_cron(chat_id=410, session_id="S")
                loop_scheduler.cancel_by_token(token)
                assert loop_scheduler.cron_suppressed_until("S") is None
            finally:
                tg.cancel_scope.cancel()

    async def test_cancel_with_upstream_id_suppresses_until_created_plus_7d(
        self, monkeypatch
    ):
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                token = _register_simple_cron(
                    chat_id=411, session_id="S", tool_use_id="tu-up"
                )
                entry = loop_scheduler._PENDING_BY_TOKEN[token]
                # Bind with suppression off so only the cancel marks it.
                monkeypatch.setattr(
                    loop_scheduler, "own_schedule_enabled", lambda: False
                )
                loop_scheduler.bind_upstream_id("tu-up", "6a9af2cb")
                assert loop_scheduler.cron_suppressed_until("S") is None
                monkeypatch.setattr(
                    loop_scheduler, "own_schedule_enabled", lambda: True
                )
                loop_scheduler.cancel_by_token(token)
                assert loop_scheduler.cron_suppressed_until("S") == pytest.approx(
                    entry.created_at_wallclock + loop_scheduler.CLI_CRON_MAX_AGE_S
                )
            finally:
                tg.cancel_scope.cancel()

    async def test_cancel_marks_nothing_when_own_schedule_false(self, monkeypatch):
        """§13 amendment 4 (#926): the kill switch marks no suppression."""
        monkeypatch.setattr(loop_scheduler, "own_schedule_enabled", lambda: False)
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                token = _register_simple_cron(
                    chat_id=412, session_id="S", tool_use_id="tu-ks"
                )
                loop_scheduler.bind_upstream_id("tu-ks", "6a9af2cb")
                loop_scheduler.cancel_by_token(token)
                assert loop_scheduler.cron_suppressed_until("S") is None
            finally:
                tg.cancel_scope.cancel()

    def test_native_cron_delete_clears_that_id_only(self):
        now = time.time()
        loop_scheduler.mark_cron_suppressed(
            "S", "aaaa1111", until=now + 100, source="t"
        )
        loop_scheduler.mark_cron_suppressed("S", "bbbb2222", until=now + 50, source="t")
        assert loop_scheduler.clear_cron_suppressed("S", "aaaa1111")
        assert loop_scheduler.cron_suppressed_until("S") == pytest.approx(now + 50)
        assert loop_scheduler.clear_cron_suppressed("S", "bbbb2222")
        assert loop_scheduler.cron_suppressed_until("S") is None
        assert not loop_scheduler.clear_cron_suppressed("S", "bbbb2222")

    async def test_native_cron_delete_via_cancel_by_upstream_id_ends_unsuppressed(
        self,
    ):
        """§13 amendment 1: the clear runs after the cancel's mark."""
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop_run_job, FakeTransport(), 1)
            try:
                _register_simple_cron(chat_id=413, session_id="S", tool_use_id="tu-d")
                loop_scheduler.bind_upstream_id("tu-d", "6a9af2cb")
                assert loop_scheduler.cron_suppressed_until("S") is not None
                assert loop_scheduler.cancel_by_upstream_id("6a9af2cb")
                assert loop_scheduler.cron_suppressed_until("S") is None
            finally:
                tg.cancel_scope.cancel()

    def test_suppression_expires_and_prunes(self):
        now = time.time()
        loop_scheduler._CRON_SUPPRESSED["S"] = {"aaaa1111": now - 1}
        assert loop_scheduler.cron_suppressed_until("S") is None
        assert "S" not in loop_scheduler._CRON_SUPPRESSED
        # A past ``until`` is never recorded.
        assert not loop_scheduler.mark_cron_suppressed(
            "S", "x", until=now - 1, source="t"
        )

    def test_mark_keeps_the_later_until(self):
        now = time.time()
        loop_scheduler.mark_cron_suppressed("S", "id", until=now + 100, source="t")
        assert not loop_scheduler.mark_cron_suppressed(
            "S", "id", until=now + 50, source="t"
        )
        assert loop_scheduler.cron_suppressed_until("S") == pytest.approx(now + 100)

    def test_native_placeholder_suppression_expires_only_by_time(self, monkeypatch):
        """#926 §14 item 1 / §13b amendment 1: the detector's ``native``
        placeholder can't be named by a CronDelete — only time clears it.
        Independent of whether the detector is live."""
        now = time.time()
        loop_scheduler.mark_cron_suppressed(
            "S", "native", until=now + 100, source="native_fire"
        )
        assert not loop_scheduler.cancel_by_upstream_id("native")
        assert loop_scheduler.cron_suppressed_until("S") == pytest.approx(now + 100)
        monkeypatch.setattr(loop_scheduler.time, "time", lambda: now + 101)
        assert loop_scheduler.cron_suppressed_until("S") is None


class TestSuppressionPersistence:
    @pytest.fixture(autouse=True)
    def _cleanup(self):
        loop_scheduler.uninstall()
        yield
        loop_scheduler.uninstall()

    async def test_v1_list_migrates_to_sentinel_and_suppression(self, tmp_path):
        import json

        from structlog.testing import capture_logs

        state_path = tmp_path / "active_loops.json"
        state_path.write_text(
            json.dumps(
                {"schema_version": 1, "entries": [], "do_not_resume": ["S1", "S2"]}
            )
        )
        with capture_logs() as logs:
            async with anyio.create_task_group() as tg:
                loop_scheduler.install(
                    tg, _noop_run_job, FakeTransport(), 1, state_path=state_path
                )
                tg.cancel_scope.cancel()
        assert loop_scheduler.is_do_not_resume("S1")
        until = loop_scheduler.cron_suppressed_until("S2")
        assert until is not None
        assert until - time.time() == pytest.approx(
            loop_scheduler.CLI_CRON_MAX_AGE_S, abs=60
        )
        marks = [e for e in logs if e["event"] == "loop.cron_suppressed_marked"]
        assert sorted(m["session"] for m in marks) == ["S1", "S2"]
        assert {m["source"] for m in marks} == {"restore_v1"}
        # Written back in the new shape, so a second restart doesn't re-migrate.
        raw = json.loads(state_path.read_text())
        assert set(raw["do_not_resume_at"]) == {"S1", "S2"}

    async def test_restored_entry_with_upstream_id_marks_suppression(self, tmp_path):
        state_path = tmp_path / "active_loops.json"
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(
                tg, _noop_run_job, FakeTransport(), 1, state_path=state_path
            )
            try:
                _register_simple_cron(chat_id=420, session_id="S", tool_use_id="tu-r")
                loop_scheduler.bind_upstream_id("tu-r", "6a9af2cb")
            finally:
                tg.cancel_scope.cancel()
        # Simulate a pre-rc20 file: the entry has an upstream id, no record.
        import json

        raw = json.loads(state_path.read_text())
        raw.pop("cron_suppressed", None)
        raw.pop("do_not_resume_at", None)
        state_path.write_text(json.dumps(raw))
        loop_scheduler.uninstall()
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(
                tg, _noop_run_job, FakeTransport(), 1, state_path=state_path
            )
            tg.cancel_scope.cancel()
        assert loop_scheduler.cron_suppressed_until("S") is not None

    async def test_written_file_keeps_do_not_resume_list_for_rc19_reader(
        self, tmp_path
    ):
        import json

        state_path = tmp_path / "active_loops.json"
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(
                tg, _noop_run_job, FakeTransport(), 1, state_path=state_path
            )
            try:
                token = _register_simple_cron(chat_id=421, session_id="S")
                loop_scheduler.cancel_by_token(token)
            finally:
                tg.cancel_scope.cancel()
        raw = json.loads(state_path.read_text())
        assert raw["schema_version"] == 1
        assert isinstance(raw["do_not_resume"], list)
        assert raw["do_not_resume"] == ["S"]
        assert isinstance(raw["do_not_resume_at"], dict)

    async def test_round_trip_suppression(self, tmp_path):
        state_path = tmp_path / "active_loops.json"
        until = time.time() + 1000
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(
                tg, _noop_run_job, FakeTransport(), 1, state_path=state_path
            )
            loop_scheduler.mark_cron_suppressed(
                "S", "abcd1234", until=until, source="t"
            )
            loop_scheduler.mark_do_not_resume("S")
            tg.cancel_scope.cancel()
        at = loop_scheduler._DO_NOT_RESUME_AT["S"]
        loop_scheduler.uninstall()
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(
                tg, _noop_run_job, FakeTransport(), 1, state_path=state_path
            )
            tg.cancel_scope.cancel()
        assert loop_scheduler.cron_suppressed_until("S") == pytest.approx(until)
        assert loop_scheduler._DO_NOT_RESUME_AT["S"] == pytest.approx(at)
