"""Command backend for Claude Code subscription usage reporting.

Only available when the current chat's engine is Claude — other engines
do not use Anthropic OAuth credentials.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ...commands import CommandBackend, CommandContext, CommandResult
from ...logging import get_logger

logger = get_logger(__name__)

_DEFAULT_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_TIMEOUT = 10.0

# User-friendly descriptions for HTTP errors from the usage API.
_HTTP_STATUS_HINTS: dict[int, str] = {
    429: "Rate limited by Anthropic \N{EM DASH} too many requests. Try again in a minute.",
    500: "Anthropic API internal error. This is temporary \N{EM DASH} try again shortly.",
    502: "Anthropic API returned a bad gateway error. Try again in a few minutes.",
    503: "Anthropic API is temporarily unavailable. Try again in a few minutes.",
    504: "Anthropic API gateway timed out. Try again shortly.",
}


def _progress_bar(pct: float, width: int = 10) -> str:
    """Render a text progress bar like ████░░░░░░."""
    filled = round(pct / 100 * width)
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def _time_until(iso_ts: str) -> str:
    """Format a reset timestamp as 'Xh Ym' from now."""
    try:
        reset = datetime.fromisoformat(iso_ts)
        now = datetime.now(UTC)
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
    """Read the OAuth access token from Claude Code credentials.

    Tries the plain-text file first (Linux), then macOS Keychain.
    Returns (token, is_expired) tuple.
    Raises FileNotFoundError if no credentials found.
    """
    raw: str | None = None

    # Try plain-text file first (Linux, or custom CLAUDE_CONFIG_DIR)
    with contextlib.suppress(FileNotFoundError):
        raw = credentials_path.read_text()

    # macOS: try Keychain
    if raw is None and sys.platform == "darwin":
        try:
            # #202: `security` is the system Keychain CLI (/usr/bin/security).
            # Partial path is intentional — we rely on the macOS default PATH.
            # No shell, fixed argv, no untrusted input.
            result = subprocess.run(  # nosec B603 B607
                [
                    "security",
                    "find-generic-password",
                    "-s",
                    "Claude Code-credentials",
                    "-w",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                raw = result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

    if raw is None:
        raise FileNotFoundError(
            f"No Claude Code credentials at {credentials_path} or macOS Keychain"
        )

    data = json.loads(raw)
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


def format_usage_compact(data: dict) -> str | None:
    """Format usage data into a compact single-line footer.

    Returns something like ``5h: 45% | 7d: 30%`` (low usage)
    or ``5h: 72% (1h 14m) | 7d: 30%`` (reset times shown when >50%).
    """
    parts: list[str] = []
    five_hour = data.get("five_hour")
    if five_hour:
        pct = five_hour["utilization"]
        if pct >= 50:
            reset = _time_until(five_hour["resets_at"])
            parts.append(f"5h: {pct:.0f}% ({reset})")
        else:
            parts.append(f"5h: {pct:.0f}%")

    seven_day = data.get("seven_day")
    if seven_day:
        pct = seven_day["utilization"]
        if pct >= 50:
            reset = _time_until(seven_day["resets_at"])
            parts.append(f"7d: {pct:.0f}% ({reset})")
        else:
            parts.append(f"7d: {pct:.0f}%")

    if not parts:
        return None
    return " | ".join(parts)


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
        from ..engine_overrides import SUBSCRIPTION_USAGE_SUPPORTED_ENGINES
        from ._resolve_engine import resolve_effective_engine

        current_engine = await resolve_effective_engine(ctx)
        if current_engine not in SUBSCRIPTION_USAGE_SUPPORTED_ENGINES:
            return CommandResult(
                text=(
                    f"Usage tracking is not available for the"
                    f" <b>{current_engine}</b> engine."
                ),
                notify=True,
                parse_mode="HTML",
            )

        try:
            data = await fetch_claude_usage()
        except FileNotFoundError:
            return CommandResult(
                text="No Claude Code credentials found (checked ~/.claude/.credentials.json"
                " and macOS Keychain). Run 'claude login' to authenticate.",
                notify=True,
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 401:
                return CommandResult(
                    text="Claude Code OAuth token expired or invalid. "
                    "Run a Claude Code session to refresh it.",
                    notify=True,
                )
            if status == 403:
                return CommandResult(
                    text="Claude Code OAuth token lacks user:profile scope.",
                    notify=True,
                )
            hint = _HTTP_STATUS_HINTS.get(status, "Unexpected error.")
            if status == 429:
                logger.warning("usage.rate_limited", status=status)
            else:
                logger.exception("usage.api_error", status=status)
            return CommandResult(
                text=f"Usage API error (HTTP {status}): {hint}",
                notify=True,
            )
        except httpx.ConnectError:
            logger.exception("usage.connect_failed")
            return CommandResult(
                text="Could not reach the Anthropic usage API"
                " \N{EM DASH} check your network connection and try again.",
                notify=True,
            )
        except httpx.TimeoutException:
            logger.exception("usage.timeout")
            return CommandResult(
                text="Anthropic usage API timed out"
                " \N{EM DASH} this is usually temporary. Try again shortly.",
                notify=True,
            )
        except Exception as exc:
            logger.exception("usage.fetch_failed", error=str(exc))
            return CommandResult(
                text=f"Failed to fetch usage: {type(exc).__name__}: {exc}",
                notify=True,
            )

        text = format_usage(data)
        return CommandResult(text=text, notify=True)


BACKEND: CommandBackend = UsageCommand()
