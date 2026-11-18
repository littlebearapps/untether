"""#1207: a final whose send certainly failed is sent again later.

D1: only a send that never left (``ConnectError`` and the like) or got an
HTTP 5xx is kept; an unknown outcome (a timeout) or a 4xx never is.
"""

from __future__ import annotations

import json
from typing import Any

import anyio
import httpx
import pytest
from structlog.testing import capture_logs

from tests.telegram_fakes import FakeBot
from untether.runner_bridge import _FINAL_FAILED_NOTICE, _FINAL_UNCONFIRMED_NOTICE
from untether.telegram import pending_finals as pf
from untether.telegram.api_models import Chat, Message
from untether.telegram.bridge import TelegramTransport
from untether.telegram.client import TelegramClient
from untether.telegram.client_api import HttpBotClient
from untether.transport import MessageRef, RenderedMessage, SendOptions, message_kind

pytestmark = pytest.mark.anyio

CHAT = 123
USER_REF = MessageRef(channel_id=CHAT, message_id=10)
CARD_REF = MessageRef(channel_id=CHAT, message_id=11)


_OUTCOMES = {"resend": "resendable", "lost": "unknown", "rejected": "failed"}


class _ScriptedBot(FakeBot):
    """``outcomes`` is consumed one per send: ``ok``; ``resend`` (5xx or never
    sent); ``lost`` (unknown outcome); ``rejected`` (4xx). Then ``ok``. A
    failed send reports its outcome through ``on_failure``, as the client
    does."""

    def __init__(self, outcomes: list[str]) -> None:
        super().__init__()
        self.outcomes = list(outcomes)
        self._next_id = 100
        self._lost: set[int] = set()

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
        on_failure: Any = None,
    ) -> Message | None:
        self._lost.discard(chat_id)
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        self.send_calls.append(
            {
                "text": text,
                "outcome": outcome,
                "reply_to_message_id": reply_to_message_id,
                "replace_message_id": replace_message_id,
            }
        )
        if outcome != "ok":
            if outcome == "lost":
                self._lost.add(chat_id)
            if on_failure is not None:
                on_failure(_OUTCOMES[outcome])
            return None
        self._next_id += 1
        return Message(message_id=self._next_id, chat=Chat(id=chat_id, type="private"))

    def pop_unconfirmed_send(self, chat_id: int) -> bool:
        found = chat_id in self._lost
        self._lost.discard(chat_id)
        return found


def _final(*chunks: str) -> RenderedMessage:
    followups = [RenderedMessage(text=text) for text in chunks[1:]]
    extra: dict[str, Any] = {"reply_markup": {"inline_keyboard": []}}
    if followups:
        extra["followups"] = followups
    return RenderedMessage(text=chunks[0], extra=extra)


async def _send_final(
    transport: TelegramTransport, message: RenderedMessage, kind: str = "final"
) -> MessageRef | None:
    with message_kind(kind):
        return await transport.send(
            channel_id=CHAT,
            message=message,
            options=SendOptions(reply_to=USER_REF, replace=CARD_REF),
        )


def _texts(bot: _ScriptedBot) -> list[str]:
    return [call["text"] for call in bot.send_calls if call["outcome"] == "ok"]


# --- HTTP client: which failed sends are worth repeating --------------------


def _http_client(handler) -> HttpBotClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpBotClient("123:abcDEF_ghij", http_client=http)


@pytest.mark.parametrize(
    ("respond", "outcome"),
    [
        (
            lambda r: httpx.Response(502, json={"ok": False, "error_code": 502}),
            "resendable",
        ),
        (lambda r: httpx.Response(500, text="oops"), "resendable"),
        (
            lambda r: (_ for _ in ()).throw(httpx.ConnectError("x", request=r)),
            "resendable",
        ),
        (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("x", request=r)), "unknown"),
        (
            lambda r: httpx.Response(400, json={"ok": False, "description": "x"}),
            "failed",
        ),
        (
            lambda r: httpx.Response(403, json={"ok": False, "description": "x"}),
            "failed",
        ),
    ],
    ids=["502", "500", "connect_error", "read_timeout", "400", "403"],
)
async def test_send_outcome_classification(respond, outcome: str) -> None:
    client = _http_client(respond)
    seen: list[str] = []
    assert (
        await client.send_message(chat_id=CHAT, text="x", on_failure=seen.append)
        is None
    )
    assert seen == [outcome]


async def test_a_good_send_reports_nothing() -> None:
    client = _http_client(
        lambda r: httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"message_id": 5, "chat": {"id": CHAT, "type": "private"}},
            },
        )
    )
    seen: list[str] = []
    assert await client.send_message(chat_id=CHAT, text="x", on_failure=seen.append)
    assert seen == []


