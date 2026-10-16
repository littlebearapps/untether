"""#1157: a follow-up sent while a chat's first (tokenless) run is starting.

With no saved session (a new chat, or right after ``/new``) the first run has
no resume token until its engine reports the session (``StartedEvent``). A
second message in that window used to see "no saved session" too and start a
second, parallel run with a new conversation, which then replaced the chat's
saved session and orphaned the first one. It must instead wait for the first
run, then resume that same conversation, one run at a time.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import anyio
import pytest

import untether.telegram.loop as telegram_loop
from tests.telegram_fakes import (
    FakeBot,
    FakeTransport,
    _empty_projects,
    _make_router,
)
from untether.markdown import MarkdownPresenter
from untether.model import (
    CompletedEvent,
    ResumeToken,
    StartedEvent,
    UntetherEvent,
)
from untether.runner_bridge import ExecBridgeConfig
from untether.runners.mock import MockRunner
from untether.telegram.bridge import TelegramBridgeConfig
from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
from untether.telegram.chat_sessions import ChatSessionStore, resolve_sessions_path
from untether.telegram.types import TelegramIncomingMessage
from untether.transport_runtime import TransportRuntime

pytestmark = pytest.mark.anyio

CHAT = 123


class StartGatedRunner(MockRunner):
    """A new run reports its session only once ``gate`` is set; a resumed run
    reports it at once. Records every call and the peak concurrency."""

    def __init__(self, engine: str, *, fail_first: bool = False) -> None:
        super().__init__(engine=engine)
        self.fail_first = fail_first
        self.calls: list[tuple[str, ResumeToken | None]] = []
        self.gate = anyio.Event()
        self.active = 0
        self.max_active = 0
        self.finished = 0
        self._new_runs = 0

    async def run(
        self, prompt: str, resume: ResumeToken | None
    ) -> AsyncIterator[UntetherEvent]:
        self.calls.append((prompt, resume))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if resume is None:
                self._new_runs += 1
                token = ResumeToken(engine=self.engine, value=f"conv-{self._new_runs}")
                await self.gate.wait()  # still starting: no session yet
                if self.fail_first and token.value == "conv-1":
                    # Fails before reporting a session (a pre-spawn
                    # refusal or a start-up error).
                    yield CompletedEvent(
                        engine=self.engine,
                        ok=False,
                        answer="",
                        error="engine failed to start",
                    )
                    return
            else:
                token = resume
            yield StartedEvent(engine=self.engine, resume=token, title="t")
            await anyio.sleep(0.05)
            yield CompletedEvent(engine=self.engine, resume=token, ok=True, answer="ok")
        finally:
            self.active -= 1
            self.finished += 1


def _msg(
    text: str, message_id: int, *, chat_type: str = "private", **kw: Any
) -> TelegramIncomingMessage:
    return TelegramIncomingMessage(
        transport="telegram",
        chat_id=CHAT,
        message_id=message_id,
        text=text,
        reply_to_message_id=None,
        reply_to_text=None,
        sender_id=CHAT,
        chat_type=chat_type,
        **kw,
    )


def _dropped_notices(transport: FakeTransport) -> list[dict[str, Any]]:
    return [c for c in transport.send_calls if "Dropped" in c["message"].text]


def _tail(prompt: str) -> str:
    """The user's text, without the preamble the bridge prepends."""
    return prompt.rsplit("\n", 1)[-1]


async def _wait_for(predicate: Any, timeout: float = 5.0) -> None:
    with anyio.fail_after(timeout):
        while not predicate():
            await anyio.sleep(0.01)


