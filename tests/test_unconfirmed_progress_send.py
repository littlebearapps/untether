"""#1029: a progress ``sendMessage`` whose outcome is unknown.

A read timeout on ``sendMessage`` says nothing about delivery: Telegram may
have posted the message, and the Bot API offers no way to look it up. These
tests cover the three layers of the fix — the HTTP client remembers that the
outcome is unknown, the run sends one tracked replacement, and the untracked
copy is removed when its ``cancel`` button is tapped (a callback query is the
only place its message id ever reaches us).
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import msgspec
import pytest

import untether.runner_bridge as rb
from tests.telegram_fakes import FakeBot, make_cfg
from untether.model import Action, ActionEvent, TurnEvent
from untether.progress import ProgressTracker
from untether.runner_bridge import (
    ExecBridgeConfig,
    IncomingMessage,
    RunningTask,
    handle_message,
    send_initial_progress,
)
from untether.runners.mock import Return, ScriptRunner
from untether.telegram.api_models import Chat, Message, Update
from untether.telegram.bridge import (
    CANCEL_MARKUP,
    TelegramPresenter,
    TelegramTransport,
    handle_callback_cancel,
)
from untether.telegram.client_api import HttpBotClient
from untether.telegram.parsing import parse_incoming_update
from untether.telegram.types import TelegramCallbackQuery
from untether.transport import MessageRef, RenderedMessage, SendOptions, message_kind

pytestmark = pytest.mark.anyio

CHAT = 123
USER_REF = MessageRef(channel_id=CHAT, message_id=10)


class _FlakyBot(FakeBot):
    """A chat whose ``sendMessage`` can lose its response.

    ``outcomes`` is consumed one per send: ``ok``, ``lost`` (Telegram posted
    the message but the response never arrived) or ``failed`` (never posted).
    Sends past the end of the list succeed.
    """

    def __init__(self, outcomes: list[str]) -> None:
        super().__init__()
        self._outcomes = list(outcomes)
        self._next_id = 100
        self._unconfirmed: set[int] = set()
        # What actually exists in the chat, whatever Untether was told.
        self.posted: list[dict[str, Any]] = []

    async def send_message(  # type: ignore[override]
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        disable_notification: bool | None = False,
        message_thread_id: int | None = None,
        entities: list[dict[str, Any]] | None = None,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
        *,
        replace_message_id: int | None = None,
    ) -> Message | None:
        self._unconfirmed.discard(chat_id)
        outcome = self._outcomes.pop(0) if self._outcomes else "ok"
        self.send_calls.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": reply_markup,
                "replace_message_id": replace_message_id,
            }
        )
        if outcome == "failed":
            return None
        message_id = self._next_id
        self._next_id += 1
        self.posted.append(
            {"message_id": message_id, "text": text, "reply_markup": reply_markup}
        )
        if outcome == "lost":
            self._unconfirmed.add(chat_id)
            return None
        return Message(message_id=message_id, chat=Chat(id=chat_id, type="private"))

    def pop_unconfirmed_send(self, chat_id: int) -> bool:
        if chat_id in self._unconfirmed:
            self._unconfirmed.discard(chat_id)
            return True
        return False


def _progress_message() -> RenderedMessage:
    return RenderedMessage(
        text="working · claude · 0s", extra={"reply_markup": CANCEL_MARKUP}
    )


async def _send_progress(transport: TelegramTransport) -> MessageRef | None:
    with message_kind("progress"):
        return await transport.send(
            channel_id=CHAT,
            message=_progress_message(),
            options=SendOptions(reply_to=USER_REF, notify=False),
        )


def _cancel_tap(message_id: int, *, date: int | None) -> TelegramCallbackQuery:
    """A cancel tap decoded from Bot API JSON, as the poll loop receives it."""
    message: dict[str, Any] = {
        "message_id": message_id,
        "chat": {"id": CHAT, "type": "private"},
    }
    if date is not None:
        message["date"] = date
    update = msgspec.convert(
        {
            "update_id": 1,
            "callback_query": {
                "id": f"cb-{message_id}",
                "from": {"id": CHAT, "is_bot": False, "first_name": "n"},
                "message": message,
                "data": "untether:cancel",
            },
        },
        type=Update,
    )
    query = parse_incoming_update(update, chat_id=CHAT)
    assert isinstance(query, TelegramCallbackQuery)
    return query


def _exec_cfg(bot: FakeBot) -> ExecBridgeConfig:
    return ExecBridgeConfig(
        transport=TelegramTransport(bot),
        presenter=TelegramPresenter(),
        final_notify=True,
    )


def _has_cancel(posted: dict[str, Any]) -> bool:
    return posted["reply_markup"] == CANCEL_MARKUP


# --- HTTP client: which failed sends might have been delivered --------------


def _http_client(handler) -> HttpBotClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpBotClient("123:abcDEF_ghij", http_client=http)


async def test_read_timeout_on_send_is_remembered_as_unconfirmed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("dead flow", request=request)

    client = _http_client(handler)
    assert await client.send_message(chat_id=CHAT, text="x") is None
    assert client.pop_unconfirmed_send(CHAT) is True
    # Read once: a later, unrelated failure is not "unknown" too.
    assert client.pop_unconfirmed_send(CHAT) is False
    assert client.pop_unconfirmed_send(999) is False


async def test_send_that_never_left_is_not_unconfirmed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = _http_client(handler)
    assert await client.send_message(chat_id=CHAT, text="x") is None
    assert client.pop_unconfirmed_send(CHAT) is False


async def test_rejected_send_is_not_unconfirmed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "description": "Bad Request: x"})

    client = _http_client(handler)
    assert await client.send_message(chat_id=CHAT, text="x") is None
    assert client.pop_unconfirmed_send(CHAT) is False


async def test_a_later_send_clears_a_stale_unconfirmed_flag() -> None:
    """Nobody reads the flag for most sends, so a new send to the chat must
    start clean — else a later rejected send would look "unknown"."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("dead flow", request=request)
        return httpx.Response(400, json={"ok": False, "description": "Bad Request: x"})

    client = _http_client(handler)
    assert await client.send_message(chat_id=CHAT, text="x") is None
    assert await client.send_message(chat_id=CHAT, text="x") is None
    assert client.pop_unconfirmed_send(CHAT) is False


