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
from structlog.testing import capture_logs

from untether.runners.run_options import EngineRunOptions
from untether.telegram.engine_overrides import EngineOverrides
from untether.telegram.loop import (
    _ANSWERED_ECHO_MAX,
    ForwardCoalescer,
    ForwardKey,
    _apply_command_barrier,
    _classify_message,
    _drain_backlog,
    _dropped_prompt_notice,
    _format_answered_echo,
    _forward_key,
    _init_quarantine_store,
    _PendingPrompt,
    _resolve_engine_run_options,
)
from untether.telegram.steer import split_followup_command
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
    reply_id: int | None = None,
    is_voice_transcribed: bool = False,
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
        reply_id=reply_id,
        is_voice_transcribed=is_voice_transcribed,
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
    async def test_drop_prevents_dispatch(self) -> None:
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
            assert coalescer.drop(key, reason="test") is p
            await anyio.sleep(0.3)

        assert len(dispatched) == 0

    @pytest.mark.anyio
    async def test_drop_nonexistent_key_is_noop(self) -> None:
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
            assert coalescer.drop((999, 0, 0), reason="test") is None
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


# ---------------------------------------------------------------------------
# #794 — prompts inside the coalesce window are merged, never dropped
# ---------------------------------------------------------------------------


async def _run_schedules(
    prompts: list[_PendingPrompt],
    *,
    debounce_s: float = 0.1,
    gap_s: float = 0.02,
) -> list[_PendingPrompt]:
    dispatched: list[_PendingPrompt] = []

    async def dispatch(p: _PendingPrompt) -> None:
        dispatched.append(p)

    pending: dict[ForwardKey, _PendingPrompt] = {}
    async with anyio.create_task_group() as tg:
        coalescer = ForwardCoalescer(
            task_group=tg,
            debounce_s=debounce_s,
            dispatch=dispatch,
            pending=pending,
        )
        for idx, prompt in enumerate(prompts):
            if idx:
                await anyio.sleep(gap_s)
            coalescer.schedule(prompt)
        await anyio.sleep(debounce_s * 3)
    return dispatched


