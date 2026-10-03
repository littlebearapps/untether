"""Command backend for setting Claude Code's permission mode via /planmode."""

from __future__ import annotations

from ...commands import CommandBackend, CommandContext, CommandResult
from ...logging import get_logger
from ...runners.run_options import CLAUDE_PLAN_AUTO_MODE, claude_cli_permission_mode
from ._permission_mode_text import (
    APPLY_TIMING_TEXT,
    NO_OVERRIDE_LABEL,
    NO_OVERRIDE_TEXT,
    cli_name_suffix,
    mode_display,
)

logger = get_logger(__name__)

PLANMODE_USAGE = (
    "usage: `/planmode`, `/planmode on`, `/planmode plan-auto`,"
    " `/planmode auto`, `/planmode off`, `/planmode show`, or"
    " `/planmode clear`"
)

# #741 `plan-auto` is Untether's own sugar (CLI plan mode + auto-approved
# ExitPlanMode).  `auto` is now the CLI's native classifier-gated mode, which
# this command shadowed until 0.35.5rc8.
PERMISSION_MODES = {
    "on": "plan",
    "plan-auto": CLAUDE_PLAN_AUTO_MODE,
    "auto": "auto",
    "off": "acceptEdits",
}

# Modes that mean "planning is active" for the bare `/planmode` toggle.
_PLANNING_MODES = ("plan", CLAUDE_PLAN_AUTO_MODE)

# Engines that support the /planmode command (Claude-style permission modes).
# Codex and Gemini have approval policies but use different semantics —
# they should use /config → Approval policy instead.
_PLANMODE_ENGINES = frozenset({"claude"})


def _summary(mode: str) -> str:
    _, mode_text = mode_display(mode)
    return mode_text.summary if mode_text is not None else mode


class PlanModeCommand:
    """Command backend for toggling Claude Code permission mode."""

    id = "planmode"
    description = "Set Claude Code permission mode: on/plan-auto/auto/off"

    async def handle(self, ctx: CommandContext) -> CommandResult | None:
        from ..chat_prefs import ChatPrefsStore, resolve_prefs_path
        from ..engine_overrides import with_override
        from ._resolve_engine import resolve_effective_engine

        config_path = ctx.config_path
        if config_path is None:
            return CommandResult(
                text="permission mode overrides unavailable (no config path).",
                notify=True,
            )

        current_engine = await resolve_effective_engine(ctx)
        if current_engine not in _PLANMODE_ENGINES:
            hint = ""
            if current_engine in {"codex", "gemini"}:
                hint = " Use /config → Approval policy instead."
            return CommandResult(
                text=(
                    f"Plan mode is only available for Claude Code."
                    f" Current engine: <b>{current_engine}</b>.{hint}"
                ),
                notify=True,
                parse_mode="HTML",
            )

        chat_prefs = ChatPrefsStore(resolve_prefs_path(config_path))
        chat_id = ctx.message.channel_id
        engine = current_engine
        args = ctx.args_text.strip().lower()

        if args == "show":
            current = await chat_prefs.get_engine_override(chat_id, engine)
            mode = current.permission_mode if current else None
            if mode is None:
                text = (
                    f"permission mode: <b>{NO_OVERRIDE_LABEL}</b>: no override"
                    f" for this chat; it {NO_OVERRIDE_TEXT}."
                )
            else:
                # #747: only acceptEdits is "off"; a hand-stored mode that
                # /planmode can't set shows its own name.
                ui_name, mode_text = mode_display(mode)
                text = f"permission mode: <b>{ui_name}</b>{cli_name_suffix(mode)}"
                if mode_text is not None:
                    text += f": {mode_text.summary}."
            return CommandResult(text=text, notify=True, parse_mode="HTML")

        if args == "":
            # Toggle: if currently plan/auto mode, turn off; otherwise turn on
            current = await chat_prefs.get_engine_override(chat_id, engine)
            current_mode = current.permission_mode if current else None
            args = "off" if current_mode in _PLANNING_MODES else "on"

        if args in PERMISSION_MODES:
            mode = PERMISSION_MODES[args]
            current = await chat_prefs.get_engine_override(chat_id, engine)
            updated = with_override(current, permission_mode=mode)
            await chat_prefs.set_engine_override(chat_id, engine, updated)
            cli_mode = claude_cli_permission_mode(mode)
            logger.info(
                "planmode.set",
                chat_id=chat_id,
                mode=args,
                cli_mode=cli_mode,
                command="planmode",
            )
            return CommandResult(
                text=(
                    f"permission mode <b>{args}</b> for this chat:"
                    f" {_summary(mode)}.\n"
                    "applies from your next message"
                    f" (<code>--permission-mode {cli_mode}</code>)."
                    f" {APPLY_TIMING_TEXT}"
                ),
                notify=True,
                parse_mode="HTML",
            )

        if args == "clear":
            current = await chat_prefs.get_engine_override(chat_id, engine)
            updated = with_override(current, permission_mode=None)
            await chat_prefs.set_engine_override(chat_id, engine, updated)
            logger.info("planmode.cleared", chat_id=chat_id, command="planmode")
            return CommandResult(
                text=(
                    "permission mode <b>override cleared</b>: no override for"
                    f" this chat; it {NO_OVERRIDE_TEXT}."
                ),
                notify=True,
                parse_mode="HTML",
            )

        return CommandResult(text=PLANMODE_USAGE, notify=True)


BACKEND: CommandBackend = PlanModeCommand()
