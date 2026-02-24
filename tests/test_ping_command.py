"""Tests for the /ping command backend."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from untether.commands import CommandContext, CommandResult
from untether.telegram.commands.ping import BACKEND, _format_uptime
from untether.transport import MessageRef


# ---------------------------------------------------------------------------
# _format_uptime
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (5, "5s"),
        (61, "1m 1s"),
        (3661, "1h 1m 1s"),
        (90061, "1d 1h 1m 1s"),
        (172800, "2d 0s"),
    ],
)
def test_format_uptime(seconds: float, expected: str) -> None:
    assert _format_uptime(seconds) == expected


# ---------------------------------------------------------------------------
# PingCommand.handle
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ping_returns_pong() -> None:
    ctx = CommandContext(
        command="ping",
        text="/ping",
        args_text="",
        args=(),
        message=MessageRef(channel_id=1, message_id=1),
        reply_to=None,
        reply_text=None,
        config_path=None,
        plugin_config={},
        runtime=AsyncMock(),
        executor=AsyncMock(),
    )
    result = await BACKEND.handle(ctx)
    assert isinstance(result, CommandResult)
    assert result.text.startswith("\U0001f3d3 pong")
    assert result.notify is True