async def test_an_earlier_5xx_never_marks_a_later_refused_send() -> None:
    """Each send starts clean: a 502 (send or edit) before a 400 send leaves
    the 400 ``failed``."""
    codes = iter([502, 502, 400])
    client = _http_client(
        lambda r: httpx.Response(next(codes), json={"ok": False, "description": "x"})
    )
    seen: list[str] = []
    await client.edit_message_text(chat_id=CHAT, message_id=1, text="x")
    await client.send_message(chat_id=CHAT, text="x", on_failure=seen.append)
    await client.send_message(chat_id=CHAT, text="y", on_failure=seen.append)
    assert seen == ["resendable", "failed"]


# --- transport: store and redeliver -----------------------------------------


async def test_502_final_is_resent_once_after_recovery() -> None:
    bot = _ScriptedBot(["resend"])
    transport = TelegramTransport(bot)
    assert await _send_final(transport, _final("answer")) is None
    assert len(transport.pending_finals) == 1

    with capture_logs() as logs:
        await transport.pending_finals.redeliver(bot)

    assert _texts(bot) == ["answer"]
    retry = bot.send_calls[-1]
    assert retry["reply_to_message_id"] == USER_REF.message_id
    assert retry["replace_message_id"] == CARD_REF.message_id  # card goes
    assert len(transport.pending_finals) == 0
    assert any(log["event"] == "final.redelivered" for log in logs)
    await transport.pending_finals.redeliver(bot)  # nothing left
    assert _texts(bot) == ["answer"]


@pytest.mark.parametrize("outcome", ["lost", "rejected"])
async def test_unknown_or_rejected_final_is_never_stored(outcome: str) -> None:
    bot = _ScriptedBot([outcome])
    transport = TelegramTransport(bot)
    assert await _send_final(transport, _final("answer")) is None
    assert len(transport.pending_finals) == 0


async def test_only_finals_are_stored() -> None:
    bot = _ScriptedBot(["resend"])
    transport = TelegramTransport(bot)
    assert await _send_final(transport, _final("card"), kind="progress") is None
    assert len(transport.pending_finals) == 0


async def test_delivered_final_is_never_stored() -> None:
    bot = _ScriptedBot([])
    transport = TelegramTransport(bot)
    assert await _send_final(transport, _final("answer")) is not None
    assert len(transport.pending_finals) == 0


async def test_failed_middle_chunk_stores_the_rest_only() -> None:
    bot = _ScriptedBot(["ok", "resend"])
    transport = TelegramTransport(bot)
    assert await _send_final(transport, _final("one", "two", "three")) is not None
    # Chunk 3 waits for chunk 2, so the order holds.
    assert _texts(bot) == ["one"]
    assert len(transport.pending_finals) == 1

    await transport.pending_finals.redeliver(bot)

    assert _texts(bot) == ["one", "two", "three"]
    assert all(call["replace_message_id"] is None for call in bot.send_calls[1:])
    assert len(transport.pending_finals) == 0


async def test_gives_up_after_max_attempts() -> None:
    bot = _ScriptedBot(["resend"] * (1 + pf.MAX_ATTEMPTS))
    transport = TelegramTransport(bot)
    await _send_final(transport, _final("answer"))
    with capture_logs() as logs:
        for _ in range(pf.MAX_ATTEMPTS):
            await transport.pending_finals.redeliver(bot)

    assert len(transport.pending_finals) == 0
    assert [
        e["log_level"] for e in logs if e["event"] == "final.redelivery_abandoned"
    ] == ["error"]
    assert bot.edit_calls[-1]["message_id"] == CARD_REF.message_id
    assert bot.edit_calls[-1]["text"] == _FINAL_FAILED_NOTICE
    assert bot.edit_calls[-1]["reply_markup"] == {"inline_keyboard": []}


async def test_unknown_outcome_on_retry_is_not_sent_again() -> None:
    bot = _ScriptedBot(["resend", "lost"])
    transport = TelegramTransport(bot)
    await _send_final(transport, _final("answer"))
    await transport.pending_finals.redeliver(bot)
    await transport.pending_finals.redeliver(bot)

    assert len(bot.send_calls) == 2
    assert len(transport.pending_finals) == 0
    assert bot.edit_calls[-1]["text"] == _FINAL_UNCONFIRMED_NOTICE


