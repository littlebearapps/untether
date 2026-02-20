"""Command backend for Claude Code subscription usage reporting."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ...commands import CommandBackend, CommandContext, CommandResult
from ...logging import get_logger

logger = get_logger(__name__)

_DEFAULT_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_TIMEOUT = 10.0


def _progress_bar(pct: float, width: int = 10) -> str:
    """Render a text progress bar like ████░░░░░░."""
    filled = round(pct / 100 * width)
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def _time_until(iso_ts: str) -> str:
    """Format a reset timestamp as 'Xh Ym' from now."""
    try:
        reset = datetime.fromisoformat(iso_ts)
        now = datetime.now(timezone.utc)
        delta = reset - now
        total_seconds = max(0, int(delta.total_seconds()))
        hours, remainder = divmod(total_seconds, 3600)
        minutes = remainder // 60
        if hours > 24:
            days = hours // 24
            hours = hours % 24
            return f"{days}d {hours}h"
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except (ValueError, TypeError):
        return "unknown"


def _read_access_token(
    credentials_path: Path = _DEFAULT_CREDENTIALS_PATH,
) -> tuple[str, bool]:
    """Read the OAuth access token from Claude credentials.

    Returns (token, is_expired) tuple.
    """
    data = json.loads(credentials_path.read_text())
    oauth = data["claudeAiOauth"]
    token = oauth["accessToken"]
    expires_at_ms = oauth.get("expiresAt", 0)
    is_expired = (time.time() * 1000) >= (expires_at_ms - 300_000)  # 5min buffer
    return token, is_expired


async def fetch_claude_usage(
    credentials_path: Path = _DEFAULT_CREDENTIALS_PATH,
) -> dict:
    """Fetch usage data from the Anthropic OAuth usage endpoint."""
    token, is_expired = _read_access_token(credentials_path)
    if is_expired:
        logger.warning("usage.token_expired", path=str(credentials_path))
        # Claude Code refreshes its own token — if it's expired, it'll be
        # refreshed next time Claude Code runs. For now, try anyway.

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            _USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "anthropic-beta": "oauth-2025-04-20",
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()


def format_usage(data: dict) -> str:
    """Format usage data into a concise Telegram message."""
    lines: list[str] = ["📊 Claude Code Usage\n"]

    five_hour = data.get("five_hour")
    if five_hour:
        pct = five_hour["utilization"]
        bar = _progress_bar(pct)
        reset = _time_until(five_hour["resets_at"])
        lines.append(f"5h window: {bar} {pct:.0f}% (resets in {reset})")

    seven_day = data.get("seven_day")
    if seven_day:
        pct = seven_day["utilization"]
        bar = _progress_bar(pct)
        reset = _time_until(seven_day["resets_at"])
        lines.append(f"Weekly:    {bar} {pct:.0f}% (resets in {reset})")

    sonnet = data.get("seven_day_sonnet")
    if sonnet:
        pct = sonnet["utilization"]
        bar = _progress_bar(pct)
        lines.append(f"Sonnet:    {bar} {pct:.0f}%")

    opus = data.get("seven_day_opus")
    if opus:
        pct = opus["utilization"]
        bar = _progress_bar(pct)
        lines.append(f"Opus:      {bar} {pct:.0f}%")

    extra = data.get("extra_usage")
    if extra and extra.get("is_enabled"):
        used = extra.get("used_credits")
        if used is not None:
            lines.append(f"Extra:     ${used:,.2f} used")

    return "\n".join(lines)


class UsageCommand:
    """Command backend for Claude Code usage reporting."""

    id = "usage"
    description = "Show Claude Code subscription usage"

    async def handle(self, ctx: CommandContext) -> CommandResult | None:
        try:
            data = await fetch_claude_usage()
        except FileNotFoundError:
            return CommandResult(
                text="No Claude credentials found at ~/.claude/.credentials.json",
                notify=True,
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 401:
                return CommandResult(
                    text="Claude OAuth token expired or invalid. "
                    "Run a Claude Code session to refresh it.",
                    notify=True,
                )
            if status == 403:
                return CommandResult(
                    text="Claude OAuth token lacks user:profile scope.",
                    notify=True,
                )
            logger.exception("usage.api_error", status=status)
            return CommandResult(
                text=f"Usage API error: HTTP {status}",
                notify=True,
            )
        except Exception as exc:
            logger.exception("usage.fetch_failed", error=str(exc))
            return CommandResult(
                text=f"Failed to fetch usage: {exc}",
                notify=True,
            )

        text = format_usage(data)
        return CommandResult(text=text, notify=True)


BACKEND: CommandBackend = UsageCommand()
