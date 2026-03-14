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
    ctx.runtime.default_context_for_chat.return_value = None
    ctx.runtime.project_default_engine.return_value = None
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
        assert ConfigCommand.early_answer_toast("pm:clr") == "Permission mode: cleared"

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
        """When engine is claude, all nav buttons present."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="claude")
        await cmd.handle(ctx)
        data = _buttons_data(_last_send_msg(ctx))
        assert "config:pm" in data
        assert "config:vb" in data
        assert "config:ag" in data
        assert "config:tr" in data
        assert "config:dp" in data

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
    async def test_planmode_guard_unsupported_engine(self, tmp_path):
        """Permission mode page shows guard for unsupported engines."""
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
        assert "Only available for Claude Code and Gemini CLI" in msg.text
        assert "config:home" in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_planmode_guard_unsupported_with_override(self, tmp_path):
        """Permission mode guard respects per-chat engine override."""
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
        assert "Only available for Claude Code and Gemini CLI" in msg.text


# ---------------------------------------------------------------------------
# Gemini approval mode (via plan mode page)
# ---------------------------------------------------------------------------


class TestGeminiApprovalMode:
    @pytest.mark.anyio
    async def test_approval_mode_page_renders(self, tmp_path):
        """Navigating to pm page with gemini engine shows approval mode."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="pm",
            text="config:pm",
            config_path=state_path,
            default_engine="gemini",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Approval mode" in msg.text
        assert "config:pm:fa" in _buttons_data(msg)
        assert "config:pm:ro" in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_set_full_access_stores_yolo(self, tmp_path):
        """Setting full access stores 'yolo' as permission_mode."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path

        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="pm:fa",
            text="config:pm:fa",
            config_path=state_path,
            default_engine="gemini",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Settings" in msg.text  # Returns to home
        assert "full access" in msg.text.lower()

        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        override = await prefs.get_engine_override(123, "gemini")
        assert override is not None
        assert override.permission_mode == "yolo"

    @pytest.mark.anyio
    async def test_set_readonly_clears_permission(self, tmp_path):
        """Setting read-only clears the permission_mode override."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(
            123, "gemini", EngineOverrides(permission_mode="yolo")
        )

        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="pm:ro",
            text="config:pm:ro",
            config_path=state_path,
            default_engine="gemini",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Settings" in msg.text
        assert "read-only" in msg.text.lower()

    @pytest.mark.anyio
    async def test_clear_returns_home(self, tmp_path):
        """Clearing approval mode returns to home page."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="pm:fa",
            text="config:pm:fa",
            config_path=state_path,
            default_engine="gemini",
        )
        await cmd.handle(ctx)
        ctx = _make_ctx(
            args_text="pm:clr",
            text="config:pm:clr",
            config_path=state_path,
            default_engine="gemini",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Settings" in msg.text

    @pytest.mark.anyio
    async def test_home_shows_approval_mode_for_gemini(self, tmp_path):
        """Home page shows 'Approval mode' label and button for gemini."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="gemini")
        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "Approval mode" in msg.text
        assert "config:pm" in _buttons_data(msg)
        # Should NOT show Claude-specific features
        assert "Plan mode" not in msg.text
        assert "Ask mode" not in msg.text

    @pytest.mark.anyio
    async def test_home_shows_full_access_label(self, tmp_path):
        """Home page shows 'full access' when yolo is set."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(
            123, "gemini", EngineOverrides(permission_mode="yolo")
        )

        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="gemini")
        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "full access" in msg.text.lower()

    @pytest.mark.anyio
    async def test_approval_mode_has_back_button(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="pm",
            text="config:pm",
            config_path=state_path,
            default_engine="gemini",
        )
        await cmd.handle(ctx)
        assert "config:home" in _buttons_data(_last_edit_msg(ctx))


class TestGeminiApprovalModeToasts:
    def test_toast_full_access(self):
        assert ConfigCommand.early_answer_toast("pm:fa") == "Approval mode: full access"

    def test_toast_read_only(self):
        assert ConfigCommand.early_answer_toast("pm:ro") == "Approval mode: read-only"


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
        assert "current:" in msg.text.lower()

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
    async def test_switch_to_unsupported_hides_reasoning(self, tmp_path):
        """After switching engine to one without reasoning, home page hides it."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="ag:gemini",
            text="config:ag:gemini",
            config_path=state_path,
            default_engine="codex",
            engine_ids=("codex", "gemini"),
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Reasoning" not in msg.text
        assert "config:rs" not in _buttons_data(msg)


