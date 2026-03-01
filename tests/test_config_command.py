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


def _last_edit_msg(ctx: MagicMock):
    """Extract the last RenderedMessage from edit calls."""
    return ctx.executor.edit.call_args[0][1]


def _last_send_msg(ctx: MagicMock):
    """Extract the last RenderedMessage from send calls."""
    return ctx.executor.send.call_args[0][0]


def _buttons_data(msg) -> list[str]:
    """Extract all callback_data values from a rendered message."""
    buttons = msg.extra["reply_markup"]["inline_keyboard"]
    return [b["callback_data"] for row in buttons for b in row]


def _buttons_labels(msg) -> list[str]:
    """Extract all button text labels from a rendered message."""
    buttons = msg.extra["reply_markup"]["inline_keyboard"]
    return [b["text"] for row in buttons for b in row]


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
# Confirmation toasts
# ---------------------------------------------------------------------------


class TestToasts:
    def test_toast_planmode_on(self):
        assert ConfigCommand.early_answer_toast("pm:on") == "Plan mode: on"

    def test_toast_planmode_off(self):
        assert ConfigCommand.early_answer_toast("pm:off") == "Plan mode: off"

    def test_toast_planmode_auto(self):
        assert ConfigCommand.early_answer_toast("pm:auto") == "Plan mode: auto"

    def test_toast_planmode_clear(self):
        assert ConfigCommand.early_answer_toast("pm:clr") == "Plan mode: cleared"

    def test_toast_verbose_on(self):
        assert ConfigCommand.early_answer_toast("vb:on") == "Verbose: on"

    def test_toast_verbose_off(self):
        assert ConfigCommand.early_answer_toast("vb:off") == "Verbose: off"

    def test_toast_verbose_clear(self):
        assert ConfigCommand.early_answer_toast("vb:clr") == "Verbose: cleared"

    def test_toast_engine_set(self):
        assert ConfigCommand.early_answer_toast("ag:codex") == "Engine: codex"

    def test_toast_engine_clear(self):
        assert ConfigCommand.early_answer_toast("ag:clr") == "Engine: cleared"

    def test_toast_trigger_all(self):
        assert ConfigCommand.early_answer_toast("tr:all") == "Trigger: all"

    def test_toast_trigger_mentions(self):
        assert ConfigCommand.early_answer_toast("tr:men") == "Trigger: mentions"

    def test_toast_trigger_clear(self):
        assert ConfigCommand.early_answer_toast("tr:clr") == "Trigger: cleared"

    def test_toast_navigation_home(self):
        """No toast for navigation to home page."""
        assert ConfigCommand.early_answer_toast("home") is None

    def test_toast_navigation_sub_page(self):
        """No toast for navigating into a sub-page (no action)."""
        assert ConfigCommand.early_answer_toast("pm") is None

    def test_toast_navigation_empty(self):
        """No toast for empty args."""
        assert ConfigCommand.early_answer_toast("") is None


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
        assert "Settings" in _last_send_msg(ctx).text

    @pytest.mark.anyio
    async def test_home_shows_plan_mode_when_claude(self, tmp_path):
        """Plan mode label and button visible when engine is claude."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="claude")
        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "Plan mode" in msg.text
        assert "config:pm" in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_home_hides_plan_mode_when_not_claude(self, tmp_path):
        """Plan mode label and button hidden when engine is not claude."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="codex")
        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "Plan mode" not in msg.text
        assert "config:pm" not in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_home_shows_engine(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="codex")
        await cmd.handle(ctx)
        assert "codex" in _last_send_msg(ctx).text

    @pytest.mark.anyio
    async def test_home_has_nav_buttons_claude(self, tmp_path):
        """When engine is claude, all 4 nav buttons present."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="claude")
        await cmd.handle(ctx)
        data = _buttons_data(_last_send_msg(ctx))
        assert "config:pm" in data
        assert "config:vb" in data
        assert "config:ag" in data
        assert "config:tr" in data

    @pytest.mark.anyio
    async def test_home_has_nav_buttons_non_claude(self, tmp_path):
        """When engine is not claude, 3 nav buttons (no plan mode)."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="codex")
        await cmd.handle(ctx)
        data = _buttons_data(_last_send_msg(ctx))
        assert "config:pm" not in data
        assert "config:vb" in data
        assert "config:ag" in data
        assert "config:tr" in data

    @pytest.mark.anyio
    async def test_home_no_config_path(self):
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=None)
        await cmd.handle(ctx)
        ctx.executor.send.assert_called_once()
        assert "Settings" in _last_send_msg(ctx).text

    @pytest.mark.anyio
    async def test_home_shows_verbose_state(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        _VERBOSE_OVERRIDES[123] = "verbose"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path)
        await cmd.handle(ctx)
        assert "on" in _last_send_msg(ctx).text.lower()


