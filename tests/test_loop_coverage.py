"""Phase 3 coverage tests for telegram/loop.py.

Tests for:
- _resolve_engine_run_options() — engine/model/permission resolution chain
- _drain_backlog() — startup message drain
- ForwardCoalescer — forward message aggregation timing
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import anyio
import pytest

from untether.runners.run_options import EngineRunOptions
from untether.telegram.engine_overrides import EngineOverrides
from untether.telegram.loop import (
    ForwardCoalescer,
    ForwardKey,
    _drain_backlog,
    _forward_key,
    _PendingPrompt,
    _resolve_engine_run_options,
)
from untether.telegram.types import TelegramIncomingMessage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _msg(
    chat_id: int = 100,
    message_id: int = 1,
    text: str = "hello",
    sender_id: int | None = 42,
    thread_id: int | None = None,
    raw: dict[str, Any] | None = None,
) -> TelegramIncomingMessage:
    return TelegramIncomingMessage(
        transport="test",
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        reply_to_message_id=None,
        reply_to_text=None,
        sender_id=sender_id,
        thread_id=thread_id,
        raw=raw,
    )


def _pending(
    msg: TelegramIncomingMessage | None = None,
    text: str = "hello",
    forwards: list[tuple[int, str]] | None = None,
) -> _PendingPrompt:
    if msg is None:
        msg = _msg()
    return _PendingPrompt(
        msg=msg,
        text=text,
        ambient_context=None,
        chat_project=None,
        topic_key=None,
        chat_session_key=None,
        reply_ref=None,
        reply_id=None,
        is_voice_transcribed=False,
        forwards=forwards if forwards is not None else [],
    )


@dataclass
class FakeTopicStore:
    overrides: dict[tuple[int, int, str], EngineOverrides] = field(default_factory=dict)

    async def get_engine_override(
        self, chat_id: int, thread_id: int, engine: str
    ) -> EngineOverrides | None:
        return self.overrides.get((chat_id, thread_id, engine))


@dataclass
class FakeChatPrefs:
    overrides: dict[tuple[int, str], EngineOverrides] = field(default_factory=dict)

    async def get_engine_override(
        self, chat_id: int, engine: str
    ) -> EngineOverrides | None:
        return self.overrides.get((chat_id, engine))


@dataclass
class FakeUpdate:
    update_id: int


@dataclass
class FakeBot:
    """Fake BotClient that returns scripted get_updates results."""

    responses: list[list[FakeUpdate] | None] = field(default_factory=list)
    _call_count: int = 0

    async def get_updates(
        self,
        offset: int | None,
        timeout_s: int = 50,
        allowed_updates: list[str] | None = None,
    ) -> list[FakeUpdate] | None:
        if self._call_count >= len(self.responses):
            return []
        result = self.responses[self._call_count]
        self._call_count += 1
        return result


@dataclass
class FakeConfig:
    bot: FakeBot


# ---------------------------------------------------------------------------
# 3a. _resolve_engine_run_options
# ---------------------------------------------------------------------------


class TestResolveEngineRunOptions:
    @pytest.mark.anyio
    async def test_no_stores_returns_none(self) -> None:
        result = await _resolve_engine_run_options(
            chat_id=100,
            thread_id=None,
            engine="claude",
            chat_prefs=None,
            topic_store=None,
        )
        assert result is None

    @pytest.mark.anyio
    async def test_chat_override_only(self) -> None:
        prefs = FakeChatPrefs(
            overrides={(100, "claude"): EngineOverrides(model="opus-4")}
        )
        result = await _resolve_engine_run_options(
            chat_id=100,
            thread_id=None,
            engine="claude",
            chat_prefs=prefs,
            topic_store=None,
        )
        assert result is not None
        assert result.model == "opus-4"

    @pytest.mark.anyio
    async def test_topic_override_only(self) -> None:
        topic = FakeTopicStore(
            overrides={(100, 5, "claude"): EngineOverrides(model="sonnet-4")}
        )
        result = await _resolve_engine_run_options(
            chat_id=100,
            thread_id=5,
            engine="claude",
            chat_prefs=None,
            topic_store=topic,
        )
        assert result is not None
        assert result.model == "sonnet-4"

    @pytest.mark.anyio
    async def test_topic_overrides_chat(self) -> None:
        """Topic-level model takes precedence over chat-level."""
        topic = FakeTopicStore(
            overrides={(100, 5, "claude"): EngineOverrides(model="topic-model")}
        )
        prefs = FakeChatPrefs(
            overrides={(100, "claude"): EngineOverrides(model="chat-model")}
        )
        result = await _resolve_engine_run_options(
            chat_id=100,
            thread_id=5,
            engine="claude",
            chat_prefs=prefs,
            topic_store=topic,
        )
        assert result is not None
        assert result.model == "topic-model"

    @pytest.mark.anyio
    async def test_chat_fills_when_topic_has_no_model(self) -> None:
        """If topic override exists but has no model, chat model fills in."""
        topic = FakeTopicStore(
            overrides={(100, 5, "claude"): EngineOverrides(reasoning="high")}
        )
        prefs = FakeChatPrefs(
            overrides={(100, "claude"): EngineOverrides(model="chat-model")}
        )
        result = await _resolve_engine_run_options(
            chat_id=100,
            thread_id=5,
            engine="claude",
            chat_prefs=prefs,
            topic_store=topic,
        )
        assert result is not None
        assert result.model == "chat-model"
        assert result.reasoning == "high"

    @pytest.mark.anyio
    async def test_no_thread_skips_topic_store(self) -> None:
        """Without a thread_id, topic_store is not consulted."""
        topic = FakeTopicStore(
            overrides={(100, 0, "claude"): EngineOverrides(model="topic-model")}
        )
        prefs = FakeChatPrefs(
            overrides={(100, "claude"): EngineOverrides(model="chat-model")}
        )
        result = await _resolve_engine_run_options(
            chat_id=100,
            thread_id=None,
            engine="claude",
            chat_prefs=prefs,
            topic_store=topic,
        )
        assert result is not None
        assert result.model == "chat-model"

    @pytest.mark.anyio
    async def test_no_overrides_returns_none(self) -> None:
        """Both stores present but no matching overrides → None."""
        prefs = FakeChatPrefs()
        topic = FakeTopicStore()
        result = await _resolve_engine_run_options(
            chat_id=100,
            thread_id=5,
            engine="claude",
            chat_prefs=prefs,
            topic_store=topic,
        )
        assert result is None

    @pytest.mark.anyio
    async def test_permission_mode_merged(self) -> None:
        prefs = FakeChatPrefs(
            overrides={(100, "claude"): EngineOverrides(permission_mode="plan")}
        )
        result = await _resolve_engine_run_options(
            chat_id=100,
            thread_id=None,
            engine="claude",
            chat_prefs=prefs,
            topic_store=None,
        )
        assert result is not None
        assert result.permission_mode == "plan"

    @pytest.mark.anyio
    async def test_returns_engine_run_options_type(self) -> None:
        prefs = FakeChatPrefs(overrides={(100, "claude"): EngineOverrides(model="x")})
        result = await _resolve_engine_run_options(
            chat_id=100,
            thread_id=None,
            engine="claude",
            chat_prefs=prefs,
            topic_store=None,
        )
        assert isinstance(result, EngineRunOptions)


# ---------------------------------------------------------------------------
# 3b. _drain_backlog
# ---------------------------------------------------------------------------


class TestDrainBacklog:
    @pytest.mark.anyio
    async def test_empty_backlog(self) -> None:
        """No pending updates → returns offset immediately."""
        bot = FakeBot(responses=[[]])
        cfg = FakeConfig(bot=bot)
        offset = await _drain_backlog(cfg, None)  # type: ignore[arg-type]
        assert offset is None

    @pytest.mark.anyio
    async def test_drains_multiple_batches(self) -> None:
        """Drains two batches of updates, returns offset past the last."""
        bot = FakeBot(
            responses=[
                [FakeUpdate(update_id=10), FakeUpdate(update_id=11)],
                [FakeUpdate(update_id=12)],
                [],  # empty → stop
            ]
        )
        cfg = FakeConfig(bot=bot)
        offset = await _drain_backlog(cfg, None)  # type: ignore[arg-type]
        assert offset == 13  # last update_id (12) + 1

    @pytest.mark.anyio
    async def test_api_failure_returns_original_offset(self) -> None:
        """get_updates returning None → returns the original offset."""
        bot = FakeBot(responses=[None])
        cfg = FakeConfig(bot=bot)
        offset = await _drain_backlog(cfg, 5)  # type: ignore[arg-type]
        assert offset == 5

    @pytest.mark.anyio
    async def test_single_batch(self) -> None:
        bot = FakeBot(
            responses=[
                [FakeUpdate(update_id=100)],
                [],
            ]
        )
        cfg = FakeConfig(bot=bot)
        offset = await _drain_backlog(cfg, None)  # type: ignore[arg-type]
        assert offset == 101

    @pytest.mark.anyio
    async def test_preserves_existing_offset(self) -> None:
        """Starting with a non-None offset passes it through correctly."""
        bot = FakeBot(responses=[[]])
        cfg = FakeConfig(bot=bot)
        offset = await _drain_backlog(cfg, 50)  # type: ignore[arg-type]
        assert offset == 50


# ---------------------------------------------------------------------------
# 3c. ForwardCoalescer
# ---------------------------------------------------------------------------


class TestForwardCoalescer:
    @pytest.mark.anyio
    async def test_schedule_dispatches_after_debounce(self) -> None:
        dispatched: list[_PendingPrompt] = []

        async def dispatch(p: _PendingPrompt) -> None:
            dispatched.append(p)

        pending: dict[ForwardKey, _PendingPrompt] = {}
        async with anyio.create_task_group() as tg:
            coalescer = ForwardCoalescer(
                task_group=tg,
                debounce_s=0.05,
                dispatch=dispatch,
                pending=pending,
            )
            p = _pending()
            coalescer.schedule(p)
            await anyio.sleep(0.15)

        assert len(dispatched) == 1
        assert dispatched[0] is p

    @pytest.mark.anyio
    async def test_schedule_no_sender_bypasses_debounce(self) -> None:
        """Messages without sender_id dispatch immediately (no debounce)."""
        dispatched: list[_PendingPrompt] = []

        async def dispatch(p: _PendingPrompt) -> None:
            dispatched.append(p)

        pending: dict[ForwardKey, _PendingPrompt] = {}
        async with anyio.create_task_group() as tg:
            coalescer = ForwardCoalescer(
                task_group=tg,
                debounce_s=1.0,
                dispatch=dispatch,
                pending=pending,
            )
            msg = _msg(sender_id=None)
            p = _pending(msg=msg)
            coalescer.schedule(p)
            await anyio.sleep(0.05)

        assert len(dispatched) == 1

    @pytest.mark.anyio
    async def test_schedule_zero_debounce_bypasses(self) -> None:
        """debounce_s=0 dispatches immediately."""
        dispatched: list[_PendingPrompt] = []

        async def dispatch(p: _PendingPrompt) -> None:
            dispatched.append(p)

        pending: dict[ForwardKey, _PendingPrompt] = {}
        async with anyio.create_task_group() as tg:
            coalescer = ForwardCoalescer(
                task_group=tg,
                debounce_s=0,
                dispatch=dispatch,
                pending=pending,
            )
            coalescer.schedule(_pending())
            await anyio.sleep(0.05)

        assert len(dispatched) == 1

    @pytest.mark.anyio
    async def test_cancel_prevents_dispatch(self) -> None:
        dispatched: list[_PendingPrompt] = []

        async def dispatch(p: _PendingPrompt) -> None:
            dispatched.append(p)

        pending: dict[ForwardKey, _PendingPrompt] = {}
        async with anyio.create_task_group() as tg:
            coalescer = ForwardCoalescer(
                task_group=tg,
                debounce_s=0.2,
                dispatch=dispatch,
                pending=pending,
            )
            p = _pending()
            coalescer.schedule(p)
            key = _forward_key(p.msg)
            coalescer.cancel(key)
            await anyio.sleep(0.3)

        assert len(dispatched) == 0

    @pytest.mark.anyio
    async def test_cancel_nonexistent_key_is_noop(self) -> None:
        dispatched: list[_PendingPrompt] = []

        async def dispatch(p: _PendingPrompt) -> None:
            dispatched.append(p)

        pending: dict[ForwardKey, _PendingPrompt] = {}
        async with anyio.create_task_group() as tg:
            coalescer = ForwardCoalescer(
                task_group=tg,
                debounce_s=0.1,
                dispatch=dispatch,
                pending=pending,
            )
            coalescer.cancel((999, 0, 0))  # no-op
            await anyio.sleep(0.05)

        assert len(dispatched) == 0

    @pytest.mark.anyio
    async def test_replace_resets_debounce(self) -> None:
        """A second schedule for the same key replaces the first."""
        dispatched: list[_PendingPrompt] = []

        async def dispatch(p: _PendingPrompt) -> None:
            dispatched.append(p)

        pending: dict[ForwardKey, _PendingPrompt] = {}
        async with anyio.create_task_group() as tg:
            coalescer = ForwardCoalescer(
                task_group=tg,
                debounce_s=0.1,
                dispatch=dispatch,
                pending=pending,
            )
            p1 = _pending(msg=_msg(message_id=1))
            p2 = _pending(msg=_msg(message_id=2))
            coalescer.schedule(p1)
            await anyio.sleep(0.05)
            coalescer.schedule(p2)
            await anyio.sleep(0.15)

        # Only the second prompt should have dispatched
        assert len(dispatched) == 1
        assert dispatched[0].msg.message_id == 2

    @pytest.mark.anyio
    async def test_replace_inherits_forwards(self) -> None:
        """When replacing, the new pending inherits forwards from the old one."""
        dispatched: list[_PendingPrompt] = []

        async def dispatch(p: _PendingPrompt) -> None:
            dispatched.append(p)

        pending: dict[ForwardKey, _PendingPrompt] = {}
        async with anyio.create_task_group() as tg:
            coalescer = ForwardCoalescer(
                task_group=tg,
                debounce_s=0.1,
                dispatch=dispatch,
                pending=pending,
            )
            p1 = _pending(
                msg=_msg(message_id=1),
                forwards=[(10, "forwarded text")],
            )
            p2 = _pending(msg=_msg(message_id=2))
            coalescer.schedule(p1)
            await anyio.sleep(0.02)
            coalescer.schedule(p2)
            await anyio.sleep(0.15)

        assert len(dispatched) == 1
        assert dispatched[0].forwards == [(10, "forwarded text")]

    @pytest.mark.anyio
    async def test_attach_forward_to_pending(self) -> None:
        dispatched: list[_PendingPrompt] = []

        async def dispatch(p: _PendingPrompt) -> None:
            dispatched.append(p)

        pending: dict[ForwardKey, _PendingPrompt] = {}
        async with anyio.create_task_group() as tg:
            coalescer = ForwardCoalescer(
                task_group=tg,
                debounce_s=0.15,
                dispatch=dispatch,
                pending=pending,
            )
            p = _pending()
            coalescer.schedule(p)
            await anyio.sleep(0.02)
            # Attach a forwarded message
            fwd = _msg(message_id=99, text="forwarded content")
            coalescer.attach_forward(fwd)
            await anyio.sleep(0.2)

        assert len(dispatched) == 1
        assert len(dispatched[0].forwards) == 1
        assert dispatched[0].forwards[0] == (99, "forwarded content")

    @pytest.mark.anyio
    async def test_attach_forward_no_sender_ignored(self) -> None:
        dispatched: list[_PendingPrompt] = []

        async def dispatch(p: _PendingPrompt) -> None:
            dispatched.append(p)

        pending: dict[ForwardKey, _PendingPrompt] = {}
        async with anyio.create_task_group() as tg:
            coalescer = ForwardCoalescer(
                task_group=tg,
                debounce_s=0.1,
                dispatch=dispatch,
                pending=pending,
            )
            p = _pending()
            coalescer.schedule(p)
            # Forward without sender → ignored
            fwd = _msg(message_id=99, text="no sender", sender_id=None)
            coalescer.attach_forward(fwd)
            await anyio.sleep(0.15)

        assert len(dispatched) == 1
        assert len(dispatched[0].forwards) == 0

    @pytest.mark.anyio
    async def test_attach_forward_no_pending_ignored(self) -> None:
        """Forward arrives but no matching pending prompt → ignored."""
        dispatched: list[_PendingPrompt] = []

        async def dispatch(p: _PendingPrompt) -> None:
            dispatched.append(p)

        pending: dict[ForwardKey, _PendingPrompt] = {}
        async with anyio.create_task_group() as tg:
            coalescer = ForwardCoalescer(
                task_group=tg,
                debounce_s=0.1,
                dispatch=dispatch,
                pending=pending,
            )
            fwd = _msg(message_id=99, text="orphan forward")
            coalescer.attach_forward(fwd)
            await anyio.sleep(0.05)

        assert len(dispatched) == 0

    @pytest.mark.anyio
    async def test_attach_forward_empty_text_ignored(self) -> None:
        dispatched: list[_PendingPrompt] = []

        async def dispatch(p: _PendingPrompt) -> None:
            dispatched.append(p)

        pending: dict[ForwardKey, _PendingPrompt] = {}
        async with anyio.create_task_group() as tg:
            coalescer = ForwardCoalescer(
                task_group=tg,
                debounce_s=0.15,
                dispatch=dispatch,
                pending=pending,
            )
            p = _pending()
            coalescer.schedule(p)
            fwd = _msg(message_id=99, text="   ")
            coalescer.attach_forward(fwd)
            await anyio.sleep(0.2)

        assert len(dispatched) == 1
        assert len(dispatched[0].forwards) == 0

    @pytest.mark.anyio
    async def test_forward_key_computation(self) -> None:
        msg = _msg(chat_id=100, thread_id=5, sender_id=42)
        assert _forward_key(msg) == (100, 5, 42)

    @pytest.mark.anyio
    async def test_forward_key_none_defaults(self) -> None:
        msg = _msg(chat_id=100, thread_id=None, sender_id=None)
        assert _forward_key(msg) == (100, 0, 0)

    @pytest.mark.anyio
    async def test_multiple_forwards_accumulated(self) -> None:
        dispatched: list[_PendingPrompt] = []

        async def dispatch(p: _PendingPrompt) -> None:
            dispatched.append(p)

        pending: dict[ForwardKey, _PendingPrompt] = {}
        async with anyio.create_task_group() as tg:
            coalescer = ForwardCoalescer(
                task_group=tg,
                debounce_s=0.3,
                dispatch=dispatch,
                pending=pending,
            )
            p = _pending()
            coalescer.schedule(p)
            await anyio.sleep(0.02)
            # Attach both forwards without yielding between them
            coalescer.attach_forward(_msg(message_id=10, text="first"))
            coalescer.attach_forward(_msg(message_id=11, text="second"))
            await anyio.sleep(0.5)

        assert len(dispatched) == 1
        assert len(dispatched[0].forwards) == 2
        assert dispatched[0].forwards[0] == (10, "first")
        assert dispatched[0].forwards[1] == (11, "second")

    @pytest.mark.anyio
    async def test_different_senders_independent(self) -> None:
        """Different sender_ids should have independent debounce slots."""
        dispatched: list[_PendingPrompt] = []

        async def dispatch(p: _PendingPrompt) -> None:
            dispatched.append(p)

        pending: dict[ForwardKey, _PendingPrompt] = {}
        async with anyio.create_task_group() as tg:
            coalescer = ForwardCoalescer(
                task_group=tg,
                debounce_s=0.05,
                dispatch=dispatch,
                pending=pending,
            )
            p1 = _pending(msg=_msg(sender_id=1, message_id=1))
            p2 = _pending(msg=_msg(sender_id=2, message_id=2))
            coalescer.schedule(p1)
            coalescer.schedule(p2)
            await anyio.sleep(0.15)

        assert len(dispatched) == 2