class TestForwardCoalescerMerge:
    @pytest.mark.anyio
    async def test_two_plain_prompts_merged_in_order(self) -> None:
        with capture_logs() as logs:
            dispatched = await _run_schedules(
                [
                    _pending(msg=_msg(message_id=1), text="first thought"),
                    _pending(msg=_msg(message_id=2), text="second thought"),
                ]
            )

        assert len(dispatched) == 1
        assert dispatched[0].text == "first thought\n\nsecond thought"
        # The run anchors on the latest message, as before.
        assert dispatched[0].msg.message_id == 2
        merged = [e for e in logs if e["event"] == "forward.prompt.merged"]
        assert len(merged) == 1
        assert merged[0]["log_level"] == "info"
        assert merged[0]["merged_count"] == 2
        assert merged[0]["merged_message_ids"] == [1]
        assert not [e for e in logs if e["event"] == "forward.prompt.replace"]

    @pytest.mark.anyio
    async def test_three_prompts_merged_in_order(self) -> None:
        with capture_logs() as logs:
            dispatched = await _run_schedules(
                [
                    _pending(msg=_msg(message_id=1), text="one"),
                    _pending(msg=_msg(message_id=2), text="two"),
                    _pending(msg=_msg(message_id=3), text="three"),
                ]
            )

        assert len(dispatched) == 1
        assert dispatched[0].text == "one\n\ntwo\n\nthree"
        merged = [e for e in logs if e["event"] == "forward.prompt.merged"]
        assert [e["merged_count"] for e in merged] == [2, 3]
        assert merged[-1]["merged_message_ids"] == [1, 2]
        run = [e for e in logs if e["event"] == "forward.prompt.run"]
        assert run[0]["merged_count"] == 3

    @pytest.mark.anyio
    async def test_merge_keeps_forwards_of_earlier_prompt(self) -> None:
        """Forward + caption: the caption's forwards survive a merge."""
        dispatched = await _run_schedules(
            [
                _pending(
                    msg=_msg(message_id=1),
                    text="summarise these",
                    forwards=[(10, "fwd a"), (11, "fwd b")],
                ),
                _pending(msg=_msg(message_id=12), text="in one line"),
            ]
        )

        assert len(dispatched) == 1
        assert dispatched[0].text == "summarise these\n\nin one line"
        assert dispatched[0].forwards == [(10, "fwd a"), (11, "fwd b")]

    @pytest.mark.anyio
    async def test_blank_earlier_text_not_joined(self) -> None:
        dispatched = await _run_schedules(
            [
                _pending(msg=_msg(message_id=1), text="   "),
                _pending(msg=_msg(message_id=2), text="real prompt"),
            ]
        )

        assert len(dispatched) == 1
        assert dispatched[0].text == "real prompt"

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("first", "second", "reason"),
        [
            (
                _pending(msg=_msg(message_id=1), text="one", reply_id=None),
                _pending(msg=_msg(message_id=2), text="two", reply_id=500),
                "reply_target",
            ),
            (
                _pending(msg=_msg(message_id=1), text="one", reply_id=400),
                _pending(msg=_msg(message_id=2), text="two", reply_id=500),
                "reply_target",
            ),
            (
                _pending(msg=_msg(message_id=1), text="one"),
                _pending(msg=_msg(message_id=2), text="/codex two"),
                "directive",
            ),
            (
                _pending(msg=_msg(message_id=1), text="one"),
                _pending(msg=_msg(message_id=2), text="@feature two"),
                "directive",
            ),
            (
                _pending(msg=_msg(message_id=1), text="one"),
                _pending(msg=_msg(message_id=2), text="two", is_voice_transcribed=True),
                "voice",
            ),
        ],
    )
    async def test_unmergeable_prompt_flushes_earlier_one(
        self, first: _PendingPrompt, second: _PendingPrompt, reason: str
    ) -> None:
        """An earlier prompt that can't be merged runs on its own, first —
        never dropped."""
        with capture_logs() as logs:
            dispatched = await _run_schedules([first, second])

        assert [p.text for p in dispatched] == [first.text, second.text]
        assert [p.msg.message_id for p in dispatched] == [1, 2]
        flushed = [e for e in logs if e["event"] == "forward.prompt.flushed"]
        assert len(flushed) == 1
        assert flushed[0]["log_level"] == "info"
        assert flushed[0]["reason"] == reason
        assert flushed[0]["message_id"] == 1

    @pytest.mark.anyio
    async def test_same_reply_target_merged(self) -> None:
        dispatched = await _run_schedules(
            [
                _pending(msg=_msg(message_id=1), text="one", reply_id=500),
                _pending(msg=_msg(message_id=2), text="two", reply_id=500),
            ]
        )

        assert len(dispatched) == 1
        assert dispatched[0].text == "one\n\ntwo"

    @pytest.mark.anyio
    async def test_directive_first_then_plain_merged_under_directive(self) -> None:
        dispatched = await _run_schedules(
            [
                _pending(msg=_msg(message_id=1), text="/codex fix the bug"),
                _pending(msg=_msg(message_id=2), text="and add a test"),
            ]
        )

        assert len(dispatched) == 1
        assert dispatched[0].text == "/codex fix the bug\n\nand add a test"

    @pytest.mark.anyio
    async def test_prompts_outside_window_run_separately(self) -> None:
        dispatched = await _run_schedules(
            [
                _pending(msg=_msg(message_id=1), text="one"),
                _pending(msg=_msg(message_id=2), text="two"),
            ],
            debounce_s=0.05,
            gap_s=0.2,
        )

        assert [p.text for p in dispatched] == ["one", "two"]


# ---------------------------------------------------------------------------
# #807 — commands are a barrier for the coalesce window
# ---------------------------------------------------------------------------

# Engine ids plus reserved command ids, as `get_reserved_commands` builds them.
_DIRECTIVES = {"codex", "claude"}
_RESERVED = {*_DIRECTIVES, "cancel", "continue", "new", "file"}


