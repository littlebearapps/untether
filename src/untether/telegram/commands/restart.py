"""Command backend for graceful restart."""

from __future__ import annotations

from ...commands import CommandBackend, CommandContext, CommandResult
from ...shutdown import is_shutting_down, request_shutdown


class RestartCommand:
    """Gracefully drain active runs and restart Untether."""

    id = "restart"
    description = "Gracefully restart Untether"

    async def handle(self, ctx: CommandContext) -> CommandResult | None:
        if is_shutting_down():
            return CommandResult(
                text="Already restarting — waiting for active runs to finish.",
                notify=True,
            )

        # #559: record the originating chat so the drain loop can confirm the
        # precise self-restart case when this chat is the sole active run.
        request_shutdown(origin_chat_id=ctx.message.channel_id)
        return CommandResult(
            text="Draining active runs… will restart shortly.",
            notify=True,
        )


BACKEND: CommandBackend = RestartCommand()
