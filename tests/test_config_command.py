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
    # #294: most config tests don't exercise the trigger manager. Default
    # to None so the home page skips the triggers indicator and the new
    # `_page_triggers` shows the unavailable branch when invoked.
    ctx.trigger_manager = None
    # #271: triggers page reads `ctx.default_chat_id`; default to None so
    # crons_for_chat / webhooks_for_chat fall back consistently.
    ctx.default_chat_id = None
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
        assert ConfigCommand.early_answer_toast("pm:on") == "Permission mode: on (plan)"

    def test_toast_planmode_off(self):
        assert (
            ConfigCommand.early_answer_toast("pm:off")
            == "Permission mode: off (acceptEdits)"
        )

    def test_toast_planmode_auto(self):
        # #741 `auto` is now Claude Code's own auto mode, not plan mode.
        assert ConfigCommand.early_answer_toast("pm:auto") == "Permission mode: auto"

    def test_toast_planmode_plan_auto(self):
        assert ConfigCommand.early_answer_toast("pm:pa") == "Permission mode: plan-auto"

    def test_toast_planmode_clear(self):
        assert ConfigCommand.early_answer_toast("pm:clr") == "Permission mode: cleared"

    def test_no_pm_toast_says_plan_mode(self):
        """#747: every Claude pm toast says "Permission mode:"."""
        for action in ("on", "off", "pa", "auto", "clr"):
            toast = ConfigCommand.early_answer_toast(f"pm:{action}")
            assert toast is not None
            assert toast.startswith("Permission mode:")
        assert ConfigCommand.early_answer_toast("pm:fa") == "Approval policy: full auto"
        assert ConfigCommand.early_answer_toast("pm:ya") == "Approval mode: full access"

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
        assert ConfigCommand.early_answer_toast("tr:all") == "Listen: all"

    def test_toast_trigger_mentions(self):
        assert ConfigCommand.early_answer_toast("tr:men") == "Listen: mentions"

    def test_toast_trigger_clear(self):
        assert ConfigCommand.early_answer_toast("tr:clr") == "Listen: cleared"

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
        assert "settings" in _last_send_msg(ctx).text.lower()

    @pytest.mark.anyio
    async def test_home_shows_plan_mode_when_claude(self, tmp_path):
        """Plan mode label and button visible when engine is claude."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="claude")
        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "Permission mode" in msg.text
        assert "config:pm" in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_home_hides_plan_mode_when_not_supported(self, tmp_path):
        """Plan mode label and button hidden for unsupported engines."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="opencode")
        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "Permission mode" not in msg.text
        assert "Approval" not in msg.text
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
    async def test_home_has_nav_buttons_non_permission_engine(self, tmp_path):
        """When engine has no permission support, no pm button."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="opencode")
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
        assert "settings" in _last_send_msg(ctx).text.lower()

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
        # #741 retitled: the page now also offers Claude Code's own auto mode,
        # which is not a plan mode.
        assert "Permission mode" in msg.text
        data = _buttons_data(msg)
        assert "config:pm:on" in data
        assert "config:pm:pa" in data
        assert "config:pm:auto" in data

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
        assert "settings" in msg.text.lower()  # Home page header
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
        assert "settings" in msg.text.lower()
        assert "default" in msg.text.lower()

    @pytest.mark.anyio
    async def test_planmode_no_config_path(self):
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="pm", text="config:pm", config_path=None)
        await cmd.handle(ctx)
        assert "Unavailable" in _last_edit_msg(ctx).text

    # --- #747 wording ---------------------------------------------------

    @staticmethod
    async def _pm_page(tmp_path) -> str:
        ctx = _make_ctx(
            args_text="pm",
            text="config:pm",
            config_path=tmp_path / "prefs.json",
            default_engine="claude",
        )
        await ConfigCommand().handle(ctx)
        return _last_edit_msg(ctx).text

    @staticmethod
    def _line(text: str, marker: str) -> str:
        matches = [ln for ln in text.splitlines() if marker in ln]
        assert len(matches) == 1, (marker, text)
        return matches[0]

    @pytest.mark.anyio
    async def test_pm_page_off_line(self, tmp_path):
        line = self._line(await self._pm_page(tmp_path), "<b>off</b>")
        assert "acceptEdits" in line
        assert "ask you here first" in line
        assert "run freely" not in line
        assert "no approval needed" not in line

    @pytest.mark.anyio
    async def test_pm_page_on_line(self, tmp_path):
        line = self._line(await self._pm_page(tmp_path), "<b>on</b>")
        assert "(plan)" in line
        assert "without editing files" in line
        assert "approve the plan" in line
        assert "every" not in line
        assert "read-only" not in line

    @pytest.mark.anyio
    async def test_pm_page_auto_line_hedged(self, tmp_path):
        line = self._line(await self._pm_page(tmp_path), "<b>auto</b>")
        assert "falls back" in line

    @pytest.mark.anyio
    async def test_pm_page_default_note(self, tmp_path):
        text = await self._pm_page(tmp_path)
        assert "engine config" in text
        assert "no approval buttons" in text
        assert "uses Claude Code's own permission mode" not in text

    @pytest.mark.anyio
    async def test_pm_page_apply_timing(self, tmp_path):
        """The behaviour behind these words is pinned by
        test_live_session_injection.py::
        test_changed_chat_options_close_session_instead_of_injecting —
        review the two together."""
        text = await self._pm_page(tmp_path)
        assert "next message" in text
        assert "background wake-ups" in text

    @pytest.mark.anyio
    async def test_pm_page_bullets_match_planmode(self, tmp_path):
        from untether.telegram.commands._permission_mode_text import (
            CLAUDE_MODE_TEXT,
        )

        text = await self._pm_page(tmp_path)
        for stored in ("acceptEdits", "plan", "plan-auto", "auto"):
            assert CLAUDE_MODE_TEXT[stored].summary in text

    @pytest.mark.anyio
    async def test_pm_page_learn_more_line_untouched(self, tmp_path):
        text = await self._pm_page(tmp_path)
        assert "📖" in text
        assert "Learn more" in text


class TestPermissionModeHomeHints:
    """#747: /config home hints for Claude permission modes."""

    @staticmethod
    async def _home_after(tmp_path, action: str | None) -> str:
        state_path = tmp_path / "prefs.json"
        if action is not None:
            ctx = _make_ctx(
                args_text=f"pm:{action}",
                text=f"config:pm:{action}",
                config_path=state_path,
                default_engine="claude",
            )
            await ConfigCommand().handle(ctx)
            return _last_edit_msg(ctx).text
        ctx = _make_ctx(config_path=state_path, default_engine="claude")
        await ConfigCommand().handle(ctx)
        return _last_send_msg(ctx).text

    @pytest.mark.anyio
    async def test_home_hint_off(self, tmp_path):
        text = await self._home_after(tmp_path, "off")
        assert "Permission mode: <b>off</b>  · edits run, others ask" in text
        assert "run freely" not in text

    @pytest.mark.anyio
    async def test_home_hint_on(self, tmp_path):
        text = await self._home_after(tmp_path, "on")
        assert "Permission mode: <b>on</b>  · approve the plan first" in text
        assert "approve actions" not in text

    @pytest.mark.anyio
    async def test_home_hint_no_override(self, tmp_path):
        text = await self._home_after(tmp_path, None)
        assert "Permission mode: <b>engine default</b>  · from engine config" in text
        assert "agent decides" not in text

    @pytest.mark.anyio
    async def test_home_hint_codex_gemini_unchanged(self, tmp_path):
        ctx = _make_ctx(config_path=tmp_path / "prefs.json", default_engine="codex")
        await ConfigCommand().handle(ctx)
        assert (
            "Approval policy: <b>full auto</b>  · Codex's own sandbox"
            in _last_send_msg(ctx).text
        )
        ctx = _make_ctx(
            config_path=tmp_path / "prefs.json",
            default_engine="gemini",
            engine_ids=("gemini",),
        )
        await ConfigCommand().handle(ctx)
        assert "read-only</b>  · write tools blocked" in _last_send_msg(ctx).text

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("stored", "ui"),
        [
            ("default", "manual"),
            ("manual", "manual"),
            ("dontAsk", "dontAsk"),
            ("bypassPermissions", "bypassPermissions"),
        ],
    )
    async def test_stored_mode_not_labelled_off(self, tmp_path, stored, ui):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(
            123, "claude", EngineOverrides(permission_mode=stored)
        )
        home = await self._home_after(tmp_path, None)
        assert f"Permission mode: <b>{ui}</b>" in home
        ctx = _make_ctx(
            args_text="pm",
            text="config:pm",
            config_path=state_path,
            default_engine="claude",
        )
        await ConfigCommand().handle(ctx)
        msg = _last_edit_msg(ctx)
        assert f"Current: <b>{ui}</b>" in msg.text
        pm_labels = [
            b["text"]
            for row in msg.extra["reply_markup"]["inline_keyboard"]
            for b in row
            if b["callback_data"].startswith("config:pm:")
        ]
        assert not any(label.startswith("✓") for label in pm_labels)

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
            default_engine="opencode",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Only available for" in msg.text
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
        assert "Only available for" in msg.text