# ---------------------------------------------------------------------------
# Plan mode sub-page
# ---------------------------------------------------------------------------


class TestPlanMode:
    @pytest.mark.anyio
    async def test_planmode_page_renders(self, tmp_path):
        """Navigating to plan mode sub-page (no action) shows sub-page."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="pm",
            text="config:pm",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        ctx.executor.edit.assert_called_once()
        msg = _last_edit_msg(ctx)
        assert "Plan mode" in msg.text
        assert "config:pm:on" in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_planmode_set_returns_home(self, tmp_path):
        """Toggling plan mode returns to home page."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="pm:on",
            text="config:pm:on",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Settings" in msg.text  # Home page header
        assert "on" in msg.text.lower()

    @pytest.mark.anyio
    async def test_planmode_clear_returns_home(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        # Set then clear
        ctx = _make_ctx(
            args_text="pm:on",
            text="config:pm:on",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        ctx = _make_ctx(
            args_text="pm:clr",
            text="config:pm:clr",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Settings" in msg.text
        assert "default" in msg.text.lower()

    @pytest.mark.anyio
    async def test_planmode_no_config_path(self):
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="pm", text="config:pm", config_path=None)
        await cmd.handle(ctx)
        assert "Unavailable" in _last_edit_msg(ctx).text

    @pytest.mark.anyio
    async def test_planmode_has_back_button(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="pm",
            text="config:pm",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        assert "config:home" in _buttons_data(_last_edit_msg(ctx))

    @pytest.mark.anyio
    async def test_planmode_guard_non_claude(self, tmp_path):
        """Plan mode page shows guard message when engine is not claude."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="pm",
            text="config:pm",
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Only available for Claude Code" in msg.text
        assert "config:home" in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_planmode_guard_non_claude_with_override(self, tmp_path):
        """Plan mode guard respects per-chat engine override."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_default_engine(123, "opencode")

        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="pm",
            text="config:pm",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Only available for Claude Code" in msg.text


# ---------------------------------------------------------------------------
# Verbose sub-page
# ---------------------------------------------------------------------------


class TestVerbose:
    @pytest.mark.anyio
    async def test_verbose_page_renders(self):
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="vb", text="config:vb")
        await cmd.handle(ctx)
        assert "Verbose" in _last_edit_msg(ctx).text

    @pytest.mark.anyio
    async def test_verbose_set_on_returns_home(self):
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="vb:on", text="config:vb:on")
        await cmd.handle(ctx)
        assert _VERBOSE_OVERRIDES.get(123) == "verbose"
        msg = _last_edit_msg(ctx)
        assert "Settings" in msg.text  # Home page

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
        assert "config:home" in _buttons_data(_last_edit_msg(ctx))


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
        assert "engine" in _last_edit_msg(ctx).text.lower()

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
        data = _buttons_data(_last_edit_msg(ctx))
        assert "config:ag:codex" in data
        assert "config:ag:claude" in data
        assert "config:ag:opencode" in data

    @pytest.mark.anyio
    async def test_engine_set_returns_home(self, tmp_path):
        """Setting an engine returns to home page."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="ag:claude",
            text="config:ag:claude",
            config_path=state_path,
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Settings" in msg.text  # Home page

    @pytest.mark.anyio
    async def test_engine_clear_returns_home(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="ag:claude",
            text="config:ag:claude",
            config_path=state_path,
        )
        await cmd.handle(ctx)
        ctx = _make_ctx(
            args_text="ag:clr",
            text="config:ag:clr",
            config_path=state_path,
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Settings" in msg.text

    @pytest.mark.anyio
    async def test_engine_invalid_shows_sub_page(self, tmp_path):
        """Setting an engine not in engine_ids stays on sub-page (no action taken)."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="ag:nonexistent",
            text="config:ag:nonexistent",
            config_path=state_path,
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "global default" in msg.text.lower()

    @pytest.mark.anyio
    async def test_engine_no_config_path(self):
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="ag", text="config:ag", config_path=None)
        await cmd.handle(ctx)
        assert "Unavailable" in _last_edit_msg(ctx).text

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
        buttons = _last_edit_msg(ctx).extra["reply_markup"]["inline_keyboard"]
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
        assert "Trigger" in _last_edit_msg(ctx).text

    @pytest.mark.anyio
    async def test_trigger_set_mentions_returns_home(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="tr:men",
            text="config:tr:men",
            config_path=state_path,
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Settings" in msg.text  # Home page

    @pytest.mark.anyio
    async def test_trigger_set_all_returns_home(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="tr:men",
            text="config:tr:men",
            config_path=state_path,
        )
        await cmd.handle(ctx)
        ctx = _make_ctx(
            args_text="tr:all",
            text="config:tr:all",
            config_path=state_path,
        )
        await cmd.handle(ctx)
        assert "Settings" in _last_edit_msg(ctx).text

    @pytest.mark.anyio
    async def test_trigger_clear_returns_home(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="tr:men",
            text="config:tr:men",
            config_path=state_path,
        )
        await cmd.handle(ctx)
        ctx = _make_ctx(
            args_text="tr:clr",
            text="config:tr:clr",
            config_path=state_path,
        )
        await cmd.handle(ctx)
        assert "Settings" in _last_edit_msg(ctx).text

    @pytest.mark.anyio
    async def test_trigger_no_config_path(self):
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="tr", text="config:tr", config_path=None)
        await cmd.handle(ctx)
        assert "Unavailable" in _last_edit_msg(ctx).text

    @pytest.mark.anyio
    async def test_trigger_has_back_button(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="tr", text="config:tr", config_path=state_path)
        await cmd.handle(ctx)
        assert "config:home" in _buttons_data(_last_edit_msg(ctx))


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
        assert "Settings" in _last_edit_msg(ctx).text

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
        assert _last_send_msg(ctx).extra["parse_mode"] == "HTML"


