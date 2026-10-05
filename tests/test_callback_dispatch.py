"""Tests for callback query dispatch to command backends."""

from __future__ import annotations

from unittest.mock import AsyncMock

import anyio
import pytest

from tests.telegram_fakes import FakeBot, FakeTransport, make_cfg
from untether.commands import CommandContext, CommandResult
from untether.runner_bridge import _EPHEMERAL_MSGS
from untether.telegram.bridge import TelegramBridgeConfig
from untether.telegram.commands import dispatch as dispatch_mod
from untether.telegram.commands.dispatch import _dispatch_callback, _parse_callback_data
from untether.telegram.types import TelegramCallbackQuery


class _StubScheduler:
    """Minimal scheduler stub for dispatch tests."""


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

    def __init__(
        self, result: CommandResult | None = None, *, raise_exc: Exception | None = None
    ):
        self._result = result
        self._raise_exc = raise_exc
        self._handle_called = 0

    async def handle(self, ctx: CommandContext) -> CommandResult | None:
        self._handle_called += 1
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
async def test_early_answer_fires_before_slow_handle(monkeypatch) -> None:
    """#247: early-answer must reach answerCallbackQuery before backend.handle()
    does any blocking work. Telegram's 30 s callback window starts when the user
    presses the button; if handle() writes slowly to a PTY before we answer,
    the client sees BotResponseTimeoutError. This regression test asserts the
    ordering invariant (answer call happens at a monotonic timestamp strictly
    earlier than the first instant handle() observes)."""
    import time as _time

    transport = FakeTransport()
    cfg = make_cfg(transport)
    bot: FakeBot = cfg.bot  # type: ignore[assignment]
    handle_entered_at: dict[str, float] = {}
    answer_called_at: dict[str, float] = {}

    class _SlowHandleBackend:
        id = "slow_cmd"
        description = "slow handle stub"
        answer_early = True

        def early_answer_toast(self, args_text: str) -> str | None:
            return "Approved"

        async def handle(self, ctx: CommandContext) -> CommandResult | None:
            handle_entered_at["t"] = _time.monotonic()
            await anyio.sleep(0.05)
            return CommandResult(text="ok")

    backend = _SlowHandleBackend()

    orig_answer = bot.answer_callback_query

    async def _timed_answer(query_id, text=None):
        answer_called_at.setdefault("t", _time.monotonic())
        return await orig_answer(query_id, text=text)

    bot.answer_callback_query = _timed_answer  # type: ignore[assignment]
    monkeypatch.setattr(dispatch_mod, "get_command", lambda *a, **kw: backend)

    await _dispatch_callback(
        cfg,
        _make_callback_query(),
        "slow_cmd",
        "args",
        None,
        {},
        AsyncMock(),
        None,
        False,
        None,
        "cb-timing",
    )

    assert "t" in answer_called_at
    assert "t" in handle_entered_at
    assert answer_called_at["t"] < handle_entered_at["t"], (
        "early-answer must call answerCallbackQuery strictly before "
        "backend.handle() runs any code (#247)"
    )
    # Early answer + no second answer in finally.
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


# ---------------------------------------------------------------------------
# #148: skip_reply sends via transport without reply_to
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_dispatch_callback_skip_reply_sends_without_reply_to(
    monkeypatch,
) -> None:
    """skip_reply=True sends message via transport without reply_to_message_id.

    This prevents 'message to be replied not found' when the callback's
    message (e.g. an outline message) has been deleted.
    """
    transport = FakeTransport()
    cfg = make_cfg(transport)
    backend = _StubBackend(
        CommandResult(text="✅ Plan approved", notify=True, skip_reply=True)
    )
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

    # Message was sent
    assert len(transport.send_calls) == 1
    call = transport.send_calls[0]
    assert "Plan approved" in call["message"].text
    # The send went directly to the transport (not via executor) so
    # reply_to must be None — not the callback's message_id (42).
    options = call["options"]
    assert options is not None
    assert options.reply_to is None


