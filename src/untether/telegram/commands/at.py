"""`/at` command — schedule a one-shot delayed run (#288).

Syntax: ``/at <duration> <prompt>``

Duration supports ``Ns`` (seconds), ``Nm`` (minutes), ``Nh`` (hours).
Range is 60s to 24h. Pending delays are lost on restart and can be
cancelled with ``/cancel``.
"""

from __future__ import annotations

import re

from ...commands import CommandBackend, CommandContext, CommandResult
from ..at_scheduler import (
    MAX_DELAY_SECONDS,
    MIN_DELAY_SECONDS,
    AtSchedulerError,
    schedule_delayed_run,
)

# ^<number><unit><whitespace><prompt rest>
_AT_PATTERN = re.compile(r"^\s*(\d+)\s*([smhSMH])\s+(.+?)\s*$", re.DOTALL)

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}

_USAGE = (
    "Usage: /at <duration> <prompt>\n"
    "\u2022 Duration: Ns | Nm | Nh "
    f"(between {MIN_DELAY_SECONDS}s and {MAX_DELAY_SECONDS // 3600}h)\n"
    "\u2022 Example: /at 30m Check the build"
)


def _format_delay(delay_s: int) -> str:
    """Human-friendly duration: '30m', '2h', '90s', '1h 30m'."""
    if delay_s < 60:
        return f"{delay_s}s"
    if delay_s < 3600:
        minutes, seconds = divmod(delay_s, 60)
        return f"{minutes}m" if seconds == 0 else f"{minutes}m {seconds}s"
    hours, remainder = divmod(delay_s, 3600)
    minutes, _ = divmod(remainder, 60)
    return f"{hours}h" if minutes == 0 else f"{hours}h {minutes}m"


def _parse_args(args_text: str) -> tuple[int, str] | None:
    """Parse ``<duration> <prompt>`` into (delay_s, prompt) or None on error."""
    match = _AT_PATTERN.match(args_text)
    if match is None:
        return None
    amount_str, unit, prompt = match.groups()
    try:
        amount = int(amount_str)
    except ValueError:
        return None
    seconds = amount * _UNIT_SECONDS[unit.lower()]
    if seconds < MIN_DELAY_SECONDS or seconds > MAX_DELAY_SECONDS:
        return None
    if not prompt.strip():
        return None
    return seconds, prompt.strip()


class AtCommand:
    """Schedule a one-shot delayed agent run."""

    id = "at"
    description = "Schedule a delayed run: /at 30m <prompt>"

    async def handle(self, ctx: CommandContext) -> CommandResult:
        if not ctx.args_text.strip():
            return CommandResult(text=_USAGE, notify=True)

        parsed = _parse_args(ctx.args_text)
        if parsed is None:
            return CommandResult(
                text=f"\u274c couldn't parse /at.\n{_USAGE}", notify=True
            )

        delay_s, prompt = parsed
        chat_id = ctx.message.channel_id
        thread_id = ctx.message.thread_id
        if not isinstance(chat_id, int):
            return CommandResult(
                text="\u274c /at is only supported in integer-id chats",
                notify=True,
            )
        thread_int = int(thread_id) if isinstance(thread_id, int) else None

        try:
            schedule_delayed_run(chat_id, thread_int, delay_s, prompt)
        except AtSchedulerError as exc:
            return CommandResult(text=f"\u274c {exc}", notify=True)

        return CommandResult(
            text=(
                f"\u23f3 Scheduled: will run in {_format_delay(delay_s)}\n"
                f"Cancel with /cancel."
            ),
            notify=True,
        )


BACKEND: CommandBackend = AtCommand()
