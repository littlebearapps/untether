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
    transport: FakeTransport | None = None,
    before_stop: Any = None,
    after_stop: Any = None,
) -> tuple[StartGatedRunner, ChatSessionStore, FakeTransport]:
    state_path = tmp_path / "untether.toml"
    store = ChatSessionStore(resolve_sessions_path(state_path))
    if followup_mode is not None:
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_followup_mode(CHAT, followup_mode)
    runner = StartGatedRunner(engine, fail_first=fail_first)
    transport = transport if transport is not None else FakeTransport()
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
        if before_stop is not None:
            await before_stop()
        else:
            await anyio.sleep(0.3)
        if stop is not None:
            # /cancel or /new while the first run is still starting.
            yield stop
            if after_stop is not None:
                await after_stop(runner)
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


class NoticeGatedTransport(FakeTransport):
    """Holds the steer fallback notice ("… queued instead") for message 11
    in flight until ``release`` is set — the window #1157's qa9 run hit."""

    def __init__(self) -> None:
        super().__init__()
        self.notice_in_flight = anyio.Event()
        self.release = anyio.Event()

    async def send(self, *, channel_id: Any, message: Any, options: Any = None) -> Any:
        if (
            message.text.startswith("\N{RIGHTWARDS ARROW WITH HOOK}")
            and options is not None
            and options.reply_to is not None
            and options.reply_to.message_id == 11
        ):
            self.notice_in_flight.set()
            await self.release.wait()
        return await super().send(
            channel_id=channel_id, message=message, options=options
        )


@pytest.mark.parametrize(
    ("command", "notice"),
    [
        ("/cancel", "🗑️ Dropped 1 message sent just before /cancel"),
        ("/new", "🗑️ Dropped 1 message sent just before /new"),
    ],
)
@pytest.mark.parametrize("engine", ["claude", "antigravity"])
async def test_cancel_or_new_drops_a_followup_still_sending_its_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
    command: str,
    notice: str,
) -> None:
    """qa9: ``/new`` ~0.5 s after M2, while M2 was still posting its steer
    fallback notice and so not yet waiting on the first start, missed it — M2
    then ran in the new conversation. It is routed from the start, so the
    drop reaches it: the 🗑️ reply, and it never runs."""
    from collections import OrderedDict

    from untether.telegram import first_start, steer

    monkeypatch.setattr(steer, "_NOTICED", OrderedDict())
    transport = NoticeGatedTransport()

    async def before_stop() -> None:
        with anyio.fail_after(5):
            await transport.notice_in_flight.wait()

    async def after_stop(runner: StartGatedRunner) -> None:
        # The command has stopped the first run; only now does M2's notice
        # send finish and M2 carry on toward the first-start wait.
        await _wait_for(lambda: runner.finished >= 1)
        transport.release.set()

    runner, store, _ = await _drive(
        tmp_path,
        engine=engine,
        followup_mode="steer",
        stop=_msg(command, 12),
        transport=transport,
        before_stop=before_stop,
        after_stop=after_stop,
    )
    assert [_tail(prompt) for prompt, _ in runner.calls] == ["first"]
    (dropped,) = _dropped_notices(transport)
    assert dropped["message"].text.startswith(notice)
    assert dropped["options"].reply_to.message_id == 11
    assert await store.get_session_resume(CHAT, None, engine) is None
    # Nothing is left behind for later messages.
    assert first_start._DROPPED == {}
    assert first_start._OPEN_ROUTES == {}


async def test_a_message_routed_after_the_drop_runs_normally() -> None:
    """Only messages routed before the drop are dropped; a later one (sent
    after /new) is not, and the record goes once no earlier route is open."""
    from untether.telegram import first_start

    key = first_start.first_start_key(None, (CHAT, None), "codex")
    assert key is not None
    early = first_start.route()
    first_start.register(key, chat_id=CHAT, thread_id=None)
    assert first_start.drop_waiting(CHAT, thread_filter=None, command="new") == 0
    late = first_start.route()
    assert first_start.dropped_since(early, key) == "new"
    assert first_start.dropped_since(late, key) is None
    first_start.close_route(late)
    assert first_start.dropped_since(early, key) == "new"
    first_start.close_route(early)
    first_start.close_route(early)  # idempotent
    assert first_start._DROPPED == {}
    assert first_start._OPEN_ROUTES == {}


