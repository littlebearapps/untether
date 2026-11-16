"""Tests for /verbose toggle command."""

from __future__ import annotations

from typing import Any

import pytest

from untether.telegram.commands.verbose import (
    _VERBOSE_OVERRIDES,
    BACKEND,
    VerboseCommand,
    get_verbosity_override,
)


@pytest.fixture(autouse=True)
def _clear_overrides():
    """Clear verbose overrides before each test."""
    _VERBOSE_OVERRIDES.clear()
    yield
    _VERBOSE_OVERRIDES.clear()


def _make_ctx(args: str = "", chat_id: int = 123) -> Any:
    """Create a minimal command context for testing."""
    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.args_text = args
    ctx.message.channel_id = chat_id
    return ctx


@pytest.mark.anyio
async def test_verbose_on():
    cmd = VerboseCommand()
    ctx = _make_ctx("on")
    result = await cmd.handle(ctx)
    assert result is not None
    assert "on" in result.text.lower()
    assert get_verbosity_override(123) == "verbose"


@pytest.mark.anyio
async def test_verbose_off():
    cmd = VerboseCommand()
    _VERBOSE_OVERRIDES[123] = "verbose"
    ctx = _make_ctx("off")
    result = await cmd.handle(ctx)
    assert result is not None
    assert "off" in result.text.lower()
    assert get_verbosity_override(123) == "compact"


@pytest.mark.anyio
async def test_verbose_toggle_on():
    """No args should toggle: off → on."""
    cmd = VerboseCommand()
    ctx = _make_ctx("")
    result = await cmd.handle(ctx)
    assert result is not None
    assert "on" in result.text.lower()
    assert get_verbosity_override(123) == "verbose"


@pytest.mark.anyio
async def test_verbose_toggle_off():
    """No args should toggle: on → off."""
    cmd = VerboseCommand()
    _VERBOSE_OVERRIDES[123] = "verbose"
    ctx = _make_ctx("")
    result = await cmd.handle(ctx)
    assert result is not None
    assert "off" in result.text.lower()
    assert get_verbosity_override(123) == "compact"


@pytest.mark.anyio
async def test_verbose_clear():
    cmd = VerboseCommand()
    _VERBOSE_OVERRIDES[123] = "verbose"
    ctx = _make_ctx("clear")
    result = await cmd.handle(ctx)
    assert result is not None
    assert "cleared" in result.text.lower()
    assert get_verbosity_override(123) is None


def test_get_verbosity_override_default():
    assert get_verbosity_override(999) is None


def test_backend_id():
    assert BACKEND.id == "verbose"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("args", "event"),
    [
        ("on", "verbose.set"),
        ("off", "verbose.set"),
        ("clear", "verbose.cleared"),
        ("", "verbose.toggled"),
    ],
)
async def test_verbose_logs_carry_sender_id(args: str, event: str) -> None:
    """#1069: who changed the chat's verbosity is logged, like `config.*`."""
    from structlog.testing import capture_logs

    ctx = _make_ctx(args)
    ctx.message.sender_id = 4242
    with capture_logs() as logs:
        await VerboseCommand().handle(ctx)
    lines = [e for e in logs if e["event"] == event]
    assert len(lines) == 1
    assert lines[0]["sender_id"] == 4242
    assert lines[0]["chat_id"] == 123