async def _drive(
    tmp_path: Path,
    *,
    engine: str,
    followup_mode: str | None = None,
    session_mode: str = "chat",
    messages: tuple[TelegramIncomingMessage, ...] | None = None,
    expected_runs: int = 2,
    fail_first: bool = False,
    stop: TelegramIncomingMessage | None = None,
    until: Any = None,
) -> tuple[StartGatedRunner, ChatSessionStore, FakeTransport]:
    state_path = tmp_path / "untether.toml"
    store = ChatSessionStore(resolve_sessions_path(state_path))
    if followup_mode is not None:
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_followup_mode(CHAT, followup_mode)
    runner = StartGatedRunner(engine, fail_first=fail_first)
    transport = FakeTransport()
    runtime = TransportRuntime(
        router=_make_router(runner),
        projects=_empty_projects(),
        config_path=state_path,
    )
    cfg = TelegramBridgeConfig(
        bot=FakeBot(),
        runtime=runtime,
        chat_id=CHAT,
        startup_msg="",
        exec_cfg=ExecBridgeConfig(
            transport=transport, presenter=MarkdownPresenter(), final_notify=True
        ),
        forward_coalesce_s=0.0,
        media_group_debounce_s=0.0,
        session_mode=session_mode,
    )
    first, *rest = messages or (_msg("first", 10), _msg("second", 11))

    async def poller(_cfg: TelegramBridgeConfig):
        yield first
        # The first run is spawned and still starting (no session yet).
        await _wait_for(lambda: len(runner.calls) == 1)
        for m in rest:
            yield m
        # Give every follow-up time to dispatch while the first is starting.
        await anyio.sleep(0.3)
        if stop is not None:
            # /cancel or /new while the first run is still starting.
            yield stop
            await _wait_for(lambda: _dropped_notices(transport))
            await anyio.sleep(0.3)  # a released waiter would have run by now
            runner.gate.set()
            return
        runner.gate.set()
        if until is not None:
            await _wait_for(lambda: until(runner, transport), timeout=3.0)
            return
        await _wait_for(lambda: runner.finished >= expected_runs)

    await telegram_loop.run_main_loop(cfg, poller)
    return runner, store, transport


@pytest.mark.parametrize("engine", ["antigravity", "claude", "codex", "opencode", "pi"])
async def test_followup_during_first_start_waits_and_resumes_same_conversation(
    tmp_path: Path, engine: str
) -> None:
    runner, store, _ = await _drive(tmp_path, engine=engine)

    assert [_tail(prompt) for prompt, _ in runner.calls] == ["first", "second"]
    assert runner.calls[0][1] is None
    # The follow-up resumes the conversation the first run started …
    assert runner.calls[1][1] == ResumeToken(engine=engine, value="conv-1")
    # … after it, never alongside it …
    assert runner.max_active == 1
    # … and the saved session is never replaced by a second conversation.
    assert await store.get_session_resume(CHAT, None, engine) == ResumeToken(
        engine=engine, value="conv-1"
    )


@pytest.mark.parametrize("mode", ["steer", "queue"])
async def test_followup_mode_does_not_matter(tmp_path: Path, mode: str) -> None:
    """AG-34c: Follow-up = Steer said "still starting … queued" but ran the
    message in parallel; plain Queue had the same race."""
    runner, store, _ = await _drive(tmp_path, engine="antigravity", followup_mode=mode)

    assert runner.max_active == 1
    assert runner.calls[1][1] == ResumeToken(engine="antigravity", value="conv-1")
    assert await store.get_session_resume(CHAT, None, "antigravity") == ResumeToken(
        engine="antigravity", value="conv-1"
    )


async def test_several_followups_queue_in_order_on_one_conversation(
    tmp_path: Path,
) -> None:
    runner, _, _ = await _drive(
        tmp_path,
        engine="codex",
        messages=(_msg("first", 10), _msg("second", 11), _msg("third", 12)),
        expected_runs=3,
    )
    assert [_tail(prompt) for prompt, _ in runner.calls] == [
        "first",
        "second",
        "third",
    ]
    token = ResumeToken(engine="codex", value="conv-1")
    assert [resume for _, resume in runner.calls] == [None, token, token]
    assert runner.max_active == 1


async def test_queued_followup_gets_a_queued_card(tmp_path: Path) -> None:
    """Once the session is known the follow-up waits like any queued message:
    it gets the usual ``queued`` progress card (cancellable via /cancel)."""
    _, _, transport = await _drive(tmp_path, engine="codex")
    queued = [
        c
        for c in transport.send_calls
        if "queued" in c["message"].text
        and c["options"] is not None
        and c["options"].reply_to is not None
        and c["options"].reply_to.message_id == 11
    ]
    assert queued