# ---------------------------------------------------------------------------
# Callback sender validation (#192)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_callback_rejected_for_unauthorised_sender() -> None:
    """In groups, callback from a user not in allowed_user_ids is rejected."""
    transport = FakeTransport()
    cfg = make_cfg(transport)
    cfg = TelegramBridgeConfig(
        bot=cfg.bot,
        runtime=cfg.runtime,
        chat_id=cfg.chat_id,
        startup_msg="",
        exec_cfg=cfg.exec_cfg,
        allowed_user_ids=(999,),  # only user 999 allowed
    )
    bot: FakeBot = cfg.bot  # type: ignore[assignment]
    backend = _StubBackend(CommandResult(text="Should not reach"))

    # sender_id=1 is NOT in allowed_user_ids=(999,)
    query = _make_callback_query("test_cmd:args")

    from unittest.mock import patch

    with patch("untether.telegram.commands.dispatch.get_command", return_value=backend):
        await _dispatch_callback(
            cfg,
            query,
            "test_cmd",
            "args",
            thread_id=None,
            running_tasks={},
            scheduler=_StubScheduler(),
            on_thread_known=None,
            stateful_mode=False,
            default_engine_override=None,
            callback_query_id="cb-123",
        )

    # Backend should NOT have been called
    assert backend._handle_called == 0
    # Callback should be answered with rejection
    assert len(bot.callback_calls) == 1
    assert bot.callback_calls[0]["text"] == "Not authorised"
    # No messages sent
    assert len(transport.send_calls) == 0


@pytest.mark.anyio
async def test_callback_allowed_for_authorised_sender() -> None:
    """Callback from a user in allowed_user_ids proceeds normally."""
    transport = FakeTransport()
    cfg = make_cfg(transport)
    cfg = TelegramBridgeConfig(
        bot=cfg.bot,
        runtime=cfg.runtime,
        chat_id=cfg.chat_id,
        startup_msg="",
        exec_cfg=cfg.exec_cfg,
        allowed_user_ids=(1,),  # sender_id=1 IS allowed
    )
    backend = _StubBackend(CommandResult(text="Approved"))

    query = _make_callback_query("test_cmd:args")

    from unittest.mock import patch

    with patch("untether.telegram.commands.dispatch.get_command", return_value=backend):
        await _dispatch_callback(
            cfg,
            query,
            "test_cmd",
            "args",
            thread_id=None,
            running_tasks={},
            scheduler=_StubScheduler(),
            on_thread_known=None,
            stateful_mode=False,
            default_engine_override=None,
            callback_query_id="cb-123",
        )

    # Backend should have been called
    assert backend._handle_called == 1


@pytest.mark.anyio
async def test_callback_allowed_when_no_user_restriction() -> None:
    """When allowed_user_ids is empty, all senders are allowed (default)."""
    transport = FakeTransport()
    cfg = make_cfg(transport)  # default: allowed_user_ids=()
    backend = _StubBackend(CommandResult(text="OK"))

    query = _make_callback_query("test_cmd:args")

    from unittest.mock import patch

    with patch("untether.telegram.commands.dispatch.get_command", return_value=backend):
        await _dispatch_callback(
            cfg,
            query,
            "test_cmd",
            "args",
            thread_id=None,
            running_tasks={},
            scheduler=_StubScheduler(),
            on_thread_known=None,
            stateful_mode=False,
            default_engine_override=None,
            callback_query_id="cb-123",
        )

    assert backend._handle_called == 1


# ---------------------------------------------------------------------------
# #389: CommandContext carries the live files.deny_globs
# ---------------------------------------------------------------------------


class _CapturingBackend:
    id = "test_cmd"
    description = "stub"

    def __init__(self) -> None:
        self.contexts: list[CommandContext] = []

    async def handle(self, ctx: CommandContext) -> CommandResult | None:
        self.contexts.append(ctx)
        return None


def _with_deny_globs(cfg: TelegramBridgeConfig, globs: list[str]) -> None:
    # In-place swap mirrors TelegramBridgeConfig.update_from (hot reload).
    cfg.files = cfg.files.model_copy(update={"deny_globs": globs})