# --- the run keeps a tracked progress message ------------------------------


async def test_initial_progress_is_sent_again_once_after_a_lost_send() -> None:
    bot = _FlakyBot(["lost"])
    cfg = _exec_cfg(bot)
    state = await send_initial_progress(
        cfg,
        channel_id=CHAT,
        reply_to=USER_REF,
        label="working",
        tracker=ProgressTracker(engine="claude"),
    )
    assert state.ref is not None
    assert state.ref.message_id == 101  # the second, confirmed message
    assert state.unconfirmed is False
    assert len(bot.send_calls) == 2


async def test_initial_progress_gives_up_after_two_lost_sends() -> None:
    """Bounded: never a new "working" message per attempt."""
    bot = _FlakyBot(["lost", "lost", "lost"])
    state = await send_initial_progress(
        _exec_cfg(bot),
        channel_id=CHAT,
        reply_to=USER_REF,
        label="working",
        tracker=ProgressTracker(engine="claude"),
    )
    assert state.ref is None
    assert state.unconfirmed is True
    assert len(bot.send_calls) == 2


async def test_initial_progress_send_that_failed_outright_is_not_repeated() -> None:
    """A send Telegram never accepted behaves as before: one attempt."""
    bot = _FlakyBot(["failed"])
    state = await send_initial_progress(
        _exec_cfg(bot),
        channel_id=CHAT,
        reply_to=USER_REF,
        label="working",
        tracker=ProgressTracker(engine="claude"),
    )
    assert state.ref is None
    assert state.unconfirmed is False
    assert len(bot.send_calls) == 1


async def test_run_keeps_a_tracked_progress_message_after_a_lost_send() -> None:
    bot = _FlakyBot(["lost"])
    cfg = _exec_cfg(bot)
    running_tasks: dict[MessageRef, RunningTask] = {}
    seen: list[list[int]] = []

    class _Runner(ScriptRunner):
        async def run(self, prompt, resume):  # type: ignore[override]
            seen.append([int(ref.message_id) for ref in running_tasks])
            async for event in super().run(prompt, resume):
                yield event

    await handle_message(
        cfg,
        runner=_Runner([Return(answer="done")], engine="codex"),
        incoming=IncomingMessage(channel_id=CHAT, message_id=10, text="hi"),
        resume_token=None,
        running_tasks=running_tasks,
    )

    # The run was reachable (replies, /cancel) through its confirmed message.
    assert seen == [[101]]
    # One untracked copy and one tracked message — never a third.
    assert [p["message_id"] for p in bot.posted if _has_cancel(p)] == [100, 101]
    # The final replaced the tracked one; the untracked copy stays unknown
    # to Untether (nothing ever addresses message 100).
    assert bot.send_calls[-1]["replace_message_id"] == 101
    touched = {c["message_id"] for c in bot.edit_calls} | {
        c["message_id"] for c in bot.delete_calls
    }
    assert 100 not in touched


async def test_run_without_progress_after_a_failed_send_is_unchanged() -> None:
    bot = _FlakyBot(["failed"])
    await handle_message(
        _exec_cfg(bot),
        runner=ScriptRunner([Return(answer="done")], engine="codex"),
        incoming=IncomingMessage(channel_id=CHAT, message_id=10, text="hi"),
        resume_token=None,
        running_tasks={},
    )
    # The failed progress attempt, then the final as a new message.
    assert len(bot.send_calls) == 2
    assert len(bot.posted) == 1
    assert "done" in bot.posted[0]["text"]


# --- a live turn stops asking once two sends went missing -------------------


