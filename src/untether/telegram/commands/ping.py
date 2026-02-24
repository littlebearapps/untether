"""Command backend that replies with bot uptime."""

from __future__ import annotations

import time

from ...commands import CommandBackend, CommandContext, CommandResult

_STARTED_AT = time.monotonic()


def _format_uptime(seconds: float) -> str:
    """Format elapsed seconds as '2d 5h 13m 7s'."""
    parts: list[str] = []
    days, seconds = divmod(int(seconds), 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


class PingCommand:
    """Command backend for bot health check and uptime."""

    id = "ping"
    description = "Check bot status and uptime"

    async def handle(self, ctx: CommandContext) -> CommandResult:
        uptime = _format_uptime(time.monotonic() - _STARTED_AT)
        return CommandResult(text=f"\U0001f3d3 pong \u2014 up {uptime}", notify=True)


BACKEND: CommandBackend = PingCommand()
