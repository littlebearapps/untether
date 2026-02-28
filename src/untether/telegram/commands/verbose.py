"""Command backend for toggling verbose progress mode via /verbose."""

from __future__ import annotations

from typing import Literal

from ...commands import CommandBackend, CommandContext, CommandResult
from ...logging import get_logger

logger = get_logger(__name__)

Verbosity = Literal["compact", "verbose"]

# Module-level override: when set, overrides config-level verbosity.
# Keyed by chat_id for future multi-chat support; None = use config default.
_VERBOSE_OVERRIDES: dict[int, Verbosity] = {}


def get_verbosity_override(chat_id: int) -> Verbosity | None:
    """Return the per-chat verbosity override, or None for config default."""
    return _VERBOSE_OVERRIDES.get(chat_id)


class VerboseCommand:
    """Command backend for toggling verbose progress mode."""

    id = "verbose"
    description = "Toggle verbose progress mode on/off"

    async def handle(self, ctx: CommandContext) -> CommandResult | None:
        chat_id = ctx.message.channel_id
        args = ctx.args_text.strip().lower()

        if args in ("on", "verbose"):
            _VERBOSE_OVERRIDES[chat_id] = "verbose"
            logger.info("verbose.set", chat_id=chat_id, verbosity="verbose")
            return CommandResult(
                text="verbose mode <b>on</b> — progress messages will show tool details.",
                notify=True,
                parse_mode="HTML",
            )

        if args in ("off", "compact"):
            _VERBOSE_OVERRIDES[chat_id] = "compact"
            logger.info("verbose.set", chat_id=chat_id, verbosity="compact")
            return CommandResult(
                text="verbose mode <b>off</b> — compact progress (default).",
                notify=True,
                parse_mode="HTML",
            )

        if args in ("clear", "reset"):
            _VERBOSE_OVERRIDES.pop(chat_id, None)
            logger.info("verbose.cleared", chat_id=chat_id)
            return CommandResult(
                text="verbose override <b>cleared</b> (using config default).",
                notify=True,
                parse_mode="HTML",
            )

        # No args: toggle
        current = _VERBOSE_OVERRIDES.get(chat_id)
        if current == "verbose":
            _VERBOSE_OVERRIDES[chat_id] = "compact"
            logger.info("verbose.toggled", chat_id=chat_id, verbosity="compact")
            return CommandResult(
                text="verbose mode <b>off</b>.",
                notify=True,
                parse_mode="HTML",
            )
        else:
            _VERBOSE_OVERRIDES[chat_id] = "verbose"
            logger.info("verbose.toggled", chat_id=chat_id, verbosity="verbose")
            return CommandResult(
                text="verbose mode <b>on</b>.",
                notify=True,
                parse_mode="HTML",
            )


BACKEND: CommandBackend = VerboseCommand()
