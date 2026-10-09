"""Command backend for ``/usage``.

Claude: subscription quota from Anthropic's OAuth usage API. Antigravity:
agy's own quota groups from the zero-token ``agy -p /usage`` (#558), plus
the last session's tokens. Other engines: the last session's tokens (#417).
"""

from __future__ import annotations

import contextlib
import html
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


# #417: how each token field renders on /usage. Codex's cached/cache-write
# input and reasoning output are SUBSETS (parentheses, Codex's own display
# vocabulary); OpenCode's cache read/write are SEPARATE from input_tokens
# (``+``). Keyed on the field name, not the engine id.
_INPUT_SUBSET_FIELDS: tuple[tuple[str, str], ...] = (
    ("cached_input_tokens", "cached"),
    ("cache_write_input_tokens", "cache write"),
)
_INPUT_ADDITIVE_FIELDS: tuple[tuple[str, str], ...] = (
    ("cache_read_tokens", "cache read"),
    ("cache_write_tokens", "cache write"),
)
_OUTPUT_SUBSET_FIELDS: tuple[str, ...] = (
    "reasoning_output_tokens",
    "reasoning_tokens",
)
_SESSION_ID_SHOWN = 8


def _format_token_breakdown(counts: dict[str, int]) -> str:
    """``168k in (142k cached) · 1.6k out (reasoning 900)`` /
    ``22k in + 21k cache read · 118 out`` (#417)."""
    from ...background_status import format_tokens

    in_part = f"{format_tokens(counts.get('input_tokens', 0))} in"
    subsets = [
        f"{format_tokens(counts[k])} {label}"
        for k, label in _INPUT_SUBSET_FIELDS
        if counts.get(k)
    ]
    if subsets:
        in_part += f" ({', '.join(subsets)})"
    for k, label in _INPUT_ADDITIVE_FIELDS:
        if counts.get(k):
            in_part += f" + {format_tokens(counts[k])} {label}"
    out_part = f"{format_tokens(counts.get('output_tokens', 0))} out"
    reasoning = next((counts[k] for k in _OUTPUT_SUBSET_FIELDS if counts.get(k)), 0)
    if reasoning:
        out_part += f" (reasoning {format_tokens(reasoning)})"
    return f"{in_part} · {out_part}"


def _session_token_lines(channel_id: object, engine: str) -> list[str] | None:
    """The #417 block for the chat's last session of ``engine`` (header line
    first), or ``None`` when this chat has no completed run of it. Shared by
    the token-only reply and Antigravity's quota reply (#558)."""
    from ...runner_bridge import _TOKEN_LEDGER_SCOPES
    from ...session_costs import get_session_cost_ledger, token_counts
    from .export import latest_session_for_chat

    esc_engine = html.escape(engine)
    sess = (
        latest_session_for_chat(channel_id, engine=engine)  # type: ignore[arg-type]
        if isinstance(channel_id, (int, str))
        else None
    )
    if sess is None:
        return None

    sid = sess.session_id
    shown_sid = sid if len(sid) <= _SESSION_ID_SHOWN else sid[:_SESSION_ID_SHOWN] + "…"
    lines = [f"📊 <b>{esc_engine}</b> · last session in this chat"]
    ledger = get_session_cost_ledger().session_tokens(engine, sid)
    session_line = f"Session <code>{html.escape(shown_sid)}</code>"
    if ledger is not None and ledger.runs > 0:
        runs = ledger.runs
        lines.append(f"{session_line} · {runs} run{'s' if runs != 1 else ''}")
        lines.append(
            f"<b>Session total:</b> {html.escape(_format_token_breakdown(ledger.totals))}"
        )
        last = html.escape(_format_token_breakdown(ledger.last_run))
        if ledger.last_source == "baseline_unknown" and runs == 1:
            last += " (includes earlier runs outside Untether)"
        lines.append(f"<b>Last run:</b> {last}")
    else:
        lines.append(session_line)
        counts = token_counts(sess.usage)
        if counts is not None:
            label = (
                "Session total"
                if _TOKEN_LEDGER_SCOPES.get(engine) == "thread_cumulative"
                else "Last run"
            )
            lines.append(
                f"<b>{label}:</b> {html.escape(_format_token_breakdown(counts))}"
            )
        else:
            lines.append("No token counts were reported for this session.")
    cost = sess.usage.get("total_cost_usd") if sess.usage else None
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        lines.append(f"<b>Last run cost:</b> ${cost:.4f}")
    return lines


def _session_token_reply(channel_id: object, engine: str) -> CommandResult:
    """``/usage`` for an engine without subscription-quota data (#417): the
    token totals of the chat's last session of ``engine``."""
    esc_engine = html.escape(engine)
    lines = _session_token_lines(channel_id, engine)
    if lines is None:
        return CommandResult(
            text=(
                f"Subscription quota tracking is not available for the"
                f" <b>{esc_engine}</b> engine, and this chat has no completed"
                f" {esc_engine} run since Untether last started. Send a prompt,"
                " then try /usage again — or use /export for a transcript."
            ),
            notify=True,
            parse_mode="HTML",
        )
    source = "its exec mode" if engine == "codex" else "its CLI"
    lines.append(
        f"Quota and plan limits are not available for {esc_engine} — {source}"
        " doesn't report them. Transcript: /export"
    )
    return CommandResult(text="\n".join(lines), notify=True, parse_mode="HTML")