# ---------------------------------------------------------------------------
# Project-level default engine
# ---------------------------------------------------------------------------


class TestProjectDefaultEngine:
    """Tests that /config respects project-level default_engine."""

    @pytest.mark.anyio
    async def test_home_uses_project_default_engine(self, tmp_path):
        """Home page resolves engine from project default, not global."""
        from untether.context import RunContext

        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="claude")
        # Project bound to this chat has default_engine="codex"
        ctx.runtime.default_context_for_chat.return_value = RunContext(
            project="codex-test"
        )
        ctx.runtime.project_default_engine.return_value = "codex"

        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "Engine: <b>codex</b>" in msg.text
        # Claude Code-specific buttons hidden
        assert "Plan mode" not in msg.text
        assert "config:pm" not in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_home_project_default_shows_default_annotation(self, tmp_path):
        """When project default matches global default, shows '(default)'."""
        from untether.context import RunContext

        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="claude")
        # Project also defaults to claude (same as global)
        ctx.runtime.default_context_for_chat.return_value = RunContext(
            project="claude-test"
        )
        ctx.runtime.project_default_engine.return_value = "claude"

        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "Engine: <b>claude (default)</b>" in msg.text
        # Claude Code buttons should be visible
        assert "Plan mode" in msg.text
        assert "config:pm" in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_home_chat_override_beats_project_default(self, tmp_path):
        """Chat-level override takes priority over project default."""
        from untether.context import RunContext
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_default_engine(123, "opencode")

        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="claude")
        # Project says codex, but chat override says opencode
        ctx.runtime.default_context_for_chat.return_value = RunContext(
            project="codex-test"
        )
        ctx.runtime.project_default_engine.return_value = "codex"

        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "Engine: <b>opencode</b>" in msg.text

    @pytest.mark.anyio
    async def test_planmode_guard_respects_project_default(self, tmp_path):
        """Plan mode guard uses project default, not global default."""
        from untether.context import RunContext

        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="pm",
            text="config:pm",
            config_path=state_path,
            default_engine="claude",
        )
        # Project default is codex — plan mode should be blocked
        ctx.runtime.default_context_for_chat.return_value = RunContext(
            project="codex-test"
        )
        ctx.runtime.project_default_engine.return_value = "codex"

        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Only available for Claude Code" in msg.text

    @pytest.mark.anyio
    async def test_engine_page_shows_effective_from_project(self, tmp_path):
        """Engine sub-page Current label reflects project default."""
        from untether.context import RunContext

        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="ag",
            text="config:ag",
            config_path=state_path,
            default_engine="claude",
        )
        ctx.runtime.default_context_for_chat.return_value = RunContext(
            project="pi-test"
        )
        ctx.runtime.project_default_engine.return_value = "pi"

        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Current: <b>pi</b>" in msg.text


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
    async def test_reasoning_shows_all_codex_levels(self, tmp_path):
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
    async def test_reasoning_shows_claude_levels(self, tmp_path):
        """Claude Code engine shows only low/medium/high (no minimal/xhigh)."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="rs",
            text="config:rs",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        data = _buttons_data(_last_edit_msg(ctx))
        assert "config:rs:low" in data
        assert "config:rs:med" in data
        assert "config:rs:hi" in data
        assert "config:rs:min" not in data
        assert "config:rs:xhi" not in data

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
    async def test_reasoning_guard_unsupported_engine(self, tmp_path):
        """Reasoning page shows guard message for unsupported engines."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="rs",
            text="config:rs",
            config_path=state_path,
            default_engine="gemini",
            engine_ids=("gemini", "claude"),
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
    async def test_home_shows_reasoning_for_claude(self, tmp_path):
        """Reasoning label and button visible when engine is claude."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "Reasoning" in msg.text
        assert "config:rs" in _buttons_data(msg)

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


# ---------------------------------------------------------------------------
# Ask questions sub-page
# ---------------------------------------------------------------------------


class TestAskQuestions:
    @pytest.mark.anyio
    async def test_ask_questions_page_renders(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="aq",
            text="config:aq",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Ask mode" in msg.text
        assert "config:aq:on" in _buttons_data(msg)
        assert "config:aq:off" in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_ask_questions_set_on(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path

        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="aq:on",
            text="config:aq:on",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Settings" in msg.text

        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        override = await prefs.get_engine_override(123, "claude")
        assert override is not None
        assert override.ask_questions is True

    @pytest.mark.anyio
    async def test_ask_questions_set_off(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path

        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="aq:off",
            text="config:aq:off",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)

        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        override = await prefs.get_engine_override(123, "claude")
        assert override is not None
        assert override.ask_questions is False

    @pytest.mark.anyio
    async def test_ask_questions_clear(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(
            123, "claude", EngineOverrides(ask_questions=True)
        )

        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="aq:clr",
            text="config:aq:clr",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)

        override = await prefs.get_engine_override(123, "claude")
        assert override is None or override.ask_questions is None

    @pytest.mark.anyio
    async def test_ask_questions_preserves_model(self, tmp_path):
        """Setting ask_questions should preserve model override."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(123, "claude", EngineOverrides(model="sonnet"))

        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="aq:on",
            text="config:aq:on",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)

        override = await prefs.get_engine_override(123, "claude")
        assert override is not None
        assert override.model == "sonnet"
        assert override.ask_questions is True

    @pytest.mark.anyio
    async def test_ask_questions_guard_non_claude(self, tmp_path):
        """Ask questions page shows guard message for non-Claude Code engines."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="aq",
            text="config:aq",
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Only available for Claude Code" in msg.text

    @pytest.mark.anyio
    async def test_ask_questions_no_config_path(self):
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="aq", text="config:aq", config_path=None)
        await cmd.handle(ctx)
        assert "Unavailable" in _last_edit_msg(ctx).text

    @pytest.mark.anyio
    async def test_ask_questions_shown_on_home_for_claude(self, tmp_path):
        """Ask button should appear on home page when engine is Claude Code."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="claude")
        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "Ask mode:" in msg.text
        assert "config:aq" in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_ask_questions_hidden_on_home_for_codex(self, tmp_path):
        """Ask button should NOT appear on home page when engine is Codex."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="codex")
        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "Ask mode:" not in msg.text
        assert "config:aq" not in _buttons_data(msg)


# ---------------------------------------------------------------------------
# Ask questions toasts
# ---------------------------------------------------------------------------


class TestAskQuestionsToasts:
    def test_toast_ask_on(self):
        assert ConfigCommand.early_answer_toast("aq:on") == "Ask mode: on"

    def test_toast_ask_off(self):
        assert ConfigCommand.early_answer_toast("aq:off") == "Ask mode: off"

    def test_toast_ask_clear(self):
        assert ConfigCommand.early_answer_toast("aq:clr") == "Ask mode: cleared"


# ---------------------------------------------------------------------------
# Diff preview toggle
# ---------------------------------------------------------------------------


class TestDiffPreview:
    @pytest.mark.anyio
    async def test_diff_preview_page_renders(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="dp",
            text="config:dp",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Diff preview" in msg.text
        assert "config:dp:on" in _buttons_data(msg)
        assert "config:dp:off" in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_diff_preview_set_on(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path

        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="dp:on",
            text="config:dp:on",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Settings" in msg.text

        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        override = await prefs.get_engine_override(123, "claude")
        assert override is not None
        assert override.diff_preview is True

    @pytest.mark.anyio
    async def test_diff_preview_set_off(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path

        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="dp:off",
            text="config:dp:off",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)

        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        override = await prefs.get_engine_override(123, "claude")
        assert override is not None
        assert override.diff_preview is False

    @pytest.mark.anyio
    async def test_diff_preview_clear(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(
            123, "claude", EngineOverrides(diff_preview=True)
        )

        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="dp:clr",
            text="config:dp:clr",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)

        override = await prefs.get_engine_override(123, "claude")
        assert override is None or override.diff_preview is None

    @pytest.mark.anyio
    async def test_diff_preview_preserves_model(self, tmp_path):
        """Setting diff_preview should preserve model override."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(123, "claude", EngineOverrides(model="sonnet"))

        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="dp:off",
            text="config:dp:off",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)

        override = await prefs.get_engine_override(123, "claude")
        assert override is not None
        assert override.model == "sonnet"
        assert override.diff_preview is False

    @pytest.mark.anyio
    async def test_diff_preview_guard_non_claude(self, tmp_path):
        """Diff preview page shows guard message for non-Claude Code engines."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="dp",
            text="config:dp",
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Only available for Claude Code" in msg.text

    @pytest.mark.anyio
    async def test_diff_preview_no_config_path(self):
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="dp", text="config:dp", config_path=None)
        await cmd.handle(ctx)
        assert "Unavailable" in _last_edit_msg(ctx).text

    @pytest.mark.anyio
    async def test_diff_preview_shown_on_home_for_claude(self, tmp_path):
        """Diff preview button should appear on home page when engine is Claude Code."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="claude")
        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "Diff preview:" in msg.text
        assert "config:dp" in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_diff_preview_hidden_on_home_for_codex(self, tmp_path):
        """Diff preview button should NOT appear on home page for Codex."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="codex")
        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "Diff preview:" not in msg.text
        assert "config:dp" not in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_diff_preview_default_label_on_home(self, tmp_path):
        """No override → home shows 'default'."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="claude")
        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "Diff preview: <b>off</b>" in msg.text

    @pytest.mark.anyio
    async def test_diff_preview_on_label_on_home(self, tmp_path):
        """diff_preview=True → home shows 'on'."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(
            123, "claude", EngineOverrides(diff_preview=True)
        )

        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="claude")
        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "Diff preview: <b>on</b>" in msg.text

    @pytest.mark.anyio
    async def test_diff_preview_off_label_on_home(self, tmp_path):
        """diff_preview=False → home shows 'off'."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(
            123, "claude", EngineOverrides(diff_preview=False)
        )

        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="claude")
        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "Diff preview: <b>off</b>" in msg.text

    @pytest.mark.anyio
    async def test_diff_preview_has_back_button(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="dp",
            text="config:dp",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        assert "config:home" in _buttons_data(_last_edit_msg(ctx))

    @pytest.mark.anyio
    async def test_diff_preview_has_clear_button(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="dp",
            text="config:dp",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        assert "config:dp:clr" in _buttons_data(_last_edit_msg(ctx))

    @pytest.mark.anyio
    async def test_diff_preview_checkmark_on(self, tmp_path):
        """When diff_preview=True, On button has checkmark."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(
            123, "claude", EngineOverrides(diff_preview=True)
        )

        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="dp",
            text="config:dp",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        labels = _buttons_labels(msg)
        assert "✓ On" in labels

    @pytest.mark.anyio
    async def test_diff_preview_default_label_on_page(self, tmp_path):
        """No override → page shows 'default (off)'."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="dp",
            text="config:dp",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "default (off)" in msg.text


# ---------------------------------------------------------------------------
# Diff preview toasts
# ---------------------------------------------------------------------------


class TestDiffPreviewToasts:
    def test_toast_diff_preview_on(self):
        assert ConfigCommand.early_answer_toast("dp:on") == "Diff preview: on"

    def test_toast_diff_preview_off(self):
        assert ConfigCommand.early_answer_toast("dp:off") == "Diff preview: off"

    def test_toast_diff_preview_clear(self):
        assert ConfigCommand.early_answer_toast("dp:clr") == "Diff preview: cleared"


class TestCostUsage:
    @pytest.mark.anyio
    async def test_cost_usage_page_renders_for_claude(self, tmp_path):
        """Claude Code sees both API cost and subscription usage toggles."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="cu",
            text="config:cu",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Cost & usage" in msg.text
        assert "API cost" in msg.text
        assert "Subscription usage" in msg.text
        buttons = _buttons_data(msg)
        assert "config:cu:ac_on" in buttons
        assert "config:cu:su_on" in buttons

    @pytest.mark.anyio
    async def test_cost_usage_page_renders_for_opencode(self, tmp_path):
        """OpenCode sees API cost but not subscription usage."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="cu",
            text="config:cu",
            config_path=state_path,
            default_engine="opencode",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "API cost" in msg.text
        assert "Subscription usage" not in msg.text
        buttons = _buttons_data(msg)
        assert "config:cu:ac_on" in buttons
        assert "config:cu:su_on" not in buttons

    @pytest.mark.anyio
    async def test_cost_usage_guard_unsupported_engine(self, tmp_path):
        """Codex/Pi show guard message — no cost data available."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="cu",
            text="config:cu",
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Not available" in msg.text

    @pytest.mark.anyio
    async def test_api_cost_set_on(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path

        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="cu:ac_on",
            text="config:cu:ac_on",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)

        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        override = await prefs.get_engine_override(123, "claude")
        assert override is not None
        assert override.show_api_cost is True

    @pytest.mark.anyio
    async def test_api_cost_set_off(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path

        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="cu:ac_off",
            text="config:cu:ac_off",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)

        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        override = await prefs.get_engine_override(123, "claude")
        assert override is not None
        assert override.show_api_cost is False

    @pytest.mark.anyio
    async def test_api_cost_clear(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(
            123, "claude", EngineOverrides(show_api_cost=True)
        )

        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="cu:ac_clr",
            text="config:cu:ac_clr",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)

        override = await prefs.get_engine_override(123, "claude")
        assert override is None or override.show_api_cost is None

    @pytest.mark.anyio
    async def test_subscription_usage_set_on(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path

        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="cu:su_on",
            text="config:cu:su_on",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)

        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        override = await prefs.get_engine_override(123, "claude")
        assert override is not None
        assert override.show_subscription_usage is True

    @pytest.mark.anyio
    async def test_subscription_usage_set_off(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path

        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="cu:su_off",
            text="config:cu:su_off",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)

        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        override = await prefs.get_engine_override(123, "claude")
        assert override is not None
        assert override.show_subscription_usage is False

    @pytest.mark.anyio
    async def test_subscription_usage_clear(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(
            123, "claude", EngineOverrides(show_subscription_usage=True)
        )

        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="cu:su_clr",
            text="config:cu:su_clr",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)

        override = await prefs.get_engine_override(123, "claude")
        assert override is None or override.show_subscription_usage is None

    @pytest.mark.anyio
    async def test_cost_usage_preserves_model(self, tmp_path):
        """Setting cost toggle should preserve model override."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(123, "claude", EngineOverrides(model="sonnet"))

        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="cu:ac_off",
            text="config:cu:ac_off",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)

        override = await prefs.get_engine_override(123, "claude")
        assert override is not None
        assert override.model == "sonnet"
        assert override.show_api_cost is False

    @pytest.mark.anyio
    async def test_cost_usage_no_config_path(self):
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="cu", text="config:cu", config_path=None)
        await cmd.handle(ctx)
        assert "Unavailable" in _last_edit_msg(ctx).text

    @pytest.mark.anyio
    async def test_cost_usage_shown_on_home_for_claude(self, tmp_path):
        """Cost & usage button should appear on home for Claude Code."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="claude")
        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "Cost & usage:" in msg.text
        assert "config:cu" in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_cost_usage_shown_on_home_for_opencode(self, tmp_path):
        """Cost & usage button should appear on home for OpenCode."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="opencode")
        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "Cost & usage:" in msg.text
        assert "config:cu" in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_cost_usage_hidden_on_home_for_codex(self, tmp_path):
        """Cost & usage button should NOT appear on home for Codex."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="codex")
        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "Cost & usage:" not in msg.text
        assert "config:cu" not in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_cost_usage_hidden_on_home_for_pi(self, tmp_path):
        """Cost & usage button should NOT appear on home for Pi."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="pi")
        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "Cost & usage:" not in msg.text
        assert "config:cu" not in _buttons_data(msg)


