"""#1068: button-only command backends (``agy_plan``, ``aq``,
``claude_control``) stay out of the bot's command menu, and typing one gets a
quiet harmless reply instead of a WARNING and an "Invalid … callback" error.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from tests.telegram_fakes import FakeTransport, _empty_projects, _make_router, make_cfg
from untether.commands import CommandBackend, get_command, is_callback_only
from untether.runners.mock import Return, ScriptRunner
from untether.telegram.bridge import build_bot_commands
from untether.telegram.commands import dispatch as dispatch_mod
from untether.telegram.commands.dispatch import CALLBACK_ONLY_TEXT, _dispatch_command
from untether.telegram.types import TelegramIncomingMessage
from untether.transport_runtime import TransportRuntime

CALLBACK_ONLY_IDS = ("agy_plan", "aq", "claude_control", "ia")


def _runtime() -> TransportRuntime:
    runner = ScriptRunner([Return(answer="ok")], engine="codex", resume_value="sid")
    return TransportRuntime(router=_make_router(runner), projects=_empty_projects())


@pytest.mark.parametrize("command_id", CALLBACK_ONLY_IDS)
def test_builtin_button_backends_are_callback_only(command_id: str) -> None:
    backend = get_command(command_id)
    assert isinstance(backend, CommandBackend)
    assert is_callback_only(backend)


@pytest.mark.parametrize("command_id", ["verbose", "config", "planmode", "ping"])
def test_typed_commands_are_not_callback_only(command_id: str) -> None:
    # `config` also answers buttons early, but `/config` is a real command.
    assert not is_callback_only(get_command(command_id))


def test_is_callback_only_needs_a_real_true() -> None:
    class Plain:
        id = "x"
        description = "x"

    class Truthy(Plain):
        callback_only = "yes"

    class Flagged(Plain):
        callback_only = True

    assert not is_callback_only(Plain())
    assert not is_callback_only(Truthy())
    assert is_callback_only(Flagged())


def test_menu_leaves_out_callback_only_backends() -> None:
    commands = [c["command"] for c in build_bot_commands(_runtime())]
    for command_id in CALLBACK_ONLY_IDS:
        assert command_id not in commands
    # Ordinary command backends are still listed.
    assert "verbose" in commands
    assert "config" in commands


def _msg(text: str) -> TelegramIncomingMessage:
    return TelegramIncomingMessage(
        transport="telegram",
        chat_id=123,
        message_id=7,
        text=text,
        reply_to_message_id=None,
        reply_to_text=None,
        sender_id=1,
    )


async def _dispatch(cfg: Any, command_id: str, args_text: str = "") -> None:
    text = f"/{command_id} {args_text}".strip()
    await _dispatch_command(
        cfg,
        _msg(text),
        text,
        command_id,
        args_text,
        {},
        AsyncMock(),
        None,
        False,
        None,
        None,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("args_text", ["", "approve:abc123"])
@pytest.mark.parametrize("command_id", CALLBACK_ONLY_IDS)
async def test_typed_callback_only_command_gets_a_quiet_reply(
    command_id: str, args_text: str
) -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    with capture_logs() as logs:
        await _dispatch(cfg, command_id, args_text)

    (call,) = transport.send_calls
    assert call["message"].text == CALLBACK_ONLY_TEXT
    assert call["options"].notify is False
    assert not [e for e in logs if e["log_level"] in ("warning", "error")]
    (line,) = [e for e in logs if e["event"] == "command.callback_only_typed"]
    assert line["command"] == command_id
    assert line["log_level"] == "debug"


@pytest.mark.anyio
async def test_typed_callback_only_command_never_reaches_handle(monkeypatch) -> None:
    """Typed text can't drive a button handler (e.g. a typed
    `/claude_control approve:<id>` never answers a permission request)."""

    class _ButtonBackend:
        id = "btn"
        description = "buttons"
        callback_only = True
        handle = AsyncMock()

    backend = _ButtonBackend()
    monkeypatch.setattr(dispatch_mod, "get_command", lambda *a, **kw: backend)
    transport = FakeTransport()
    await _dispatch(make_cfg(transport), "btn", "approve:abc")
    backend.handle.assert_not_awaited()
    assert transport.send_calls[0]["message"].text == CALLBACK_ONLY_TEXT


def test_callback_only_text_is_plain() -> None:
    assert "/" not in CALLBACK_ONLY_TEXT
    assert "Invalid" not in CALLBACK_ONLY_TEXT