@pytest.mark.anyio
async def test_callback_context_carries_file_deny_globs(monkeypatch) -> None:
    cfg = make_cfg(FakeTransport())
    backend = _CapturingBackend()
    monkeypatch.setattr(dispatch_mod, "get_command", lambda *a, **kw: backend)

    async def _dispatch() -> None:
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

    _with_deny_globs(cfg, ["x/**"])
    await _dispatch()
    _with_deny_globs(cfg, ["y/**", "z"])
    await _dispatch()

    assert backend.contexts[0].file_deny_globs == ("x/**",)
    assert backend.contexts[1].file_deny_globs == ("y/**", "z")


@pytest.mark.anyio
async def test_command_context_carries_file_deny_globs(monkeypatch) -> None:
    from untether.telegram.commands.dispatch import _dispatch_command
    from untether.telegram.types import TelegramIncomingMessage

    cfg = make_cfg(FakeTransport())
    backend = _CapturingBackend()
    monkeypatch.setattr(dispatch_mod, "get_command", lambda *a, **kw: backend)
    msg = TelegramIncomingMessage(
        transport="telegram",
        chat_id=123,
        message_id=7,
        text="/test_cmd",
        reply_to_message_id=None,
        reply_to_text=None,
        sender_id=1,
    )

    async def _dispatch() -> None:
        await _dispatch_command(
            cfg,
            msg,
            "/test_cmd",
            "test_cmd",
            "",
            {},
            AsyncMock(),
            None,
            False,
            None,
            None,
        )

    _with_deny_globs(cfg, ["x/**"])
    await _dispatch()
    _with_deny_globs(cfg, ["w"])
    await _dispatch()

    assert [c.file_deny_globs for c in backend.contexts] == [("x/**",), ("w",)]


@pytest.mark.anyio
async def test_950_command_context_carries_engine_default_and_context(
    monkeypatch,
) -> None:
    """#950: a command sees the topic/chat engine default and the ambient
    (topic/chat-bound) context a plain prompt in that chat would use."""
    from untether.context import RunContext
    from untether.telegram.commands.dispatch import _dispatch_command
    from untether.telegram.types import TelegramIncomingMessage

    cfg = make_cfg(FakeTransport())
    backend = _CapturingBackend()
    monkeypatch.setattr(dispatch_mod, "get_command", lambda *a, **kw: backend)
    msg = TelegramIncomingMessage(
        transport="telegram",
        chat_id=123,
        message_id=7,
        text="/test_cmd",
        reply_to_message_id=None,
        reply_to_text=None,
        sender_id=1,
    )
    ambient = RunContext(project="beta", branch="feat")
    await _dispatch_command(
        cfg,
        msg,
        "/test_cmd",
        "test_cmd",
        "",
        {},
        AsyncMock(),
        None,
        False,
        "opencode",
        None,
        ambient_context=ambient,
    )
    await _dispatch_command(
        cfg, msg, "/test_cmd", "test_cmd", "", {}, AsyncMock(), None, False, None, None
    )

    assert backend.contexts[0].default_engine_override == "opencode"
    assert backend.contexts[0].ambient_context == ambient
    assert backend.contexts[1].default_engine_override is None
    assert backend.contexts[1].ambient_context is None


# ---------------------------------------------------------------------------
# #418 — CommandResult.attachment is delivered as a Telegram document
# ---------------------------------------------------------------------------


class _AttachmentBackend:
    id = "test_cmd"
    description = "stub"

    def __init__(self, result: CommandResult) -> None:
        self.result = result

    async def handle(self, ctx: CommandContext) -> CommandResult | None:
        return self.result


class _FailingDocBot(FakeBot):
    async def send_document(self, *args, **kwargs):  # type: ignore[override]
        # records the call, then reports the upload as failed (None)
        await super().send_document(*args, **kwargs)


def _attachment_result(text: str = "📄 summary", **kwargs) -> CommandResult:
    from untether.commands import CommandAttachment

    return CommandResult(
        text=text,
        attachment=CommandAttachment(
            filename="export.md", content=b"# full transcript", fallback_text="preview"
        ),
        **kwargs,
    )


