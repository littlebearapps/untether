"""#1207: a final whose send certainly failed is sent again later.

D1: only a send that never left (``ConnectError`` and the like) or got an
HTTP 5xx is kept; an unknown outcome (a timeout) or a 4xx never is.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from tests.telegram_fakes import FakeBot
from untether.runner_bridge import _FINAL_FAILED_NOTICE, _FINAL_UNCONFIRMED_NOTICE
from untether.telegram import pending_finals as pf
from untether.telegram.api_models import Chat, Message
from untether.telegram.bridge import TelegramTransport
from untether.telegram.client_api import HttpBotClient
from untether.transport import MessageRef, RenderedMessage, SendOptions, message_kind

pytestmark = pytest.mark.anyio

CHAT = 123
USER_REF = MessageRef(channel_id=CHAT, message_id=10)
CARD_REF = MessageRef(channel_id=CHAT, message_id=11)


class _ScriptedBot(FakeBot):
    """``outcomes`` is consumed one per send: ``ok``; ``resend`` (5xx or never
    sent); ``lost`` (unknown outcome); ``rejected`` (4xx). Then ``ok``."""

    def __init__(self, outcomes: list[str]) -> None:
        super().__init__()
        self.outcomes = list(outcomes)
        self._next_id = 100
        self._flags: dict[str, set[int]] = {"resend": set(), "lost": set()}

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
        for flags in self._flags.values():
            flags.discard(chat_id)
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
            if outcome in self._flags:
                self._flags[outcome].add(chat_id)
            return None
        self._next_id += 1
        return Message(message_id=self._next_id, chat=Chat(id=chat_id, type="private"))

    def pop_resendable_send(self, chat_id: int) -> bool:
        return self._pop("resend", chat_id)

    def pop_unconfirmed_send(self, chat_id: int) -> bool:
        return self._pop("lost", chat_id)

    def _pop(self, kind: str, chat_id: int) -> bool:
        found = chat_id in self._flags[kind]
        self._flags[kind].discard(chat_id)
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
    ("respond", "resendable"),
    [
        (lambda r: httpx.Response(502, json={"ok": False, "error_code": 502}), True),
        (lambda r: httpx.Response(500, text="oops"), True),
        (lambda r: (_ for _ in ()).throw(httpx.ConnectError("x", request=r)), True),
        (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("x", request=r)), False),
        (lambda r: httpx.Response(400, json={"ok": False, "description": "x"}), False),
        (lambda r: httpx.Response(403, json={"ok": False, "description": "x"}), False),
    ],
    ids=["502", "500", "connect_error", "read_timeout", "400", "403"],
)
async def test_send_outcome_classification(respond, resendable: bool) -> None:
    client = _http_client(respond)
    assert await client.send_message(chat_id=CHAT, text="x") is None
    assert client.pop_resendable_send(CHAT) is resendable
    assert client.pop_resendable_send(CHAT) is False  # read once


async def test_edit_5xx_is_never_a_resendable_send() -> None:
    client = _http_client(lambda r: httpx.Response(502, text="bad gateway"))
    assert await client.edit_message_text(chat_id=CHAT, message_id=1, text="x") is None
    assert client.pop_resendable_send(CHAT) is False


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