async def _run_with_commands(
    steps: list[_PendingPrompt | str],
    *,
    debounce_s: float = 0.1,
    gap_s: float = 0.02,
) -> tuple[list[str], list[tuple[_PendingPrompt, str]]]:
    """Like ``_run_schedules``, but a ``str`` step is a message routed the
    way ``route_message`` routes it: the #775 steer split, then the #807
    barrier, then either a command (started via ``start_soon``, as the loop
    does) or a prompt scheduled on the coalescer.

    Returns the order things ran in (``prompt:<text>`` / ``command:<id>``)
    and every barrier drop.
    """
    order: list[str] = []
    drops: list[tuple[_PendingPrompt, str]] = []

    async def dispatch(p: _PendingPrompt) -> None:
        order.append(f"prompt:{p.text}")

    async def handle_command(command_id: str) -> None:
        order.append(f"command:{command_id}")

    pending: dict[ForwardKey, _PendingPrompt] = {}
    async with anyio.create_task_group() as tg:
        coalescer = ForwardCoalescer(
            task_group=tg,
            debounce_s=debounce_s,
            dispatch=dispatch,
            pending=pending,
        )
        next_id = 100
        for idx, step in enumerate(steps):
            if idx:
                await anyio.sleep(gap_s)
            if isinstance(step, _PendingPrompt):
                coalescer.schedule(step)
                continue
            next_id += 1
            msg = _msg(message_id=next_id, text=step)
            classification = _classify_message(msg, files_enabled=False)
            command_id = classification.command_id
            text = classification.text
            override: str | None = None
            split = split_followup_command(command_id, classification.args_text)
            if split is not None:
                override, text = split
                command_id = None
            dropped = _apply_command_barrier(
                coalescer,
                _forward_key(msg),
                command_id=command_id,
                is_cancel=classification.is_cancel,
                reserved_commands=_RESERVED,
            )
            if dropped is not None:
                drops.append(dropped)
            if command_id is not None and command_id not in _DIRECTIVES:
                tg.start_soon(handle_command, command_id)
                continue
            prompt = _pending(msg=msg, text=text)
            prompt.followup_override = override
            coalescer.schedule(prompt)
        await anyio.sleep(min(debounce_s * 3, 0.5))
        tg.cancel_scope.cancel()
    return order, drops