_AGY_ENGINE = "antigravity"


async def _antigravity_debug_lines(cmd: str) -> list[str]:
    from ...utils.antigravity_quota import quota_cache_stats
    from ..backend import _cli_version

    stats = quota_cache_stats()
    lines = ["", "<b>🔧 debug</b>"]
    if stats.last_success_wall is None:
        lines.append("• cache: no successful fetch yet")
    else:
        wall = datetime.fromtimestamp(stats.last_success_wall, tz=UTC).isoformat(
            timespec="seconds"
        )
        age = f" ({stats.age_s:.0f}s ago)" if stats.age_s is not None else ""
        lines.append(f"• cache: last success {wall}{age}")
    kind = stats.last_error_kind
    lines.append(
        f"• last error: <code>{html.escape(kind)}</code>"
        if kind
        else "• last error: none"
    )
    lines.append(f"• CLI: <code>{html.escape(cmd)}</code>")
    version = await _cli_version(cmd)  # #951 cache: no spawn when warm
    lines.append(f"• agy version: {html.escape(version or 'unknown')}")
    return lines


async def _antigravity_usage_reply(
    ctx: CommandContext, debug_mode: bool
) -> CommandResult:
    """``/usage`` for Antigravity (#558): agy's own quota groups from the
    zero-token ``agy -p /usage`` (cached 60 s, on demand only), then the #417
    last-session token block. Every agy string is escaped; errors show a
    short kind, never agy's raw text (R10)."""
    from ...runners.antigravity import AUTH_TEXT, AntigravityRunner
    from ...utils import antigravity_quota as agy_quota

    lines = ["📊 <b>Antigravity quota</b>"]
    runner: AntigravityRunner | None = None
    try:
        resolved = ctx.runtime.resolve_runner(
            resume_token=None, engine_override=_AGY_ENGINE
        )
    except Exception:  # noqa: BLE001 — fall through to the "not available" line
        resolved = None
    if resolved is not None and resolved.available:
        candidate = resolved.runner
        if isinstance(candidate, AntigravityRunner):
            runner = candidate
    if runner is None:
        issue = getattr(resolved, "issue", None) if resolved is not None else None
        detail = f" ({html.escape(str(issue)[:200])})" if issue else ""
        lines.append(f"Antigravity CLI isn't available on this host{detail}.")
    else:
        try:
            snapshot = await agy_quota.get_quota(runner)
        except agy_quota.AntigravityNotSignedIn:
            lines.append(html.escape(AUTH_TEXT))
        except agy_quota.AgySlashError as exc:
            if exc.kind == "timeout":
                lines.append(
                    "agy didn't answer /usage within"
                    f" {agy_quota.QUOTA_TIMEOUT_S:.0f} s — try again shortly."
                )
            else:
                lines.append(
                    f"Couldn't read agy's quota ({html.escape(exc.kind[:40])})."
                )
        except Exception as exc:  # noqa: BLE001 — logged by the cache
            lines.append(
                f"Couldn't read agy's quota ({html.escape(type(exc).__name__)})."
            )
        else:
            lines.extend(agy_quota.format_quota_html(snapshot.groups))
            lines.append("<i>From agy /usage (no quota spent).</i>")

    channel_id = getattr(ctx.message, "channel_id", None)
    session = _session_token_lines(channel_id, _AGY_ENGINE)
    lines.append("")
    if session is None:
        lines.append(
            "No completed antigravity run in this chat since Untether last started."
        )
    else:
        lines.append("<b>Last session in this chat</b>")
        lines.extend(session[1:])
    if debug_mode:
        cmd = runner.command() if runner is not None else "agy"
        lines.extend(await _antigravity_debug_lines(cmd))
    return CommandResult(text="\n".join(lines), notify=True, parse_mode="HTML")


class UsageCommand:
    """Command backend for usage reporting: Claude Code subscription quota,
    Antigravity quota (#558), or the last session's token totals for other
    engines (#417)."""

    id = "usage"
    description = (
        "Show usage (Claude, Antigravity: quota; other engines: session tokens)"
    )

    async def handle(self, ctx: CommandContext) -> CommandResult | None:
        from ..engine_overrides import SUBSCRIPTION_USAGE_SUPPORTED_ENGINES
        from ._resolve_engine import resolve_effective_engine

        # #410: ``/usage debug`` appends a debug section with cache age,
        # last error, OAuth token expiry, and the schema-mismatch counter.
        debug_mode = ctx.args_text.strip().lower() == "debug"

        current_engine = await resolve_effective_engine(ctx)
        if current_engine == _AGY_ENGINE:
            # #558: agy's own quota, on demand — never Claude's usage cache
            # or lock, and not in SUBSCRIPTION_USAGE_SUPPORTED_ENGINES (that
            # set also shows the /config subscription-footer toggle).
            return await _antigravity_usage_reply(ctx, debug_mode)
        if current_engine not in SUBSCRIPTION_USAGE_SUPPORTED_ENGINES:
            # #417: token totals for the chat's last session instead of a
            # flat "not available" (``/usage debug`` too — the debug block
            # is Claude-OAuth specific).
            channel_id = getattr(ctx.message, "channel_id", None)
            return _session_token_reply(channel_id, current_engine)

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
