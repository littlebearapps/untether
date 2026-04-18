"""Command backend for `/health` — live system + triggers + cost snapshot (#348)."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from ...commands import CommandBackend, CommandContext, CommandResult


def _read_meminfo_fields(fields: tuple[str, ...]) -> dict[str, int]:
    """Parse /proc/meminfo and return the requested fields as KB integers.

    Returns an empty dict on non-Linux or any read/parse failure; callers
    render a fallback rather than erroring out. Each call re-reads — no
    cache — because /health is called rarely and staleness would be
    misleading.
    """
    if not sys.platform.startswith("linux"):
        return {}
    out: dict[str, int] = {}
    wanted = {f.lower() for f in fields}
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                name, _, rest = line.partition(":")
                key = name.strip().lower()
                if key not in wanted:
                    continue
                parts = rest.strip().split()
                if parts:
                    try:
                        out[name.strip()] = int(parts[0])
                    except ValueError:
                        continue
    except (OSError, FileNotFoundError, PermissionError):
        return {}
    return out


def _format_mb(kb: int) -> str:
    """Render KB as 'X.Y GB' or 'N MB' depending on magnitude."""
    if kb >= 1024 * 1024:
        return f"{kb / (1024 * 1024):.1f} GB"
    if kb >= 1024:
        return f"{kb // 1024} MB"
    return f"{kb} KB"


def _format_uptime_s(seconds: float) -> str:
    days, seconds = divmod(int(seconds), 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def _self_diag_line() -> str | None:
    """Render a one-line Untether self-diagnostic (PID, RSS, FDs)."""
    from ...utils.proc_diag import collect_proc_diag

    diag = collect_proc_diag(os.getpid())
    if diag is None:
        return None
    rss = f"{diag.rss_kb // 1024} MB" if diag.rss_kb else "?"
    fds = str(diag.fd_count) if diag.fd_count is not None else "?"
    children = len(diag.child_pids)
    return f"untether pid={diag.pid} · RSS {rss} · {fds} FDs · {children} children"


def _trigger_summary(ctx: CommandContext) -> str:
    mgr = ctx.trigger_manager
    if mgr is None:
        return "triggers: disabled"
    cron_count = len(mgr.cron_ids())
    webhook_count = len(mgr.webhook_ids())
    if cron_count == 0 and webhook_count == 0:
        return "triggers: none configured"
    parts: list[str] = []
    if cron_count:
        parts.append(f"{cron_count} cron{'s' if cron_count != 1 else ''}")
    if webhook_count:
        parts.append(f"{webhook_count} webhook{'s' if webhook_count != 1 else ''}")
    return "triggers: " + ", ".join(parts)


def _today_cost_line(config_path: Path | None) -> str | None:
    """Show today's accumulated cost across all engines, if tracker is active."""
    try:
        from ...cost_tracker import get_daily_cost
    except ImportError:
        return None
    try:
        total = get_daily_cost()
    except Exception:  # noqa: BLE001 — /health must never crash on cost loader
        return None
    return f"today's API cost: ${total:.2f}"


def render_health_snapshot(ctx: CommandContext) -> str:
    """Compose the /health body from independent data sources.

    Each section degrades gracefully: if a data source is unavailable the
    section is either skipped or shows a fallback marker, never crashes the
    whole command. Keeps /health useful when (say) trigger_manager is None
    or the cost tracker hasn't initialised.
    """
    lines: list[str] = ["🏥 <b>Untether health</b>"]

    # System RAM / Swap
    mem = _read_meminfo_fields(("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"))
    if mem:
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", 0)
        swap_total = mem.get("SwapTotal", 0)
        swap_free = mem.get("SwapFree", 0)
        used = max(0, total - avail)
        swap_used = max(0, swap_total - swap_free)
        usage_line = (
            f"• RAM: {_format_mb(used)} used · {_format_mb(avail)} available "
            f"({100 * used // total if total else 0}%)"
        )
        lines.append(usage_line)
        if swap_total > 0:
            lines.append(f"• Swap: {_format_mb(swap_used)} / {_format_mb(swap_total)}")
    else:
        lines.append("• RAM: unavailable (non-Linux or /proc error)")

    # Self diagnostics
    self_line = _self_diag_line()
    if self_line is not None:
        lines.append(f"• {self_line}")

    # Triggers
    lines.append(f"• {_trigger_summary(ctx)}")

    # Cost
    cost_line = _today_cost_line(ctx.config_path)
    if cost_line is not None:
        lines.append(f"• {cost_line}")

    # Uptime (reuse /ping's module-level timer so we don't start a second one)
    try:
        from .ping import _STARTED_AT, _format_uptime
    except ImportError:
        pass
    else:
        if _STARTED_AT > 0:
            lines.append(f"• uptime: {_format_uptime(time.monotonic() - _STARTED_AT)}")

    return "\n".join(lines)


class HealthCommand:
    """Command backend for /health — compact system + state snapshot."""

    id = "health"
    description = "Show Untether health snapshot (RAM, triggers, cost, uptime)"

    async def handle(self, ctx: CommandContext) -> CommandResult:
        return CommandResult(
            text=render_health_snapshot(ctx),
            notify=False,
            parse_mode="HTML",
        )


BACKEND: CommandBackend = HealthCommand()