async def test_a_drop_with_nothing_in_flight_records_nothing() -> None:
    from untether.telegram import first_start

    key = first_start.first_start_key(None, (CHAT, None), "codex")
    assert key is not None
    first_start.register(key, chat_id=CHAT, thread_id=None)
    first_start.drop_waiting(CHAT, thread_filter=None, command="cancel")
    assert first_start._DROPPED == {}
    assert first_start.get(key) is None


class SlowNoticeTransport(FakeTransport):
    """#1164: the steer fallback notice for message 11 is slow to send (in a
    group, 1-3 s behind the outbox's pacing). It is held until a later
    follow-up has joined the first-start wait — what the unfixed code let
    happen — or, once ordering holds them back, until a short timeout."""

    def __init__(self) -> None:
        super().__init__()
        self.released = anyio.Event()

    async def send(self, *, channel_id: Any, message: Any, options: Any = None) -> Any:
        if (
            message.text.startswith("\N{RIGHTWARDS ARROW WITH HOOK}")
            and options is not None
            and options.reply_to is not None
            and options.reply_to.message_id == 11
        ):
            from untether.telegram import first_start

            with anyio.move_on_after(0.5):
                while not any(s.waiting for s in first_start._PENDING.values()):
                    await anyio.sleep(0.01)
            self.released.set()
        return await super().send(
            channel_id=channel_id, message=message, options=options
        )


@pytest.mark.parametrize("engine", ["claude", "antigravity"])
async def test_followups_keep_arrival_order_while_one_sends_its_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    """#1164: B, C and D sent while the first run starts (Follow-up = Steer).
    Only B gets the "queued" notice (once per run); while it was still being
    sent, C and D joined the queue first, so the finals came out A C D B.
    They run in the order they were sent."""
    from collections import OrderedDict

    from untether.telegram import steer

    monkeypatch.setattr(steer, "_NOTICED", OrderedDict())
    transport = SlowNoticeTransport()

    async def before_stop() -> None:
        with anyio.fail_after(5):
            await transport.released.wait()
        await anyio.sleep(0.3)

    runner, store, _ = await _drive(
        tmp_path,
        engine=engine,
        followup_mode="steer",
        messages=(
            _msg("first", 10),
            _msg("second", 11),
            _msg("third", 12),
            _msg("fourth", 13),
        ),
        expected_runs=4,
        transport=transport,
        before_stop=before_stop,
    )
    notices = [
        c["options"].reply_to.message_id
        for c in transport.send_calls
        if c["message"].text.startswith("\N{RIGHTWARDS ARROW WITH HOOK}")
    ]
    assert notices == [11]
    assert [_tail(prompt) for prompt, _ in runner.calls] == [
        "first",
        "second",
        "third",
        "fourth",
    ]
    token = ResumeToken(engine=engine, value="conv-1")
    assert [resume for _, resume in runner.calls] == [None, token, token, token]
    assert runner.max_active == 1


async def test_arrival_order_line_hands_the_turn_on_in_order() -> None:
    """#1164: the line itself — first in goes first, a prompt that gives up
    before its turn doesn't hold the others, and an empty line is dropped."""
    from untether.telegram import arrival_order

    scope = (CHAT, None, (CHAT, None))
    a = arrival_order.take(scope)
    b = arrival_order.take(scope)
    c = arrival_order.take(scope)
    other = arrival_order.take((CHAT, 7, None))
    assert a.turn.is_set() and other.turn.is_set()
    assert not b.turn.is_set() and not c.turn.is_set()

    arrival_order.done(b)  # gave up before its turn (e.g. cancelled)
    assert not c.turn.is_set()
    arrival_order.done(a)
    arrival_order.done(a)  # idempotent
    with anyio.fail_after(1):
        await arrival_order.wait_turn(c)
    await arrival_order.wait_turn(None)  # unordered (/continue): no wait
    arrival_order.done(None)
    arrival_order.done(c)
    arrival_order.done(other)
    assert arrival_order._LINES == {}


