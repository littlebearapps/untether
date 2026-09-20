"""Command backend for Claude Code subscription usage reporting.

Only available when the current chat's engine is Claude — other engines
do not use Anthropic OAuth credentials.
"""

from __future__ import annotations

import contextlib
import html
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
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


def _read_token_expiry_ms(
    credentials_path: Path = _DEFAULT_CREDENTIALS_PATH,
) -> int | None:
    """Return the OAuth token's ``expiresAt`` (ms since epoch), or ``None``.

    #410: surfaced in the ``/usage debug`` section so operators can see
    whether a silent footer is the result of token expiry vs upstream API
    error vs schema drift, without grepping ``journalctl``. Best-effort —
    swallows every credential-read exception and returns ``None`` so the
    debug section degrades gracefully.
    """
    try:
        _, _, expires_at_ms = _read_access_token_with_expiry(credentials_path)
    except Exception:  # noqa: BLE001
        return None
    return expires_at_ms


def _read_access_token_with_expiry(
    credentials_path: Path = _DEFAULT_CREDENTIALS_PATH,
) -> tuple[str, bool, int]:
    """Like ``_read_access_token`` but also returns ``expires_at_ms`` (#410)."""
    raw = _read_credentials_raw(credentials_path)
    if raw is None:
        raise FileNotFoundError(
            f"No Claude Code credentials at {credentials_path} or macOS Keychain"
        )
    data = json.loads(raw)
    oauth = data["claudeAiOauth"]
    token = oauth["accessToken"]
    expires_at_ms = oauth.get("expiresAt", 0)
    is_expired = (time.time() * 1000) >= (expires_at_ms - 300_000)
    return token, is_expired, expires_at_ms


def _read_credentials_raw(credentials_path: Path) -> str | None:
    """Shared credential-blob reader for ``_read_access_token`` and the
    expiry helper (#410). Returns the raw JSON text or ``None``."""
    raw: str | None = None
    with contextlib.suppress(FileNotFoundError):
        raw = credentials_path.read_text()
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
    return raw


def _read_access_token(
    credentials_path: Path = _DEFAULT_CREDENTIALS_PATH,
) -> tuple[str, bool]:
    """Read the OAuth access token from Claude Code credentials.

    Tries the plain-text file first (Linux), then macOS Keychain.
    Returns (token, is_expired) tuple.
    Raises FileNotFoundError if no credentials found.

    #410: now a thin shim around ``_read_access_token_with_expiry`` so the
    debug surface and the runtime fetch path stay in sync.
    """
    token, is_expired, _ = _read_access_token_with_expiry(credentials_path)
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


def _format_debug_section() -> str:
    """Render the ``/usage debug`` block (#410).

    Surfaces: last successful fetch wall time, cache age, last error, OAuth
    token expiry, schema-mismatch counter. Operator-facing signal so a
    silent subscription footer can be triaged without grepping
    ``journalctl``.
    """
    from ...runner_bridge import get_usage_schema_mismatch_count
    from ...utils.usage_cache import get_cache_stats

    stats = get_cache_stats()
    mismatch = get_usage_schema_mismatch_count()
    expiry_ms = _read_token_expiry_ms()

    lines: list[str] = ["", "<b>🔧 debug</b>"]

    if stats.last_success_wall_seconds is None:
        lines.append("• cache: no successful fetch yet")
    else:
        wall = datetime.fromtimestamp(
            stats.last_success_wall_seconds, tz=UTC
        ).isoformat(timespec="seconds")
        age = stats.cache_age_seconds
        age_label = "fresh" if age is not None and age <= 60 else "stale"
        if age is not None:
            lines.append(f"• cache: last success {wall} ({age:.0f}s ago, {age_label})")
        else:
            lines.append(f"• cache: last success {wall}")

    if stats.last_error_kind:
        msg = stats.last_error_message or "(no message)"
        # Truncate long messages so the debug block stays compact.
        if len(msg) > 120:
            msg = msg[:117] + "…"
        lines.append(f"• last error: <code>{stats.last_error_kind}</code>: {msg}")
    else:
        lines.append("• last error: none")

    if expiry_ms:
        expiry_dt = datetime.fromtimestamp(expiry_ms / 1000, tz=UTC).isoformat(
            timespec="seconds"
        )
        remaining_ms = expiry_ms - int(time.time() * 1000)
        if remaining_ms <= 0:
            lines.append(f"• OAuth token: expired ({expiry_dt})")
        else:
            mins = remaining_ms // 60_000
            if mins >= 60:
                hours = mins // 60
                rem = mins % 60
                lines.append(f"• OAuth token: expires {expiry_dt} (in {hours}h {rem}m)")
            else:
                lines.append(f"• OAuth token: expires {expiry_dt} (in {mins}m)")
    else:
        lines.append("• OAuth token: expiry unknown")

    lines.append(f"• schema mismatches this process: {mismatch}")
    return "\n".join(lines)