# ---------------------------------------------------------------------------
# Engine-aware home page transitions
# ---------------------------------------------------------------------------


class TestEngineAwareTransitions:
    @pytest.mark.anyio
    async def test_switch_to_claude_reveals_plan_mode(self, tmp_path):
        """After switching engine to claude, home page shows plan mode."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        # Set engine to claude (returns home)
        ctx = _make_ctx(
            args_text="ag:claude",
            text="config:ag:claude",
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Plan mode" in msg.text
        assert "config:pm" in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_switch_from_claude_hides_plan_mode(self, tmp_path):
        """After switching engine away from claude, home page hides plan mode."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        # Start with claude, switch to codex
        ctx = _make_ctx(
            args_text="ag:codex",
            text="config:ag:codex",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Plan mode" not in msg.text
        assert "config:pm" not in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_switch_to_codex_reveals_reasoning(self, tmp_path):
        """After switching engine to codex, home page shows reasoning."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="ag:codex",
            text="config:ag:codex",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Reasoning" in msg.text
        assert "config:rs" in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_switch_from_codex_hides_reasoning(self, tmp_path):
        """After switching engine away from codex, home page hides reasoning."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="ag:claude",
            text="config:ag:claude",
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Reasoning" not in msg.text
        assert "config:rs" not in _buttons_data(msg)


# ---------------------------------------------------------------------------
# Model sub-page
# ---------------------------------------------------------------------------


class TestModel:
    @pytest.mark.anyio
    async def test_model_page_renders(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="md", text="config:md", config_path=state_path)
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Model" in msg.text
        assert "default" in msg.text.lower()

    @pytest.mark.anyio
    async def test_model_shows_current_engine(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="md",
            text="config:md",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        assert "claude" in _last_edit_msg(ctx).text

    @pytest.mark.anyio
    async def test_model_shows_override_value(self, tmp_path):
        """When a model override is set, sub-page shows it."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(
            123, "codex", EngineOverrides(model="gpt-4.1-mini")
        )

        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="md",
            text="config:md",
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)
        assert "gpt-4.1-mini" in _last_edit_msg(ctx).text

    @pytest.mark.anyio
    async def test_model_clear_returns_home(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(
            123, "codex", EngineOverrides(model="gpt-4.1-mini")
        )

        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="md:clr",
            text="config:md:clr",
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Settings" in msg.text  # Home page

    @pytest.mark.anyio
    async def test_model_clear_removes_override(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(
            123, "codex", EngineOverrides(model="gpt-4.1-mini")
        )

        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="md:clr",
            text="config:md:clr",
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)

        override = await prefs.get_engine_override(123, "codex")
        assert override is None or override.model is None

    @pytest.mark.anyio
    async def test_model_clear_preserves_other_overrides(self, tmp_path):
        """Clearing model preserves reasoning and permission_mode."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(
            123,
            "codex",
            EngineOverrides(model="gpt-4.1", reasoning="high"),
        )

        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="md:clr",
            text="config:md:clr",
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)

        override = await prefs.get_engine_override(123, "codex")
        assert override is not None
        assert override.model is None
        assert override.reasoning == "high"

    @pytest.mark.anyio
    async def test_model_no_config_path(self):
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="md", text="config:md", config_path=None)
        await cmd.handle(ctx)
        assert "Unavailable" in _last_edit_msg(ctx).text

    @pytest.mark.anyio
    async def test_model_has_back_button(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="md", text="config:md", config_path=state_path)
        await cmd.handle(ctx)
        assert "config:home" in _buttons_data(_last_edit_msg(ctx))

    @pytest.mark.anyio
    async def test_model_has_clear_button(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="md", text="config:md", config_path=state_path)
        await cmd.handle(ctx)
        assert "config:md:clr" in _buttons_data(_last_edit_msg(ctx))

    @pytest.mark.anyio
    async def test_home_shows_model_label(self, tmp_path):
        """Model label always appears on home page."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path)
        await cmd.handle(ctx)
        assert "Model" in _last_send_msg(ctx).text

    @pytest.mark.anyio
    async def test_home_shows_model_button(self, tmp_path):
        """Model button always appears on home page."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path)
        await cmd.handle(ctx)
        assert "config:md" in _buttons_data(_last_send_msg(ctx))

    @pytest.mark.anyio
    async def test_home_model_shows_override(self, tmp_path):
        """Home page model label shows override value when set."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(123, "codex", EngineOverrides(model="o4-mini"))

        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="codex")
        await cmd.handle(ctx)
        assert "o4-mini" in _last_send_msg(ctx).text


# ---------------------------------------------------------------------------
# Reasoning sub-page
# ---------------------------------------------------------------------------


class TestReasoning:
    @pytest.mark.anyio
    async def test_reasoning_page_renders(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="rs",
            text="config:rs",
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Reasoning" in msg.text
        assert "config:rs:min" in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_reasoning_shows_all_levels(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="rs",
            text="config:rs",
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)
        data = _buttons_data(_last_edit_msg(ctx))
        assert "config:rs:min" in data
        assert "config:rs:low" in data
        assert "config:rs:med" in data
        assert "config:rs:hi" in data
        assert "config:rs:xhi" in data

    @pytest.mark.anyio
    async def test_reasoning_set_returns_home(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="rs:hi",
            text="config:rs:hi",
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Settings" in msg.text

    @pytest.mark.anyio
    async def test_reasoning_set_persists(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path

        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="rs:med",
            text="config:rs:med",
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)

        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        override = await prefs.get_engine_override(123, "codex")
        assert override is not None
        assert override.reasoning == "medium"

    @pytest.mark.anyio
    async def test_reasoning_set_all_levels(self, tmp_path):
        """All 5 reasoning levels map correctly."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path

        expected = {
            "min": "minimal",
            "low": "low",
            "med": "medium",
            "hi": "high",
            "xhi": "xhigh",
        }
        state_path = tmp_path / "prefs.json"

        for action, level in expected.items():
            cmd = ConfigCommand()
            ctx = _make_ctx(
                args_text=f"rs:{action}",
                text=f"config:rs:{action}",
                config_path=state_path,
                default_engine="codex",
            )
            await cmd.handle(ctx)
            prefs = ChatPrefsStore(resolve_prefs_path(state_path))
            override = await prefs.get_engine_override(123, "codex")
            assert override is not None
            assert override.reasoning == level, f"rs:{action} should set {level}"

    @pytest.mark.anyio
    async def test_reasoning_clear_returns_home(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(123, "codex", EngineOverrides(reasoning="high"))

        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="rs:clr",
            text="config:rs:clr",
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Settings" in msg.text

    @pytest.mark.anyio
    async def test_reasoning_clear_removes_override(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(123, "codex", EngineOverrides(reasoning="high"))

        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="rs:clr",
            text="config:rs:clr",
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)

        override = await prefs.get_engine_override(123, "codex")
        assert override is None or override.reasoning is None

    @pytest.mark.anyio
    async def test_reasoning_clear_preserves_model(self, tmp_path):
        """Clearing reasoning preserves model override."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(
            123,
            "codex",
            EngineOverrides(model="gpt-4.1", reasoning="high"),
        )

        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="rs:clr",
            text="config:rs:clr",
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)

        override = await prefs.get_engine_override(123, "codex")
        assert override is not None
        assert override.model == "gpt-4.1"
        assert override.reasoning is None

    @pytest.mark.anyio
    async def test_reasoning_guard_non_codex(self, tmp_path):
        """Reasoning page shows guard message when engine is not codex."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="rs",
            text="config:rs",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Only available for engines" in msg.text
        assert "config:home" in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_reasoning_no_config_path(self):
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="rs", text="config:rs", config_path=None)
        await cmd.handle(ctx)
        assert "Unavailable" in _last_edit_msg(ctx).text

    @pytest.mark.anyio
    async def test_reasoning_has_back_button(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="rs",
            text="config:rs",
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)
        assert "config:home" in _buttons_data(_last_edit_msg(ctx))

    @pytest.mark.anyio
    async def test_reasoning_checkmark_on_active(self, tmp_path):
        """Active reasoning level shows checkmark."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(123, "codex", EngineOverrides(reasoning="high"))

        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="rs",
            text="config:rs",
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)
        labels = _buttons_labels(_last_edit_msg(ctx))
        assert any("✓" in label and "High" in label for label in labels)

    @pytest.mark.anyio
    async def test_home_shows_reasoning_for_codex(self, tmp_path):
        """Reasoning label and button visible when engine is codex."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "Reasoning" in msg.text
        assert "config:rs" in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_home_hides_reasoning_for_claude(self, tmp_path):
        """Reasoning label and button hidden when engine is claude."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "Reasoning" not in msg.text
        assert "config:rs" not in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_home_reasoning_shows_override(self, tmp_path):
        """Home page reasoning label shows override value when set."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(
            123, "codex", EngineOverrides(reasoning="medium")
        )

        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="codex")
        await cmd.handle(ctx)
        assert "medium" in _last_send_msg(ctx).text


# ---------------------------------------------------------------------------
# Reasoning toasts
# ---------------------------------------------------------------------------


class TestReasoningToasts:
    def test_toast_reasoning_minimal(self):
        assert ConfigCommand.early_answer_toast("rs:min") == "Reasoning: minimal"

    def test_toast_reasoning_low(self):
        assert ConfigCommand.early_answer_toast("rs:low") == "Reasoning: low"

    def test_toast_reasoning_medium(self):
        assert ConfigCommand.early_answer_toast("rs:med") == "Reasoning: medium"

    def test_toast_reasoning_high(self):
        assert ConfigCommand.early_answer_toast("rs:hi") == "Reasoning: high"

    def test_toast_reasoning_xhigh(self):
        assert ConfigCommand.early_answer_toast("rs:xhi") == "Reasoning: xhigh"

    def test_toast_reasoning_clear(self):
        assert ConfigCommand.early_answer_toast("rs:clr") == "Reasoning: cleared"

    def test_toast_model_clear(self):
        assert ConfigCommand.early_answer_toast("md:clr") == "Model: cleared"