class TestForwardCoalescerBarrier:
    @pytest.mark.anyio
    async def test_807_new_drops_pending_with_notice(self) -> None:
        with capture_logs() as logs:
            order, drops = await _run_with_commands(
                [_pending(msg=_msg(message_id=1), text="A"), "/new"]
            )

        assert order == ["command:new"]
        ((dropped, command),) = drops
        assert dropped.text == "A"
        assert command == "new"
        assert _dropped_prompt_notice(dropped, command=command) == (
            "🗑️ Dropped 1 message sent just before /new — "
            "send it again if you still need it."
        )
        (event,) = [e for e in logs if e["event"] == "forward.prompt.dropped"]
        assert event["log_level"] == "info"
        assert event["reason"] == "new"
        assert event["merged_count"] == 1
        assert event["message_id"] == 1
        assert not [e for e in logs if e["event"] == "forward.prompt.flushed"]

    @pytest.mark.anyio
    async def test_807_cancel_drops_pending_with_notice(self) -> None:
        """Two merged prompts are dropped together, and the notice says so."""
        with capture_logs() as logs:
            order, drops = await _run_with_commands(
                [
                    _pending(msg=_msg(message_id=1), text="A1"),
                    _pending(msg=_msg(message_id=2), text="A2"),
                    "/cancel",
                ]
            )

        assert order == ["command:cancel"]
        ((dropped, command),) = drops
        assert command == "cancel"
        assert dropped.text == "A1\n\nA2"
        assert _dropped_prompt_notice(dropped, command=command) == (
            "🗑️ Dropped 2 messages sent just before /cancel — "
            "send them again if you still need them."
        )
        (event,) = [e for e in logs if e["event"] == "forward.prompt.dropped"]
        assert event["reason"] == "cancel"
        assert event["merged_count"] == 2

    @pytest.mark.anyio
    async def test_807_continue_never_silently_drops(self) -> None:
        """`/continue` used to call a bare ``cancel()``: the prompt vanished
        with only a debug log. It now drops with a notice."""
        assert not hasattr(ForwardCoalescer, "cancel")
        with capture_logs() as logs:
            order, drops = await _run_with_commands(
                [_pending(msg=_msg(message_id=1), text="A"), "/continue"]
            )

        assert order == ["command:continue"]
        ((dropped, command),) = drops
        assert command == "continue"
        assert "just before /continue" in _dropped_prompt_notice(
            dropped, command=command
        )
        (event,) = [e for e in logs if e["event"] == "forward.prompt.dropped"]
        assert event["reason"] == "continue"

    @pytest.mark.anyio
    async def test_807_plain_command_flushes_first(self) -> None:
        with capture_logs() as logs:
            order, drops = await _run_with_commands(
                [_pending(msg=_msg(message_id=1), text="A"), "/ping"],
                # A window far longer than the test: A can only have run
                # because the command flushed it.
                debounce_s=30.0,
            )

        assert order == ["prompt:A", "command:ping"]
        assert drops == []
        (event,) = [e for e in logs if e["event"] == "forward.prompt.flushed"]
        assert event["reason"] == "command"
        assert event["message_id"] == 1
        assert not [e for e in logs if e["event"] == "forward.prompt.dropped"]

    @pytest.mark.anyio
    async def test_807_a_new_b_runs_b_only_in_new_session(self) -> None:
        order, drops = await _run_with_commands(
            [
                _pending(msg=_msg(message_id=1), text="A"),
                "/new",
                _pending(msg=_msg(message_id=3), text="B"),
            ]
        )

        # A is gone; B runs on its own (not merged behind A), after /new.
        assert order == ["command:new", "prompt:B"]
        assert [d.text for d, _ in drops] == ["A"]

    @pytest.mark.anyio
    async def test_807_steer_text_is_not_a_barrier(self) -> None:
        """`/steer <text>` is split to a prompt before the barrier, so it
        coalesces like one (kept apart from A by the #775 merge rule)."""
        with capture_logs() as logs:
            order, drops = await _run_with_commands(
                [
                    _pending(msg=_msg(message_id=1), text="A"),
                    "/steer also check the logs",
                ]
            )

        assert order == ["prompt:A", "prompt:also check the logs"]
        assert drops == []
        flushed = [e for e in logs if e["event"] == "forward.prompt.flushed"]
        assert [e["reason"] for e in flushed] == ["followup_mode"]

    @pytest.mark.anyio
    async def test_807_no_pending_no_notice(self) -> None:
        with capture_logs() as logs:
            order, drops = await _run_with_commands(["/new", "/cancel", "/ping"])

        assert order == ["command:new", "command:cancel", "command:ping"]
        assert drops == []
        assert not [
            e
            for e in logs
            if e["event"] in {"forward.prompt.dropped", "forward.prompt.flushed"}
        ]

    @pytest.mark.anyio
    async def test_807_directive_is_not_a_barrier(self) -> None:
        """`/codex <text>` is a prompt: the #794 merge rule flushes A with
        reason ``directive``, not ``command``."""
        with capture_logs() as logs:
            order, _ = await _run_with_commands(
                [_pending(msg=_msg(message_id=1), text="A"), "/codex list files"]
            )

        assert order == ["prompt:A", "prompt:/codex list files"]
        flushed = [e for e in logs if e["event"] == "forward.prompt.flushed"]
        assert [e["reason"] for e in flushed] == ["directive"]

    def test_807_notice_counts_attached_forwards(self) -> None:
        pending = _pending(text="summarise", forwards=[(10, "a"), (11, "b")])
        assert _dropped_prompt_notice(pending, command="new").startswith(
            "🗑️ Dropped 3 messages sent just before /new"
        )


# ---------------------------------------------------------------------------
# #528 — AskUserQuestion text-reply echo helper
# ---------------------------------------------------------------------------


def test_format_answered_echo_short_text_returned_verbatim() -> None:
    text = (
        "You tell me, please - please list all of the next tasks now here in the chat"
    )
    assert len(text) <= _ANSWERED_ECHO_MAX
    assert _format_answered_echo(text) == f"↩️ Answered: {text}"


def test_format_answered_echo_long_text_ellipsised_not_hard_truncated() -> None:
    text = "abcdefghij" * 40  # 400 chars, comfortably above _ANSWERED_ECHO_MAX (300)
    out = _format_answered_echo(text)
    assert out.startswith("↩️ Answered: ")
    body = out.removeprefix("↩️ Answered: ")
    assert body.endswith("…")
    # Body retains _ANSWERED_ECHO_MAX-1 chars of original + the ellipsis
    assert len(body) == _ANSWERED_ECHO_MAX
    assert body[:-1] == text[: _ANSWERED_ECHO_MAX - 1]


