"""#1161: a URL-shaped model id shown in Telegram gets the #1151 URL redaction
(``utils.error_display.redact_url_secrets``).

#1151 redacted engine error cards, but the ``🏷`` footer (including the
cancelled/error footer #1155 seeds from the passed model), the ``/model``
replies, the ``/agent`` summary and the ``/config`` model lines still echoed a
model such as ``https://user:pw@host/cb?token=…`` in clear. Plain model ids
must render exactly as before.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.telegram_fakes import FakeTransport, make_cfg
from untether.markdown import MarkdownPresenter, _short_model_name, format_meta_line
from untether.runner_bridge import ExecBridgeConfig, IncomingMessage, handle_message
from untether.runners.mock import ErrorReturn, ScriptRunner
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.telegram.chat_prefs import ChatPrefsStore
from untether.telegram.commands.agent import _handle_agent_command
from untether.telegram.commands.config import ConfigCommand
from untether.telegram.commands.model import _handle_model_command
from untether.telegram.engine_overrides import EngineOverrides
from untether.telegram.types import TelegramIncomingMessage

# The #1161 repro value (the `qa9/` prefix makes it a valid OpenCode-style id).
SECRET_MODEL = "qa9/https://user:pw5ecret@example.invalid/cb?token=T0K123&code=C0D456"
LEAKS = ("T0K123", "C0D456", "pw5ecret", "user:")
REDACTED_MODEL = (
    "qa9/https://[REDACTED]@example.invalid/cb?token=[REDACTED]&code=[REDACTED]"
)
PLAIN_MODELS = ["anthropic/claude-sonnet-5", "gpt-6.1-sol", "ollama/qwen2.5-coder:32b"]


def _assert_no_leak(text: str) -> None:
    for leak in LEAKS:
        assert leak not in text, leak


# ── footer meta line ────────────────────────────────────────────────────────


def test_meta_line_redacts_url_model() -> None:
    line = format_meta_line({"model": SECRET_MODEL, "effort": "high"})
    assert line is not None
    _assert_no_leak(line)
    assert REDACTED_MODEL in line
    assert line.endswith("high")


@pytest.mark.parametrize("model", PLAIN_MODELS)
def test_meta_line_plain_model_unchanged(model: str) -> None:
    assert format_meta_line({"model": model}) == _short_model_name(model)


@pytest.mark.anyio
async def test_error_card_footer_redacts_passed_model() -> None:
    """#1155 seeds the passed model into the footer of a final that arrives
    before the engine's own StartedEvent — that footer is redacted too."""
    transport = FakeTransport()
    runner = ScriptRunner(
        [ErrorReturn(error="Model not found.", usage={"num_turns": 1})],
        engine="opencode",
        resume_value="sess-1161",
    )
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=True
    )
    with apply_run_options(EngineRunOptions(model=SECRET_MODEL)):
        await handle_message(
            cfg,
            runner=runner,
            incoming=IncomingMessage(channel_id=123, message_id=10, text="reply OK"),
            resume_token=None,
        )
    texts = [c["message"].text for c in (*transport.send_calls, *transport.edit_calls)]
    assert texts
    joined = "\n".join(texts)
    _assert_no_leak(joined)
    assert "[REDACTED]" in joined


# ── /model and /agent replies ───────────────────────────────────────────────


def _msg(text: str) -> TelegramIncomingMessage:
    return TelegramIncomingMessage(
        transport="telegram",
        chat_id=123,
        message_id=10,
        text=text,
        reply_to_message_id=None,
        reply_to_text=None,
        sender_id=456,
        chat_type="private",
    )


async def _model(
    transport: FakeTransport, chat_prefs: ChatPrefsStore, args: str
) -> str:
    await _handle_model_command(
        make_cfg(transport),
        _msg(f"/model {args}"),
        args,
        ambient_context=None,
        topic_store=None,
        chat_prefs=chat_prefs,
    )
    return transport.send_calls[-1]["message"].text


@pytest.mark.anyio
async def test_model_set_and_show_redact_url_model(tmp_path: Path) -> None:
    transport = FakeTransport()
    chat_prefs = ChatPrefsStore(tmp_path / "telegram_chat_prefs_state.json")

    set_reply = await _model(transport, chat_prefs, f"set codex {SECRET_MODEL}")
    # The real value is stored — only the display is redacted.
    override = await chat_prefs.get_engine_override(123, "codex")
    assert override is not None and override.model == SECRET_MODEL
    _assert_no_leak(set_reply)
    assert REDACTED_MODEL in set_reply

    show_reply = await _model(transport, chat_prefs, "")
    _assert_no_leak(show_reply)
    assert REDACTED_MODEL in show_reply

    await _handle_agent_command(
        make_cfg(transport),
        _msg("/agent"),
        "",
        ambient_context=None,
        topic_store=None,
        chat_prefs=chat_prefs,
    )
    agent_reply = transport.send_calls[-1]["message"].text
    _assert_no_leak(agent_reply)
    assert REDACTED_MODEL in agent_reply


@pytest.mark.anyio
@pytest.mark.parametrize("model", PLAIN_MODELS)
async def test_model_set_and_show_plain_model_unchanged(
    tmp_path: Path, model: str
) -> None:
    transport = FakeTransport()
    chat_prefs = ChatPrefsStore(tmp_path / "telegram_chat_prefs_state.json")
    set_reply = await _model(transport, chat_prefs, f"set codex {model}")
    assert f"set to {model} for codex" in set_reply
    show_reply = await _model(transport, chat_prefs, "")
    assert f"model: {model} (chat default)" in show_reply


# ── /config pages ───────────────────────────────────────────────────────────


def _config_ctx(args_text: str, text: str, config_path: Path) -> MagicMock:
    ctx = MagicMock()
    ctx.args_text = args_text
    ctx.text = text
    ctx.message.channel_id = 123
    ctx.config_path = config_path
    ctx.runtime.engine_ids = ("codex", "claude")
    ctx.runtime.default_engine = "codex"
    ctx.runtime.default_context_for_chat.return_value = None
    ctx.runtime.project_default_engine.return_value = None
    ctx.executor = AsyncMock()
    ctx.executor.send = AsyncMock(return_value=None)
    ctx.executor.edit = AsyncMock(return_value=None)
    ctx.trigger_manager = None
    ctx.default_chat_id = None
    return ctx


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("args_text", "text", "sent_by"),
    [("", "/config", "send"), ("ag", "config:ag", "edit")],
)
async def test_config_pages_redact_and_escape_url_model(
    tmp_path: Path, args_text: str, text: str, sent_by: str
) -> None:
    from untether.telegram.chat_prefs import resolve_prefs_path

    config_path = tmp_path / "untether.toml"
    prefs = ChatPrefsStore(resolve_prefs_path(config_path))
    await prefs.set_engine_override(123, "codex", EngineOverrides(model=SECRET_MODEL))

    ctx = _config_ctx(args_text, text, config_path)
    await ConfigCommand().handle(ctx)
    call = ctx.executor.send if sent_by == "send" else ctx.executor.edit
    rendered = call.call_args[0][0 if sent_by == "send" else 1].text
    _assert_no_leak(rendered)
    # HTML page: the redacted value is escaped (its ``&`` would otherwise be
    # read as an entity start).
    assert (
        "qa9/https://[REDACTED]@example.invalid/cb?token=[REDACTED]&amp;code=[REDACTED]"
        in rendered
    )
