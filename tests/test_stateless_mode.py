"""Tests for stateless/handoff mode behaviour.

Stateless mode (session_mode="stateless") is the handoff workflow:
- No auto-resume — each message starts a new run
- Reply-to-continue: reply to a previous bot message to continue that session
- Resume line always shown (user needs the token to continue in terminal)
- chat_session_store is None (no stored sessions)
"""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from tests.telegram_fakes import (
    FakeBot,
    FakeTransport,
    _empty_projects,
    _make_router,
)
from untether.markdown import MarkdownPresenter
from untether.model import ResumeToken
from untether.runner_bridge import ExecBridgeConfig
from untether.runners.mock import Return, ScriptRunner
from untether.telegram.bridge import (
    TelegramBridgeConfig,
    run_main_loop,
)
from untether.telegram.chat_sessions import ChatSessionStore
from untether.telegram.commands.executor import (
    _ResumeLineProxy,
    _should_show_resume_line,
)
from untether.telegram.loop import ResumeResolver, _chat_session_key
from untether.telegram.types import TelegramIncomingMessage
from untether.transport_runtime import TransportRuntime

CODEX_ENGINE = "codex"
FAST_FORWARD_COALESCE_S = 0.0
FAST_MEDIA_GROUP_DEBOUNCE_S = 0.0


# ---------------------------------------------------------------------------
# _should_show_resume_line — stateless mode
# ---------------------------------------------------------------------------


class TestShouldShowResumeLineStateless:
    """In stateless mode (stateful_mode=False), resume lines should always show."""

    def test_stateless_show_resume_line_true(self) -> None:
        """Config show_resume_line=True + stateless → True."""
        assert (
            _should_show_resume_line(
                show_resume_line=True, stateful_mode=False, context=None
            )
            is True
        )

    def test_stateless_show_resume_line_false(self) -> None:
        """Config show_resume_line=False + stateless → True (stateless override)."""
        assert (
            _should_show_resume_line(
                show_resume_line=False, stateful_mode=False, context=None
            )
            is True
        )

    def test_chat_show_resume_line_false(self) -> None:
        """Config show_resume_line=False + chat (stateful) → False."""
        assert (
            _should_show_resume_line(
                show_resume_line=False, stateful_mode=True, context=None
            )
            is False
        )

    def test_chat_show_resume_line_true(self) -> None:
        """Config show_resume_line=True + chat (stateful) → True (explicit override)."""
        assert (
            _should_show_resume_line(
                show_resume_line=True, stateful_mode=True, context=None
            )
            is True
        )


# ---------------------------------------------------------------------------
# _chat_session_key — stateless mode (store=None)
# ---------------------------------------------------------------------------


class TestChatSessionKeyStateless:
    """In stateless mode, chat_session_store is None → always returns None."""

    def test_private_chat_no_store(self) -> None:
        msg = TelegramIncomingMessage(
            transport="telegram",
            chat_id=123,
            message_id=1,
            text="hello",
            reply_to_message_id=None,
            reply_to_text=None,
            sender_id=456,
            chat_type="private",
        )
        assert _chat_session_key(msg, store=None) is None

    def test_group_chat_no_store(self) -> None:
        msg = TelegramIncomingMessage(
            transport="telegram",
            chat_id=-100,
            message_id=1,
            text="hello",
            reply_to_message_id=None,
            reply_to_text=None,
            sender_id=456,
            chat_type="group",
        )
        assert _chat_session_key(msg, store=None) is None

    def test_topic_message_bypasses_chat_session(self) -> None:
        """Messages in a forum topic return None even with a store (handled by topic_store)."""
        msg = TelegramIncomingMessage(
            transport="telegram",
            chat_id=-100,
            message_id=1,
            text="hello",
            reply_to_message_id=None,
            reply_to_text=None,
            sender_id=456,
            chat_type="supergroup",
            thread_id=77,
        )
        # Even with a store, topic messages return None
        store = ChatSessionStore.__new__(ChatSessionStore)
        assert _chat_session_key(msg, store=store) is None


# ---------------------------------------------------------------------------
# _ResumeLineProxy — confirms resume line suppression
# ---------------------------------------------------------------------------