class TestCostUsageToasts:
    def test_toast_api_cost_on(self):
        assert ConfigCommand.early_answer_toast("cu:ac_on") == "API cost: on"

    def test_toast_api_cost_off(self):
        assert ConfigCommand.early_answer_toast("cu:ac_off") == "API cost: off"

    def test_toast_api_cost_clear(self):
        assert ConfigCommand.early_answer_toast("cu:ac_clr") == "API cost: cleared"

    def test_toast_sub_usage_on(self):
        assert ConfigCommand.early_answer_toast("cu:su_on") == "Sub usage: on"

    def test_toast_sub_usage_off(self):
        assert ConfigCommand.early_answer_toast("cu:su_off") == "Sub usage: off"

    def test_toast_sub_usage_clear(self):
        assert ConfigCommand.early_answer_toast("cu:su_clr") == "Sub usage: cleared"


# ---------------------------------------------------------------------------
# Docs links on sub-pages
# ---------------------------------------------------------------------------


class TestDocsLinks:
    """Each sub-page should include a docs link."""

    _DOCS_BASE = "littlebearapps.com/tools/untether/how-to/"

    @pytest.mark.anyio
    async def test_planmode_has_docs_link(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="pm",
            text="config:pm",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        assert self._DOCS_BASE in _last_edit_msg(ctx).text

    @pytest.mark.anyio
    async def test_verbose_has_docs_link(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="vb",
            text="config:vb",
            config_path=state_path,
        )
        await cmd.handle(ctx)
        assert self._DOCS_BASE in _last_edit_msg(ctx).text

    @pytest.mark.anyio
    async def test_engine_has_docs_link(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="ag",
            text="config:ag",
            config_path=state_path,
        )
        await cmd.handle(ctx)
        assert self._DOCS_BASE in _last_edit_msg(ctx).text

    @pytest.mark.anyio
    async def test_trigger_has_docs_link(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="tr",
            text="config:tr",
            config_path=state_path,
        )
        await cmd.handle(ctx)
        assert self._DOCS_BASE in _last_edit_msg(ctx).text

    @pytest.mark.anyio
    async def test_model_has_docs_link(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="md",
            text="config:md",
            config_path=state_path,
        )
        await cmd.handle(ctx)
        assert self._DOCS_BASE in _last_edit_msg(ctx).text

    @pytest.mark.anyio
    async def test_ask_mode_has_docs_link(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="aq",
            text="config:aq",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        assert self._DOCS_BASE in _last_edit_msg(ctx).text

    @pytest.mark.anyio
    async def test_diff_preview_has_docs_link(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="dp",
            text="config:dp",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        assert self._DOCS_BASE in _last_edit_msg(ctx).text

    @pytest.mark.anyio
    async def test_cost_usage_has_docs_link(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="cu",
            text="config:cu",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        assert self._DOCS_BASE in _last_edit_msg(ctx).text

    @pytest.mark.anyio
    async def test_home_has_docs_links(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="claude")
        await cmd.handle(ctx)
        text = _last_send_msg(ctx).text
        assert "Settings guide" in text
        assert "Troubleshooting" in text
        assert self._DOCS_BASE in text


# ---------------------------------------------------------------------------
# Home page grouped sections
# ---------------------------------------------------------------------------


class TestHomePageSections:
    @pytest.mark.anyio
    async def test_claude_home_has_agent_controls_section(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="claude")
        await cmd.handle(ctx)
        text = _last_send_msg(ctx).text
        assert "Agent controls" in text
        assert "Claude Code" in text

    @pytest.mark.anyio
    async def test_non_claude_home_no_agent_controls(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="codex")
        await cmd.handle(ctx)
        text = _last_send_msg(ctx).text
        assert "Agent controls" not in text

    @pytest.mark.anyio
    async def test_home_has_display_section(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="claude")
        await cmd.handle(ctx)
        assert "Display" in _last_send_msg(ctx).text

    @pytest.mark.anyio
    async def test_home_has_routing_section(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="claude")
        await cmd.handle(ctx)
        assert "Routing" in _last_send_msg(ctx).text

    @pytest.mark.anyio
    async def test_home_micro_hint_verbose_off(self, tmp_path):
        """Verbose=off should show 'compact progress' hint."""
        state_path = tmp_path / "prefs.json"
        _VERBOSE_OVERRIDES[123] = "compact"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="claude")
        await cmd.handle(ctx)
        assert "compact progress" in _last_send_msg(ctx).text

    @pytest.mark.anyio
    async def test_home_micro_hint_trigger_all(self, tmp_path):
        """Trigger=all should show 'respond to everything' hint."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="claude")
        await cmd.handle(ctx)
        assert "respond to everything" in _last_send_msg(ctx).text

    @pytest.mark.anyio
    async def test_home_no_versions_line(self, tmp_path, monkeypatch):
        """Versions line moved to About page — should not appear on home."""
        from untether.telegram import backend as telegram_backend

        monkeypatch.setattr(
            telegram_backend, "_detect_cli_version", lambda cmd: "1.0.0"
        )
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="claude")
        await cmd.handle(ctx)
        text = _last_send_msg(ctx).text
        assert "py " not in text
        assert "claude 1.0.0" not in text

    @pytest.mark.anyio
    async def test_home_has_about_button(self, tmp_path):
        """Home page should have an About button."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="claude")
        await cmd.handle(ctx)
        assert "config:ab" in _buttons_data(_last_send_msg(ctx))

    @pytest.mark.anyio
    async def test_home_has_about_button_codex(self, tmp_path):
        """About button appears for non-Claude engines too."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="codex")
        await cmd.handle(ctx)
        assert "config:ab" in _buttons_data(_last_send_msg(ctx))

    @pytest.mark.anyio
    async def test_home_has_about_button_gemini(self, tmp_path):
        """About button appears for Gemini engine."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="gemini")
        await cmd.handle(ctx)
        assert "config:ab" in _buttons_data(_last_send_msg(ctx))


# ---------------------------------------------------------------------------
# About page
# ---------------------------------------------------------------------------


class TestAboutPage:
    @pytest.mark.anyio
    async def test_about_shows_version(self, tmp_path):
        """About page should show Untether version."""
        from untether import __version__

        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="ab", text="config:ab", config_path=state_path)
        await cmd.handle(ctx)
        text = _last_edit_msg(ctx).text
        assert __version__ in text
        assert "About Untether" in text

    @pytest.mark.anyio
    async def test_about_shows_versions_line(self, tmp_path, monkeypatch):
        """About page should show engine versions."""
        from untether.telegram import backend as telegram_backend

        monkeypatch.setattr(
            telegram_backend, "_detect_cli_version", lambda cmd: "1.0.0"
        )
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="ab", text="config:ab", config_path=state_path)
        await cmd.handle(ctx)
        text = _last_edit_msg(ctx).text
        assert "py " in text
        assert "claude 1.0.0" in text

    @pytest.mark.anyio
    async def test_about_shows_github_links(self, tmp_path):
        """About page should show GitHub repo and issue links."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="ab", text="config:ab", config_path=state_path)
        await cmd.handle(ctx)
        text = _last_edit_msg(ctx).text
        assert "github.com/littlebearapps/untether" in text
        assert "Report a bug" in text
        assert "Feature request" in text

    @pytest.mark.anyio
    async def test_about_has_back_button(self, tmp_path):
        """About page should have a back button."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="ab", text="config:ab", config_path=state_path)
        await cmd.handle(ctx)
        assert "config:home" in _buttons_data(_last_edit_msg(ctx))