def test_format_answered_echo_boundary_exactly_max() -> None:
    text = "x" * _ANSWERED_ECHO_MAX
    out = _format_answered_echo(text)
    # No ellipsis when exactly at limit
    assert out == f"↩️ Answered: {text}"
    assert "…" not in out


def test_format_answered_echo_boundary_one_over_max() -> None:
    text = "x" * (_ANSWERED_ECHO_MAX + 1)
    out = _format_answered_echo(text)
    body = out.removeprefix("↩️ Answered: ")
    assert body.endswith("…")
    assert len(body) == _ANSWERED_ECHO_MAX


# ---------------------------------------------------------------------------
# #631 (T6) — eager QuarantineStore startup init
# ---------------------------------------------------------------------------


class TestInitQuarantineStore:
    """``_init_quarantine_store`` is called once from ``poll_updates``, at
    the same startup lifecycle point as the offset-persistence writer, so
    the process-wide QuarantineStore singleton is resolved from the
    ACTUAL loaded config path rather than lazily re-deriving it from
    UNTETHER_CONFIG_PATH/HOME on first use mid-run. Driving ``poll_updates``
    itself would require a full FakeBot/polling harness this file doesn't
    otherwise build, so the init logic is exercised directly via the
    extracted helper (per the file's existing pattern of testing small
    loop.py units in isolation, e.g. ``_drain_backlog``).
    """

    def test_631_startup_initialises_quarantine_store(self, tmp_path) -> None:
        from untether.session_quarantine import (
            get_quarantine_store,
            set_quarantine_store,
        )

        config_path = tmp_path / "untether.toml"
        config_path.write_text("")

        set_quarantine_store(None)
        try:
            _init_quarantine_store(config_path)
            store = get_quarantine_store()
            assert store.path == config_path.with_name("session_quarantine.json")
        finally:
            set_quarantine_store(None)

    def test_631_startup_init_never_raises_on_unexpected_load_error(
        self, tmp_path, monkeypatch
    ) -> None:
        """``QuarantineStore.load()`` already survives corrupt JSON
        internally (it logs and falls back to an empty store) — the
        helper's ``except`` only guards against truly unexpected errors.
        Force one via monkeypatch and assert the helper swallows it,
        logs ``quarantine.startup_init_failed``, and never raises —
        startup must never fail because of this file."""
        from structlog.testing import capture_logs

        from untether.session_quarantine import QuarantineStore, set_quarantine_store

        def _raise(cls, path):
            raise RuntimeError("boom")

        monkeypatch.setattr(QuarantineStore, "load", classmethod(_raise))

        config_path = tmp_path / "untether.toml"
        set_quarantine_store(None)
        try:
            with capture_logs() as logs:
                _init_quarantine_store(config_path)  # must not raise
            assert any(r.get("event") == "quarantine.startup_init_failed" for r in logs)
        finally:
            set_quarantine_store(None)


# ---------------------------------------------------------------------------
# #654 — queued-behind-background-work wait note
# ---------------------------------------------------------------------------