async def fetch_antigravity_usage(
    *,
    conversation_id: str | None = None,
    antigravity_cmd: str | None = None,
    timeout_seconds: float = _TIMEOUT,
) -> dict[str, Any]:
    """Fetch usage and quota data from Antigravity CLI by running `/usage` inside an agy session."""
    from ...runners.antigravity import default_antigravity_cmd

    cmd = antigravity_cmd or default_antigravity_cmd()
    if not shutil.which(cmd) and not Path(cmd).exists():
        raise FileNotFoundError(
            f"Antigravity CLI (agy) not found at {cmd}. Run 'curl -fsSL https://antigravity.google/install.sh | bash' to install."
        )

    args = [cmd, "--output-format", "stream-json"]
    if conversation_id:
        args.extend(["--conversation", conversation_id])
    args.extend(["-p", "/usage"])

    try:
        with anyio.fail_after(timeout_seconds):
            result = await anyio.run_process(
                args,
                check=False,
            )
    except TimeoutError:
        raise TimeoutError("Antigravity CLI timed out while fetching usage.") from None

    if result.returncode != 0:
        err_msg = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"agy exited with code {result.returncode}: {err_msg or 'unknown error'}"
        )

    usage_data: dict[str, Any] = {}
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("event") == "command_result":
            cmd = ev.get("command")
            if isinstance(cmd, dict):
                cmd_data = cmd.get("data")
                if isinstance(cmd_data, dict) and "groups" in cmd_data:
                    usage_data = cmd_data
        elif ev.get("event") == "result":
            res = ev.get("result")
            if isinstance(res, dict):
                res_cmd = res.get("command")
                if isinstance(res_cmd, dict):
                    res_cmd_data = res_cmd.get("data")
                    if isinstance(res_cmd_data, dict) and "groups" in res_cmd_data:
                        usage_data = res_cmd_data

    groups = usage_data.get("groups") or []
    five_hour: dict[str, Any] | None = None
    seven_day: dict[str, Any] | None = None

    for group in groups:
        for bucket in group.get("buckets", []):
            rem = bucket.get("remaining_fraction")
            reset = bucket.get("reset_time")
            b_id = str(bucket.get("id", ""))
            window = str(bucket.get("window", ""))
            if rem is not None:
                utilization = round(max(0.0, min(100.0, (1.0 - float(rem)) * 100)), 1)
                if (window == "5h" or b_id.endswith("-5h")) and (
                    five_hour is None or utilization > five_hour["utilization"]
                ):
                    five_hour = {"utilization": utilization, "resets_at": reset}
                elif (window == "weekly" or b_id.endswith("-weekly")) and (
                    seven_day is None or utilization > seven_day["utilization"]
                ):
                    seven_day = {"utilization": utilization, "resets_at": reset}

    return {
        "engine": "antigravity",
        "groups": groups,
        "description": usage_data.get("description"),
        "five_hour": five_hour,
        "seven_day": seven_day,
    }


def format_antigravity_usage(data: dict) -> str:
    """Format Antigravity usage and quota data into a concise Telegram message."""
    lines: list[str] = ["📊 Antigravity Usage\n"]
    groups = data.get("groups") or []
    if not groups:
        return "📊 Antigravity Usage\n\nNo quota groups returned."

    for group in groups:
        name = html.escape(str(group.get("name") or "Quota"))
        lines.append(f"<b>{name}</b>")
        for bucket in group.get("buckets", []):
            b_name = html.escape(str(bucket.get("name") or bucket.get("window", "Quota")))
            rem = bucket.get("remaining_fraction")
            reset = bucket.get("reset_time")
            if rem is not None:
                pct_used = max(0.0, min(100.0, (1.0 - float(rem)) * 100))
                bar = _progress_bar(pct_used)
                pct_left = float(rem) * 100
                reset_str = (
                    f" (resets in {_time_until(reset)})"
                    if reset and pct_used > 0
                    else ""
                )
                lines.append(
                    f"• {b_name}: {bar} {pct_used:.0f}% ({pct_left:.0f}% left{reset_str})"
                )
        lines.append("")
    return "\n".join(lines).rstrip()


