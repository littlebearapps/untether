"""Command backend for per-engine session statistics."""

from __future__ import annotations

import asyncio
import json
import shutil
import time

from ...commands import CommandBackend, CommandContext, CommandResult
from ...session_stats import get_stats


def _format_duration(ms: int) -> str:
    """Format milliseconds as human-readable duration."""
    seconds = ms // 1000
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _format_last_run(ts: float) -> str:
    """Format timestamp as relative time."""
    if ts <= 0:
        return "never"
    diff = time.time() - ts
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{int(diff // 60)}m ago"
    if diff < 86400:
        return f"{int(diff // 3600)}h ago"
    return f"{int(diff // 86400)}d ago"


def _period_label(period: str) -> str:
    if period == "today":
        return "Today"
    if period == "week":
        return "This Week"
    return "All Time"


def format_stats_message(
    engine: str | None,
    period: str,
) -> str:
    """Format stats for display. Returns HTML string."""
    stats = get_stats(engine=engine, period=period)
    label = _period_label(period)

    if not stats:
        scope = f" ({engine})" if engine else ""
        return f"\U0001f4ca <b>Session Stats \u2014 {label}{scope}</b>\n\nNo sessions recorded."

    lines = [f"\U0001f4ca <b>Session Stats \u2014 {label}</b>\n"]
    total_runs = 0
    total_actions = 0
    total_duration = 0
    total_triggered = 0
    total_manual = 0

    for s in sorted(stats, key=lambda x: x.run_count, reverse=True):
        breakdown = ""
        if s.triggered_count or s.manual_count:
            breakdown = f" ({s.triggered_count} triggered, {s.manual_count} manual)"
        lines.append(
            f"<b>{s.engine}</b>: {s.run_count} runs, "
            f"{s.action_count} actions, "
            f"{_format_duration(s.duration_ms)}, "
            f"last {_format_last_run(s.last_run_ts)}{breakdown}"
        )
        total_runs += s.run_count
        total_actions += s.action_count
        total_duration += s.duration_ms
        total_triggered += s.triggered_count
        total_manual += s.manual_count

    if len(stats) > 1:
        total_breakdown = ""
        if total_triggered or total_manual:
            total_breakdown = f" ({total_triggered} triggered, {total_manual} manual)"
        lines.append(
            f"\n<b>Total</b>: {total_runs} runs, "
            f"{total_actions} actions, "
            f"{_format_duration(total_duration)}{total_breakdown}"
        )

    return "\n".join(lines)


async def _check_engine_auth(cli: str, args: list[str]) -> str | None:
    """Run an engine's auth status command. Returns status text or None."""
    if shutil.which(cli) is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        return stdout.decode("utf-8", errors="replace").strip() if stdout else None
    except (TimeoutError, OSError):
        return None


async def get_auth_status() -> list[str]:
    """Get auth status for all known engines."""
    lines: list[str] = []

    # Claude Code
    raw = await _check_engine_auth("claude", ["claude", "auth", "status", "--json"])
    if raw:
        try:
            data = json.loads(raw)
            if data.get("loggedIn"):
                method = data.get("authMethod", "unknown")
                lines.append(f"<b>claude</b>: \u2705 {method}")
            else:
                lines.append("<b>claude</b>: \u274c not logged in")
        except (json.JSONDecodeError, KeyError):
            lines.append("<b>claude</b>: \u2753 unknown")
    elif shutil.which("claude"):
        lines.append("<b>claude</b>: \u2753 status unavailable")

    # Codex
    raw = await _check_engine_auth("codex", ["codex", "login", "status"])
    if raw:
        from .auth import strip_ansi

        clean = strip_ansi(raw).strip()
        if "logged in" in clean.lower():
            # e.g. "Logged in using ChatGPT"
            lines.append(f"<b>codex</b>: \u2705 {clean.lower()}")
        else:
            lines.append("<b>codex</b>: \u274c not logged in")
    elif shutil.which("codex"):
        lines.append("<b>codex</b>: \u2753 status unavailable")

    # OpenCode — check auth file directly (CLI output uses box-drawing chars)
    from pathlib import Path

    oc_auth = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    if shutil.which("opencode"):
        if oc_auth.exists():
            try:
                data = json.loads(oc_auth.read_text(encoding="utf-8"))
                providers = [k for k in data if data[k]]
                if providers:
                    lines.append(
                        f"<b>opencode</b>: \u2705 {len(providers)} provider(s)"
                    )
                else:
                    lines.append("<b>opencode</b>: \u274c no credentials")
            except (json.JSONDecodeError, OSError):
                lines.append("<b>opencode</b>: \u2753 unknown")
        else:
            lines.append("<b>opencode</b>: \u274c no credentials")

    # Pi — check auth file
    pi_auth = Path.home() / ".pi" / "agent" / "auth.json"
    if shutil.which("pi"):
        if pi_auth.exists():
            try:
                data = json.loads(pi_auth.read_text(encoding="utf-8"))
                providers = [k for k in data if data[k]]
                if providers:
                    lines.append(f"<b>pi</b>: \u2705 {len(providers)} provider(s)")
                else:
                    lines.append("<b>pi</b>: \u274c no credentials")
            except (json.JSONDecodeError, OSError):
                lines.append("<b>pi</b>: \u2753 unknown")
        else:
            lines.append("<b>pi</b>: \u274c no credentials")

    return lines


class StatsCommand:
    """Command backend for session statistics."""

    id = "stats"
    description = "Show per-engine session statistics and auth status"

    async def handle(self, ctx: CommandContext) -> CommandResult:
        # Parse args: /stats [engine] [period] or /stats auth
        engine: str | None = None
        period = "today"
        show_auth = False

        for arg in ctx.args:
            lower = arg.lower()
            if lower in ("today", "week", "all"):
                period = lower
            elif lower == "auth":
                show_auth = True
            else:
                engine = lower

        if show_auth:
            auth_lines = await get_auth_status()
            if auth_lines:
                text = "\U0001f511 <b>Auth Status</b>\n\n" + "\n".join(auth_lines)
            else:
                text = "\U0001f511 <b>Auth Status</b>\n\nNo engines found."
            return CommandResult(text=text, notify=True, parse_mode="HTML")

        text = format_stats_message(engine=engine, period=period)
        return CommandResult(text=text, notify=True, parse_mode="HTML")


BACKEND: CommandBackend = StatsCommand()
