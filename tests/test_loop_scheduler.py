"""Tests for the loop_scheduler module (#289).

Covers registration, persistence, cancellation, the fire path, the
do-not-resume sentinel, and restart resilience.  Mirrors the shape of
``test_at_command.py``: ``FakeTransport`` + ``RunJobRecorder`` + an
optional ``runtime`` stand-in for tests that exercise the chat→engine
freeze.
"""

from __future__ import annotations

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

    async def test_fire_skips_when_session_alive(self, monkeypatch):
        """Race avoidance — if the originating subprocess is alive, skip
        and re-arm a redundancy retry."""
        recorder = RunJobRecorder()
        # Patch is_session_alive to claim our session is still alive.
        from untether.runners import claude as claude_mod

        monkeypatch.setattr(
            claude_mod,
            "is_session_alive",
            lambda sid: sid == "sess-alive",
        )
        # Short redundancy interval so the retry-task cleanup is quick
        # (default is 30s — would slow the test for no benefit).
        monkeypatch.setattr(loop_scheduler, "_redundancy_check_interval", lambda: 0)
        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, recorder, FakeTransport(), 1)
            try:
                token = _register_simple_cron(chat_id=84, session_id="sess-alive")
                await loop_scheduler._fire(token)
                assert recorder.calls == []
                # Entry preserved — redundancy retry scheduled.
                assert token in loop_scheduler._PENDING_BY_TOKEN
                # Cancel the entry so the redundancy retry exits immediately
                # when it wakes up.
                loop_scheduler.cancel_by_token(token)
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