class HeldResumeRunner(MockRunner):
    """Every run reports its session at once; the first one then stays
    running until ``release`` is set (or it is cancelled)."""

    def __init__(self, engine: str) -> None:
        super().__init__(engine=engine)
        self.calls: list[tuple[str, ResumeToken | None]] = []
        self.release = anyio.Event()

    async def run(
        self, prompt: str, resume: ResumeToken | None
    ) -> AsyncIterator[UntetherEvent]:
        self.calls.append((prompt, resume))
        token = resume or ResumeToken(engine=self.engine, value="fresh")
        yield StartedEvent(engine=self.engine, resume=token, title="t")
        if len(self.calls) == 1:
            await self.release.wait()
        yield CompletedEvent(engine=self.engine, resume=token, ok=True, answer="ok")


class QueuedCardGatedTransport(FakeTransport):
    """Holds message 11's ``queued`` card in flight until ``release``."""

    def __init__(self) -> None:
        super().__init__()
        self.card_in_flight = anyio.Event()
        self.release = anyio.Event()

    async def send(self, *, channel_id: Any, message: Any, options: Any = None) -> Any:
        if (
            "queued" in message.text
            and options is not None
            and options.reply_to is not None
            and options.reply_to.message_id == 11
        ):
            self.card_in_flight.set()
            await self.release.wait()
        return await super().send(
            channel_id=channel_id, message=message, options=options
        )


async def _drive_in_line_with_session(
    command: str, tmp_path: Path
) -> tuple[HeldResumeRunner, FakeTransport]:
    """A session is already running (A on conv-0). C holds the turn while its
    queued card is sent and D waits in line behind it; then ``command``."""
    from untether.telegram import arrival_order

    engine = "codex"
    state_path = tmp_path / "untether.toml"
    store = ChatSessionStore(resolve_sessions_path(state_path))
    await store.set_session_resume(
        CHAT, None, ResumeToken(engine=engine, value="conv-0")
    )
    runner = HeldResumeRunner(engine)
    transport = QueuedCardGatedTransport()
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
        session_mode="chat",
    )

    async def poller(_cfg: TelegramBridgeConfig):
        yield _msg("A", 10)
        await _wait_for(lambda: len(runner.calls) == 1)
        yield _msg("C", 11)
        with anyio.fail_after(5):
            await transport.card_in_flight.wait()
        yield _msg("D", 12)
        await _wait_for(lambda: bool(arrival_order._LINES))
        await anyio.sleep(0.1)  # D is in line behind C
        yield _msg(command, 13)
        await anyio.sleep(0.2)  # the command has run
        transport.release.set()
        runner.release.set()
        await _wait_for(
            lambda: (
                _dropped_notices(transport)
                or "D" in [_tail(prompt) for prompt, _ in runner.calls]
            )
        )
        await anyio.sleep(0.5)  # a released D would have run by now

    await telegram_loop.run_main_loop(cfg, poller)
    assert arrival_order._LINES == {}
    return runner, transport


async def test_new_drops_a_followup_still_in_line(tmp_path: Path) -> None:
    """#1164 review: D, in line behind C (sending its queued card), has no
    queued card and isn't waiting on a first start, so /new missed it and
    it started the new conversation. It is dropped with the 🗑️ reply and
    never runs (the #1157 rule)."""
    runner, transport = await _drive_in_line_with_session("/new", tmp_path)
    assert "D" not in [_tail(prompt) for prompt, _ in runner.calls]
    (dropped,) = _dropped_notices(transport)
    assert dropped["message"].text.startswith(
        "🗑️ Dropped 1 message sent just before /new"
    )
    assert dropped["options"].reply_to.message_id == 12