async def _dispatch_attachment(
    monkeypatch, cfg, result: CommandResult, *, thread_id: int | None = None
) -> None:
    from untether.telegram.commands.dispatch import _dispatch_command
    from untether.telegram.types import TelegramIncomingMessage

    backend = _AttachmentBackend(result)
    monkeypatch.setattr(dispatch_mod, "get_command", lambda *a, **kw: backend)
    msg = TelegramIncomingMessage(
        transport="telegram",
        chat_id=123,
        message_id=7,
        text="/test_cmd",
        reply_to_message_id=None,
        reply_to_text=None,
        sender_id=1,
        thread_id=thread_id,
    )
    await _dispatch_command(
        cfg, msg, "/test_cmd", "test_cmd", "", {}, AsyncMock(), None, False, None, None
    )


@pytest.mark.anyio
async def test_418_dispatch_sends_document_via_bot(monkeypatch) -> None:
    from structlog.testing import capture_logs

    transport = FakeTransport()
    cfg = make_cfg(transport)
    with capture_logs() as logs:
        await _dispatch_attachment(monkeypatch, cfg, _attachment_result())
    assert len(cfg.bot.document_calls) == 1
    call = cfg.bot.document_calls[0]
    assert call["chat_id"] == 123
    assert call["filename"] == "export.md"
    assert call["content"] == b"# full transcript"
    assert call["caption"] == "📄 summary"
    assert call["reply_to_message_id"] == 7
    assert call["message_thread_id"] is None
    assert call["disable_notification"] is False
    assert transport.send_calls == []
    sent = [e for e in logs if e["event"] == "command.attachment_sent"]
    assert len(sent) == 1 and sent[0]["size_bytes"] == len(b"# full transcript")


@pytest.mark.anyio
async def test_418_dispatch_caption_truncated(monkeypatch) -> None:
    cfg = make_cfg(FakeTransport())
    await _dispatch_attachment(monkeypatch, cfg, _attachment_result("x" * 2000))
    caption = cfg.bot.document_calls[0]["caption"]
    assert len(caption) == 1024
    assert caption.endswith("…")


def test_418_fit_caption_counts_utf16_units() -> None:
    from untether.runner_bridge import _utf16_len
    from untether.telegram.commands.dispatch import _fit_caption

    text = "😀" * 1000  # 2 UTF-16 units each
    cut = _fit_caption(text)
    assert _utf16_len(cut) <= 1024
    assert cut.endswith("…")
    assert _fit_caption("short") == "short"


@pytest.mark.anyio
async def test_418_dispatch_upload_failure_falls_back(monkeypatch) -> None:
    from structlog.testing import capture_logs

    transport = FakeTransport()
    cfg = make_cfg(transport)
    cfg.bot = _FailingDocBot()
    with capture_logs() as logs:
        await _dispatch_attachment(monkeypatch, cfg, _attachment_result())
    assert len(transport.send_calls) == 1
    assert transport.send_calls[0]["message"].text == "preview"
    failed = [e for e in logs if e["event"] == "command.attachment_failed"]
    assert len(failed) == 1 and failed[0]["log_level"] == "warning"


@pytest.mark.anyio
async def test_418_dispatch_too_large_falls_back(monkeypatch) -> None:
    from structlog.testing import capture_logs

    monkeypatch.setattr(dispatch_mod, "_DOCUMENT_MAX_BYTES", 10)
    transport = FakeTransport()
    cfg = make_cfg(transport)
    with capture_logs() as logs:
        await _dispatch_attachment(monkeypatch, cfg, _attachment_result())
    assert cfg.bot.document_calls == []
    assert [c["message"].text for c in transport.send_calls] == ["preview"]
    assert [e for e in logs if e["event"] == "command.attachment_too_large"]


def test_418_document_cap_is_10_mb() -> None:
    assert dispatch_mod._DOCUMENT_MAX_BYTES == 10 * 1024 * 1024


