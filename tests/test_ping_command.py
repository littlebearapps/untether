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


def _make_ctx(
    chat_id: int = 1,
    trigger_manager=None,
    default_chat_id: int | None = None,
) -> CommandContext:
    return CommandContext(
        command="ping",
        text="/ping",
        args_text="",
        args=(),
        message=MessageRef(channel_id=chat_id, message_id=1),
        reply_to=None,
        reply_text=None,
        config_path=None,
        plugin_config={},
        runtime=AsyncMock(),
        executor=AsyncMock(),
        trigger_manager=trigger_manager,
        default_chat_id=default_chat_id,
    )


@pytest.mark.anyio
async def test_ping_returns_pong() -> None:
    result = await BACKEND.handle(_make_ctx())
    assert isinstance(result, CommandResult)
    assert result.text.startswith("\U0001f3d3 pong")
    assert result.notify is True
    # No trigger line when manager absent.
    assert "\u23f0 triggers" not in result.text


# ---------------------------------------------------------------------------
# /ping trigger indicator (#271)
# ---------------------------------------------------------------------------


def _make_manager(**overrides):
    from untether.triggers.manager import TriggerManager
    from untether.triggers.settings import parse_trigger_config

    raw = {"enabled": True}
    raw.update(overrides)
    return TriggerManager(parse_trigger_config(raw))


@pytest.mark.anyio
async def test_ping_no_trigger_line_when_empty() -> None:
    mgr = _make_manager()
    result = await BACKEND.handle(_make_ctx(chat_id=1, trigger_manager=mgr))
    assert "\u23f0 triggers" not in result.text


@pytest.mark.anyio
async def test_ping_single_cron_targeting_chat() -> None:
    mgr = _make_manager(
        crons=[
            {
                "id": "daily-review",
                "schedule": "0 9 * * *",
                "prompt": "hi",
                "chat_id": 5000,
                "timezone": "Australia/Melbourne",
            }
        ]
    )
    result = await BACKEND.handle(_make_ctx(chat_id=5000, trigger_manager=mgr))
    assert "\u23f0 triggers: 1 cron (daily-review, 9:00 AM daily (Melbourne))" in (
        result.text
    )


@pytest.mark.anyio
async def test_ping_multiple_crons_shows_count() -> None:
    mgr = _make_manager(
        crons=[
            {"id": "a", "schedule": "0 9 * * *", "prompt": "x", "chat_id": 10},
            {"id": "b", "schedule": "0 10 * * *", "prompt": "y", "chat_id": 10},
        ]
    )
    result = await BACKEND.handle(_make_ctx(chat_id=10, trigger_manager=mgr))
    assert "\u23f0 triggers: 2 crons" in result.text


@pytest.mark.anyio
async def test_ping_webhooks_appear_when_targeting_chat() -> None:
    mgr = _make_manager(
        webhooks=[
            {
                "id": "h1",
                "path": "/hooks/one",
                "auth": "none",
                "prompt_template": "hi {{text}}",
                "chat_id": 999,
            }
        ]
    )
    result = await BACKEND.handle(_make_ctx(chat_id=999, trigger_manager=mgr))
    assert "\u23f0 triggers: 1 webhook" in result.text


@pytest.mark.anyio
async def test_ping_other_chat_not_affected() -> None:
    mgr = _make_manager(
        crons=[{"id": "a", "schedule": "0 9 * * *", "prompt": "x", "chat_id": 10}]
    )
    result = await BACKEND.handle(_make_ctx(chat_id=999, trigger_manager=mgr))
    assert "\u23f0 triggers" not in result.text


@pytest.mark.anyio
async def test_ping_default_chat_fallback_matches_unscoped_triggers() -> None:
    """Unscoped triggers (chat_id=None) fall back to default_chat_id."""
    mgr = _make_manager(crons=[{"id": "any", "schedule": "0 9 * * *", "prompt": "x"}])
    result = await BACKEND.handle(
        _make_ctx(chat_id=555, trigger_manager=mgr, default_chat_id=555)
    )
    assert "\u23f0 triggers: 1 cron (any," in result.text