async def test_too_old_final_is_abandoned_unsent() -> None:
    now = [1000.0]
    bot = _ScriptedBot(["resend"])
    transport = TelegramTransport(bot, wall_clock=lambda: now[0])
    await _send_final(transport, _final("answer"))
    now[0] += pf.MAX_AGE_S
    await transport.pending_finals.redeliver(bot)

    assert len(bot.send_calls) == 1
    assert len(transport.pending_finals) == 0
    assert bot.edit_calls[-1]["text"] == _FINAL_FAILED_NOTICE


async def test_kick_only_with_entries() -> None:
    pending = pf.PendingFinals()
    pending.kick()
    assert pending._kicked is False


# --- restart -----------------------------------------------------------------


async def test_pending_final_survives_a_restart(tmp_path) -> None:
    path = tmp_path / pf.STATE_FILENAME
    bot = _ScriptedBot(["resend"])
    first = TelegramTransport(bot)
    first.pending_finals.attach(path)
    assert not path.exists()  # nothing pending, nothing written
    await _send_final(first, _final("answer"))
    assert len(json.loads(path.read_text())) == 1

    second = TelegramTransport(bot)
    second.pending_finals.attach(path)
    assert second.pending_finals._kicked is True  # due at once
    await second.pending_finals.redeliver(bot)

    assert _texts(bot) == ["answer"]
    assert json.loads(path.read_text()) == {}


async def test_corrupt_state_file_loads_empty(tmp_path) -> None:
    path = tmp_path / pf.STATE_FILENAME
    path.write_text("{not json")
    pending = pf.PendingFinals()
    with capture_logs() as logs:
        pending.attach(path)
    assert len(pending) == 0
    assert any(log["event"] == "pending_finals.load_failed" for log in logs)


# --- the real outbox: classification per request, not per chat --------------


def _ok(message_id: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "ok": True,
            "result": {
                "message_id": message_id,
                "chat": {"id": CHAT, "type": "private"},
            },
        },
    )


def _real_client(handler) -> TelegramClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TelegramClient("123:abcDEF_ghij", http_client=http, private_chat_rps=0)


def _sent_text(request: httpx.Request) -> str:
    return json.loads(request.content)["text"]


async def test_retry_and_live_send_to_one_chat_keep_their_own_outcomes() -> None:
    """A redelivery that gets a 502 while a live send to the same chat times
    out: the retry is counted (kept), never read as the live send's unknown
    outcome and dropped; the live send is never stored."""

    def handler(request: httpx.Request) -> httpx.Response:
        if _sent_text(request) == "live":
            raise httpx.ReadTimeout("lost", request=request)
        return httpx.Response(502, json={"ok": False, "error_code": 502})

    client = _real_client(handler)
    await client._outbox.ensure_worker()  # the worker lives in this task
    transport = TelegramTransport(client)
    transport.pending_finals.add(
        chat_id=CHAT,
        chunks=[RenderedMessage(text="answer")],
        reply_to_message_id=USER_REF.message_id,
        thread_id=None,
        replace_message_id=CARD_REF.message_id,
        notify=True,
    )
    async with anyio.create_task_group() as tg:
        tg.start_soon(transport.pending_finals.redeliver, client)
        with message_kind("progress"):
            await client.send_message(chat_id=CHAT, text="live", wait=False)
    await client.flush_outbox(timeout=5)

    (entry,) = transport.pending_finals._entries.values()
    assert entry["attempts"] == 1
    assert [c["text"] for c in entry["chunks"]] == ["answer"]
    await client.close()


async def test_cancelled_waiter_still_keeps_a_queued_final_that_fails() -> None:
    """#928's early-delivery bound cancels the waiter while chunk 1 is in
    flight; the rest are queued. Chunks that later fail with a 5xx are kept
    one by one; the chunk that lands is never kept."""
    unblock = anyio.Event()
    started = anyio.Event()
    replies = {"one": 502, "two": 200, "three": 502}

    async def handler(request: httpx.Request) -> httpx.Response:
        text = _sent_text(request)
        if text == "one":
            started.set()
            await unblock.wait()
        if replies[text] == 200:
            return _ok(50)
        return httpx.Response(502, json={"ok": False, "error_code": 502})

    client = _real_client(handler)
    await client._outbox.ensure_worker()  # the worker lives in this task
    transport = TelegramTransport(client)
    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:

            async def cut_short() -> None:
                await started.wait()
                tg.cancel_scope.cancel()

            tg.start_soon(cut_short)
            await _send_final(transport, _final("one", "two", "three"))
    unblock.set()
    await client.flush_outbox(timeout=5)

    kept = sorted(
        (e["chunks"][0]["text"], len(e["chunks"]), e["replace_message_id"])
        for e in transport.pending_finals._entries.values()
    )
    assert kept == [("one", 1, CARD_REF.message_id), ("three", 1, None)]
    await client.close()