async def test_cancel_of_a_run_with_its_session_leaves_followups_in_line(
    tmp_path: Path,
) -> None:
    """#1157 semantics: /cancel stopping a run that has its session leaves
    its follow-ups to run. D, still in line behind C, is no exception: it
    queues and runs after C, on the same conversation."""
    runner, transport = await _drive_in_line_with_session("/cancel", tmp_path)
    assert not _dropped_notices(transport)
    tails = [_tail(prompt) for prompt, _ in runner.calls]
    assert tails == ["A", "C", "D"]
    token = ResumeToken(engine="codex", value="conv-0")
    assert [resume for _, resume in runner.calls] == [token, token, token]


@pytest.mark.parametrize("command", ["/cancel", "/new"])
async def test_cancel_or_new_during_first_start_drops_followups_in_line(
    tmp_path: Path, command: str
) -> None:
    """#1164: while the first run starts, 11 waits on it (holding the turn)
    and 12 waits in line behind 11. /cancel or /new drops both, each with
    its own 🗑️ reply; only the first message ever ran."""
    runner, store, transport = await _drive(
        tmp_path,
        engine="codex",
        messages=(_msg("first", 10), _msg("second", 11), _msg("third", 12)),
        stop=_msg(command, 13),
    )
    assert [_tail(prompt) for prompt, _ in runner.calls] == ["first"]
    dropped = _dropped_notices(transport)
    assert sorted(c["options"].reply_to.message_id for c in dropped) == [11, 12]
    assert all(
        c["message"].text.startswith(f"🗑️ Dropped 1 message sent just before {command}")
        for c in dropped
    )
    assert await store.get_session_resume(CHAT, None, "codex") is None


async def test_arrival_order_drop_skips_started_prompts_and_other_threads() -> None:
    """#1164 review: only prompts that haven't started their turn are dropped,
    and only in the matching chat/thread."""
    from untether.telegram import arrival_order

    head = arrival_order.take((CHAT, None, (CHAT, None)))
    behind = arrival_order.take((CHAT, None, (CHAT, None)))
    other_thread = arrival_order.take((CHAT, 7, None))
    other_chat = arrival_order.take((CHAT + 1, None, None))
    assert await arrival_order.wait_turn(head) is None  # started

    dropped = arrival_order.drop_waiting(
        CHAT, thread_filter=lambda thread_id: thread_id is None, command="new"
    )
    assert dropped == 1
    assert head.dropped_by is None
    assert other_thread.dropped_by is None and other_chat.dropped_by is None
    arrival_order.done(head)
    assert await arrival_order.wait_turn(behind) == "new"
    for ticket in (behind, other_thread, other_chat):
        arrival_order.done(ticket)
    assert arrival_order._LINES == {}


def test_arrival_scope_is_per_sender_without_a_session_scope() -> None:
    """Stateless: nothing to keep in one session's order, so another member's
    messages never wait behind this one's. A forum topic shares one line."""
    a = _msg("a", 1, chat_type="group")
    b = replace(a, sender_id=456)
    scope = telegram_loop._arrival_scope
    assert scope(a, None, None) != scope(b, None, None)
    topic_a = replace(a, thread_id=5)
    topic_b = replace(b, thread_id=5)
    assert scope(topic_a, None, (CHAT, 5)) == scope(topic_b, None, (CHAT, 5))


class HeldFirstRunRunner(StartGatedRunner):
    """Like :class:`StartGatedRunner`, but the first run stays running after
    it reports its session until ``hold`` is set — a live run C and D could
    be steered into."""

    def __init__(self, engine: str) -> None:
        super().__init__(engine)
        self.hold = anyio.Event()
        self.first_live = False

    async def run(
        self, prompt: str, resume: ResumeToken | None
    ) -> AsyncIterator[UntetherEvent]:
        first = not self.calls
        async for event in super().run(prompt, resume):
            if first and isinstance(event, StartedEvent):
                self.first_live = True
            if first and isinstance(event, CompletedEvent):
                await self.hold.wait()
                self.first_live = False
            yield event