async def test_turn_progress_is_not_requested_again_after_giving_up() -> None:
    created: list[int] = []

    async def create(ctx: rb._TurnCtx) -> None:
        created.append(ctx.turn)
        ctx.progress_gave_up = True  # both sends had an unknown outcome

    async def noop(*_args: Any) -> None:
        return None

    router = rb.FollowupTurnRouter(
        new_tracker=lambda: ProgressTracker(engine="claude"),
        create_progress=create,
        close_progress=noop,
        deliver=noop,
        default_reply_to=USER_REF,
        followup_notify=False,
    )
    await router.on_turn(
        TurnEvent(engine="claude", phase="started", turn=2, reason="task_finished")
    )
    for n in range(3):
        await router.on_event(
            ActionEvent(
                engine="claude",
                action=Action(id=f"toolu_{n}", kind="command", title="ls"),
                phase="started",
            )
        )
    assert created == [2]


# --- the untracked copy's cancel button ------------------------------------


async def test_cancel_on_the_untracked_copy_removes_it_and_cancels_nothing() -> None:
    bot = _FlakyBot(["lost"])
    transport = TelegramTransport(bot)
    assert await _send_progress(transport) is None
    ghost_id = bot.posted[0]["message_id"]

    cfg = make_cfg(transport)  # type: ignore[arg-type]
    other_run = RunningTask()
    running_tasks = {MessageRef(channel_id=CHAT, message_id=555): other_run}

    await handle_callback_cancel(
        cfg, _cancel_tap(ghost_id, date=int(time.time())), running_tasks
    )

    assert other_run.cancel_requested.is_set() is False
    assert bot.delete_calls == [{"chat_id": CHAT, "message_id": ghost_id}]
    toast = cfg.bot.callback_calls[-1]["text"]  # type: ignore[attr-defined]
    assert "removed" in toast
    assert "nothing is currently running" not in toast


async def test_cancel_on_the_untracked_copy_is_claimed_once() -> None:
    bot = _FlakyBot(["lost"])
    transport = TelegramTransport(bot)
    await _send_progress(transport)
    cfg = make_cfg(transport)  # type: ignore[arg-type]
    now = int(time.time())

    await handle_callback_cancel(cfg, _cancel_tap(100, date=now), {})
    # Another untracked message in the chat is not swept up by the same record.
    await handle_callback_cancel(cfg, _cancel_tap(777, date=now), {})

    assert bot.delete_calls == [{"chat_id": CHAT, "message_id": 100}]
    assert (
        cfg.bot.callback_calls[-1]["text"]  # type: ignore[attr-defined]
        == "nothing is currently running for that message."
    )


@pytest.mark.parametrize("date", [None, 0, 1_000_000])
async def test_cancel_on_an_unrelated_untracked_message_is_unchanged(
    date: int | None,
) -> None:
    """Only a message posted while the lost send was in flight is the copy."""
    bot = _FlakyBot(["lost"])
    transport = TelegramTransport(bot)
    await _send_progress(transport)
    cfg = make_cfg(transport)  # type: ignore[arg-type]

    await handle_callback_cancel(cfg, _cancel_tap(42, date=date), {})

    assert bot.delete_calls == []
    assert (
        cfg.bot.callback_calls[-1]["text"]  # type: ignore[attr-defined]
        == "nothing is currently running for that message."
    )


async def test_cancel_after_a_send_that_failed_outright_is_unchanged() -> None:
    bot = _FlakyBot(["failed"])
    transport = TelegramTransport(bot)
    assert await _send_progress(transport) is None
    cfg = make_cfg(transport)  # type: ignore[arg-type]

    await handle_callback_cancel(cfg, _cancel_tap(100, date=int(time.time())), {})

    assert bot.delete_calls == []
    assert (
        cfg.bot.callback_calls[-1]["text"]  # type: ignore[attr-defined]
        == "nothing is currently running for that message."
    )


async def test_lost_send_without_a_cancel_button_is_not_recorded() -> None:
    """A final or notice has no cancel button — nothing can tap back."""
    bot = _FlakyBot(["lost"])
    transport = TelegramTransport(bot)
    with message_kind("final"):
        await transport.send(
            channel_id=CHAT,
            message=RenderedMessage(text="answer"),
            options=SendOptions(reply_to=USER_REF),
        )
    cfg = make_cfg(transport)  # type: ignore[arg-type]

    await handle_callback_cancel(cfg, _cancel_tap(100, date=int(time.time())), {})

    assert bot.delete_calls == []


async def test_tracked_run_is_still_cancelled_from_its_own_message() -> None:
    """The replacement message cancels its run exactly as before."""
    bot = _FlakyBot(["lost"])
    transport = TelegramTransport(bot)
    await _send_progress(transport)
    tracked = await _send_progress(transport)
    assert tracked is not None
    cfg = make_cfg(transport)  # type: ignore[arg-type]
    run = RunningTask()

    await handle_callback_cancel(
        cfg,
        _cancel_tap(int(tracked.message_id), date=int(time.time())),
        {MessageRef(channel_id=CHAT, message_id=tracked.message_id): run},
    )

    assert run.cancel_requested.is_set() is True
    assert bot.delete_calls == []