async def test_first_run_that_never_starts_lets_the_followup_start_fresh(
    tmp_path: Path,
) -> None:
    """A first run that ends without reporting a session (failed start,
    cancelled) leaves nothing to resume: the waiting follow-up then starts
    the chat's conversation itself — still one run at a time."""
    runner, store, _ = await _drive(tmp_path, engine="codex", fail_first=True)
    assert [resume for _, resume in runner.calls] == [None, None]
    assert runner.max_active == 1
    assert await store.get_session_resume(CHAT, None, "codex") == ResumeToken(
        engine="codex", value="conv-2"
    )


async def test_other_sender_in_a_group_is_not_held(tmp_path: Path) -> None:
    """Group chats keep one session per sender: another member's first
    message is its own conversation and still starts at once."""
    a = _msg("from a", 10, chat_type="group")
    b = replace(_msg("from b", 11, chat_type="group"), sender_id=456)
    runner, _, _ = await _drive(tmp_path, engine="codex", messages=(a, b))
    assert [resume for _, resume in runner.calls] == [None, None]
    assert runner.max_active == 2


async def test_stateless_mode_still_runs_messages_independently(
    tmp_path: Path,
) -> None:
    """Without saved sessions every message is its own run (by design)."""
    runner, _, _ = await _drive(tmp_path, engine="codex", session_mode="stateless")
    assert [resume for _, resume in runner.calls] == [None, None]
    assert runner.max_active == 2


@pytest.mark.parametrize(
    ("command", "notice"),
    [
        ("/cancel", "🗑️ Dropped 1 message sent just before /cancel"),
        ("/new", "🗑️ Dropped 1 message sent just before /new"),
    ],
)
async def test_cancel_or_new_before_the_session_drops_the_waiting_message(
    tmp_path: Path, command: str, notice: str
) -> None:
    """The waiting message has no queued card, so /cancel (no-reply
    fallback) or /new stopping the starting run drops it too, with a reply,
    instead of releasing it as a fresh run nobody saw queued."""
    runner, store, transport = await _drive(
        tmp_path,
        engine="antigravity",
        followup_mode="steer",
        stop=_msg(command, 12),
    )
    assert [_tail(prompt) for prompt, _ in runner.calls] == ["first"]
    (dropped,) = _dropped_notices(transport)
    assert dropped["message"].text.startswith(notice)
    assert dropped["options"].reply_to.message_id == 11
    assert await store.get_session_resume(CHAT, None, "antigravity") is None


async def test_cancel_reply_to_the_starting_run_drops_the_waiting_message(
    tmp_path: Path,
) -> None:
    """Same through a /cancel that replies to the first run's card."""
    stop = replace(_msg("/cancel", 12), reply_to_message_id=1)
    runner, _, transport = await _drive(tmp_path, engine="codex", stop=stop)
    assert len(runner.calls) == 1
    (dropped,) = _dropped_notices(transport)
    assert dropped["options"].reply_to.message_id == 11


async def test_a_failed_session_save_still_releases_the_waiter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saving the first run's session raising must not hold the waiting
    message until that run ends (for a live Claude session, its whole idle
    hold): it is released with the reported token straight away and gets
    its queued card while the first run is still winding down."""

    async def broken_save(self: Any, *args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(ChatSessionStore, "set_session_resume", broken_save)

    def queued_card_sent(runner: StartGatedRunner, transport: FakeTransport) -> bool:
        return any(
            "queued" in c["message"].text
            and c["options"] is not None
            and c["options"].reply_to is not None
            and c["options"].reply_to.message_id == 11
            for c in transport.send_calls
        )

    runner, _, transport = await _drive(
        tmp_path, engine="codex", until=queued_card_sent
    )
    assert queued_card_sent(runner, transport)
    assert runner.max_active == 1
    if len(runner.calls) > 1:
        assert runner.calls[1][1] == ResumeToken(engine="codex", value="conv-1")
