"""Command backend for toggling verbose progress mode via /verbose."""

from __future__ import annotations

from typing import Literal

from ...commands import CommandBackend, CommandContext, CommandResult
from ...logging import get_logger
from ...transport import ChannelId
from .overrides import SETTINGS_ADMIN_GATE, AdminGate

logger = get_logger(__name__)

Verbosity = Literal["compact", "verbose"]

# Module-level override: when set, overrides config-level verbosity.
# Keyed by chat_id for future multi-chat support; None = use config default.
_VERBOSE_OVERRIDES: dict[ChannelId, Verbosity] = {}


def get_verbosity_override(chat_id: ChannelId) -> Verbosity | None:
    """Return the per-chat verbosity override, or None for config default."""
    return _VERBOSE_OVERRIDES.get(chat_id)


class VerboseCommand:
    """Command backend for toggling verbose progress mode."""

    id = "verbose"
    description = "Toggle verbose progress mode on/off"

    @staticmethod
    def admin_gate(args_text: str) -> AdminGate | None:
        """#1042: changing the chat's verbosity needs a group admin.

        `/verbose` writes the setting `/config vb:*` writes, so it takes the
        same gate (run by the dispatch before ``handle``). Every form changes
        it — an unrecognised argument toggles, like the bare command — so
        there is no read-only form to leave open.
        """
        return SETTINGS_ADMIN_GATE

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