# ---------------------------------------------------------------------------
# Codex approval policy (via plan mode page)
# ---------------------------------------------------------------------------


class TestLoopMode:
    """Cover the new ``/config:loop`` sub-page (#289)."""

    @pytest.mark.anyio
    async def test_loop_page_renders(self, tmp_path):
        """Navigating to loop sub-page shows the toggle UI."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="loop",
            text="config:loop",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Loop mode" in msg.text
        # Toggle row + cost-budget deeplink + back
        assert "config:loop:on" in _buttons_data(msg)
        assert "config:loop:off" in _buttons_data(msg)
        assert "config:loop:clr" in _buttons_data(msg)
        assert "config:cu" in _buttons_data(msg)
        assert "config:home" in _buttons_data(msg)
        # Cost+quota warning must be visible before user toggles ON
        assert "Cost" in msg.text
        assert "quota" in msg.text.lower()

    @pytest.mark.anyio
    async def test_loop_page_hidden_for_non_claude(self, tmp_path):
        """LOOP_SUPPORTED_ENGINES = {claude} — Codex must show the
        unavailable message instead of the toggle."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="loop",
            text="config:loop",
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Only available for Claude Code" in msg.text
        # No toggle buttons in this branch
        assert "config:loop:on" not in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_loop_set_on_returns_home(self, tmp_path):
        """Toggling Loop on returns to home page."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="loop:on",
            text="config:loop:on",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "settings" in msg.text.lower()  # home page header

    @pytest.mark.anyio
    async def test_loop_clear_resets_per_chat_override(self, tmp_path):
        """Clear → loop_enabled goes back to None (follows global)."""
        from untether.telegram.chat_prefs import (
            ChatPrefsStore,
            resolve_prefs_path,
        )

        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        # Set on, then clear.
        ctx = _make_ctx(
            args_text="loop:on",
            text="config:loop:on",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        ctx = _make_ctx(
            args_text="loop:clr",
            text="config:loop:clr",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        # Verify persisted state is None
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        override = await prefs.get_engine_override(123, "claude")
        assert override is None or override.loop_enabled is None

    @pytest.mark.anyio
    async def test_loop_no_config_path(self):
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="loop", text="config:loop", config_path=None)
        await cmd.handle(ctx)
        assert "Unavailable" in _last_edit_msg(ctx).text

    @pytest.mark.anyio
    async def test_loop_button_in_home_for_claude(self, tmp_path):
        """The 🔁 Loop mode button must render on the Claude home page."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="claude")
        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "config:loop" in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_loop_button_hidden_in_home_for_codex(self, tmp_path):
        """The 🔁 Loop mode button must NOT render on a Codex home page."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="codex")
        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "config:loop" not in _buttons_data(msg)


class TestCodexApprovalPolicy:
    @pytest.mark.anyio
    async def test_approval_policy_page_renders(self, tmp_path):
        """Navigating to pm page with codex engine shows approval policy."""
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
        assert "Approval policy" in msg.text
        assert "full auto" in msg.text.lower()
        data = _buttons_data(msg)
        assert "config:pm:fa" in data
        assert "config:pm:safe" in data

    @pytest.mark.anyio
    async def test_set_safe_mode(self, tmp_path):
        """Setting safe mode stores permission_mode='safe'."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path

        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="pm:safe",
            text="config:pm:safe",
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        override = await prefs.get_engine_override(123, "codex")
        assert override is not None
        assert override.permission_mode == "safe"

    @pytest.mark.anyio
    async def test_set_full_auto_clears_permission(self, tmp_path):
        """Setting full auto clears permission_mode."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        from untether.telegram.engine_overrides import EngineOverrides

        await prefs.set_engine_override(
            123, "codex", EngineOverrides(permission_mode="safe")
        )

        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="pm:fa",
            text="config:pm:fa",
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)
        override = await prefs.get_engine_override(123, "codex")
        # "full auto" maps to "auto" which clears to None
        assert override is None or override.permission_mode is None

    @pytest.mark.anyio
    async def test_home_page_shows_codex_permission(self, tmp_path):
        """Home page shows approval policy for codex engine."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(
            123, "codex", EngineOverrides(permission_mode="safe")
        )

        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="codex")
        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "safe" in msg.text.lower()

    @pytest.mark.anyio
    async def test_codex_page_copy_describes_sandbox(self, tmp_path):
        """#830: the page describes the real sandbox behaviour, not a placebo."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="pm",
            text="config:pm",
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)
        text = _last_edit_msg(ctx).text
        assert "read-only" in text
        assert "sandbox" in text
        assert "/tmp" in text
        assert "untrusted" not in text

    @pytest.mark.anyio
    async def test_codex_home_hint_safe_read_only(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="codex")
        await cmd.handle(ctx)
        assert "Codex's own sandbox" in _last_send_msg(ctx).text

        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(
            123, "codex", EngineOverrides(permission_mode="safe")
        )
        ctx = _make_ctx(config_path=state_path, default_engine="codex")
        await cmd.handle(ctx)
        text = _last_send_msg(ctx).text
        assert "read-only sandbox" in text
        assert "untrusted" not in text


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
        assert "config:pm:ya" in _buttons_data(msg)
        assert "config:pm:ro" in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_set_full_access_stores_yolo(self, tmp_path):
        """Setting full access stores 'yolo' as permission_mode."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path

        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="pm:ya",
            text="config:pm:ya",
            config_path=state_path,
            default_engine="gemini",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "settings" in msg.text.lower()  # Returns to home
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
        assert "settings" in msg.text.lower()
        assert "read-only" in msg.text.lower()

    @pytest.mark.anyio
    async def test_clear_returns_home(self, tmp_path):
        """Clearing approval mode returns to home page."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="pm:ya",
            text="config:pm:ya",
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
        assert "settings" in msg.text.lower()

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
        assert "Permission mode" not in msg.text
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
    async def test_set_auto_edit_stores_mode(self, tmp_path):
        """Setting edit files stores 'auto_edit' as permission_mode."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path

        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="pm:ae",
            text="config:pm:ae",
            config_path=state_path,
            default_engine="gemini",
        )
        await cmd.handle(ctx)
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        override = await prefs.get_engine_override(123, "gemini")
        assert override is not None
        assert override.permission_mode == "auto_edit"

    @pytest.mark.anyio
    async def test_home_shows_edit_files_label(self, tmp_path):
        """Home page shows 'edit files' when auto_edit is set."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(
            123, "gemini", EngineOverrides(permission_mode="auto_edit")
        )

        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="gemini")
        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "edit files" in msg.text.lower()

    @pytest.mark.anyio
    async def test_approval_page_shows_three_options(self, tmp_path):
        """Gemini approval page shows read-only, edit files, and full access."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="pm",
            text="config:pm",
            config_path=state_path,
            default_engine="gemini",
        )
        await cmd.handle(ctx)
        data = _buttons_data(_last_edit_msg(ctx))
        assert "config:pm:ro" in data
        assert "config:pm:ae" in data
        assert "config:pm:ya" in data

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
        assert ConfigCommand.early_answer_toast("pm:ya") == "Approval mode: full access"

    def test_toast_codex_full_auto(self):
        assert ConfigCommand.early_answer_toast("pm:fa") == "Approval policy: full auto"

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
        assert "settings" in msg.text.lower()  # Home page

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
    async def test_engine_page_marks_deprecated_engines(self, tmp_path):
        """Deprecated engines stay selectable but carry a warning glyph and an
        explanatory line — they are not hidden or blocked."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="ag",
            text="config:ag",
            config_path=state_path,
            engine_ids=("claude", "gemini", "amp"),
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        labels = _buttons_labels(msg)
        assert any("gemini \u26a0\ufe0f" in label for label in labels)
        assert any("amp \u26a0\ufe0f" in label for label in labels)
        # claude must NOT be marked
        assert not any("claude \u26a0\ufe0f" in label for label in labels)
        # still selectable
        data = _buttons_data(msg)
        assert "config:ag:gemini" in data
        assert "config:ag:amp" in data
        # explanatory line present
        assert "deprecated" in msg.text.lower()

    @pytest.mark.anyio
    async def test_engine_page_no_deprecation_notice_when_none_present(self, tmp_path):
        """A host with only supported engines sees no deprecation copy."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="ag",
            text="config:ag",
            config_path=state_path,
            engine_ids=("claude", "codex"),
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "deprecated" not in msg.text.lower()
        assert not any("\u26a0\ufe0f" in label for label in _buttons_labels(msg))

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
        assert "settings" in msg.text.lower()  # Home page

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
        assert "settings" in msg.text.lower()

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
        assert "engine:" in msg.text.lower()

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
        assert "Listen" in _last_edit_msg(ctx).text

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
        assert "settings" in msg.text.lower()  # Home page

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
        assert "settings" in _last_edit_msg(ctx).text.lower()

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
        assert "settings" in _last_edit_msg(ctx).text.lower()

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
        assert "settings" in _last_edit_msg(ctx).text.lower()

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
        assert "Permission mode" in msg.text
        assert "config:pm" in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_switch_from_claude_hides_plan_mode(self, tmp_path):
        """After switching engine away from claude, home page hides plan mode."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        # Start with claude, switch to opencode (no permission mode)
        ctx = _make_ctx(
            args_text="ag:opencode",
            text="config:ag:opencode",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Permission mode" not in msg.text
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
        # Claude Code-specific "Plan mode" label hidden; shows "Approval policy"
        assert "Permission mode" not in msg.text
        assert "Approval policy" in msg.text
        assert "config:pm" in _buttons_data(msg)

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
        assert "Permission mode" in msg.text
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
        # Project default is opencode — permission mode should be blocked
        ctx.runtime.default_context_for_chat.return_value = RunContext(
            project="opencode-test"
        )
        ctx.runtime.project_default_engine.return_value = "opencode"

        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Only available for" in msg.text

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
        assert "Engine: <b>pi</b>" in msg.text


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
    async def test_model_clear_redirects_to_engine_page(self, tmp_path):
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
        assert "Engine & model" in msg.text  # Redirects to merged engine page

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
        """Model page redirects to engine page which has a clear model button."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="md", text="config:md", config_path=state_path)
        await cmd.handle(ctx)
        assert "config:ag:md_clr" in _buttons_data(_last_edit_msg(ctx))

    @pytest.mark.anyio
    async def test_home_shows_model_label(self, tmp_path):
        """Model label always appears on home page."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path)
        await cmd.handle(ctx)
        assert "Model" in _last_send_msg(ctx).text

    @pytest.mark.anyio
    async def test_home_shows_engine_model_button(self, tmp_path):
        """Engine & model button appears on home page (merged from separate buttons)."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path)
        await cmd.handle(ctx)
        assert "config:ag" in _buttons_data(_last_send_msg(ctx))

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
        assert "config:rs:low" in _buttons_data(msg)
        # #416: Codex `minimal` is retired.
        assert "config:rs:min" not in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_reasoning_shows_codex_levels(self, tmp_path):
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
        data = _buttons_data(msg)
        assert "config:rs:low" in data
        assert "config:rs:med" in data
        assert "config:rs:hi" in data
        assert "config:rs:xhi" in data
        assert "config:rs:min" not in data
        assert "config:rs:max" not in data
        assert "minimal" not in msg.text

    @pytest.mark.anyio
    async def test_reasoning_shows_claude_levels(self, tmp_path):
        """Claude Code engine shows low/medium/high/xhigh/max (no minimal).

        `xhigh` was added alongside Opus 4.7 — see #351. Codex already
        supported `xhigh` since #272; Claude picks it up here.
        """
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
        assert "config:rs:xhi" in data
        assert "config:rs:max" in data
        assert "config:rs:min" not in data

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
        assert "settings" in msg.text.lower()

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
        """Reasoning levels persist correctly for each engine. Codex and
        Claude support different level sets; the validator (#309) rejects
        cross-engine mismatches like `rs:max` on codex."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path

        per_engine: dict[str, dict[str, str]] = {
            "codex": {
                "low": "low",
                "med": "medium",
                "hi": "high",
                "xhi": "xhigh",
            },
            "claude": {
                "low": "low",
                "med": "medium",
                "hi": "high",
                "xhi": "xhigh",
                "max": "max",
            },
        }

        for engine, expected in per_engine.items():
            state_path = tmp_path / f"prefs-{engine}.json"
            for action, level in expected.items():
                cmd = ConfigCommand()
                ctx = _make_ctx(
                    args_text=f"rs:{action}",
                    text=f"config:rs:{action}",
                    config_path=state_path,
                    default_engine=engine,
                )
                await cmd.handle(ctx)
                prefs = ChatPrefsStore(resolve_prefs_path(state_path))
                override = await prefs.get_engine_override(123, engine)
                assert override is not None, f"{engine}/rs:{action} did not persist"
                assert override.reasoning == level, (
                    f"{engine}/rs:{action} should set {level}"
                )

    @pytest.mark.anyio
    async def test_reasoning_set_max_rejected_for_codex(self, tmp_path):
        """#309 regression: codex does not support `max`; manual callback_data
        attempting `rs:max` against codex must be rejected, not silently
        persisted."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path

        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="rs:max",
            text="config:rs:max",
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        override = await prefs.get_engine_override(123, "codex")
        # Either no override at all, or reasoning is None — but never `max`.
        assert override is None or override.reasoning != "max"

    @pytest.mark.anyio
    async def test_reasoning_set_min_rejected_for_codex(self, tmp_path):
        """#416: a stale `config:rs:min` (a pre-upgrade /config message)
        persists nothing and re-renders the Reasoning page."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path

        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="rs:min",
            text="config:rs:min",
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        override = await prefs.get_engine_override(123, "codex")
        assert override is None or override.reasoning != "minimal"
        data = _buttons_data(_last_edit_msg(ctx))
        assert "config:rs:low" in data
        assert "config:rs:min" not in data

    @pytest.mark.anyio
    async def test_reasoning_page_stale_minimal_pref(self, tmp_path):
        """#416: a saved `minimal` shows as the default it runs on, with no
        button checked."""
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(
            123, "codex", EngineOverrides(reasoning="minimal")
        )
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="rs",
            text="config:rs",
            config_path=state_path,
            default_engine="codex",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "minimal not supported" in msg.text
        assert "Current: <b>default" in msg.text
        assert not any("✓" in label for label in _buttons_labels(msg))

    @pytest.mark.anyio
    async def test_home_stale_minimal_pref_shows_default(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "prefs.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(
            123, "codex", EngineOverrides(reasoning="minimal")
        )
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="codex")
        await cmd.handle(ctx)
        text = _last_send_msg(ctx).text
        assert "Reasoning: <b>default</b>" in text
        assert "minimal not supported" in text

    @pytest.mark.anyio
    @pytest.mark.parametrize("engine", ["claude", "codex"])
    async def test_reasoning_page_renders_every_allowed_level(self, tmp_path, engine):
        """Guards the `_LEVEL_BUTTON_MAP[level]` lookup against tuple edits."""
        from untether.telegram.engine_overrides import (
            REASONING_SUPPORTED_ENGINES,
            allowed_reasoning_levels,
        )

        assert engine in REASONING_SUPPORTED_ENGINES
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="rs",
            text="config:rs",
            config_path=tmp_path / "prefs.json",
            default_engine=engine,
        )
        await cmd.handle(ctx)
        level_buttons = [
            d
            for d in _buttons_data(_last_edit_msg(ctx))
            if d.startswith("config:rs:") and d != "config:rs:clr"
        ]
        assert len(level_buttons) == len(allowed_reasoning_levels(engine))

    def test_reasoning_supported_engines_all_parametrised(self):
        from untether.telegram.engine_overrides import REASONING_SUPPORTED_ENGINES

        assert set(REASONING_SUPPORTED_ENGINES) == {"claude", "codex"}

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
        assert "settings" in msg.text.lower()

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
    async def test_reasoning_default_label_shows_engine_level(self, tmp_path):
        """When no override is set, shows resolved default from engine settings."""
        import json

        state_path = tmp_path / "prefs.json"
        fake_claude_dir = tmp_path / ".claude"
        fake_claude_dir.mkdir()
        (fake_claude_dir / "settings.json").write_text(
            json.dumps({"effortLevel": "high"})
        )

        from unittest.mock import patch

        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="rs",
            text="config:rs",
            config_path=state_path,
            default_engine="claude",
        )
        with patch("pathlib.Path.home", return_value=tmp_path):
            await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "default (high)" in msg.text

    @pytest.mark.anyio
    async def test_reasoning_default_label_fallback(self, tmp_path):
        """When engine default is unreadable, shows plain 'default'."""
        state_path = tmp_path / "prefs.json"

        from unittest.mock import patch

        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="rs",
            text="config:rs",
            config_path=state_path,
            default_engine="claude",
        )
        # No settings file exists at tmp_path/.claude/settings.json
        with patch("pathlib.Path.home", return_value=tmp_path):
            await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "default" in msg.text
        assert "default (" not in msg.text

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
        """Effort label and button visible when engine is claude."""
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "Effort" in msg.text
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
    def test_toast_reasoning_minimal_removed(self):
        # #416: no toast for the retired `min` action.
        assert ConfigCommand.early_answer_toast("rs:min") is None

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
        # Toggle row: default on -> shows toggle-off button and clear
        assert "config:aq:off" in _buttons_data(msg)
        assert "config:aq:clr" in _buttons_data(msg)

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
        assert "settings" in msg.text.lower()

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
        # Toggle row: default off -> shows toggle-on button and clear
        assert "config:dp:on" in _buttons_data(msg)
        assert "config:dp:clr" in _buttons_data(msg)

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
        assert "settings" in msg.text.lower()

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
        """When diff_preview=True, toggle button shows checkmark."""
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
        """No override → page shows resolved 'off'."""
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
        assert "Current: <b>off</b>" in msg.text


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
        # Toggle rows: cost default on -> shows off toggle; sub default off -> shows on toggle
        assert "config:cu:ac_off" in buttons
        assert "config:cu:su_on" in buttons

    @pytest.mark.anyio
    async def test_cost_usage_page_uses_footer_defaults(self, tmp_path, monkeypatch):
        """Unset per-chat toggles show the ``[footer]`` values, matching the
        home page (rc15 integration finding: the page said ``Subscription
        usage: off`` while home said ``sub on`` and finals showed ⚡)."""
        from types import SimpleNamespace

        import untether.settings as settings_mod
        from untether.settings import FooterSettings

        footer = FooterSettings(show_api_cost=False, show_subscription_usage=True)
        monkeypatch.setattr(
            settings_mod,
            "load_settings_if_exists",
            lambda *a, **k: (SimpleNamespace(footer=footer, cost_budget=None), None),
        )
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="cu",
            text="config:cu",
            config_path=tmp_path / "prefs.json",
            default_engine="claude",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "<b>API cost</b>: off" in msg.text
        assert "<b>Subscription usage</b>: on" in msg.text
        buttons = _buttons_data(msg)
        assert "config:cu:ac_on" in buttons
        assert "config:cu:su_off" in buttons

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
        # Toggle row: cost default on -> shows off toggle; no sub row
        assert "config:cu:ac_off" in buttons
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

    _DOCS_BASE = "littlebearapps.com/help/untether/"

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
        assert "Help guides" in text
        assert "Report a bug" in text
        assert "Settings guide" not in text
        assert "Troubleshooting" not in text
        # #296 D3: help-centre index + the About page's bug template.
        assert 'href="https://littlebearapps.com/help/untether/"' in text
        assert "issues/new?template=bug_report.yml" in text

    @pytest.mark.anyio
    async def test_loop_has_docs_link(self, tmp_path):
        ctx = _make_ctx(
            args_text="loop",
            text="config:loop",
            config_path=tmp_path / "prefs.json",
            default_engine="claude",
        )
        await ConfigCommand().handle(ctx)
        assert (
            "littlebearapps.com/help/untether/schedule-tasks/#loop-mode"
            in _last_edit_msg(ctx).text
        )

    @pytest.mark.anyio
    @pytest.mark.parametrize("with_manager", [True, False])
    async def test_triggers_has_docs_link(self, tmp_path, with_manager):
        from untether.triggers.manager import TriggerManager

        ctx = _make_ctx(args_text="tg", text="config:tg")
        ctx.trigger_manager = TriggerManager() if with_manager else None
        await ConfigCommand().handle(ctx)
        assert (
            "littlebearapps.com/help/untether/webhooks-and-cron/"
            in _last_edit_msg(ctx).text
        )

    @pytest.mark.anyio
    async def test_resume_line_links_tutorial(self, tmp_path):
        ctx = _make_ctx(
            args_text="rl",
            text="config:rl",
            config_path=tmp_path / "prefs.json",
            default_engine="claude",
        )
        await ConfigCommand().handle(ctx)
        assert (
            "help/untether/conversation-modes/#resume-lines-in-chat-mode"
            in _last_edit_msg(ctx).text
        )

    @pytest.mark.anyio
    async def test_codex_approval_links_interactive_approval(self, tmp_path):
        ctx = _make_ctx(
            args_text="pm", text="config:pm", config_path=tmp_path / "prefs.json"
        )
        await ConfigCommand().handle(ctx)
        assert (
            "help/untether/interactive-approval/#codex-cli--approval-policy"
            in _last_edit_msg(ctx).text
        )

    @pytest.mark.anyio
    async def test_ask_mode_links_answering_questions(self, tmp_path):
        ctx = _make_ctx(
            args_text="aq",
            text="config:aq",
            config_path=tmp_path / "prefs.json",
            default_engine="claude",
        )
        await ConfigCommand().handle(ctx)
        assert (
            "help/untether/interactive-approval/#answering-questions"
            in _last_edit_msg(ctx).text
        )

    @pytest.mark.anyio
    async def test_engine_page_has_models_link(self, tmp_path):
        ctx = _make_ctx(
            args_text="ag", text="config:ag", config_path=tmp_path / "prefs.json"
        )
        await ConfigCommand().handle(ctx)
        text = _last_edit_msg(ctx).text
        assert "help/untether/switch-engines/" in text
        assert "help/untether/model-reasoning/" in text
        assert ">Engines</a> · <a" in text


# ---------------------------------------------------------------------------
# #296: ⏰ Triggers home row
# ---------------------------------------------------------------------------


def _trigger_manager(*, crons: int = 0):
    from untether.triggers.manager import TriggerManager
    from untether.triggers.settings import parse_trigger_config

    if crons == 0:
        return TriggerManager()
    return TriggerManager(
        parse_trigger_config(
            {
                "enabled": True,
                "crons": [
                    {"id": f"c{i}", "schedule": "0 9 * * *", "prompt": "x"}
                    for i in range(crons)
                ],
            }
        )
    )


class TestHomeTriggersRow:
    @staticmethod
    async def _home(tmp_path, mgr, engine: str = "claude"):
        ctx = _make_ctx(config_path=tmp_path / "prefs.json", default_engine=engine)
        ctx.trigger_manager = mgr
        await ConfigCommand().handle(ctx)
        return _last_send_msg(ctx)

    @staticmethod
    def _last_row(msg) -> list[dict[str, str]]:
        return msg.extra["reply_markup"]["inline_keyboard"][-1]

    @pytest.mark.anyio
    async def test_no_row_without_manager(self, tmp_path):
        msg = await self._home(tmp_path, None)
        assert not any(d.startswith("config:tg") for d in _buttons_data(msg))

    @pytest.mark.anyio
    async def test_nav_and_pause_when_configured(self, tmp_path):
        row = self._last_row(await self._home(tmp_path, _trigger_manager(crons=1)))
        assert [b["callback_data"] for b in row] == ["config:tg", "config:tg:pause"]
        assert row[0]["text"].startswith("⏰ Triggers")
        assert row[1]["text"].startswith("⏸")

    @pytest.mark.anyio
    async def test_nav_and_resume_when_paused(self, tmp_path):
        mgr = _trigger_manager(crons=1)
        mgr.pause()
        row = self._last_row(await self._home(tmp_path, mgr))
        assert [b["callback_data"] for b in row] == ["config:tg", "config:tg:resume"]
        assert row[1]["text"] == "▶️ Resume triggers"

    @pytest.mark.anyio
    async def test_nav_only_when_enabled_but_empty(self, tmp_path):
        row = self._last_row(await self._home(tmp_path, _trigger_manager()))
        assert [b["callback_data"] for b in row] == ["config:tg"]

    @pytest.mark.anyio
    @pytest.mark.parametrize("engine", ["codex", "opencode"])
    async def test_row_present_for_codex_and_opencode(self, tmp_path, engine):
        msg = await self._home(tmp_path, _trigger_manager(crons=1), engine=engine)
        assert [b["callback_data"] for b in self._last_row(msg)] == [
            "config:tg",
            "config:tg:pause",
        ]

    @pytest.mark.anyio
    async def test_status_line_uses_alarm_emoji(self, tmp_path):
        text = (await self._home(tmp_path, _trigger_manager(crons=1))).text
        assert "⏰ Triggers: <b>active</b>" in text
        assert "Triggers (cron/webhook)" not in text
        assert not any("📡" in line and "Trigger" in line for line in text.splitlines())

    @pytest.mark.anyio
    async def test_listen_keeps_satellite_emoji(self, tmp_path):
        msg = await self._home(tmp_path, _trigger_manager(crons=1))
        labels = {
            b["text"]: b["callback_data"]
            for row in msg.extra["reply_markup"]["inline_keyboard"]
            for b in row
        }
        assert labels["📡 Listen"] == "config:tr"
        assert not any("📡" in label and "Trigger" in label for label in labels)


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
    async def test_non_permission_engine_home_no_agent_controls(self, tmp_path):
        state_path = tmp_path / "prefs.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="opencode")
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


# ---------------------------------------------------------------------------
# Resume line toggle (#128)
# ---------------------------------------------------------------------------


class TestResumeLine:
    @pytest.mark.anyio
    async def test_resume_line_page_renders(self, tmp_path):
        state_path = tmp_path / "state.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="rl",
            text="config:rl",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Resume line" in msg.text
        data = _buttons_data(msg)
        # Toggle row: shows on or off toggle (depending on config default) and clear
        assert "config:rl:on" in data or "config:rl:off" in data
        assert "config:rl:clr" in data

    @pytest.mark.anyio
    async def test_resume_line_set_on(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path

        state_path = tmp_path / "state.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="rl:on",
            text="config:rl:on",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "settings" in msg.text.lower()

        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        override = await prefs.get_engine_override(123, "claude")
        assert override is not None
        assert override.show_resume_line is True

    @pytest.mark.anyio
    async def test_resume_line_set_off(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path

        state_path = tmp_path / "state.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="rl:off",
            text="config:rl:off",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)

        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        override = await prefs.get_engine_override(123, "claude")
        assert override is not None
        assert override.show_resume_line is False

    @pytest.mark.anyio
    async def test_resume_line_clear(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "state.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(
            123, "claude", EngineOverrides(show_resume_line=True)
        )

        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="rl:clr",
            text="config:rl:clr",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)

        override = await prefs.get_engine_override(123, "claude")
        assert override is None or override.show_resume_line is None

    @pytest.mark.anyio
    async def test_resume_line_shown_on_home(self, tmp_path):
        state_path = tmp_path / "state.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(config_path=state_path, default_engine="claude")
        await cmd.handle(ctx)
        msg = _last_send_msg(ctx)
        assert "Resume line:" in msg.text
        assert "config:rl" in _buttons_data(msg)

    @pytest.mark.anyio
    async def test_resume_line_no_config_path(self):
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="rl", text="config:rl", config_path=None)
        await cmd.handle(ctx)
        assert "Unavailable" in _last_edit_msg(ctx).text


# ---------------------------------------------------------------------------
# Resume line toasts
# ---------------------------------------------------------------------------


class TestResumeLineToasts:
    def test_toast_rl_on(self):
        assert ConfigCommand.early_answer_toast("rl:on") == "Resume line: on"

    def test_toast_rl_off(self):
        assert ConfigCommand.early_answer_toast("rl:off") == "Resume line: off"

    def test_toast_rl_clr(self):
        assert ConfigCommand.early_answer_toast("rl:clr") == "Resume line: cleared"

    def test_toast_rl_nav(self):
        assert ConfigCommand.early_answer_toast("rl") is None


# ---------------------------------------------------------------------------
# Budget settings (#129)
# ---------------------------------------------------------------------------


class TestBudgetSettings:
    @pytest.mark.anyio
    async def test_budget_section_renders_on_cost_page(self, tmp_path):
        """Navigate to cu page with claude engine, verify budget toggle rows."""
        state_path = tmp_path / "state.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="cu",
            text="config:cu",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Budget" in msg.text
        data = _buttons_data(msg)
        # Toggle rows show one toggle + clear per row
        assert "config:cu:bg_clr" in data
        assert "config:cu:bc_clr" in data
        # At least one of on/off per toggle
        assert "config:cu:bg_on" in data or "config:cu:bg_off" in data
        assert "config:cu:bc_on" in data or "config:cu:bc_off" in data

    @pytest.mark.anyio
    async def test_budget_toggle_on(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path

        state_path = tmp_path / "state.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="cu:bg_on",
            text="config:cu:bg_on",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)
        msg = _last_edit_msg(ctx)
        assert "Cost & usage" in msg.text

        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        override = await prefs.get_engine_override(123, "claude")
        assert override is not None
        assert override.budget_enabled is True

    @pytest.mark.anyio
    async def test_budget_toggle_off(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path

        state_path = tmp_path / "state.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="cu:bg_off",
            text="config:cu:bg_off",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)

        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        override = await prefs.get_engine_override(123, "claude")
        assert override is not None
        assert override.budget_enabled is False

    @pytest.mark.anyio
    async def test_budget_clear(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
        from untether.telegram.engine_overrides import EngineOverrides

        state_path = tmp_path / "state.json"
        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        await prefs.set_engine_override(
            123, "claude", EngineOverrides(budget_enabled=True)
        )

        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="cu:bg_clr",
            text="config:cu:bg_clr",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)

        override = await prefs.get_engine_override(123, "claude")
        assert override is None or override.budget_enabled is None

    @pytest.mark.anyio
    async def test_auto_cancel_toggle_on(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path

        state_path = tmp_path / "state.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="cu:bc_on",
            text="config:cu:bc_on",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)

        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        override = await prefs.get_engine_override(123, "claude")
        assert override is not None
        assert override.budget_auto_cancel is True

    @pytest.mark.anyio
    async def test_auto_cancel_toggle_off(self, tmp_path):
        from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path

        state_path = tmp_path / "state.json"
        cmd = ConfigCommand()
        ctx = _make_ctx(
            args_text="cu:bc_off",
            text="config:cu:bc_off",
            config_path=state_path,
            default_engine="claude",
        )
        await cmd.handle(ctx)

        prefs = ChatPrefsStore(resolve_prefs_path(state_path))
        override = await prefs.get_engine_override(123, "claude")
        assert override is not None
        assert override.budget_auto_cancel is False


# ---------------------------------------------------------------------------
# Budget toasts
# ---------------------------------------------------------------------------


class TestBudgetToasts:
    def test_toast_bg_on(self):
        assert ConfigCommand.early_answer_toast("cu:bg_on") == "Budget: on"

    def test_toast_bg_off(self):
        assert ConfigCommand.early_answer_toast("cu:bg_off") == "Budget: off"

    def test_toast_bg_clr(self):
        assert ConfigCommand.early_answer_toast("cu:bg_clr") == "Budget: cleared"

    def test_toast_bc_on(self):
        assert ConfigCommand.early_answer_toast("cu:bc_on") == "Auto-cancel: on"

    def test_toast_bc_off(self):
        assert ConfigCommand.early_answer_toast("cu:bc_off") == "Auto-cancel: off"

    def test_toast_bc_clr(self):
        assert ConfigCommand.early_answer_toast("cu:bc_clr") == "Auto-cancel: cleared"


# ── #294: /config triggers (tg) page ────────────────────────────────────


class TestTriggersPage:
    @pytest.mark.anyio
    async def test_title_uses_alarm_clock(self, tmp_path):
        ctx = _make_ctx(args_text="tg", text="config:tg")
        ctx.trigger_manager = _trigger_manager(crons=1)
        await ConfigCommand().handle(ctx)
        msg = _last_edit_msg(ctx)
        assert msg.text.startswith("<b>⏰ Triggers")
        assert not any(
            "📡" in label and "Trigger" in label for label in _buttons_labels(msg)
        )

    @pytest.mark.anyio
    async def test_no_trigger_manager_shows_unavailable(self, tmp_path):
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="tg", text="config:tg")
        ctx.trigger_manager = None
        await cmd.handle(ctx)
        text = _last_edit_msg(ctx).text
        assert "Triggers" in text
        assert "Unavailable" in text

    @pytest.mark.anyio
    async def test_no_triggers_configured_shows_empty_message(self, tmp_path):
        from untether.triggers.manager import TriggerManager

        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="tg", text="config:tg")
        ctx.trigger_manager = TriggerManager()
        await cmd.handle(ctx)
        text = _last_edit_msg(ctx).text
        assert "Triggers" in text
        assert "No crons or webhooks configured" in text

    @pytest.mark.anyio
    async def test_pause_action_pauses_manager(self, tmp_path):
        from untether.triggers.manager import TriggerManager
        from untether.triggers.settings import parse_trigger_config

        mgr = TriggerManager(
            parse_trigger_config(
                {
                    "enabled": True,
                    "crons": [
                        {"id": "a", "schedule": "0 9 * * *", "prompt": "x"},
                    ],
                }
            )
        )
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="tg:pause", text="config:tg:pause")
        ctx.trigger_manager = mgr
        await cmd.handle(ctx)
        assert mgr.is_paused is True
        text = _last_edit_msg(ctx).text
        # Status reflects the new paused state.
        assert "paused" in text

    @pytest.mark.anyio
    async def test_resume_action_resumes_manager(self, tmp_path):
        from untether.triggers.manager import TriggerManager
        from untether.triggers.settings import parse_trigger_config

        mgr = TriggerManager(
            parse_trigger_config(
                {
                    "enabled": True,
                    "crons": [
                        {"id": "a", "schedule": "0 9 * * *", "prompt": "x"},
                    ],
                }
            )
        )
        mgr.pause()
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="tg:resume", text="config:tg:resume")
        ctx.trigger_manager = mgr
        await cmd.handle(ctx)
        assert mgr.is_paused is False

    def test_toast_pause_resume(self):
        assert ConfigCommand.early_answer_toast("tg:pause") == "⏸ Triggers paused"
        assert ConfigCommand.early_answer_toast("tg:resume") == "▶️ Triggers resumed"


# ── #271 Tier 2 + Tier 3: per-chat trigger list + last-fired ──────────────


class TestTriggersPagePerChat:
    @pytest.fixture(autouse=True)
    def _reset_history(self):
        from untether.triggers import history

        history.reset_history()
        yield
        history.reset_history()

    @pytest.mark.anyio
    async def test_lists_crons_for_current_chat(self, tmp_path):
        from untether.triggers.manager import TriggerManager
        from untether.triggers.settings import parse_trigger_config

        cfg = parse_trigger_config(
            {
                "enabled": True,
                "crons": [
                    {
                        "id": "morning",
                        "schedule": "0 9 * * *",
                        "prompt": "good morning",
                        "chat_id": 123,
                        "project": "lba-1",
                        "engine": "claude",
                    },
                ],
            }
        )
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="tg", text="config:tg", chat_id=123)
        ctx.trigger_manager = TriggerManager(cfg)
        await cmd.handle(ctx)
        text = _last_edit_msg(ctx).text
        assert "<b>Crons</b>" in text
        assert "morning" in text
        # describe_cron output for "0 9 * * *"
        assert "9:00" in text
        assert "lba-1" in text
        assert "claude" in text
        assert "last <i>never</i>" in text

    @pytest.mark.anyio
    async def test_lists_webhooks_for_current_chat(self, tmp_path):
        from untether.triggers.manager import TriggerManager
        from untether.triggers.settings import parse_trigger_config

        cfg = parse_trigger_config(
            {
                "enabled": True,
                "webhooks": [
                    {
                        "id": "gh-push",
                        "path": "/webhooks/github",
                        "auth": "hmac-sha256",
                        "secret": "s" * 32,
                        "prompt_template": "push from ${repository}",
                        "chat_id": 123,
                        "project": "untether",
                    },
                ],
            }
        )
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="tg", text="config:tg", chat_id=123)
        ctx.trigger_manager = TriggerManager(cfg)
        await cmd.handle(ctx)
        text = _last_edit_msg(ctx).text
        assert "<b>Webhooks</b>" in text
        assert "gh-push" in text
        assert "/webhooks/github" in text
        assert "auth=<i>hmac-sha256</i>" in text
        assert "untether" in text

    @pytest.mark.anyio
    async def test_filters_to_current_chat(self, tmp_path):
        from untether.triggers.manager import TriggerManager
        from untether.triggers.settings import parse_trigger_config

        cfg = parse_trigger_config(
            {
                "enabled": True,
                "crons": [
                    {
                        "id": "mine",
                        "schedule": "0 9 * * *",
                        "prompt": "x",
                        "chat_id": 123,
                    },
                    {
                        "id": "other-chat",
                        "schedule": "0 9 * * *",
                        "prompt": "x",
                        "chat_id": 999,
                    },
                ],
            }
        )
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="tg", text="config:tg", chat_id=123)
        ctx.trigger_manager = TriggerManager(cfg)
        await cmd.handle(ctx)
        text = _last_edit_msg(ctx).text
        assert "mine" in text
        assert "other-chat" not in text

    @pytest.mark.anyio
    async def test_default_chat_id_fallback(self, tmp_path):
        from untether.triggers.manager import TriggerManager
        from untether.triggers.settings import parse_trigger_config

        # Cron has no chat_id; should resolve via default_chat_id.
        cfg = parse_trigger_config(
            {
                "enabled": True,
                "crons": [
                    {
                        "id": "global",
                        "schedule": "0 9 * * *",
                        "prompt": "x",
                    },
                ],
            }
        )
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="tg", text="config:tg", chat_id=123)
        ctx.trigger_manager = TriggerManager(cfg)
        ctx.default_chat_id = 123
        await cmd.handle(ctx)
        text = _last_edit_msg(ctx).text
        assert "global" in text

    @pytest.mark.anyio
    async def test_omits_subsection_when_no_chat_triggers(self, tmp_path):
        from untether.triggers.manager import TriggerManager
        from untether.triggers.settings import parse_trigger_config

        # All triggers belong to a different chat.
        cfg = parse_trigger_config(
            {
                "enabled": True,
                "crons": [
                    {
                        "id": "elsewhere",
                        "schedule": "0 9 * * *",
                        "prompt": "x",
                        "chat_id": 999,
                    },
                ],
            }
        )
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="tg", text="config:tg", chat_id=123)
        ctx.trigger_manager = TriggerManager(cfg)
        await cmd.handle(ctx)
        text = _last_edit_msg(ctx).text
        # Status line still shows total count, but the per-chat lists are absent.
        assert "1" in text  # cron count in status
        assert "<b>Crons</b>" not in text
        assert "<b>Webhooks</b>" not in text

    @pytest.mark.anyio
    async def test_renders_last_fired_when_history_present(self, tmp_path):
        from untether.triggers import history
        from untether.triggers.manager import TriggerManager
        from untether.triggers.settings import parse_trigger_config

        history.init_history(tmp_path / "untether.toml")
        history.record_fired("morning")

        cfg = parse_trigger_config(
            {
                "enabled": True,
                "crons": [
                    {
                        "id": "morning",
                        "schedule": "0 9 * * *",
                        "prompt": "x",
                        "chat_id": 123,
                    },
                ],
            }
        )
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="tg", text="config:tg", chat_id=123)
        ctx.trigger_manager = TriggerManager(cfg)
        await cmd.handle(ctx)
        text = _last_edit_msg(ctx).text
        # Should show "just now" since we just recorded.
        assert "last <i>just now</i>" in text

    @pytest.mark.anyio
    async def test_cron_list_caps_at_ten_with_overflow_marker(self, tmp_path):
        from untether.triggers.manager import TriggerManager
        from untether.triggers.settings import parse_trigger_config

        cfg = parse_trigger_config(
            {
                "enabled": True,
                "crons": [
                    {
                        "id": f"c{i:02d}",
                        "schedule": "0 9 * * *",
                        "prompt": "x",
                        "chat_id": 123,
                    }
                    for i in range(13)
                ],
            }
        )
        cmd = ConfigCommand()
        ctx = _make_ctx(args_text="tg", text="config:tg", chat_id=123)
        ctx.trigger_manager = TriggerManager(cfg)
        await cmd.handle(ctx)
        text = _last_edit_msg(ctx).text
        # First 10 listed; remaining 3 collapsed into the overflow marker.
        assert "c00" in text
        assert "c09" in text
        assert "c10" not in text
        assert "…and 3 more" in text