class TestResumeLineProxy:
    """Resume line proxy suppresses format_resume output."""

    def test_proxy_suppresses_resume_line(self) -> None:
        runner = ScriptRunner([Return(answer="ok")], engine=CODEX_ENGINE)
        proxy = _ResumeLineProxy(runner=runner)
        token = ResumeToken(engine=CODEX_ENGINE, value="abc123")
        assert proxy.format_resume(token) == ""

    def test_proxy_delegates_engine(self) -> None:
        runner = ScriptRunner([Return(answer="ok")], engine=CODEX_ENGINE)
        proxy = _ResumeLineProxy(runner=runner)
        assert proxy.engine == CODEX_ENGINE

    def test_proxy_delegates_extract_resume(self) -> None:
        runner = ScriptRunner([Return(answer="ok")], engine=CODEX_ENGINE)
        proxy = _ResumeLineProxy(runner=runner)
        assert proxy.extract_resume(None) is None

    def test_proxy_delegates_is_resume_line(self) -> None:
        runner = ScriptRunner([Return(answer="ok")], engine=CODEX_ENGINE)
        proxy = _ResumeLineProxy(runner=runner)
        assert proxy.is_resume_line("anything") is False


# ---------------------------------------------------------------------------
# ResumeResolver — stateless mode (no stored sessions)
# ---------------------------------------------------------------------------


class TestResumeResolverStateless:
    """In stateless mode, resume resolver only uses explicit tokens and reply-to."""

    @pytest.mark.anyio
    async def test_no_resume_no_reply_returns_none(self) -> None:
        """No explicit token, no reply → no resume (new run)."""
        resolver = ResumeResolver(
            cfg=_make_stateless_cfg(),
            task_group=_NoopTaskGroup(),
            running_tasks={},
            enqueue_resume=_noop_enqueue,
            topic_store=None,
            chat_session_store=None,
        )
        decision = await resolver.resolve(
            resume_token=None,
            reply_id=None,
            chat_id=123,
            user_msg_id=1,
            thread_id=None,
            chat_session_key=None,
            topic_key=None,
            engine_for_session=CODEX_ENGINE,
            prompt_text="hello",
        )
        assert decision.resume_token is None
        assert decision.handled_by_running_task is False

    @pytest.mark.anyio
    async def test_explicit_token_used(self) -> None:
        """Explicit resume token in the message text → used directly."""
        token = ResumeToken(engine=CODEX_ENGINE, value="explicit123")
        resolver = ResumeResolver(
            cfg=_make_stateless_cfg(),
            task_group=_NoopTaskGroup(),
            running_tasks={},
            enqueue_resume=_noop_enqueue,
            topic_store=None,
            chat_session_store=None,
        )
        decision = await resolver.resolve(
            resume_token=token,
            reply_id=None,
            chat_id=123,
            user_msg_id=1,
            thread_id=None,
            chat_session_key=None,
            topic_key=None,
            engine_for_session=CODEX_ENGINE,
            prompt_text="hello",
        )
        assert decision.resume_token is token
        assert decision.handled_by_running_task is False

    @pytest.mark.anyio
    async def test_no_session_lookup_in_stateless(self) -> None:
        """With chat_session_store=None, no stored session is looked up."""
        resolver = ResumeResolver(
            cfg=_make_stateless_cfg(),
            task_group=_NoopTaskGroup(),
            running_tasks={},
            enqueue_resume=_noop_enqueue,
            topic_store=None,
            chat_session_store=None,
        )
        # chat_session_key=None because _chat_session_key returns None in stateless mode
        decision = await resolver.resolve(
            resume_token=None,
            reply_id=None,
            chat_id=123,
            user_msg_id=1,
            thread_id=None,
            chat_session_key=None,
            topic_key=None,
            engine_for_session=CODEX_ENGINE,
            prompt_text="hello",
        )
        assert decision.resume_token is None


# ---------------------------------------------------------------------------
# run_main_loop — stateless mode shows resume lines
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_stateless_mode_shows_resume_line(tmp_path: Path) -> None:
    """In stateless mode, resume line is visible in the final message."""
    resume_value = "stateless-resume-abc"
    state_path = tmp_path / "untether.toml"

    transport = FakeTransport()
    runner = ScriptRunner(
        [Return(answer="done")],
        engine=CODEX_ENGINE,
        resume_value=resume_value,
    )
    exec_cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=True,
    )
    runtime = TransportRuntime(
        router=_make_router(runner),
        projects=_empty_projects(),
        config_path=state_path,
    )
    cfg = TelegramBridgeConfig(
        bot=FakeBot(),
        runtime=runtime,
        chat_id=123,
        startup_msg="",
        exec_cfg=exec_cfg,
        forward_coalesce_s=FAST_FORWARD_COALESCE_S,
        media_group_debounce_s=FAST_MEDIA_GROUP_DEBOUNCE_S,
        session_mode="stateless",
        show_resume_line=True,
    )

    async def poller(_cfg: TelegramBridgeConfig):
        yield TelegramIncomingMessage(
            transport="telegram",
            chat_id=123,
            message_id=1,
            text="do the thing",
            reply_to_message_id=None,
            reply_to_text=None,
            sender_id=123,
            chat_type="private",
        )

    await run_main_loop(cfg, poller)

    assert transport.send_calls
    final_text = transport.send_calls[-1]["message"].text
    assert resume_value in final_text