@pytest.mark.anyio
async def test_418_dispatch_skip_reply_attachment(monkeypatch) -> None:
    cfg = make_cfg(FakeTransport())
    await _dispatch_attachment(
        monkeypatch, cfg, _attachment_result(skip_reply=True, notify=False)
    )
    call = cfg.bot.document_calls[0]
    assert call["reply_to_message_id"] is None
    assert call["disable_notification"] is True


@pytest.mark.anyio
async def test_418_dispatch_without_attachment_sends_text(monkeypatch) -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    await _dispatch_attachment(monkeypatch, cfg, CommandResult(text="plain"))
    assert cfg.bot.document_calls == []
    assert [c["message"].text for c in transport.send_calls] == ["plain"]


@pytest.mark.anyio
async def test_418_forum_thread_routed(monkeypatch) -> None:
    cfg = make_cfg(FakeTransport())
    await _dispatch_attachment(monkeypatch, cfg, _attachment_result(), thread_id=10)
    assert cfg.bot.document_calls[0]["message_thread_id"] == 10


# ---------------------------------------------------------------------------
# #685 — the claude_control early toast reads (and reserves) the request
# ---------------------------------------------------------------------------


@pytest.fixture
def control_registries():
    from untether.runners import claude as claude_mod

    def _wipe() -> None:
        for reg in (
            claude_mod._ACTIVE_RUNNERS,
            claude_mod._SESSION_STDIN,
            claude_mod._REQUEST_TO_SESSION,
            claude_mod._REQUEST_TO_INPUT,
            claude_mod._REQUEST_TO_TOOL_NAME,
            claude_mod._HANDLED_REQUESTS,
            claude_mod._INFLIGHT_CONTROL_RESPONSES,
        ):
            reg.clear()

    _wipe()
    _EPHEMERAL_MSGS.clear()
    yield claude_mod
    _wipe()
    _EPHEMERAL_MSGS.clear()


def _register_request(claude_mod, request_id: str) -> AsyncMock:
    session_id = "sess-dispatch-685"
    claude_mod._ACTIVE_RUNNERS[session_id] = (
        claude_mod.ClaudeRunner(claude_cmd="claude"),
        0.0,
    )
    stdin = AsyncMock()
    claude_mod._SESSION_STDIN[session_id] = stdin
    claude_mod._REQUEST_TO_SESSION[request_id] = session_id
    claude_mod._REQUEST_TO_INPUT[request_id] = {}
    claude_mod._REQUEST_TO_TOOL_NAME[request_id] = "Bash"
    return stdin


async def _dispatch_control(cfg, data: str, callback_query_id: str) -> None:
    command_id, args_text = _parse_callback_data(data)
    await _dispatch_callback(
        cfg,
        _make_callback_query(data),
        command_id,
        args_text,
        None,
        {},
        AsyncMock(),
        None,
        False,
        None,
        callback_query_id,
    )


@pytest.mark.anyio
async def test_685_claude_control_early_toast_uses_registry(
    monkeypatch, control_registries
) -> None:
    from untether.telegram.commands.claude_control import ClaudeControlCommand

    claude_mod = control_registries
    claude_mod.mark_request_handled("req-done", action="approve", channel_id=123)
    transport = FakeTransport()
    cfg = make_cfg(transport)
    bot: FakeBot = cfg.bot  # type: ignore[assignment]
    monkeypatch.setattr(
        dispatch_mod, "get_command", lambda *a, **kw: ClaudeControlCommand()
    )

    await _dispatch_control(cfg, "claude_control:approve:req-done", "cb-late")

    assert [c["text"] for c in bot.callback_calls] == ["Already answered"]
    assert len(transport.send_calls) == 1
    sent = transport.send_calls[0]
    assert sent["message"].text == "ℹ️ Already answered — approved"
    assert sent["options"].notify is False


