"""Tests for callback query dispatch to command backends."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from takopi.commands import CommandBackend, CommandContext, CommandResult
from takopi.runner_bridge import _EPHEMERAL_MSGS
from takopi.telegram.commands import dispatch as dispatch_mod
from takopi.telegram.commands.dispatch import _dispatch_callback, _parse_callback_data
from takopi.telegram.types import TelegramCallbackQuery
from tests.telegram_fakes import FakeBot, FakeTransport, make_cfg


class TestParseCallbackData:
    """Tests for _parse_callback_data function."""

    def test_simple_command(self) -> None:
        """Parse callback data with only command_id."""
        command_id, args_text = _parse_callback_data("ralph")
        assert command_id == "ralph"
        assert args_text == ""

    def test_command_with_single_arg(self) -> None:
        """Parse callback data with command_id and one argument."""
        command_id, args_text = _parse_callback_data("ralph:clarify")
        assert command_id == "ralph"
        assert args_text == "clarify"

    def test_command_with_multiple_args(self) -> None:
        """Parse callback data with command_id and multiple colon-separated args."""
        command_id, args_text = _parse_callback_data("ralph:clarify:123:abc")
        assert command_id == "ralph"
        assert args_text == "clarify:123:abc"

    def test_command_lowercase_normalization(self) -> None:
        """Ensure command_id is lowercased, args_text preserved."""
        command_id, args_text = _parse_callback_data("Ralph:Clarify")
        assert command_id == "ralph"
        assert args_text == "Clarify"

    def test_empty_args_after_colon(self) -> None:
        """Handle callback data with trailing colon (empty args)."""
        command_id, args_text = _parse_callback_data("ralph:")
        assert command_id == "ralph"
        assert args_text == ""

    def test_complex_args_with_special_chars(self) -> None:
        """Parse args containing special characters like = and &."""
        command_id, args_text = _parse_callback_data("mycommand:action=yes&id=42")
        assert command_id == "mycommand"
        assert args_text == "action=yes&id=42"

    def test_command_with_numbers(self) -> None:
        """Parse callback data with numeric command and args."""
        command_id, args_text = _parse_callback_data("cmd123:456")
        assert command_id == "cmd123"
        assert args_text == "456"

    def test_command_with_underscores(self) -> None:
        """Parse callback data with underscores in command and args."""
        command_id, args_text = _parse_callback_data("my_command:my_arg")
        assert command_id == "my_command"
        assert args_text == "my_arg"

    def test_command_with_dashes(self) -> None:
        """Parse callback data with dashes in command and args."""
        command_id, args_text = _parse_callback_data("my-command:my-arg")
        assert command_id == "my-command"
        assert args_text == "my-arg"

    def test_empty_string(self) -> None:
        """Handle empty callback data (edge case)."""
        command_id, args_text = _parse_callback_data("")
        assert command_id == ""
        assert args_text == ""

    def test_only_colon(self) -> None:
        """Handle callback data that is only a colon."""
        command_id, args_text = _parse_callback_data(":")
        assert command_id == ""
        assert args_text == ""

    def test_whitespace_preserved(self) -> None:
        """Whitespace in args should be preserved."""
        command_id, args_text = _parse_callback_data("cmd:arg with spaces")
        assert command_id == "cmd"
        assert args_text == "arg with spaces"

    def test_json_like_args(self) -> None:
        """Parse args that look like JSON (no nested colons in this example)."""
        command_id, args_text = _parse_callback_data('cmd:{"key":"value"}')
        assert command_id == "cmd"
        # First split at : means args_text contains full JSON after first colon
        assert args_text == '{"key":"value"}'

    def test_url_like_args(self) -> None:
        """Parse args containing URL-like patterns with colons."""
        command_id, args_text = _parse_callback_data("cmd:https://example.com")
        assert command_id == "cmd"
        assert args_text == "https://example.com"


class TestParseCallbackDataEdgeCases:
    """Edge case tests for _parse_callback_data."""

    def test_unicode_command(self) -> None:
        """Handle unicode characters in command_id (lowercased)."""
        command_id, args_text = _parse_callback_data("Ümläut:arg")
        assert command_id == "ümläut"
        assert args_text == "arg"

    def test_unicode_args(self) -> None:
        """Handle unicode characters in args (preserved)."""
        command_id, args_text = _parse_callback_data("cmd:日本語")
        assert command_id == "cmd"
        assert args_text == "日本語"

    def test_very_long_args(self) -> None:
        """Handle very long argument strings."""
        long_arg = "x" * 1000
        command_id, args_text = _parse_callback_data(f"cmd:{long_arg}")
        assert command_id == "cmd"
        assert args_text == long_arg

    def test_multiple_colons_in_args(self) -> None:
        """Ensure only first colon is used as delimiter."""
        command_id, args_text = _parse_callback_data("cmd:a:b:c:d")
        assert command_id == "cmd"
        assert args_text == "a:b:c:d"


# ---------------------------------------------------------------------------
# _dispatch_callback toast tests
# ---------------------------------------------------------------------------


class _StubBackend:
    """Minimal command backend for dispatch tests."""

    id = "test_cmd"
    description = "stub"

    def __init__(self, result: CommandResult | None = None, *, raise_exc: Exception | None = None):
        self._result = result
        self._raise_exc = raise_exc

    async def handle(self, ctx: CommandContext) -> CommandResult | None:
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._result


def _make_callback_query(data: str = "test_cmd:args") -> TelegramCallbackQuery:
    return TelegramCallbackQuery(
        transport="telegram",
        chat_id=123,
        message_id=42,
        callback_query_id="cb-123",
        data=data,
        sender_id=1,
    )


@pytest.mark.anyio
async def test_dispatch_callback_registers_ephemeral_with_callback_query_id(
    monkeypatch,
) -> None:
    """With callback_query_id, result is sent as persistent message AND registered for cleanup."""
    transport = FakeTransport()
    cfg = make_cfg(transport)
    bot: FakeBot = cfg.bot  # type: ignore[assignment]
    backend = _StubBackend(CommandResult(text="Approved permission request"))
    monkeypatch.setattr(dispatch_mod, "get_command", lambda *a, **kw: backend)

    # Clean registry before test
    _EPHEMERAL_MSGS.clear()

    await _dispatch_callback(
        cfg,
        _make_callback_query(),
        "test_cmd",
        "args",
        None,  # thread_id
        {},  # running_tasks
        AsyncMock(),  # scheduler
        None,  # on_thread_known
        False,  # stateful_mode
        None,  # default_engine_override
        "cb-123",  # callback_query_id
    )

    # Persistent message sent
    assert any("Approved" in s["message"].text for s in transport.send_calls)
    # Callback answered to clear spinner
    assert len(bot.callback_calls) == 1
    # Feedback message registered as ephemeral (keyed by chat_id, progress_message_id)
    assert (123, 42) in _EPHEMERAL_MSGS
    assert len(_EPHEMERAL_MSGS[(123, 42)]) == 1

    _EPHEMERAL_MSGS.clear()


@pytest.mark.anyio
async def test_dispatch_callback_no_ephemeral_without_callback_query_id(
    monkeypatch,
) -> None:
    """Without callback_query_id, result is sent as persistent message, NOT registered."""
    transport = FakeTransport()
    cfg = make_cfg(transport)
    bot: FakeBot = cfg.bot  # type: ignore[assignment]
    backend = _StubBackend(CommandResult(text="Approved permission request"))
    monkeypatch.setattr(dispatch_mod, "get_command", lambda *a, **kw: backend)

    _EPHEMERAL_MSGS.clear()

    await _dispatch_callback(
        cfg,
        _make_callback_query(),
        "test_cmd",
        "args",
        None,
        {},
        AsyncMock(),
        None,
        False,
        None,
        # No callback_query_id
    )

    # Persistent message sent
    assert any("Approved" in s["message"].text for s in transport.send_calls)
    # No callback answer
    assert len(bot.callback_calls) == 0
    # NOT registered as ephemeral
    assert (123, 42) not in _EPHEMERAL_MSGS


@pytest.mark.anyio
async def test_dispatch_callback_answers_on_error(monkeypatch) -> None:
    """On command error, callback is still answered to clear loading spinner."""
    transport = FakeTransport()
    cfg = make_cfg(transport)
    bot: FakeBot = cfg.bot  # type: ignore[assignment]
    backend = _StubBackend(raise_exc=RuntimeError("boom"))
    monkeypatch.setattr(dispatch_mod, "get_command", lambda *a, **kw: backend)

    await _dispatch_callback(
        cfg,
        _make_callback_query(),
        "test_cmd",
        "args",
        None,
        {},
        AsyncMock(),
        None,
        False,
        None,
        "cb-123",
    )

    # Callback should be answered even on error
    assert len(bot.callback_calls) >= 1
    assert bot.callback_calls[0]["text"] is not None
    assert "boom" in bot.callback_calls[0]["text"]


@pytest.mark.anyio
async def test_dispatch_callback_answers_when_result_is_none(monkeypatch) -> None:
    """When command returns None, callback is still answered (via finally)."""
    transport = FakeTransport()
    cfg = make_cfg(transport)
    bot: FakeBot = cfg.bot  # type: ignore[assignment]
    backend = _StubBackend(result=None)
    monkeypatch.setattr(dispatch_mod, "get_command", lambda *a, **kw: backend)

    await _dispatch_callback(
        cfg,
        _make_callback_query(),
        "test_cmd",
        "args",
        None,
        {},
        AsyncMock(),
        None,
        False,
        None,
        "cb-123",
    )

    # Callback should be answered via finally block (no text, just clear spinner)
    assert len(bot.callback_calls) == 1
    assert bot.callback_calls[0]["text"] is None


# ---------------------------------------------------------------------------
# Early callback answering tests
# ---------------------------------------------------------------------------


class _EarlyAnswerBackend:
    """Backend that supports early callback answering."""

    id = "early_cmd"
    description = "early answer stub"
    answer_early = True

    def __init__(self, toast: str | None, result: CommandResult | None = None):
        self._toast = toast
        self._result = result

    def early_answer_toast(self, args_text: str) -> str | None:
        return self._toast

    async def handle(self, ctx: CommandContext) -> CommandResult | None:
        return self._result


@pytest.mark.anyio
async def test_early_answer_clears_spinner_before_handle(monkeypatch) -> None:
    """When answer_early=True and toast is returned, callback is answered before handle()."""
    transport = FakeTransport()
    cfg = make_cfg(transport)
    bot: FakeBot = cfg.bot  # type: ignore[assignment]
    backend = _EarlyAnswerBackend(toast="Approved", result=CommandResult(text="Done"))
    monkeypatch.setattr(dispatch_mod, "get_command", lambda *a, **kw: backend)

    await _dispatch_callback(
        cfg,
        _make_callback_query(),
        "early_cmd",
        "args",
        None,
        {},
        AsyncMock(),
        None,
        False,
        None,
        "cb-123",
    )

    # Should be answered exactly once (early answer, not double-answered in finally)
    assert len(bot.callback_calls) == 1
    assert bot.callback_calls[0]["text"] == "Approved"


@pytest.mark.anyio
async def test_early_answer_none_toast_falls_through(monkeypatch) -> None:
    """When early_answer_toast returns None, callback is answered in finally (no toast)."""
    transport = FakeTransport()
    cfg = make_cfg(transport)
    bot: FakeBot = cfg.bot  # type: ignore[assignment]
    backend = _EarlyAnswerBackend(toast=None, result=None)
    monkeypatch.setattr(dispatch_mod, "get_command", lambda *a, **kw: backend)

    await _dispatch_callback(
        cfg,
        _make_callback_query(),
        "early_cmd",
        "args",
        None,
        {},
        AsyncMock(),
        None,
        False,
        None,
        "cb-123",
    )

    # Answered once in finally, no toast text
    assert len(bot.callback_calls) == 1
    assert bot.callback_calls[0]["text"] is None


@pytest.mark.anyio
async def test_no_early_answer_without_attribute(monkeypatch) -> None:
    """Backends without answer_early don't get early answering."""
    transport = FakeTransport()
    cfg = make_cfg(transport)
    bot: FakeBot = cfg.bot  # type: ignore[assignment]
    backend = _StubBackend(result=None)
    monkeypatch.setattr(dispatch_mod, "get_command", lambda *a, **kw: backend)

    await _dispatch_callback(
        cfg,
        _make_callback_query(),
        "test_cmd",
        "args",
        None,
        {},
        AsyncMock(),
        None,
        False,
        None,
        "cb-123",
    )

    # Only finally-block answer, no early toast
    assert len(bot.callback_calls) == 1
    assert bot.callback_calls[0]["text"] is None