def _format_antigravity_debug_section(
    *,
    conversation_id: str | None = None,
    antigravity_cmd: str | None = None,
) -> str:
    """Render the ``/usage debug`` block for Antigravity."""
    from ...utils.usage_cache import get_cache_stats

    stats = get_cache_stats("antigravity")
    lines: list[str] = ["", "<b>🔧 debug</b>"]

    if stats.last_success_wall_seconds is None:
        lines.append("• cache: no successful fetch yet")
    else:
        wall = datetime.fromtimestamp(
            stats.last_success_wall_seconds, tz=UTC
        ).isoformat(timespec="seconds")
        age = stats.cache_age_seconds
        age_label = "fresh" if age is not None and age <= 60 else "stale"
        if age is not None:
            lines.append(f"• cache: last success {wall} ({age:.0f}s ago, {age_label})")
        else:
            lines.append(f"• cache: last success {wall}")

    if stats.last_error_kind:
        msg = stats.last_error_message or "(no message)"
        if len(msg) > 120:
            msg = msg[:117] + "…"
        lines.append(f"• last error: <code>{stats.last_error_kind}</code>: {msg}")
    else:
        lines.append("• last error: none")

    lines.append(f"• session: <code>{conversation_id or '(none)'}</code>")
    cmd = antigravity_cmd or "agy"
    lines.append(f"• CLI binary: <code>{cmd}</code>")
    return "\n".join(lines)


async def _resolve_antigravity_session_id(ctx: CommandContext) -> str | None:
    if ctx.config_path is None:
        return None
    try:
        if ctx.message.thread_id is not None:
            from ..topic_state import TopicStateStore, resolve_state_path

            topic_store = TopicStateStore(resolve_state_path(ctx.config_path))
            token = await topic_store.get_session_resume(
                int(ctx.message.channel_id), int(ctx.message.thread_id), "antigravity"
            )
            if token and token.value:
                return token.value

        from ..chat_sessions import ChatSessionStore, resolve_sessions_path

        chat_store = ChatSessionStore(resolve_sessions_path(ctx.config_path))
        token = await chat_store.get_session_resume(
            int(ctx.message.channel_id), owner_id=None, engine="antigravity"
        )
        if token and token.value:
            return token.value
    except Exception:  # noqa: BLE001
        logger.warning("usage.session_resolve_failed", exc_info=True)
    return None


def _resolve_antigravity_cmd(ctx: CommandContext) -> str:
    if ctx.config_path is not None:
        with contextlib.suppress(Exception):
            from ...config import read_config

            cfg = read_config(ctx.config_path)
            raw = cfg.get("antigravity", {}).get("cmd") or cfg.get(
                "antigravity", {}
            ).get("antigravity_cmd")
            if raw and isinstance(raw, str):
                return os.path.expanduser(raw)
    from ...runners.antigravity import default_antigravity_cmd

    return default_antigravity_cmd()


class UsageCommand:
    """Command backend for Claude Code and Antigravity subscription usage reporting."""

    id = "usage"
    description = "Show subscription usage"

    async def handle(self, ctx: CommandContext) -> CommandResult | None:
        from ..engine_overrides import SUBSCRIPTION_USAGE_SUPPORTED_ENGINES
        from ._resolve_engine import resolve_effective_engine

        # #410: ``/usage debug`` appends a debug section with cache age,
        # last error, OAuth token expiry, and the schema-mismatch counter.
        debug_mode = ctx.args_text.strip().lower() == "debug"

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

        if current_engine == "antigravity":
            conversation_id = await _resolve_antigravity_session_id(ctx)
            antigravity_cmd = _resolve_antigravity_cmd(ctx)
            from ...utils.usage_cache import fetch_antigravity_usage_cached

            try:
                data = await fetch_antigravity_usage_cached(
                    conversation_id=conversation_id,
                    antigravity_cmd=antigravity_cmd,
                )
            except FileNotFoundError as exc:
                return CommandResult(
                    text=f"Antigravity CLI error: {exc}",
                    notify=True,
                )
            except TimeoutError:
                return CommandResult(
                    text="Antigravity CLI timed out while fetching usage stats.",
                    notify=True,
                )
            except Exception as exc:
                logger.exception("usage.antigravity_failed", error=str(exc))
                return CommandResult(
                    text=f"Failed to fetch Antigravity usage: {type(exc).__name__}: {exc}",
                    notify=True,
                )

            text = format_antigravity_usage(data)
            if debug_mode:
                text = (
                    text
                    + "\n"
                    + _format_antigravity_debug_section(
                        conversation_id=conversation_id,
                        antigravity_cmd=antigravity_cmd,
                    )
                )
            return CommandResult(text=text, notify=True, parse_mode="HTML")

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
        if debug_mode:
            # #410: HTML-formatted debug section uses <b>/<code> tags so the
            # structured fields render legibly on mobile. Switch parse_mode
            # accordingly so Telegram renders them.
            text = text + "\n" + _format_debug_section()
            return CommandResult(text=text, notify=True, parse_mode="HTML")
        return CommandResult(text=text, notify=True)


BACKEND: CommandBackend = UsageCommand()