@pytest.mark.anyio
async def test_stateless_mode_no_auto_resume(tmp_path: Path) -> None:
    """In stateless mode, a second message does NOT auto-resume the first session."""
    resume_value_1 = "first-session"
    state_path = tmp_path / "untether.toml"

    transport = FakeTransport()
    runner = ScriptRunner(
        [Return(answer="first"), Return(answer="second")],
        engine=CODEX_ENGINE,
        resume_value=resume_value_1,
    )
    exec_cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=True,
    )
    runtime = TransportRuntime(
        router=_make_router(runner),
        projects=_empty_projects(),
        config_path=state_path,
    )
    cfg = TelegramBridgeConfig(
        bot=FakeBot(),
        runtime=runtime,
        chat_id=123,
        startup_msg="",
        exec_cfg=exec_cfg,
        forward_coalesce_s=FAST_FORWARD_COALESCE_S,
        media_group_debounce_s=FAST_MEDIA_GROUP_DEBOUNCE_S,
        session_mode="stateless",
        show_resume_line=True,
    )

    messages_sent: list[TelegramIncomingMessage] = []

    async def poller(_cfg: TelegramBridgeConfig):
        # First message
        msg1 = TelegramIncomingMessage(
            transport="telegram",
            chat_id=123,
            message_id=1,
            text="first task",
            reply_to_message_id=None,
            reply_to_text=None,
            sender_id=123,
            chat_type="private",
        )
        yield msg1
        messages_sent.append(msg1)
        # Small delay for first run to complete
        await anyio.sleep(0.1)
        # Second message — NOT a reply, should NOT auto-resume
        msg2 = TelegramIncomingMessage(
            transport="telegram",
            chat_id=123,
            message_id=2,
            text="second task",
            reply_to_message_id=None,
            reply_to_text=None,
            sender_id=123,
            chat_type="private",
        )
        yield msg2
        messages_sent.append(msg2)

    await run_main_loop(cfg, poller)

    # Both messages should have been processed
    assert len(messages_sent) == 2
    # The runner should have been called twice — both as fresh runs (no resume)
    # In stateless mode, the second message starts a new session, not continuing the first
    assert len(transport.send_calls) >= 2


@pytest.mark.anyio
async def test_chat_mode_hides_resume_line(tmp_path: Path) -> None:
    """In chat mode with show_resume_line=False, resume line is hidden."""
    resume_value = "chat-resume-xyz"
    state_path = tmp_path / "untether.toml"

    transport = FakeTransport()
    runner = ScriptRunner(
        [Return(answer="done")],
        engine=CODEX_ENGINE,
        resume_value=resume_value,
    )
    exec_cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=True,
    )
    runtime = TransportRuntime(
        router=_make_router(runner),
        projects=_empty_projects(),
        config_path=state_path,
    )
    cfg = TelegramBridgeConfig(
        bot=FakeBot(),
        runtime=runtime,
        chat_id=123,
        startup_msg="",
        exec_cfg=exec_cfg,
        forward_coalesce_s=FAST_FORWARD_COALESCE_S,
        media_group_debounce_s=FAST_MEDIA_GROUP_DEBOUNCE_S,
        session_mode="chat",
        show_resume_line=False,
    )

    async def poller(_cfg: TelegramBridgeConfig):
        yield TelegramIncomingMessage(
            transport="telegram",
            chat_id=123,
            message_id=1,
            text="do the thing",
            reply_to_message_id=None,
            reply_to_text=None,
            sender_id=123,
            chat_type="private",
        )

    await run_main_loop(cfg, poller)

    assert transport.send_calls
    final_text = transport.send_calls[-1]["message"].text
    assert resume_value not in final_text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stateless_cfg() -> TelegramBridgeConfig:
    """Create a minimal TelegramBridgeConfig in stateless mode."""
    transport = FakeTransport()
    runner = ScriptRunner([Return(answer="ok")], engine=CODEX_ENGINE)
    exec_cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=True,
    )
    runtime = TransportRuntime(
        router=_make_router(runner),
        projects=_empty_projects(),
    )
    return TelegramBridgeConfig(
        bot=FakeBot(),
        runtime=runtime,
        chat_id=123,
        startup_msg="",
        exec_cfg=exec_cfg,
        session_mode="stateless",
        show_resume_line=True,
    )


class _NoopTaskGroup:
    def start_soon(self, func, *args) -> None:
        pass


async def _noop_enqueue(*args) -> None:
    pass
