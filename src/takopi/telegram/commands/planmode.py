"""Command backend for toggling Claude Code plan mode via /planmode."""

from __future__ import annotations

from ...commands import CommandBackend, CommandContext, CommandResult
from ...logging import get_logger

logger = get_logger(__name__)

PLANMODE_USAGE = (
    "usage: `/planmode`, `/planmode on`, `/planmode off`, or `/planmode clear`"
)

PERMISSION_MODES = {
    "on": "plan",
    "off": "acceptEdits",
}


class PlanModeCommand:
    """Command backend for toggling Claude Code permission mode."""

    id = "planmode"
    description = "Toggle Claude Code plan mode on/off"

    async def handle(self, ctx: CommandContext) -> CommandResult | None:
        from ..chat_prefs import ChatPrefsStore, resolve_prefs_path
        from ..engine_overrides import EngineOverrides

        config_path = ctx.config_path
        if config_path is None:
            return CommandResult(
                text="plan mode overrides unavailable (no config path).",
                notify=True,
            )

        chat_prefs = ChatPrefsStore(resolve_prefs_path(config_path))
        chat_id = ctx.message.channel_id
        engine = "claude"
        args = ctx.args_text.strip().lower()

        if args in {"", "show"}:
            current = await chat_prefs.get_engine_override(chat_id, engine)
            mode = current.permission_mode if current else None
            if mode == "plan":
                label = "on (plan mode)"
            elif mode is not None:
                label = f"off ({mode})"
            else:
                label = "default (uses engine config)"
            return CommandResult(text=f"plan mode: {label}", notify=True)

        if args in PERMISSION_MODES:
            mode = PERMISSION_MODES[args]
            current = await chat_prefs.get_engine_override(chat_id, engine)
            updated = EngineOverrides(
                model=current.model if current else None,
                reasoning=current.reasoning if current else None,
                permission_mode=mode,
            )
            await chat_prefs.set_engine_override(chat_id, engine, updated)
            label = "on" if args == "on" else "off"
            return CommandResult(
                text=f"plan mode {label} for this chat.\nnew sessions will use `--permission-mode {mode}`.",
                notify=True,
            )

        if args == "clear":
            current = await chat_prefs.get_engine_override(chat_id, engine)
            updated = EngineOverrides(
                model=current.model if current else None,
                reasoning=current.reasoning if current else None,
                permission_mode=None,
            )
            await chat_prefs.set_engine_override(chat_id, engine, updated)
            return CommandResult(
                text="plan mode override cleared (using engine config default).",
                notify=True,
            )

        return CommandResult(text=PLANMODE_USAGE, notify=True)


BACKEND: CommandBackend = PlanModeCommand()