class TestQueuedWaitNote:
    """#654: `_queued_wait_note` explains WHY a queued follow-up is waiting
    when it queues behind a Claude session lingering post-result."""

    @staticmethod
    def _token(engine: str = "claude", value: str = "sess-654"):
        from untether.model import ResumeToken

        return ResumeToken(engine=engine, value=value)

    def test_non_claude_engine_returns_none(self) -> None:
        from untether.telegram.loop import _queued_wait_note

        assert _queued_wait_note(self._token(engine="codex")) is None

    def test_no_live_owner_returns_none(self, monkeypatch) -> None:
        from untether.telegram.loop import _queued_wait_note

        monkeypatch.setattr(
            "untether.runners.claude.session_linger_info", lambda sid: None
        )
        assert _queued_wait_note(self._token()) is None

    def test_mid_run_owner_returns_none(self, monkeypatch) -> None:
        """A session that has not delivered its result yet is a normal
        mid-run queue — the active progress message already explains it."""
        from untether.telegram.loop import _queued_wait_note

        monkeypatch.setattr(
            "untether.runners.claude.session_linger_info", lambda sid: (False, 0)
        )
        assert _queued_wait_note(self._token()) is None

    def test_post_result_with_background_tasks(self, monkeypatch) -> None:
        from untether.telegram.loop import _queued_wait_note

        monkeypatch.setattr(
            "untether.runners.claude.session_linger_info", lambda sid: (True, 2)
        )
        note = _queued_wait_note(self._token())
        assert note is not None
        assert "2 background task" in note
        assert "/cancel" in note

    def test_post_result_single_task_singular(self, monkeypatch) -> None:
        from untether.telegram.loop import _queued_wait_note

        monkeypatch.setattr(
            "untether.runners.claude.session_linger_info", lambda sid: (True, 1)
        )
        note = _queued_wait_note(self._token())
        assert note is not None
        assert "1 background task" in note
        assert "1 background tasks" not in note

    def test_post_result_no_registered_tasks(self, monkeypatch) -> None:
        """#655 shape: busy post-result session with no registered handles
        still gets an explanatory note, without a task count."""
        from untether.telegram.loop import _queued_wait_note

        monkeypatch.setattr(
            "untether.runners.claude.session_linger_info", lambda sid: (True, 0)
        )
        note = _queued_wait_note(self._token())
        assert note is not None
        assert "background task" not in note
        assert "/cancel" in note

    def test_linger_info_failure_returns_none(self, monkeypatch) -> None:
        from untether.telegram.loop import _queued_wait_note

        def _boom(sid: str):
            raise RuntimeError("registry unavailable")

        monkeypatch.setattr("untether.runners.claude.session_linger_info", _boom)
        assert _queued_wait_note(self._token()) is None

    # #781: under live sessions (#776) a follow-up is written into the live
    # process when its current turn ends — it does NOT wait for background
    # tasks. The note must say so, and must not promise the old wait.

    def test_live_session_with_background_tasks_781(self, monkeypatch) -> None:
        from untether.telegram.loop import _queued_wait_note

        monkeypatch.setattr(
            "untether.runners.claude.session_linger_info", lambda sid: (True, 2)
        )
        monkeypatch.setattr(
            "untether.runners.claude.is_session_accepting", lambda sid: True
        )
        note = _queued_wait_note(self._token())
        assert note is not None
        assert "current turn" in note
        assert "background tasks keep running" in note
        assert "when they finish" not in note
        assert "Queued behind" not in note

    def test_live_session_without_background_tasks_781(self, monkeypatch) -> None:
        from untether.telegram.loop import _queued_wait_note

        monkeypatch.setattr(
            "untether.runners.claude.session_linger_info", lambda sid: (True, 0)
        )
        monkeypatch.setattr(
            "untether.runners.claude.is_session_accepting", lambda sid: True
        )
        note = _queued_wait_note(self._token())
        assert note is not None
        assert "current turn" in note
        assert "background" not in note
        assert "still finishing up" not in note

    def test_live_session_mid_run_still_none_781(self, monkeypatch) -> None:
        """Mid-run (no result yet): the active progress message explains the
        queue, live or not."""
        from untether.telegram.loop import _queued_wait_note

        monkeypatch.setattr(
            "untether.runners.claude.session_linger_info", lambda sid: (False, 1)
        )
        monkeypatch.setattr(
            "untether.runners.claude.is_session_accepting", lambda sid: True
        )
        assert _queued_wait_note(self._token()) is None

    def test_live_disabled_keeps_background_wait_wording_781(self, monkeypatch) -> None:
        """`[watchdog] live_sessions = false` (or a closing live session):
        the follow-up really does wait for the background work."""
        from untether.telegram.loop import _queued_wait_note

        monkeypatch.setattr(
            "untether.runners.claude.session_linger_info", lambda sid: (True, 1)
        )
        monkeypatch.setattr(
            "untether.runners.claude.is_session_accepting", lambda sid: False
        )
        note = _queued_wait_note(self._token())
        assert note is not None
        assert "Queued behind the previous run's 1 background task" in note
        assert "/cancel" in note
