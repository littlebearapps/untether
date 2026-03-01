"""Tests for /config inline settings menu command."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from untether.telegram.commands.config import (
    BACKEND,
    ConfigCommand,
    _check,
    _is_callback,
)
from untether.telegram.commands.verbose import _VERBOSE_OVERRIDES


@pytest.fixture(autouse=True)
def _clear_verbose():
    _VERBOSE_OVERRIDES.clear()
    yield
    _VERBOSE_OVERRIDES.clear()


def _make_ctx(
    args_text: str = "",
    text: str = "/config",
    chat_id: int = 123,
    config_path: Path | None = None,
    engine_ids: tuple[str, ...] = ("codex", "claude"),
    default_engine: str = "codex",
) -> MagicMock:
    """Build a minimal CommandContext-like object for testing."""
    ctx = MagicMock()
    ctx.args_text = args_text
    ctx.text = text
    ctx.message.channel_id = chat_id
    ctx.config_path = config_path
    ctx.runtime.engine_ids = engine_ids
    ctx.runtime.default_engine = default_engine
    ctx.executor = AsyncMock()
    ctx.executor.send = AsyncMock(return_value=None)
    ctx.executor.edit = AsyncMock(return_value=None)
    return ctx


# ---------------------------------------------------------------------------
# Backend metadata
# ---------------------------------------------------------------------------


def test_backend_id():
    assert BACKEND.id == "config"


def test_backend_description():
    assert BACKEND.description


def test_answer_early():
    cmd = ConfigCommand()
    assert cmd.answer_early is True


def test_early_answer_toast_is_none():
    cmd = ConfigCommand()
    assert cmd.early_answer_toast("pm:on") is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_check_active():
    assert _check("Off", active=True) == "✓ Off"


def test_check_inactive():
    assert _check("Off", active=False) == "Off"


def test_is_callback_true():
    ctx = _make_ctx(text="config:pm:on")
    assert _is_callback(ctx) is True


def test_is_callback_false():
    ctx = _make_ctx(text="/config")
    assert _is_callback(ctx) is False


# ---------------------------------------------------------------------------
# Home page
# ---------------------------------------------------------------------------


class TestHomePage:
    @pytest.mark.anyio
    async def test_home_sends_new_message(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path)
        await cmd.handle(ctx)
        ctx.executor.send.assert_called_once()
        ctx.executor.edit.assert_not_called()

    @pytest.mark.anyio
    async def test_home_callback_edits_message(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="home", text="config:home", config_path=state_path)
        await cmd.handle(ctx)
        ctx.executor.edit.assert_called_once()
        ctx.executor.send.assert_not_called()

    @pytest.mark.anyio
    async def test_home_shows_settings_header(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path)
        await cmd.handle(ctx)
        msg = ctx.executor.send.call_args[0][0]
        assert "Settings" in msg.text

    @pytest.mark.anyio
    async def test_home_shows_plan_mode(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path)
        await cmd.handle(ctx)
        msg = ctx.executor.send.call_args[0][0]
        assert "Plan mode" in msg.text

    @pytest.mark.anyio
    async def test_home_shows_engine(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="codex")
        await cmd.handle(ctx)
        msg = ctx.executor.send.call_args[0][0]
        assert "codex" in msg.text

    @pytest.mark.anyio
    async def test_home_has_nav_buttons(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path)
        await cmd.handle(ctx)
        msg = ctx.executor.send.call_args[0][0]
        buttons = msg.extra["reply_markup"]["inline_keyboard"]
        callback_data = [b["callback_data"] for row in buttons for b in row]
        assert "config:pm" in callback_data
        assert "config:vb" in callback_data
        assert "config:ag" in callback_data
        assert "config:tr" in callback_data

    @pytest.mark.anyio
    async def test_home_no_config_path(self):
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=None)
        await cmd.handle(ctx)
        ctx.executor.send.assert_called_once()
        msg = ctx.executor.send.call_args[0][0]
        # Should still render with defaults
        assert "Settings" in msg.text

    @pytest.mark.anyio
    async def test_home_shows_verbose_state(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        _VERBOSE_OVERRIDES[123] = "verbose"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path)
        await cmd.handle(ctx)
        msg = ctx.executor.send.call_args[0][0]
        assert "on" in msg.text.lower()


# ---------------------------------------------------------------------------
# Plan mode sub-page
# ---------------------------------------------------------------------------


class TestPlanMode:
    @pytest.mark.anyio
    async def test_planmode_page_renders(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="pm", text="config:pm", config_path=state_path)
        await cmd.handle(ctx)
        ctx.executor.edit.assert_called_once()
        msg = ctx.executor.edit.call_args[0][1]
        assert "Plan mode" in msg.text

    @pytest.mark.anyio
    async def test_planmode_set_on(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="pm:on", text="config:pm:on", config_path=state_path)
        await cmd.handle(ctx)
        msg = ctx.executor.edit.call_args[0][1]
        # Should show checkmark on "On"
        buttons = msg.extra["reply_markup"]["inline_keyboard"]
        labels = [b["text"] for row in buttons for b in row]
        assert any("✓" in lbl and "On" in lbl for lbl in labels)

    @pytest.mark.anyio
    async def test_planmode_set_auto(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="pm:auto", text="config:pm:auto", config_path=state_path
        )
        await cmd.handle(ctx)
        msg = ctx.executor.edit.call_args[0][1]
        assert "auto" in msg.text.lower()

    @pytest.mark.anyio
    async def test_planmode_set_off(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        # First set to on
        ctx = _make_ctx(args_text="pm:on", text="config:pm:on", config_path=state_path)
        await cmd.handle(ctx)
        # Then set to off
        ctx = _make_ctx(
            args_text="pm:off", text="config:pm:off", config_path=state_path
        )
        await cmd.handle(ctx)
        msg = ctx.executor.edit.call_args[0][1]
        buttons = msg.extra["reply_markup"]["inline_keyboard"]
        labels = [b["text"] for row in buttons for b in row]
        assert any("✓" in lbl and "Off" in lbl for lbl in labels)

    @pytest.mark.anyio
    async def test_planmode_clear(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        # Set then clear
        ctx = _make_ctx(args_text="pm:on", text="config:pm:on", config_path=state_path)
        await cmd.handle(ctx)
        ctx = _make_ctx(
            args_text="pm:clr", text="config:pm:clr", config_path=state_path
        )
        await cmd.handle(ctx)
        msg = ctx.executor.edit.call_args[0][1]
        assert "default" in msg.text.lower()

    @pytest.mark.anyio
    async def test_planmode_no_config_path(self):
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="pm", text="config:pm", config_path=None)
        await cmd.handle(ctx)
        msg = ctx.executor.edit.call_args[0][1]
        assert "Unavailable" in msg.text

    @pytest.mark.anyio
    async def test_planmode_has_back_button(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="pm", text="config:pm", config_path=state_path)
        await cmd.handle(ctx)
        msg = ctx.executor.edit.call_args[0][1]
        buttons = msg.extra["reply_markup"]["inline_keyboard"]
        callback_data = [b["callback_data"] for row in buttons for b in row]
        assert "config:home" in callback_data


# ---------------------------------------------------------------------------
# Verbose sub-page
# ---------------------------------------------------------------------------


class TestVerbose:
    @pytest.mark.anyio
    async def test_verbose_page_renders(self):
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="vb", text="config:vb")
        await cmd.handle(ctx)
        msg = ctx.executor.edit.call_args[0][1]
        assert "Verbose" in msg.text

    @pytest.mark.anyio
    async def test_verbose_set_on(self):
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="vb:on", text="config:vb:on")
        await cmd.handle(ctx)
        assert _VERBOSE_OVERRIDES.get(123) == "verbose"
        msg = ctx.executor.edit.call_args[0][1]
        buttons = msg.extra["reply_markup"]["inline_keyboard"]
        labels = [b["text"] for row in buttons for b in row]
        assert any("✓" in lbl and "On" in lbl for lbl in labels)

    @pytest.mark.anyio
    async def test_verbose_set_off(self):
        _VERBOSE_OVERRIDES[123] = "verbose"
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="vb:off", text="config:vb:off")
        await cmd.handle(ctx)
        assert _VERBOSE_OVERRIDES.get(123) == "compact"

    @pytest.mark.anyio
    async def test_verbose_clear(self):
        _VERBOSE_OVERRIDES[123] = "verbose"
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="vb:clr", text="config:vb:clr")
        await cmd.handle(ctx)
        assert 123 not in _VERBOSE_OVERRIDES

    @pytest.mark.anyio
    async def test_verbose_has_back_button(self):
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="vb", text="config:vb")
        await cmd.handle(ctx)
        msg = ctx.executor.edit.call_args[0][1]
        buttons = msg.extra["reply_markup"]["inline_keyboard"]
        callback_data = [b["callback_data"] for row in buttons for b in row]
        assert "config:home" in callback_data


# ---------------------------------------------------------------------------
# Engine sub-page
# ---------------------------------------------------------------------------


class TestEngine:
    @pytest.mark.anyio
    async def test_engine_page_renders(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="ag", text="config:ag", config_path=state_path)
        await cmd.handle(ctx)
        msg = ctx.executor.edit.call_args[0][1]
        assert "engine" in msg.text.lower()

    @pytest.mark.anyio
    async def test_engine_shows_available(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="ag",
            text="config:ag",
            config_path=state_path,
            engine_ids=("codex", "claude", "opencode"),
        )
        await cmd.handle(ctx)
        msg = ctx.executor.edit.call_args[0][1]
        buttons = msg.extra["reply_markup"]["inline_keyboard"]
        callback_data = [b["callback_data"] for row in buttons for b in row]
        assert "config:ag:codex" in callback_data
        assert "config:ag:claude" in callback_data
        assert "config:ag:opencode" in callback_data

    @pytest.mark.anyio
    async def test_engine_set(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="ag:claude", text="config:ag:claude", config_path=state_path
        )
        await cmd.handle(ctx)
        msg = ctx.executor.edit.call_args[0][1]
        buttons = msg.extra["reply_markup"]["inline_keyboard"]
        labels = [b["text"] for row in buttons for b in row]
        assert any("✓" in lbl and "claude" in lbl for lbl in labels)

    @pytest.mark.anyio
    async def test_engine_clear(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        # Set then clear
        ctx = _make_ctx(
            args_text="ag:claude", text="config:ag:claude", config_path=state_path
        )
        await cmd.handle(ctx)
        ctx = _make_ctx(
            args_text="ag:clr", text="config:ag:clr", config_path=state_path
        )
        await cmd.handle(ctx)
        msg = ctx.executor.edit.call_args[0][1]
        assert "global default" in msg.text.lower()

    @pytest.mark.anyio
    async def test_engine_invalid_ignored(self, tmp_path):
        """Setting an engine not in engine_ids should be silently ignored."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="ag:nonexistent",
            text="config:ag:nonexistent",
            config_path=state_path,
        )
        await cmd.handle(ctx)
        msg = ctx.executor.edit.call_args[0][1]
        # Should show global default, not "nonexistent"
        assert "global default" in msg.text.lower()

    @pytest.mark.anyio
    async def test_engine_no_config_path(self):
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="ag", text="config:ag", config_path=None)
        await cmd.handle(ctx)
        msg = ctx.executor.edit.call_args[0][1]
        assert "Unavailable" in msg.text

    @pytest.mark.anyio
    async def test_engine_buttons_packed_two_per_row(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="ag",
            text="config:ag",
            config_path=state_path,
            engine_ids=("codex", "claude", "opencode"),
        )
        await cmd.handle(ctx)
        msg = ctx.executor.edit.call_args[0][1]
        buttons = msg.extra["reply_markup"]["inline_keyboard"]
        # 3 engines → 2 rows (2+1), plus 1 footer row (clear + back) = 3 rows
        engine_rows = [
            r
            for r in buttons
            if any("config:ag:" in b.get("callback_data", "") for b in r)
            and not any("clr" in b.get("callback_data", "") for b in r)
        ]
        assert len(engine_rows) == 2  # 2+1 split