@pytest.mark.anyio
async def test_685_dispatch_reserves_claim_before_early_answer(
    monkeypatch, control_registries
) -> None:
    """Two taps started together: the first toasts Approved, the second
    Already answered — decided before either early answer is awaited."""
    from untether.telegram.commands.claude_control import ClaudeControlCommand

    claude_mod = control_registries
    stdin = _register_request(claude_mod, "req-both")
    transport = FakeTransport()
    cfg = make_cfg(transport)
    bot: FakeBot = cfg.bot  # type: ignore[assignment]
    monkeypatch.setattr(
        dispatch_mod, "get_command", lambda *a, **kw: ClaudeControlCommand()
    )
    gate = anyio.Event()
    toasts: dict[str, str | None] = {}
    orig_answer = bot.answer_callback_query

    async def _gated_answer(query_id, text=None):
        toasts.setdefault(query_id, text)
        await gate.wait()
        return await orig_answer(query_id, text=text)

    bot.answer_callback_query = _gated_answer  # type: ignore[assignment]

    async with anyio.create_task_group() as tg:
        tg.start_soon(_dispatch_control, cfg, "claude_control:approve:req-both", "cb-1")
        tg.start_soon(_dispatch_control, cfg, "claude_control:approve:req-both", "cb-2")
        for _ in range(50):
            await anyio.lowlevel.checkpoint()
            if len(toasts) == 2:
                break
        gate.set()

    assert toasts == {"cb-1": "Approved", "cb-2": "Already answered"}
    assert stdin.send.await_count == 1
    assert claude_mod._INFLIGHT_CONTROL_RESPONSES == {}


@pytest.mark.anyio
async def test_685_claim_released_when_handle_raises(
    monkeypatch, control_registries
) -> None:
    from untether.runners.claude import ControlRequestStatus
    from untether.telegram.commands.claude_control import ClaudeControlCommand

    claude_mod = control_registries
    _register_request(claude_mod, "req-boom")
    cfg = make_cfg(FakeTransport())

    class _Exploding(ClaudeControlCommand):
        async def handle(self, ctx: CommandContext) -> CommandResult | None:
            raise RuntimeError("boom")

    monkeypatch.setattr(dispatch_mod, "get_command", lambda *a, **kw: _Exploding())

    await _dispatch_control(cfg, "claude_control:approve:req-boom", "cb-boom")

    assert claude_mod._INFLIGHT_CONTROL_RESPONSES == {}
    lookup = claude_mod.classify_control_request("req-boom")
    assert lookup.status is ControlRequestStatus.PENDING


@pytest.mark.anyio
async def test_388_dispatch_foreign_chat_tap_gets_not_found(
    monkeypatch, control_registries
) -> None:
    """#388: a claude_control callback from chat 123 for a request whose
    buttons were posted in chat 111 reads as expired and writes nothing."""
    from structlog.testing import capture_logs

    from untether.telegram.commands.claude_control import ClaudeControlCommand

    claude_mod = control_registries
    stdin = _register_request(claude_mod, "req-388")
    claude_mod._REQUEST_TO_CHANNEL["req-388"] = 111
    transport = FakeTransport()
    cfg = make_cfg(transport)
    bot: FakeBot = cfg.bot  # type: ignore[assignment]
    monkeypatch.setattr(
        dispatch_mod, "get_command", lambda *a, **kw: ClaudeControlCommand()
    )

    with capture_logs() as logs:
        await _dispatch_control(cfg, "claude_control:approve:req-388", "cb-388")

    assert [c["text"] for c in bot.callback_calls] == ["This request has expired"]
    assert stdin.send.await_count == 0
    assert "req-388" in claude_mod._REQUEST_TO_SESSION
    assert "req-388" not in claude_mod._INFLIGHT_CONTROL_RESPONSES
    nf = [r for r in logs if r.get("event") == "claude_control.not_found"]
    assert nf and nf[0]["reason"] == "channel_mismatch"
    assert nf[0]["channel_id"] == 123
    assert nf[0]["origin_channel_id"] == 111


# ---------------------------------------------------------------------------
# #388 phase 2 — opt-in "only the originator can approve"
# ---------------------------------------------------------------------------


