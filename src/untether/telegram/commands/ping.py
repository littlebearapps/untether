"""Command backend that replies with bot uptime."""

from __future__ import annotations

import time

from ...commands import CommandBackend, CommandContext, CommandResult

_STARTED_AT: float = 0.0


def reset_uptime() -> None:
    """Reset the uptime counter (called on service start)."""
    global _STARTED_AT
    _STARTED_AT = time.monotonic()


# Set initial value at import time; reset_uptime() is called again from
# the Telegram loop on each service start to handle /restart correctly.
reset_uptime()


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


def _trigger_indicator(ctx: CommandContext) -> str | None:
    """Render a per-chat trigger summary line for ``/ping`` (#271).

    Returns ``None`` if the chat has no triggers targeting it. Formats:
    - Single cron: ``\u23f0 triggers: 1 cron (daily-review, 9:00 AM daily (Melbourne))``
    - Multiple: ``\u23f0 triggers: 2 crons, 1 webhook``
    """
    mgr = ctx.trigger_manager
    if mgr is None:
        return None
    chat_id = ctx.message.channel_id
    if not isinstance(chat_id, int):
        return None
    crons = mgr.crons_for_chat(chat_id, default_chat_id=ctx.default_chat_id)
    webhooks = mgr.webhooks_for_chat(chat_id, default_chat_id=ctx.default_chat_id)
    if not crons and not webhooks:
        return None

    parts: list[str] = []
    if crons:
        from ...triggers.describe import describe_cron

        if len(crons) == 1:
            c = crons[0]
            desc = describe_cron(c.schedule, c.timezone or mgr.default_timezone)
            parts.append(f"1 cron ({c.id}, {desc})")
        else:
            parts.append(f"{len(crons)} crons")
    if webhooks:
        suffix = "s" if len(webhooks) != 1 else ""
        parts.append(f"{len(webhooks)} webhook{suffix}")
    return "\u23f0 triggers: " + ", ".join(parts)


class PingCommand:
    """Command backend for bot health check and uptime."""

    id = "ping"
    description = "Check bot status and uptime"

    async def handle(self, ctx: CommandContext) -> CommandResult:
        uptime = _format_uptime(time.monotonic() - _STARTED_AT)
        lines = [f"\U0001f3d3 pong \u2014 up {uptime}"]
        indicator = _trigger_indicator(ctx)
        if indicator is not None:
            lines.append(indicator)
        return CommandResult(text="\n".join(lines), notify=True)


BACKEND: CommandBackend = PingCommand()