class FakeSteerBackend:
    """A steer target that accepts a steer while the first run is live and
    records it. agy's one-step turn never runs such a line (#1164 live QA):
    here the runner never sees it either."""

    supports_steer = True

    def __init__(self, engine: str, runner: HeldFirstRunRunner) -> None:
        self.engine = engine
        self.display_name = engine
        self.runner = runner
        self.steered: list[str] = []

    def get(self, session_id: str) -> Any:
        if not self.runner.first_live:
            return None
        from types import SimpleNamespace

        return SimpleNamespace(closing=False)

    async def steer(self, session_id: str, text: str, **kwargs: Any) -> str:
        self.steered.append(text)
        return "steered"


@pytest.mark.parametrize("engine", ["claude", "antigravity"])
async def test_later_followups_queue_behind_an_earlier_one_instead_of_steering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    """#1164 live QA (Steer): B waits on the first start and keeps its turn,
    so C and D get theirs only once A has reported its session. A is live by
    then, and C and D were steered into it ahead of B (agy then lost them;
    Claude ran C+D before B). While B is queued and hasn't run, they queue
    behind it: runs A B C D, nothing steered into A."""
    from collections import OrderedDict

    from untether.telegram import arrival_order, steer

    monkeypatch.setattr(steer, "_NOTICED", OrderedDict())
    state_path = tmp_path / "untether.toml"
    prefs = ChatPrefsStore(resolve_prefs_path(state_path))
    await prefs.set_followup_mode(CHAT, "steer")
    runner = HeldFirstRunRunner(engine)
    backend = FakeSteerBackend(engine, runner)
    monkeypatch.setattr(steer, "steer_backend", lambda _engine: backend)
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
        session_mode="chat",
    )

    async def poller(_cfg: TelegramBridgeConfig):
        yield _msg("A", 10)
        await _wait_for(lambda: len(runner.calls) == 1)
        for message_id, text in ((11, "B"), (12, "C"), (13, "D")):
            yield _msg(text, message_id)
        await anyio.sleep(0.3)  # B waits on the start; C and D in line
        runner.gate.set()  # A reports its session and stays live
        await _wait_for(lambda: runner.first_live)
        # C and D get their turns while A is still live.
        await _wait_for(lambda: not arrival_order._LINES)
        await anyio.sleep(0.2)
        runner.hold.set()
        # A steered line never runs (agy's one-step turn): count it as done.
        await _wait_for(lambda: runner.finished + len(backend.steered) >= 4)
        await anyio.sleep(0.1)

    await telegram_loop.run_main_loop(cfg, poller)

    assert backend.steered == []
    assert [_tail(prompt) for prompt, _ in runner.calls] == ["A", "B", "C", "D"]
    token = ResumeToken(engine=engine, value="conv-1")
    assert [resume for _, resume in runner.calls] == [None, token, token, token]
    assert runner.max_active == 1


async def test_scheduler_has_unstarted_tracks_queued_and_held_jobs() -> None:
    """#1164: ``has_unstarted`` is true while a job is queued or held behind
    the previous run, and false again once it runs — it never sticks."""
    from untether.scheduler import ThreadScheduler

    token = ResumeToken(engine="codex", value="s1")
    started = anyio.Event()
    finish = anyio.Event()

    async def run_job(job: Any) -> None:
        started.set()
        await finish.wait()

    async with anyio.create_task_group() as tg:
        scheduler = ThreadScheduler(task_group=tg, run_job=run_job)
        previous = anyio.Event()
        await scheduler.note_thread_known(token, previous)
        assert not scheduler.has_unstarted(token)
        await scheduler.enqueue_resume(CHAT, 1, "x", token)
        assert scheduler.has_unstarted(token)
        await anyio.sleep(0.05)  # taken by the worker, held behind previous
        assert scheduler.has_unstarted(token)
        previous.set()
        with anyio.fail_after(2):
            await started.wait()
        assert not scheduler.has_unstarted(token)
        finish.set()
    assert scheduler._unstarted == {}