def _originator_setup(monkeypatch, claude_mod, *, enabled: bool, originator):
    from untether.telegram.commands.claude_control import ClaudeControlCommand

    stdin = _register_request(claude_mod, "req-orig")
    if originator is not None:
        claude_mod._REQUEST_TO_ORIGINATOR["req-orig"] = originator
    transport = FakeTransport()
    cfg = make_cfg(transport)
    cfg.approval_originator_only = enabled
    monkeypatch.setattr(
        dispatch_mod, "get_command", lambda *a, **kw: ClaudeControlCommand()
    )
    return cfg, transport, stdin


@pytest.mark.anyio
async def test_388_originator_only_rejects_other_user(
    monkeypatch, control_registries
) -> None:
    from structlog.testing import capture_logs

    from untether.telegram.approval_originator import NOT_ORIGINATOR_TEXT

    claude_mod = control_registries
    cfg, transport, stdin = _originator_setup(
        monkeypatch, claude_mod, enabled=True, originator=2
    )
    bot: FakeBot = cfg.bot  # type: ignore[assignment]

    with capture_logs() as logs:
        await _dispatch_control(cfg, "claude_control:approve:req-orig", "cb-o1")

    assert [c["text"] for c in bot.callback_calls] == [NOT_ORIGINATOR_TEXT]
    assert stdin.send.await_count == 0
    assert "req-orig" in claude_mod._REQUEST_TO_SESSION
    assert claude_mod._INFLIGHT_CONTROL_RESPONSES == {}
    assert transport.send_calls == []
    warn = [r for r in logs if r.get("event") == "callback.not_originator"]
    assert len(warn) == 1
    assert warn[0]["log_level"] == "warning"
    assert warn[0]["sender_id"] == 1
    assert warn[0]["originator_id"] == 2
    assert warn[0]["request_id"] == "req-orig"
    assert warn[0]["chat_id"] == 123


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("enabled", "originator"),
    [(True, 1), (True, None), (False, 2)],
    ids=["same-user", "no-originator", "setting-off"],
)
async def test_388_originator_only_allows(
    monkeypatch, control_registries, enabled, originator
) -> None:
    """The originator, a run with no human originator (cron/webhook/at/loop)
    and the default (off) all approve as before."""
    claude_mod = control_registries
    cfg, _transport, stdin = _originator_setup(
        monkeypatch, claude_mod, enabled=enabled, originator=originator
    )
    bot: FakeBot = cfg.bot  # type: ignore[assignment]

    await _dispatch_control(cfg, "claude_control:approve:req-orig", "cb-o2")

    assert [c["text"] for c in bot.callback_calls] == ["Approved"]
    assert stdin.send.await_count == 1


@pytest.mark.anyio
async def test_388_originator_only_rejects_ask_option_tap(
    monkeypatch, control_registries
) -> None:
    """An AskUserQuestion option tap from another user is refused and the
    flow is left untouched."""
    from untether.telegram.approval_originator import NOT_ORIGINATOR_TEXT
    from untether.telegram.commands.ask_question import AskQuestionCommand

    claude_mod = control_registries
    _register_request(claude_mod, "req-ask")
    claude_mod._REQUEST_TO_ORIGINATOR["req-ask"] = 2
    flow = claude_mod.AskQuestionState(
        request_id="req-ask",
        channel_id=123,
        questions=[{"question": "Pick?", "options": [{"label": "A"}]}],
    )
    claude_mod._ASK_QUESTION_FLOWS["req-ask"] = flow
    cfg = make_cfg(FakeTransport())
    cfg.approval_originator_only = True
    bot: FakeBot = cfg.bot  # type: ignore[assignment]
    monkeypatch.setattr(
        dispatch_mod, "get_command", lambda *a, **kw: AskQuestionCommand()
    )
    try:
        await _dispatch_control(cfg, "aq:opt:0", "cb-aq")
        assert [c["text"] for c in bot.callback_calls] == [NOT_ORIGINATOR_TEXT]
        assert flow.current_index == 0
        assert flow.answers == {}
        assert claude_mod._ASK_QUESTION_FLOWS["req-ask"] is flow
    finally:
        claude_mod._ASK_QUESTION_FLOWS.pop("req-ask", None)