# ---------------------------------------------------------------------------
# Trigger sub-page
# ---------------------------------------------------------------------------


class TestTrigger:
    @pytest.mark.anyio
    async def test_trigger_page_renders(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="tr", text="config:tr", config_path=state_path)
        await cmd.handle(ctx)
        msg = ctx.executor.edit.call_args[0][1]
        assert "Trigger" in msg.text

    @pytest.mark.anyio
    async def test_trigger_set_mentions(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="tr:men", text="config:tr:men", config_path=state_path
        )
        await cmd.handle(ctx)
        msg = ctx.executor.edit.call_args[0][1]
        buttons = msg.extra["reply_markup"]["inline_keyboard"]
        labels = [b["text"] for row in buttons for b in row]
        assert any("✓" in lbl and "Mentions" in lbl for lbl in labels)

    @pytest.mark.anyio
    async def test_trigger_set_all(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        # Set to mentions first
        ctx = _make_ctx(
            args_text="tr:men", text="config:tr:men", config_path=state_path
        )
        await cmd.handle(ctx)
        # Then set to all
        ctx = _make_ctx(
            args_text="tr:all", text="config:tr:all", config_path=state_path
        )
        await cmd.handle(ctx)
        msg = ctx.executor.edit.call_args[0][1]
        buttons = msg.extra["reply_markup"]["inline_keyboard"]
        labels = [b["text"] for row in buttons for b in row]
        assert any("✓" in lbl and "All" in lbl for lbl in labels)

    @pytest.mark.anyio
    async def test_trigger_clear(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="tr:men", text="config:tr:men", config_path=state_path
        )
        await cmd.handle(ctx)
        ctx = _make_ctx(
            args_text="tr:clr", text="config:tr:clr", config_path=state_path
        )
        await cmd.handle(ctx)
        msg = ctx.executor.edit.call_args[0][1]
        # After clear, should be "all" (default)
        buttons = msg.extra["reply_markup"]["inline_keyboard"]
        labels = [b["text"] for row in buttons for b in row]
        assert any("✓" in lbl and "All" in lbl for lbl in labels)

    @pytest.mark.anyio
    async def test_trigger_no_config_path(self):
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="tr", text="config:tr", config_path=None)
        await cmd.handle(ctx)
        msg = ctx.executor.edit.call_args[0][1]
        assert "Unavailable" in msg.text

    @pytest.mark.anyio
    async def test_trigger_has_back_button(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="tr", text="config:tr", config_path=state_path)
        await cmd.handle(ctx)
        msg = ctx.executor.edit.call_args[0][1]
        buttons = msg.extra["reply_markup"]["inline_keyboard"]
        callback_data = [b["callback_data"] for row in buttons for b in row]
        assert "config:home" in callback_data


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


class TestRouting:
    @pytest.mark.anyio
    async def test_unknown_page_shows_home(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="xyz", text="config:xyz", config_path=state_path)
        await cmd.handle(ctx)
        msg = ctx.executor.edit.call_args[0][1]
        assert "Settings" in msg.text

    @pytest.mark.anyio
    async def test_returns_none(self, tmp_path):
        """Handle always returns None (message sent/edited directly)."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path)
        result = await cmd.handle(ctx)
        assert result is None

    @pytest.mark.anyio
    async def test_parse_mode_is_html(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path)
        await cmd.handle(ctx)
        msg = ctx.executor.send.call_args[0][0]
        assert msg.extra["parse_mode"] == "HTML"
