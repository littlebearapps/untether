from __future__ import annotations

import contextlib
import os
import signal as _signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio

from .context import RunContext
from .error_hints import get_error_hint as _get_error_hint
from .logging import bind_run_context, get_logger
from .markdown import format_meta_line, render_event_cli
from .model import ActionEvent, CompletedEvent, ResumeToken, StartedEvent, UntetherEvent
from .presenter import Presenter
from .progress import ProgressTracker
from .runner import _APPROVAL_PENDING_REFIRE_S, Runner
from .transport import (
    ChannelId,
    MessageId,
    MessageRef,
    RenderedMessage,
    SendOptions,
    ThreadId,
    Transport,
)

logger = get_logger(__name__)


@dataclass
class _StuckAfterToolResultState:
    """Per-episode state for the stuck-after-tool_result detector (#322).

    Created on first detection; reset when the stream emits any event (which
    clears `_stuck_state` in on_event). Tracks whether Tier 2 adapter-kill
    recovery has been attempted and whether Tier 3 final cancel fired.
    """

    first_detected_at: float
    recovery_attempted: bool = False
    recovery_attempted_at: float = 0.0
    cancelled: bool = False


# Child-process cmdline substrings that identify MCP adapter subprocesses
# we're willing to SIGTERM during Tier 2 recovery.  `mcp-remote` (geelen's
# npm bridge) is the specific adapter implicated in #322; other
# `@modelcontextprotocol/*` stdio bridges share the same failure mode.
_MCP_ADAPTER_CMDLINE_HINTS = ("mcp-remote", "@modelcontextprotocol")

# ---------------------------------------------------------------------------
# Ephemeral message registry
# ---------------------------------------------------------------------------
# Callback handlers (e.g. approve/deny) can register messages here so that
# ProgressEdits.delete_ephemeral() cleans them up when the run finishes.
# Keyed by (channel_id, progress_message_id).

_EPHEMERAL_MSGS: dict[tuple[ChannelId, MessageId], list[MessageRef]] = {}

# #203: companion timestamp map so stale entries from crashed/abnormally-
# exited runs can be swept after _REGISTRY_TTL_SECONDS.  Kept parallel to
# avoid changing the value shape consumed by read paths.
_EPHEMERAL_MSGS_TS: dict[tuple[ChannelId, MessageId], float] = {}

_REGISTRY_TTL_SECONDS = 3600.0  # 1 hour


def register_ephemeral_message(
    channel_id: ChannelId,
    anchor_message_id: MessageId,
    ref: MessageRef,
) -> None:
    """Register a message for deletion when the anchored run finishes."""
    import time as _time

    key = (channel_id, anchor_message_id)
    _EPHEMERAL_MSGS.setdefault(key, []).append(ref)
    _EPHEMERAL_MSGS_TS[key] = _time.monotonic()


# Outline message cleanup registry.
# Maps session_id → (transport, list of outline refs).
# Populated by ProgressEdits._send_outline(), consumed by
# delete_outline_messages() which the callback handler calls.

_OUTLINE_REGISTRY: dict[str, tuple[Any, list[MessageRef]]] = {}
# #203: companion timestamp map (see _EPHEMERAL_MSGS_TS).
_OUTLINE_REGISTRY_TS: dict[str, float] = {}


def register_outline_cleanup(
    session_id: str,
    transport: Any,
    refs: list[MessageRef],
) -> None:
    """Register outline refs for a session so the callback handler can delete them."""
    import time as _time

    _OUTLINE_REGISTRY[session_id] = (transport, refs)
    _OUTLINE_REGISTRY_TS[session_id] = _time.monotonic()


async def delete_outline_messages(session_id: str) -> None:
    """Delete outline messages for a session.  Called from callback handler.

    Also clears the shared refs list so ProgressEdits detects the cleanup
    and removes the stale keyboard on its next render cycle.
    """
    entry = _OUTLINE_REGISTRY.pop(session_id, None)
    _OUTLINE_REGISTRY_TS.pop(session_id, None)
    if entry is None:
        return
    transport, refs = entry
    for ref in refs:
        try:
            await transport.delete(ref=ref)
        except Exception:  # noqa: BLE001
            logger.warning("outline_cleanup.delete_failed", exc_info=True)
    refs.clear()


def sweep_stale_registries(now: float | None = None) -> int:
    """Drop ephemeral/outline entries older than _REGISTRY_TTL_SECONDS.

    Runs in-process every time any ProgressEdits._stall_monitor tick fires
    (via the caller) — handles the case where a run crashes or exits
    abnormally without the usual delete_ephemeral/delete_outline_messages
    cleanup path firing.  Returns the number of entries pruned.  #203.
    """
    import time as _time

    if now is None:
        now = _time.monotonic()

    pruned = 0
    for key, ts in list(_EPHEMERAL_MSGS_TS.items()):
        if now - ts > _REGISTRY_TTL_SECONDS:
            _EPHEMERAL_MSGS.pop(key, None)
            _EPHEMERAL_MSGS_TS.pop(key, None)
            pruned += 1
    for sid, ts in list(_OUTLINE_REGISTRY_TS.items()):
        if now - ts > _REGISTRY_TTL_SECONDS:
            _OUTLINE_REGISTRY.pop(sid, None)
            _OUTLINE_REGISTRY_TS.pop(sid, None)
            pruned += 1
    if pruned:
        logger.info("runner_bridge.registries_swept", pruned=pruned)
    return pruned


# ---------------------------------------------------------------------------
# Progress message persistence (orphan cleanup across restarts)
# ---------------------------------------------------------------------------

_PROGRESS_PERSISTENCE_PATH: Path | None = None


def set_progress_persistence_path(path: Path | None) -> None:
    """Set the path for progress message persistence (called from loop.py)."""
    global _PROGRESS_PERSISTENCE_PATH
    _PROGRESS_PERSISTENCE_PATH = path


# Usage alert thresholds (percentage of 5h window)
_USAGE_WARN_PCT = 70
_USAGE_CRITICAL_PCT = 90


def _load_footer_settings():
    """Load footer settings from config, returning defaults if unavailable."""
    try:
        from .settings import FooterSettings, load_settings_if_exists

        result = load_settings_if_exists()
        if result is None:
            return FooterSettings()
        settings, _ = result
        return settings.footer
    except Exception:  # noqa: BLE001
        logger.warning("footer_settings.load_failed", exc_info=True)
        from .settings import FooterSettings

        return FooterSettings()


def _load_watchdog_settings():
    """Load watchdog settings from config, returning None if unavailable."""
    try:
        from .settings import load_settings_if_exists

        result = load_settings_if_exists()
        if result is None:
            return None
        settings, _ = result
        return settings.watchdog
    except Exception:  # noqa: BLE001
        logger.warning("watchdog_settings.load_failed", exc_info=True)
        return None


def _load_progress_settings():
    """Load progress settings from config, returning defaults if unavailable.

    Read fresh per-run by ``handle_message`` so edits to ``[progress]`` in
    ``untether.toml`` apply on the next run without restarting the bot
    (#269). Sibling of ``_load_footer_settings`` / ``_load_watchdog_settings``.
    """
    from .settings import ProgressSettings

    try:
        from .settings import load_settings_if_exists

        result = load_settings_if_exists()
        if result is None:
            return ProgressSettings()
        settings, _ = result
        return settings.progress
    except Exception:  # noqa: BLE001
        logger.warning("progress_settings.load_failed", exc_info=True)
        return ProgressSettings()


def _load_auto_continue_settings():
    """Load auto-continue settings from config, returning defaults if unavailable."""
    try:
        from .settings import AutoContinueSettings, load_settings_if_exists

        result = load_settings_if_exists()
        if result is None:
            return AutoContinueSettings()
        settings, _ = result
        return settings.auto_continue
    except Exception:  # noqa: BLE001
        logger.warning("auto_continue_settings.load_failed", exc_info=True)
        from .settings import AutoContinueSettings

        return AutoContinueSettings()


def _is_signal_death(rc: int | None) -> bool:
    """Return True if the return code indicates the process was killed by a signal.

    rc=143 (SIGTERM/128+15), rc=137 (SIGKILL/128+9), or negative values
    (Python's representation of signal death, e.g. -9 for SIGKILL).
    """
    if rc is None:
        return False
    if rc < 0:
        return True  # negative = killed by signal (Python convention)
    return rc > 128  # 128+N = killed by signal N (shell convention)


def _should_auto_continue(
    *,
    last_event_type: str | None,
    engine: str,
    cancelled: bool,
    resume_value: str | None,
    auto_continued_count: int,
    max_retries: int,
    proc_returncode: int | None = None,
) -> bool:
    """Detect Claude Code silent session termination bug (#34142, #30333).

    Returns True when the last raw JSONL event was a tool_result ("user")
    meaning Claude never got a turn to process the results before the CLI
    exited.

    Does NOT trigger on signal deaths (SIGTERM/SIGKILL from earlyoom or
    other external killers) — those have rc>128 or rc<0.  The upstream bug
    exits with rc=0.
    """
    if cancelled:
        return False
    if engine != "claude":
        return False
    if last_event_type != "user":
        return False
    if not resume_value:
        return False
    if _is_signal_death(proc_returncode):
        return False
    return auto_continued_count < max_retries


def _format_outbox_skipped_notice(skipped: list[tuple[str, str]]) -> str:
    """#524: human-readable notice for outbox entries that were dropped
    rather than delivered. Headline framing matches the agent's intent:
    the user (and the agent reading in next-turn context) should see what
    the agent meant to send and why it didn't ship.

    Sorted by name, capped at 10 entries (rest collapsed to "...").
    """
    lines = ["\U0001f4ce Outbox skipped (unsupported / blocked):"]
    items = sorted(skipped, key=lambda kv: kv[0])
    cap = 10
    for name, reason in items[:cap]:
        suffix = "/" if reason == "directory" else ""
        lines.append(f"- {name}{suffix} — {reason}")
    if len(items) > cap:
        lines.append(f"- … and {len(items) - cap} more")
    return "\n".join(lines)


async def _surface_outbox_skipped(
    cfg: ExecBridgeConfig,
    incoming: IncomingMessage,
    user_ref: MessageRef,
    skipped: list[tuple[str, str]],
    outbox_config: Any,
) -> None:
    """#524 rc20 follow-up: send the 📎 Outbox skipped notice as a follow-up
    Telegram message. Extracted so the same surface fires from both the
    normal-completion and pre-auto-continue paths in handle_message, and
    from the run_ok=False branch where outbox delivery itself is skipped
    but the user still needs to know what the agent intended to send.

    The "..." pseudo-entry is the max-files-exceeded notice which we keep
    in logs but skip from the user-facing block (the per-file reason there
    isn't actionable).
    """
    if not skipped:
        return
    if not getattr(outbox_config, "outbox_notify_skipped", True):
        return
    notable = [(name, reason) for (name, reason) in skipped if name != "..."]
    if not notable:
        return
    text = _format_outbox_skipped_notice(notable)
    try:
        await cfg.transport.send(
            channel_id=incoming.channel_id,
            message=RenderedMessage(text=text, extra={}),
            options=SendOptions(
                reply_to=user_ref,
                notify=False,
                thread_id=incoming.thread_id,
            ),
        )
    except Exception:  # noqa: BLE001
        logger.warning("outbox.skipped_notice_failed", exc_info=True)


def _format_auto_continue_notice(auto_continued_count: int) -> str:
    """#551 Tier 1: build the Telegram notice text shown when auto-continue
    fires. The 🔁 prefix distinguishes auto-resume from a fresh start so
    users don't ``/cancel`` the salvage. Appends an attempt suffix once we
    are past the first retry.
    """
    notice = "\U0001f501 Auto-resuming session after upstream Claude Code event"
    if auto_continued_count > 0:
        notice += f" (attempt {auto_continued_count + 1})"
    return notice


_DEFAULT_PREAMBLE = (
    "[Untether] You are running via Untether, a Telegram bridge for coding agents. "
    "The user is interacting through Telegram on a mobile device.\n\n"
    "Key constraints:\n"
    "- The user can ONLY see your final assistant text messages\n"
    "- Tool calls, thinking blocks, file contents, and terminal output are invisible\n"
    "- Keep the user informed by writing clear status updates as visible text\n"
    "- If hooks fire at session end, your final response MUST still contain the "
    "user's requested content. Hook concerns are secondary — briefly note them "
    "AFTER the main content, never instead of it.\n\n"
    "Configuration changes (`untether.toml`):\n"
    "- Untether hot-reloads `~/.untether/untether.toml` automatically — "
    "edits take effect within ~1 second of saving.\n"
    "- Do NOT run `systemctl --user restart untether` after editing config. "
    "The restart is unnecessary, and because it shuts down the very session "
    "issuing the command, the graceful drain will time out (120s) and your "
    "final answer to the user will be silently dropped.\n"
    "- Restart-only keys (`bot_token`, `chat_id`, `session_mode`, `topics`, "
    "`message_overflow`) are flagged at reload time — if you didn't see "
    "such a warning, no restart is needed.\n\n"
    "Plan-mode requirements (when you call `ExitPlanMode`):\n"
    "- Your `plan` parameter MUST be a concise 3–5 bullet summary of your "
    "findings, decisions, or proposed changes — never just a file path. "
    "Keep it short: the plan is shown to the user for approval, not as the "
    "final deliverable.\n"
    "- After `ExitPlanMode` is approved, your next assistant message — "
    "which becomes the user's final Telegram message — should be a brief "
    "CLI-style summary: 3–7 bullets or 1–2 short paragraphs covering key "
    "findings, recommendations, decisions made, and next steps. Aim for "
    "~500–1500 characters total. Do NOT re-paste the full plan content — "
    "the user has already seen it during approval. Brevity is the goal; "
    'do not just write "Plan approved" either.\n\n'
    "Every response that completes work MUST end with a structured summary "
    "(keep each section brief — headline bullets, not full content; aim "
    "for ~500–1500 characters total across the whole summary):\n"
    "  ## Summary\n"
    "  ### Completed\n"
    "  - [What was done — short bullets with file paths/line numbers]\n"
    "  - [Key decisions made and why — one line each]\n"
    "  ### Plan/Document Created (if applicable)\n"
    "  - [Path AND a 3–5 bullet headline summary — the user has already "
    "seen the plan during approval, so this is a pointer + headline, not "
    "a re-paste of the full content]\n"
    "  ### Files for Review (if applicable)\n"
    "  - To send files to the user, write them to `.untether-outbox/`\n"
    "  - Example: `mkdir -p .untether-outbox && cp docs/plan.md .untether-outbox/`\n"
    "  - Files are delivered as Telegram documents when the run completes\n"
    "  - The user can also request any project file with `/file get <path>`\n"
    "  ### Next Steps\n"
    "  - [Remaining work, if any]\n"
    "  ### Decisions Needed (if any)\n"
    "  - [Blocking questions — state your recommended option clearly]"
)


def _load_preamble_settings():
    """Load preamble settings from config, returning defaults if unavailable."""
    try:
        from .settings import PreambleSettings, load_settings_if_exists

        result = load_settings_if_exists()
        if result is None:
            return PreambleSettings()
        settings, _ = result
        return settings.preamble
    except Exception:  # noqa: BLE001
        logger.warning("preamble_settings.load_failed", exc_info=True)
        from .settings import PreambleSettings

        return PreambleSettings()


def _apply_preamble(prompt: str) -> str:
    """Prepend the context preamble to the prompt if enabled."""
    cfg = _load_preamble_settings()
    if not cfg.enabled:
        logger.debug("preamble.disabled")
        return prompt
    text = cfg.text if cfg.text is not None else _DEFAULT_PREAMBLE
    if not text:
        logger.debug("preamble.disabled")
        return prompt

    # Append AskUserQuestion guidance based on per-chat toggle
    from .runners.run_options import get_run_options

    run_opts = get_run_options()
    # Default is ON (ask_questions=None treated as True)
    ask_questions = run_opts.ask_questions if run_opts else None
    if ask_questions is False:
        text += (
            "\n\nDo NOT call AskUserQuestion. Proceed with reasonable defaults. "
            "State any assumptions in your Decisions Needed summary section."
        )
    else:
        text += (
            "\n\nWhen you need clarification from the user, use AskUserQuestion "
            "with clear options. The user will see interactive buttons to choose from."
        )

    source = "default"
    if cfg.text is not None:
        source = "config"
    if cfg.text is not None and cfg.text != _DEFAULT_PREAMBLE:
        source = "override"
    logger.info("preamble.applied", preamble_len=len(text), source=source)
    return f"{text}\n\n---\n\n{prompt}"


def _resolve_presenter(
    default_presenter: Presenter, channel_id: ChannelId
) -> Presenter:
    """Return a presenter with the effective verbosity for this channel.

    Checks for a per-chat /verbose override. If one exists and differs from
    the default presenter's formatter, creates a new presenter with the
    overridden verbosity. Otherwise returns the default.
    """
    try:
        from .markdown import MarkdownFormatter
        from .telegram.bridge import TelegramPresenter
        from .telegram.commands.verbose import get_verbosity_override

        override = get_verbosity_override(channel_id)
        if override is None:
            return default_presenter
        # Only create a new presenter if the override differs
        if (
            isinstance(default_presenter, TelegramPresenter)
            and default_presenter._formatter.verbosity == override
        ):
            return default_presenter
        if isinstance(default_presenter, TelegramPresenter):
            formatter = MarkdownFormatter(
                max_actions=default_presenter._formatter.max_actions,
                command_width=default_presenter._formatter.command_width,
                verbosity=override,
            )
            return TelegramPresenter(
                formatter=formatter,
                message_overflow=default_presenter._message_overflow,
            )
    except Exception:  # noqa: BLE001
        logger.debug("resolve_presenter.failed", exc_info=True)
    return default_presenter


# #410: schema-mismatch surfacing — promoted from one-shot per-process to
# per-call counter so the issue-watcher actually creates an issue when API-
# shape drift starts happening (one-shot logs only fire once per restart, so
# operators were missing ongoing drift between restarts). Counter is exposed
# for the /usage debug section.
_USAGE_SCHEMA_MISMATCH_COUNT = 0
# #410: legacy boolean kept temporarily for any external code that imported
# `_USAGE_SCHEMA_WARNED`. It now mirrors "count > 0" rather than gating
# subsequent warnings — the new counter logs every call.
_USAGE_SCHEMA_WARNED = False
_USAGE_EXPECTED_WINDOW_FIELDS = frozenset({"utilization", "resets_at"})


def get_usage_schema_mismatch_count() -> int:
    """Return the running count of subscription-usage schema mismatches (#410).

    Used by the ``/usage`` debug section. Tests reset by setting
    ``_USAGE_SCHEMA_MISMATCH_COUNT = 0`` directly on the module.
    """
    return _USAGE_SCHEMA_MISMATCH_COUNT


def _validate_usage_schema(data: dict[str, Any]) -> None:
    """Log a warning every time the subscription-usage payload is missing
    expected fields. Does not mutate `data` — downstream code already handles
    missing sections defensively; this is purely an observability signal so
    API-shape drift is noticed instead of silently ignored.

    #410: changed from one-shot-per-process to per-call so the
    issue-watcher fires for ongoing drift. The structlog event includes a
    cumulative ``count`` field so callers can rate-limit on their side if
    they want.
    """
    global _USAGE_SCHEMA_MISMATCH_COUNT, _USAGE_SCHEMA_WARNED
    missing: list[str] = []
    for window in ("five_hour", "seven_day"):
        section = data.get(window)
        if section is None:
            continue
        if not isinstance(section, dict):
            missing.append(f"{window}:not_a_dict")
            continue
        missing.extend(
            f"{window}.{field_name}"
            for field_name in _USAGE_EXPECTED_WINDOW_FIELDS
            if field_name not in section
        )
    if missing:
        _USAGE_SCHEMA_MISMATCH_COUNT += 1
        _USAGE_SCHEMA_WARNED = True
        logger.warning(
            "claude_usage.schema_mismatch",
            missing=missing,
            count=_USAGE_SCHEMA_MISMATCH_COUNT,
        )


async def _maybe_append_usage_footer(
    msg: RenderedMessage,
    *,
    always_show: bool = False,
) -> RenderedMessage:
    """Fetch Claude Code usage and append a footer.

    When *always_show* is True, always appends a compact usage line.
    When False (default), only appends warnings at >=70% threshold.
    """
    try:
        from .telegram.commands.usage import _time_until, format_usage_compact
        from .utils.usage_cache import fetch_claude_usage_cached

        data = await fetch_claude_usage_cached()
        _validate_usage_schema(data)

        if always_show:
            compact = format_usage_compact(data)
            if compact:
                footer = f"\n\u26a1 {compact}"
                return RenderedMessage(
                    text=_insert_before_resume(msg.text, footer),
                    extra=msg.extra,
                )
            return msg

        # Threshold-based warning (existing behaviour)
        five_hour = data.get("five_hour")
        seven_day = data.get("seven_day")
        if not five_hour:
            return msg

        pct_5h = five_hour["utilization"]
        if pct_5h < _USAGE_WARN_PCT:
            return msg

        pct_7d = seven_day["utilization"] if seven_day else 0
        reset = _time_until(five_hour["resets_at"])

        if pct_5h >= 100:
            footer = f"\n\U0001f6d1 5h limit hit \u2014 resets in {reset}"
        elif pct_5h >= _USAGE_CRITICAL_PCT:
            _7d_part = f" | 7d: {pct_7d:.0f}%" if pct_7d else ""
            footer = f"\n\u26a0\ufe0f 5h: {pct_5h:.0f}% ({reset}){_7d_part}"
        else:
            _7d_part = f" | 7d: {pct_7d:.0f}%" if pct_7d else ""
            footer = f"\n\u26a15h: {pct_5h:.0f}% ({reset}){_7d_part}"

        return RenderedMessage(
            text=_insert_before_resume(msg.text, footer), extra=msg.extra
        )
    except Exception:  # noqa: BLE001 — cosmetic footer must never block final message
        logger.debug("usage_footer.failed", exc_info=True)
        return msg


def _format_run_cost(usage: dict[str, Any] | None) -> str | None:
    """Format run cost/usage from CompletedEvent into a footer line."""
    if not usage:
        return None
    cost = usage.get("total_cost_usd")
    token_usage = usage.get("usage")
    has_tokens = isinstance(token_usage, dict) and (
        token_usage.get("input_tokens", 0) or token_usage.get("output_tokens", 0)
    )
    if cost is None and not has_tokens:
        return None
    parts: list[str] = []
    if cost is not None:
        if cost >= 0.01:
            parts.append(f"${cost:.2f}")
        else:
            parts.append(f"${cost:.4f}")
    turns = usage.get("num_turns")
    if turns is not None:
        parts.append(f"{turns} tn")
    duration_ms = usage.get("duration_ms")
    if duration_ms:
        secs = duration_ms / 1000
        if secs >= 60:
            mins = int(secs // 60)
            remaining = int(secs % 60)
            parts.append(f"{mins}m {remaining}s")
        else:
            parts.append(f"{secs:.1f}s")
    if has_tokens:
        input_tokens = token_usage.get("input_tokens", 0)
        output_tokens = token_usage.get("output_tokens", 0)
        if input_tokens or output_tokens:

            def _fmt_tokens(n: int) -> str:
                if n >= 1_000_000:
                    return f"{n / 1_000_000:.1f}M"
                if n >= 1_000:
                    return f"{n / 1_000:.1f}k"
                return str(n)

            parts.append(f"{_fmt_tokens(input_tokens)}/{_fmt_tokens(output_tokens)}")
    return " · ".join(parts) or None


def _check_cost_budget(
    usage: dict[str, Any] | None,
) -> tuple[str | None, object | None]:
    """Check run cost against budget.

    Returns ``(alert_text, alert_object)`` where *alert_object* is a
    :class:`CostAlert` (or *None*) containing ``ratio`` and ``level`` fields
    for inline budget suffix rendering.

    Per-chat overrides for ``budget_enabled`` and ``budget_auto_cancel``
    are read from :func:`get_run_options` when available.
    """
    if not usage:
        return None, None
    cost = usage.get("total_cost_usd")
    if cost is None or cost <= 0:
        return None, None
    try:
        from .cost_tracker import (
            CostBudget,
            check_run_budget,
            format_cost_alert,
            record_run_cost,
        )
        from .runners.run_options import get_run_options
        from .settings import load_settings_if_exists

        record_run_cost(cost)

        result = load_settings_if_exists()
        if result is None:
            return None, None
        settings, _ = result
        budget_cfg = settings.cost_budget

        # Per-chat overrides take priority over global config
        run_options = get_run_options()
        if run_options is not None and run_options.budget_enabled is not None:
            budget_enabled = run_options.budget_enabled
        else:
            budget_enabled = budget_cfg.enabled
        if not budget_enabled:
            return None, None

        if run_options is not None and run_options.budget_auto_cancel is not None:
            auto_cancel = run_options.budget_auto_cancel
        else:
            auto_cancel = budget_cfg.auto_cancel

        budget = CostBudget(
            max_cost_per_run=budget_cfg.max_cost_per_run,
            max_cost_per_day=budget_cfg.max_cost_per_day,
            warn_at_pct=budget_cfg.warn_at_pct,
            auto_cancel=auto_cancel,
        )
        alert = check_run_budget(cost, budget)
        if alert is not None:
            return format_cost_alert(alert), alert
        return None, None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "cost_budget.check_failed",
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        return None, None


def _format_budget_suffix(alert: object) -> str:
    """Format a CostAlert as an inline suffix for the cost line."""
    level = getattr(alert, "level", "")
    ratio = getattr(alert, "ratio", 0.0)
    if level == "exceeded":
        return " \U0001f6d1 budget"  # 🛑
    if ratio > 0:
        return f" \u26a0\ufe0f {ratio:.0f}%"  # ⚠️
    return ""


def _record_export_event(
    evt: UntetherEvent, resume: ResumeToken | None, *, channel_id: ChannelId = 0
) -> None:
    """Record an event for the /export command."""
    try:
        from .telegram.commands.export import record_session_event, record_session_usage

        session_id = resume.value if resume else None
        if not session_id and isinstance(evt, StartedEvent) and evt.resume:
            session_id = evt.resume.value
        if not session_id:
            return
        event_dict: dict[str, Any] = {"type": evt.type}
        if isinstance(evt, StartedEvent):
            event_dict["engine"] = evt.engine
            event_dict["title"] = evt.title
        elif isinstance(evt, ActionEvent):
            event_dict["phase"] = evt.phase
            event_dict["ok"] = evt.ok
            event_dict["action"] = {
                "id": evt.action.id,
                "kind": evt.action.kind,
                "title": evt.action.title,
            }
        elif isinstance(evt, CompletedEvent):
            event_dict["ok"] = evt.ok
            event_dict["answer"] = evt.answer
            event_dict["error"] = evt.error
            if evt.usage:
                record_session_usage(session_id, evt.usage, channel_id=channel_id)
        record_session_event(session_id, event_dict, channel_id=channel_id)
        if isinstance(evt, ActionEvent):
            logger.debug(
                "action.recorded",
                kind=evt.action.kind,
                title=evt.action.title,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "export_event.record_failed",
            error=str(exc),
            error_type=exc.__class__.__name__,
        )


def _log_runner_event(evt: UntetherEvent) -> None:
    for line in render_event_cli(evt):
        logger.debug(
            "runner.event.cli",
            line=line,
            event_type=getattr(evt, "type", None),
            engine=getattr(evt, "engine", None),
        )


def _strip_resume_lines(text: str, *, is_resume_line: Callable[[str], bool]) -> str:
    prompt = "\n".join(
        line for line in text.splitlines() if not is_resume_line(line)
    ).strip()
    return prompt or "continue"


def _flatten_exception_group(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        flattened: list[BaseException] = []
        for exc in error.exceptions:
            flattened.extend(_flatten_exception_group(exc))
        return flattened
    return [error]


_RESUME_LINE_MARKER = "\n\n\u21a9\ufe0f "  # ↩️ with variation selector


def _insert_before_resume(text: str, insertion: str) -> str:
    """Insert text before the resume line, or append at end if no resume line."""
    if _RESUME_LINE_MARKER in text:
        idx = text.index(_RESUME_LINE_MARKER)
        return text[:idx] + insertion + text[idx:]
    return text + insertion


def _format_error(error: BaseException) -> str:
    cancel_exc = anyio.get_cancelled_exc_class()
    flattened = [
        exc
        for exc in _flatten_exception_group(error)
        if not isinstance(exc, cancel_exc)
    ]
    if len(flattened) == 1:
        return str(flattened[0]) or flattened[0].__class__.__name__
    if not flattened:
        return str(error) or error.__class__.__name__
    messages = [str(exc) for exc in flattened if str(exc)]
    if not messages:
        return str(error) or error.__class__.__name__
    if len(messages) == 1:
        return messages[0]
    return "\n".join(messages)


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    channel_id: ChannelId
    message_id: MessageId
    text: str
    reply_to: MessageRef | None = None
    thread_id: ThreadId | None = None


@dataclass(frozen=True, slots=True)
class ExecBridgeConfig:
    transport: Transport
    presenter: Presenter
    final_notify: bool
    min_render_interval: float = 0.0
    send_file: Callable[..., Awaitable[Any]] | None = None
    outbox_config: Any | None = None


@dataclass(slots=True)
class RunningTask:
    resume: ResumeToken | None = None
    resume_ready: anyio.Event = field(default_factory=anyio.Event)
    cancel_requested: anyio.Event = field(default_factory=anyio.Event)
    done: anyio.Event = field(default_factory=anyio.Event)
    context: RunContext | None = None


RunningTasks = dict[MessageRef, RunningTask]


async def _send_or_edit_message(
    transport: Transport,
    *,
    channel_id: ChannelId,
    message: RenderedMessage,
    edit_ref: MessageRef | None = None,
    reply_to: MessageRef | None = None,
    notify: bool = True,
    replace_ref: MessageRef | None = None,
    thread_id: ThreadId | None = None,
) -> tuple[MessageRef | None, bool]:
    msg = message
    followups = message.extra.get("followups")
    if followups:
        extra = dict(message.extra)
        if reply_to is not None:
            extra.setdefault("followup_reply_to_message_id", reply_to.message_id)
        if thread_id is not None:
            extra.setdefault("followup_thread_id", thread_id)
        extra.setdefault("followup_notify", notify)
        msg = RenderedMessage(text=message.text, extra=extra)
    if edit_ref is not None:
        logger.debug(
            "transport.edit_message",
            channel_id=edit_ref.channel_id,
            message_id=edit_ref.message_id,
            rendered=msg.text,
        )
        edited = await transport.edit(ref=edit_ref, message=msg)
        if edited is not None:
            return edited, True
        logger.warning(
            "transport.edit_failed_fallback_send",
            channel_id=channel_id,
            edit_message_id=edit_ref.message_id,
        )

    logger.debug(
        "transport.send_message",
        channel_id=channel_id,
        reply_to_message_id=reply_to.message_id if reply_to else None,
        rendered=msg.text,
    )
    sent = await transport.send(
        channel_id=channel_id,
        message=msg,
        options=SendOptions(
            reply_to=reply_to,
            notify=notify,
            replace=replace_ref,
            thread_id=thread_id,
        ),
    )
    return sent, False


class ProgressEdits:
    def __init__(
        self,
        *,
        transport: Transport,
        presenter: Presenter,
        channel_id: ChannelId,
        progress_ref: MessageRef | None,
        tracker: ProgressTracker,
        started_at: float,
        clock: Callable[[], float],
        last_rendered: RenderedMessage | None,
        resume_formatter: Callable[[ResumeToken], str] | None = None,
        label: str = "working",
        context_line: str | None = None,
        thread_id: ThreadId | None = None,
        min_render_interval: float = 0.0,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    ) -> None:
        self.transport = transport
        self.presenter = presenter
        self.channel_id = channel_id
        self.progress_ref = progress_ref
        self.tracker = tracker
        self.started_at = started_at
        self.clock = clock
        self.last_rendered = last_rendered
        self.resume_formatter = resume_formatter
        self.label = label
        self.context_line = context_line
        self.thread_id = thread_id
        self._approval_notified: bool = False
        self._approval_notify_ref: MessageRef | None = None
        # #591: set once the final answer has been (or is about to be)
        # delivered ahead of subprocess exit. Suppresses further progress
        # repaints and the #470 post-result closing message so neither can
        # overwrite or trail the already-delivered answer.
        self._finalizing: bool = False
        self._min_render_interval = min_render_interval
        self._sleep = sleep
        self._last_render_at: float = 0.0
        self._has_rendered: bool = False
        self._last_event_at: float = clock()
        self._stall_warned: bool = False
        self._stall_warn_count: int = 0
        self._total_stall_warn_count: int = 0
        self._last_stall_warn_at: float = 0.0
        self._peak_idle: float = 0.0
        self._prev_diag: Any = None
        self._stall_check_interval: float = 60.0
        self._stall_repeat_seconds: float = 180.0
        self._prev_recent_events: list[tuple[float, str]] | None = None
        self._frozen_ring_count: int = 0
        # #481: heartbeat tick cadence. The stall monitor loop sleeps
        # ``min(_heartbeat_interval, _stall_check_interval)`` per tick, so
        # in production ticks fire every 30 s instead of 60 s; the stall
        # threshold + ``_stall_repeat_seconds`` wall-clock gates still
        # control warning frequency unchanged.
        self._heartbeat_interval: float = 30.0
        # #481: bash grace window for the stall_bash_grace_suppressed branch.
        self._bash_grace_seconds: float = 60.0
        # Stuck-after-tool_result detector (#322). Instance overrides of the
        # class-level defaults, populated from WatchdogSettings in
        # handle_message.
        self._stuck_after_tool_result_enabled: bool = False
        self._stuck_after_tool_result_timeout: float = 300.0
        self._stuck_after_tool_result_recovery_enabled: bool = True
        self._stuck_after_tool_result_recovery_delay: float = 60.0
        self._stuck_state: _StuckAfterToolResultState | None = None
        # #333 Tier 2: one-shot guard so we only log the limbo detection
        # once per session, not on every 60 s stall tick.
        self._post_result_limbo_logged: bool = False
        # #526: pacing for the ``subprocess.approval_pending`` INFO event so
        # an approval-waiting session emits at most every 30 min — gives
        # operators a heartbeat without padding warn-filters with WARNs
        # that would otherwise fire identically to genuine stalls.
        self._last_approval_pending_emit_at: float = 0.0
        self.pid: int | None = None
        self.stream: Any = None  # JsonlStreamState, set from run_runner_with_cancel
        self.cancel_event: anyio.Event | None = None  # threaded from RunningTask
        self.event_seq = 0
        self.rendered_seq = 0
        self._outline_sent: bool = False
        self._outline_refs: list[MessageRef] = []
        self._outline_just_resolved: bool = False
        self.signal_send, self.signal_recv = anyio.create_memory_object_stream(1)

    async def run(self) -> None:
        if self.progress_ref is None:
            return
        stall_scope = anyio.CancelScope()

        async def _monitor() -> None:
            with stall_scope:
                await self._stall_monitor()

        async with anyio.create_task_group() as bg_tg:
            bg_tg.start_soon(_monitor)
            await self._run_loop(bg_tg)
            stall_scope.cancel()

    def _heartbeat_tick(self) -> None:
        """#481: per-tick visibility refresh.

        Runs on EVERY monitor loop tick (both heartbeat-only and stall-check
        ticks). Three responsibilities, none of which touch stall counters:

        1. Mutate ``action.detail['countdown_s']`` for any open
           ScheduleWakeup/Monitor action whose deadline lives in
           ``engine_state.live_wakeups`` / ``live_monitors``. The verbose
           detail formatter reads this on the next render.
        2. Fire the post-result closing message exactly once when the
           Claude watchdog has stamped ``post_result_closed_at`` (#470).
        3. Bump ``event_seq`` to wake the render loop when any open action
           is older than 60 s — this keeps the elapsed-time tail current
           in the chat (otherwise the message looks frozen during long
           BashOutput polling cycles).
        """
        stream = self.stream
        engine_state = getattr(stream, "engine_state", None) if stream else None
        live_wakeups = (
            getattr(engine_state, "live_wakeups", None) if engine_state else None
        )
        live_monitors = (
            getattr(engine_state, "live_monitors", None) if engine_state else None
        )
        now = self.clock()
        # 1) Countdown mutation — ScheduleWakeup + Monitor.
        if live_wakeups or live_monitors:
            for action_state in self.tracker._actions.values():
                if action_state.completed:
                    continue
                aid = str(action_state.action.id or "")
                if not aid:
                    continue
                deadline: float | None = None
                if live_wakeups and aid in live_wakeups:
                    deadline = live_wakeups[aid]
                elif live_monitors and aid in live_monitors:
                    deadline = live_monitors[aid]
                if deadline is None:
                    continue
                # Deadline 0.0 = unknown → leave countdown_s unset so the
                # formatter falls back to delaySeconds-from-input rendering.
                if deadline > 0:
                    action_state.action.detail["countdown_s"] = max(0.0, deadline - now)

        # 2) Post-result closing message — one-shot. Skipped once the final
        # answer was delivered early (#591): the closing notice exists to
        # give feedback while the user is still waiting for the answer, so
        # it is pure noise after delivery.
        if (
            engine_state is not None
            and not self._finalizing
            and getattr(engine_state, "post_result_closed_at", None) is not None
            and not getattr(engine_state, "post_result_closing_sent", False)
        ):
            mins = int(getattr(engine_state, "post_result_idle_minutes", 0.0))
            text = f"✓ turn complete · session closed after {mins}m idle"
            with contextlib.suppress(
                anyio.WouldBlock,
                anyio.BrokenResourceError,
                anyio.ClosedResourceError,
            ):
                self.signal_send.send_nowait(None)
            # Schedule the actual transport.send via the run loop's task
            # group — the heartbeat tick is sync inside _stall_monitor's
            # async loop, so we just stash a flag and let the caller fire
            # the actual send (next tick reads post_result_closing_sent).
            engine_state.post_result_closing_sent = True
            # Hand the message off to the bridge's async send via a
            # one-element queue field.
            self._pending_closing_message = text

        # 3) Long-running tail refresh — bump event_seq so the renderer
        #    redraws with the fresh elapsed-time tail.
        for action_state in self.tracker._actions.values():
            if action_state.completed:
                continue
            if action_state.started_at == 0.0:
                continue
            if (now - action_state.started_at) > 60.0:
                self._bump_heartbeat()
                break

    async def _flush_pending_closing_message(self) -> None:
        """#470: send the one-shot post-result closing Telegram message.

        Called from _stall_monitor after _heartbeat_tick. Idempotent — the
        ``_pending_closing_message`` field is None except for the single
        tick after the watchdog stamps post_result_closed_at.
        """
        text = getattr(self, "_pending_closing_message", None)
        if not text:
            return
        self._pending_closing_message = None
        try:
            await self.transport.send(
                channel_id=self.channel_id,
                message=RenderedMessage(text=text),
                options=SendOptions(thread_id=self.thread_id),
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "progress_edits.post_result_closing_send_failed", exc_info=True
            )

    # #593: cancel-enforcement tuning. Class-level so tests can shrink them.
    _CANCEL_ESCALATION_S: float = 30.0
    _CANCEL_ESCALATION_POLL_S: float = 0.5
    _CANCEL_SIGKILL_GRACE_S: float = 5.0

    async def _enforce_cancel_teardown(self) -> None:
        """#593: make the stall auto-cancel decision actually tear down.

        ``cancel_event.set()`` only cancels the run task group; the
        generator unwind can then stall behind the shielded subcountdown or
        an OOM-starved event loop (observed on nsd: 14m52s between
        ``stall_auto_cancel`` and ``handle.cancelled``, a dead-weight
        subprocess occupying the chat slot through an active OOM crisis).
        Poll for natural teardown up to ``_CANCEL_ESCALATION_S``; if the
        subprocess is still alive, kill it directly (descendant-aware,
        SIGTERM → grace → SIGKILL). Shielded so the enclosing scopes'
        cancellation can't strip the safety net; the early-exit poll keeps
        the shield cheap on the normal path (subprocess dies within
        seconds of the cancel).
        """
        pid = self.pid
        if pid is None:
            # Nothing to enforce against — no PID was ever learned (spawn
            # itself hung, or a non-subprocess runner).
            logger.warning(
                "progress_edits.cancel_enforcement_no_pid",
                channel_id=self.channel_id,
            )
            return

        def _alive() -> bool:
            stream = self.stream
            if stream is not None and stream.proc_returncode is not None:
                return False
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except OSError:
                return True
            return True

        from .utils.subprocess import signal_pid_group

        with anyio.CancelScope(shield=True):
            deadline = time.monotonic() + self._CANCEL_ESCALATION_S
            while time.monotonic() < deadline:
                if not _alive():
                    return
                await anyio.sleep(self._CANCEL_ESCALATION_POLL_S)
            if not _alive():
                return
            logger.warning(
                "progress_edits.cancel_escalated",
                channel_id=self.channel_id,
                pid=pid,
                escalation_s=self._CANCEL_ESCALATION_S,
            )
            signal_pid_group(pid, _signal.SIGTERM)
            grace_deadline = time.monotonic() + self._CANCEL_SIGKILL_GRACE_S
            while time.monotonic() < grace_deadline:
                if not _alive():
                    return
                await anyio.sleep(self._CANCEL_ESCALATION_POLL_S)
            if _alive():
                signal_pid_group(pid, _signal.SIGKILL)

    async def _stall_monitor(self) -> None:
        """Periodically check for event stalls, log diagnostics, and notify.

        Two cadences (#481):
        - **Heartbeat tick** every ``_heartbeat_interval`` (default 30 s):
          updates countdowns, fires closing message, refreshes elapsed
          tail. No stall counters touched.
        - **Stall check** every ``_stall_check_interval`` (default 60 s):
          full diagnostics, threshold selection, suppression matrix,
          notification or auto-cancel.

        The loop sleeps ``min(heartbeat_interval, stall_check_interval)``
        per tick. The stall path runs only when enough wall-clock has
        elapsed since the last stall check, preserving the existing
        ``stall_repeat_seconds`` ≈ 3-tick math the test suite relies on.
        """
        from .utils.proc_diag import (
            collect_proc_diag,
            is_cpu_active,
            is_tree_cpu_active,
        )

        # Initialise pending closing-message slot used by _heartbeat_tick.
        self._pending_closing_message: str | None = None

        while True:
            # #481: tick at the FASTER of the two cadences — heartbeat
            # (30 s default) drives the long-running tail and closing
            # message; stall warnings still gate themselves at wall-clock
            # ``_stall_repeat_seconds`` (180 s default) so faster ticks
            # don't cause warning spam (the gate at line 992-993 below
            # bails out when too soon to repeat). Tests that override
            # ``_stall_check_interval`` to 0.01 s still get fast ticks.
            tick_interval = min(self._heartbeat_interval, self._stall_check_interval)
            await anyio.sleep(tick_interval)

            # Heartbeat tick — cheap (no proc_diag, just dict scans).
            self._heartbeat_tick()
            await self._flush_pending_closing_message()

            # #203: piggy-back a TTL sweep of module-level registries on this
            # periodic tick.  Cheap when idle (empty dicts → early return).
            sweep_stale_registries()
            elapsed = self.clock() - self._last_event_at
            self._peak_idle = max(self._peak_idle, elapsed)

            # Collect diagnostics on every cycle so we always have a CPU
            # baseline for the next check (fixes cpu_active=None on first
            # stall warning) and can use child/TCP info for threshold
            # selection.
            diag = collect_proc_diag(self.pid) if self.pid else None
            cpu_active = (
                is_cpu_active(self._prev_diag, diag)
                if self._prev_diag and diag
                else None
            )
            tree_active = (
                is_tree_cpu_active(self._prev_diag, diag)
                if self._prev_diag and diag
                else None
            )
            self._prev_diag = diag

            # Use longer threshold when waiting for user approval, running a
            # tool, or when child processes are active (Agent subagents).
            mcp_server = self._has_running_mcp_tool()
            if self._has_pending_approval():
                # #526 rc20 follow-up: first reminder at 600 s (so users
                # get a visible "no action needed" message in the same
                # window as a normal stall), subsequent reminders gated
                # by the 1800 s refire threshold.
                if self._last_approval_pending_emit_at == 0.0:
                    threshold = self._STALL_THRESHOLD_APPROVAL_FIRST
                else:
                    threshold = self._STALL_THRESHOLD_APPROVAL
                threshold_reason = "pending_approval"
            elif mcp_server is not None:
                threshold = self._STALL_THRESHOLD_MCP_TOOL
                threshold_reason = "running_mcp_tool"
            elif self._has_active_children(diag):
                threshold = self._STALL_THRESHOLD_SUBAGENT
                threshold_reason = "active_children"
            elif self._has_running_tool():
                threshold = self._STALL_THRESHOLD_TOOL
                threshold_reason = "running_tool"
            else:
                threshold = self._STALL_THRESHOLD_SECONDS
                threshold_reason = "normal"
            if elapsed < threshold:
                continue
            logger.info(
                "progress_edits.stall_threshold_selected",
                channel_id=self.channel_id,
                threshold=threshold,
                reason=threshold_reason,
                elapsed=round(elapsed, 1),
            )
            now = self.clock()
            if (
                self._stall_warned
                and (now - self._last_stall_warn_at) < self._stall_repeat_seconds
            ):
                continue

            self._stall_warned = True
            self._stall_warn_count += 1
            self._total_stall_warn_count += 1
            self._last_stall_warn_at = now

            # #470/#481: compute the 5 expected-wait booleans once. Used to
            # gate BOTH the auto-cancel arm (below) and the notification
            # branches (further down — those add a ``not frozen_escalate``
            # master gate so genuinely-frozen sessions still warn). Auto-
            # cancel is gated unconditionally — a session that's about to
            # gracefully close (#470 watchdog) or legitimately waiting on
            # a pending timer (#481) must not be killed.
            _post_result_idle = self._is_post_result_idle()
            _wakeup_state = self._has_pending_wakeup()
            _monitor_state = self._has_active_monitor()
            _bash_grace = self._has_recent_bash_action(self._bash_grace_seconds)
            _bash_fresh = self._has_fresh_bash_output(threshold / 2.0)
            # #333 Tier 2 (defense-in-depth): post-result idle alone is no
            # longer enough to suppress auto-cancel indefinitely. The
            # claude.py watchdog (Tier 1) should close the subprocess
            # within ``post_result_idle_timeout + grace`` (≈ 660 s). If
            # we're still in post-result idle past the limbo threshold
            # AND no other expected-wait flag is set, treat as limbo and
            # let auto-cancel fire. Older expected-wait suppression is
            # preserved for the legitimate case (e.g. ScheduleWakeup, an
            # active Monitor, or a long bash polling loop).
            _post_result_age = self._post_result_idle_age_seconds()
            _post_result_limbo = (
                _post_result_idle
                and _post_result_age is not None
                and _post_result_age > self._POST_RESULT_LIMBO_THRESHOLD_S
            )
            _real_pending = (
                _wakeup_state is not None
                or _monitor_state is not None
                or _bash_grace
                or _bash_fresh
            )
            _expected_wait = (
                _post_result_idle and not _post_result_limbo
            ) or _real_pending

            # #333 Tier 2: one-shot warning when limbo is detected. This
            # complements the claude.py watchdog's ``runner.limbo_detected``
            # event from the runner side — both signals get picked up by
            # ``untether-issue-watcher`` and indicate Tier 1 missed an
            # edge case (subprocess wouldn't die to SIGTERM/SIGKILL, or
            # the watchdog itself never ran).
            if _post_result_limbo and not self._post_result_limbo_logged:
                self._post_result_limbo_logged = True
                logger.warning(
                    "progress_edits.post_result_limbo_detected",
                    channel_id=self.channel_id,
                    pid=self.pid,
                    post_result_age_s=round(_post_result_age or 0.0, 1),
                    limbo_threshold_s=self._POST_RESULT_LIMBO_THRESHOLD_S,
                    stall_warn_count=self._stall_warn_count,
                )

            last_action = self._last_action_summary()

            recent = list(self.stream.recent_events) if self.stream else []
            stderr_hint = (
                self.stream.stderr_capture[-3:]
                if self.stream and self.stream.stderr_capture
                else None
            )

            # #526: when the stall is the user reading the plan /
            # deliberating on an approval, demote the WARN to a different
            # structured INFO (``subprocess.approval_pending``) and pace it
            # to once per 30 minutes. The chat-side rendering below still
            # emits the friendly "⏳ Awaiting your approval (N min)" copy
            # (#494-C) — operators just stop getting warn-filter spam for
            # what is by definition not a hang. The daemon
            # (``untether-issue-watcher``) and ``/monitor`` are configured
            # to treat WARNs as auto-fileable, so this also stops
            # spurious GitHub issue creation (closes #533).
            if threshold_reason == "pending_approval":
                if (
                    self._last_approval_pending_emit_at == 0.0
                    or now - self._last_approval_pending_emit_at
                    >= _APPROVAL_PENDING_REFIRE_S
                ):
                    self._last_approval_pending_emit_at = now
                    logger.info(
                        "subprocess.approval_pending",
                        channel_id=self.channel_id,
                        engine=getattr(self.tracker, "engine", None),
                        pid=self.pid,
                        seconds_since_last_event=round(elapsed, 1),
                        last_action=last_action,
                        recent_events=[(round(t, 1), lbl) for t, lbl in recent[-3:]],
                        approval_pending=True,
                        source="bridge",
                    )
            else:
                logger.warning(
                    "progress_edits.stall_detected",
                    channel_id=self.channel_id,
                    seconds_since_last_event=round(elapsed, 1),
                    last_event_seq=self.event_seq,
                    stall_warn_count=self._stall_warn_count,
                    pid=self.pid,
                    last_action=last_action,
                    last_event_type=(
                        self.stream.last_event_type if self.stream else None
                    ),
                    process_alive=diag.alive if diag else None,
                    process_state=diag.state if diag else None,
                    tcp_established=diag.tcp_established if diag else None,
                    tcp_total=diag.tcp_total if diag else None,
                    rss_kb=diag.rss_kb if diag else None,
                    fd_count=diag.fd_count if diag else None,
                    cpu_active=cpu_active,
                    tree_active=tree_active,
                    recent_events=[(round(t, 1), lbl) for t, lbl in recent[-5:]],
                    stderr_hint=stderr_hint,
                    approval_pending=False,
                )

            # Auto-cancel: dead process, no-PID zombie, or absolute cap.
            # #470/#481: when an expected-wait state is active, skip the
            # ``max_warnings`` arm — auto-cancel was designed for "the
            # subprocess is stuck", not for "the watchdog is doing its job"
            # (post-result idle) or "we're waiting on a legitimate timer"
            # (ScheduleWakeup/Monitor/Bash polling). The ``process_dead``
            # and ``no_pid_no_events`` arms still fire — those mean the
            # subprocess actually crashed/never started, which is fatal
            # regardless of the wait state.
            auto_cancel_reason: str | None = None
            if diag and diag.alive is False:
                auto_cancel_reason = "process_dead"
            elif (
                self.pid is None
                and self.event_seq == 0
                and self._stall_warn_count >= self._STALL_MAX_WARNINGS_NO_PID
            ):
                auto_cancel_reason = "no_pid_no_events"
            elif _expected_wait:
                # Don't auto-cancel during expected waits even if
                # warn_count has accumulated. Each new tick will re-check
                # whether the wait state still holds; once Claude resumes
                # emitting events, _stall_warned resets via _last_event_at
                # and the warn_count effectively rolls back.
                self._bump_stall_suppression("expected_wait")
                logger.info(
                    "progress_edits.stall_auto_cancel_suppressed_expected_wait",
                    channel_id=self.channel_id,
                    stall_warn_count=self._stall_warn_count,
                    pid=self.pid,
                    post_result=_post_result_idle,
                    pending_wakeup=_wakeup_state is not None,
                    active_monitor=_monitor_state is not None,
                    bash_grace=_bash_grace,
                    bash_fresh=_bash_fresh,
                )
            elif self._stall_warn_count >= self._STALL_MAX_WARNINGS:
                # Suppress auto-cancel when process is actively working
                # (CPU ticks incrementing between diagnostic snapshots).
                # Extended thinking phases produce no JSONL events but the
                # process is alive and busy — killing it is a false positive.
                #
                # tree_active covers the case where the main process is
                # sleeping but child processes (subagents, tool subprocesses)
                # are burning CPU. Without this branch, long subagent runs are
                # killed after MAX_WARNINGS even though the child tree is
                # making progress (#309 CodeRabbit feedback).
                if cpu_active is True:
                    logger.info(
                        "progress_edits.stall_suppressed_by_activity",
                        channel_id=self.channel_id,
                        stall_warn_count=self._stall_warn_count,
                        pid=self.pid,
                    )
                elif tree_active is True and self._has_active_children(diag):
                    logger.info(
                        "progress_edits.stall_suppressed_by_tree_activity",
                        channel_id=self.channel_id,
                        stall_warn_count=self._stall_warn_count,
                        pid=self.pid,
                        child_pids=diag.child_pids if diag else [],
                    )
                else:
                    auto_cancel_reason = "max_warnings"

            if auto_cancel_reason is not None:
                logger.warning(
                    "progress_edits.stall_auto_cancel",
                    channel_id=self.channel_id,
                    reason=auto_cancel_reason,
                    stall_warn_count=self._stall_warn_count,
                    pid=self.pid,
                    event_seq=self.event_seq,
                )
                if self.cancel_event is not None:
                    self.cancel_event.set()
                try:
                    await self.transport.send(
                        channel_id=self.channel_id,
                        message=RenderedMessage(
                            text=f"Auto-cancelled: session appears stuck ({auto_cancel_reason})."
                        ),
                        options=SendOptions(thread_id=self.thread_id),
                    )
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "progress_edits.stall_auto_cancel_notify_failed", exc_info=True
                    )
                # #593: enforcement — the cancel DECISION must end in actual
                # teardown. cancel_event only cancels the run task group;
                # the generator unwind can stall behind a shielded
                # subcountdown or an OOM-starved event loop (observed:
                # 14m52s between stall_auto_cancel and handle.cancelled on
                # nsd). If the subprocess is still alive after the
                # escalation window, kill it directly (descendant-aware).
                await self._enforce_cancel_teardown()
                # Close signal stream so _run_loop exits
                self.signal_send.close()
                return

            # Track whether the recent_events ring buffer has changed since
            # last stall check.  A frozen buffer means no new JSONL events
            # arrived — the process may be stuck in a retry loop despite
            # burning CPU.
            recent_snapshot = [(round(t, 1), lbl) for t, lbl in recent[-5:]]
            if self._prev_recent_events == recent_snapshot:
                self._frozen_ring_count += 1
            else:
                self._frozen_ring_count = 0
            self._prev_recent_events = recent_snapshot

            # Suppress Telegram notification when process is CPU-active
            # (extended thinking, background agents). Instead, trigger a
            # heartbeat re-render so the elapsed time counter keeps ticking.
            #
            # Exception 1: if the ring buffer has been frozen for 3+ checks,
            # the process is likely stuck (retry loop, hung API call, dead
            # thinking) — escalate to a notification despite CPU activity.
            # Exception 2: if the main process is sleeping (state=S), CPU
            # activity is from child processes (hung Bash tool, stuck curl),
            # not from Claude doing extended thinking — notify the user.
            _FROZEN_ESCALATION_THRESHOLD = 3
            frozen_escalate = self._frozen_ring_count >= _FROZEN_ESCALATION_THRESHOLD
            main_sleeping = diag is not None and diag.state == "S"
            _tool_running = self._has_running_tool() or mcp_server is not None

            # Stuck-after-tool_result detector (#322) runs BEFORE the generic
            # notification branches so its specific message + recovery path
            # wins when the pattern matches. Tier 1 logs, Tier 2 SIGTERMs MCP
            # adapters, Tier 3 cancels. Non-matching cases fall through to
            # the existing generic handling.
            if self._detect_stuck_after_tool_result(cpu_active=cpu_active):
                result = await self._handle_stuck_after_tool_result(
                    diag=diag,
                    mcp_server=mcp_server,
                    last_action=last_action,
                )
                if result == "cancelled":
                    # Tier 3: signal_send closed, run_loop will exit
                    return
                # Tier 1/2: suppress generic notification this tick, bump the
                # render loop so the user sees the "hung" message render with
                # updated elapsed time, then continue to next stall check.
                self.event_seq += 1
                with contextlib.suppress(
                    anyio.WouldBlock,
                    anyio.BrokenResourceError,
                    anyio.ClosedResourceError,
                ):
                    self.signal_send.send_nowait(None)
                continue

            # #470/#481: expected-wait suppression matrix. Gated by
            # ``not frozen_escalate`` — a genuinely-frozen session
            # (no JSONL events for 3+ stall ticks AND CPU still active)
            # falls through to the existing notification path so the
            # user gets a real warning. Each branch logs its own info
            # event so journalctl can audit which rule fired. The
            # heartbeat bump keeps the elapsed-time tail current
            # without resetting stall counters.
            if not frozen_escalate and _post_result_idle:
                self._bump_stall_suppression("post_result")
                logger.info(
                    "progress_edits.stall_post_result_suppressed",
                    channel_id=self.channel_id,
                    seconds_since_last_event=round(elapsed, 1),
                    stall_warn_count=self._stall_warn_count,
                    pid=self.pid,
                )
                self._bump_heartbeat()
            elif not frozen_escalate and _wakeup_state is not None:
                soonest, count = _wakeup_state
                logger.info(
                    "progress_edits.stall_schedule_wakeup_suppressed",
                    channel_id=self.channel_id,
                    seconds_since_last_event=round(elapsed, 1),
                    stall_warn_count=self._stall_warn_count,
                    pid=self.pid,
                    soonest_remaining_s=round(soonest, 1),
                    wakeup_count=count,
                )
                self._bump_heartbeat()
            elif not frozen_escalate and _monitor_state is not None:
                soonest, count = _monitor_state
                logger.info(
                    "progress_edits.stall_monitor_active_suppressed",
                    channel_id=self.channel_id,
                    seconds_since_last_event=round(elapsed, 1),
                    stall_warn_count=self._stall_warn_count,
                    pid=self.pid,
                    soonest_remaining_s=round(soonest, 1),
                    monitor_count=count,
                )
                self._bump_heartbeat()
            elif not frozen_escalate and _bash_grace:
                logger.info(
                    "progress_edits.stall_bash_grace_suppressed",
                    channel_id=self.channel_id,
                    seconds_since_last_event=round(elapsed, 1),
                    stall_warn_count=self._stall_warn_count,
                    pid=self.pid,
                    bash_grace_seconds=self._bash_grace_seconds,
                )
                self._bump_heartbeat()
            elif not frozen_escalate and _bash_fresh:
                logger.info(
                    "progress_edits.stall_long_bash_suppressed",
                    channel_id=self.channel_id,
                    seconds_since_last_event=round(elapsed, 1),
                    stall_warn_count=self._stall_warn_count,
                    pid=self.pid,
                    freshness_threshold_s=round(threshold / 2.0, 1),
                )
                self._bump_heartbeat()
            elif cpu_active is True and not frozen_escalate and not main_sleeping:
                logger.info(
                    "progress_edits.stall_suppressed_notification",
                    channel_id=self.channel_id,
                    seconds_since_last_event=round(elapsed, 1),
                    stall_warn_count=self._stall_warn_count,
                    pid=self.pid,
                    frozen_ring_count=self._frozen_ring_count,
                )
                # Heartbeat: bump event_seq to wake the render loop and
                # refresh the progress message with updated elapsed time.
                # Does NOT reset _last_event_at or stall counters.
                self.event_seq += 1
                with contextlib.suppress(
                    anyio.WouldBlock,
                    anyio.BrokenResourceError,
                    anyio.ClosedResourceError,
                ):
                    self.signal_send.send_nowait(None)
            elif (
                cpu_active is True
                and main_sleeping
                and _tool_running
                and self._stall_warn_count > 1
            ):
                # Tool subprocess actively working — first warning already
                # sent, suppress repeats until CPU goes idle.  The ring
                # buffer being "frozen" is expected when a tool runs (no
                # JSONL events while waiting for a child process), so we
                # intentionally do NOT check frozen_escalate here.
                # Keeps #168 fix (first warning fires for sleeping+child
                # scenarios) while eliminating spam for legitimately
                # long-running commands.
                logger.info(
                    "progress_edits.stall_tool_active_suppressed",
                    channel_id=self.channel_id,
                    seconds_since_last_event=round(elapsed, 1),
                    stall_warn_count=self._stall_warn_count,
                    pid=self.pid,
                )
                self.event_seq += 1
                with contextlib.suppress(
                    anyio.WouldBlock,
                    anyio.BrokenResourceError,
                    anyio.ClosedResourceError,
                ):
                    self.signal_send.send_nowait(None)
            elif (
                tree_active is True
                and main_sleeping
                and self._has_active_children(diag)
                and self._stall_warn_count > 1
            ):
                # Subagent child processes actively working — first warning
                # already sent, suppress repeats.  Similar to tool-active
                # suppression but triggered by tree CPU (child processes)
                # instead of tracked tool state.
                self._bump_stall_suppression("children_active")
                logger.info(
                    "progress_edits.stall_children_active_suppressed",
                    channel_id=self.channel_id,
                    seconds_since_last_event=round(elapsed, 1),
                    stall_warn_count=self._stall_warn_count,
                    pid=self.pid,
                    child_pids=diag.child_pids if diag else [],
                    tcp_total=diag.tcp_total if diag else 0,
                )
                self.event_seq += 1
                with contextlib.suppress(
                    anyio.WouldBlock,
                    anyio.BrokenResourceError,
                    anyio.ClosedResourceError,
                ):
                    self.signal_send.send_nowait(None)
            else:
                # Telegram notification (cpu_active=False/None, or frozen
                # ring buffer escalation despite CPU activity)
                mins = int(elapsed // 60)
                mcp_hung = mcp_server is not None and frozen_escalate
                # Initialised here (not inside the final else) so the
                # _genuinely_stuck predicate below can reference it safely
                # from every branch.
                _tool_name: str | None = None
                if mcp_hung:
                    logger.warning(
                        "progress_edits.mcp_tool_hung",
                        channel_id=self.channel_id,
                        mcp_server=mcp_server,
                        frozen_ring_count=self._frozen_ring_count,
                        seconds_since_last_event=round(elapsed, 1),
                        pid=self.pid,
                    )
                    parts = [
                        f"⏳ MCP tool may be hung: {mcp_server} ({mins} min, no new events)"
                    ]
                elif frozen_escalate:
                    logger.warning(
                        "progress_edits.frozen_ring_escalation",
                        channel_id=self.channel_id,
                        frozen_ring_count=self._frozen_ring_count,
                        seconds_since_last_event=round(elapsed, 1),
                        pid=self.pid,
                    )
                    # When a known tool is running and main process is sleeping
                    # (waiting for child), use reassuring message instead of
                    # alarming "No progress" — the tool subprocess is working.
                    _frozen_tool = None
                    if last_action:
                        for _pfx in ("tool:", "note:", "command:"):
                            if last_action.startswith(_pfx):
                                _rest = last_action[len(_pfx) :]
                                _frozen_tool = (
                                    "Bash"
                                    if _pfx == "command:"
                                    else _rest.split(" ", 1)[0].split(":", 1)[0]
                                )
                                break
                    if _frozen_tool and main_sleeping and cpu_active is True:
                        parts = [
                            f"⏳ {_frozen_tool} command still running ({mins} min)"
                        ]
                    else:
                        parts = [
                            f"⏳ No progress for {mins} min (CPU active, no new events)"
                        ]
                elif threshold_reason == "pending_approval":
                    # #494-C: user is waiting on an approval button; the stall
                    # warning is expected, not a sign the agent has frozen.
                    # Distinguish from genuine "no progress" copy so the user
                    # realises the buttons above are theirs to action.
                    # #526 rc20 follow-up: nsd evidence (2026-05-18) showed
                    # users cancelling at ~13 min because the original copy
                    # didn't make the "tap a button" affordance explicit
                    # enough — they assumed the session had hung.
                    parts = [
                        f"⏳ Awaiting your approval ({mins} min) — tap a "
                        "button above to proceed (no action needed otherwise)"
                    ]
                elif mcp_server is not None:
                    parts = [f"⏳ MCP tool running: {mcp_server} ({mins} min)"]
                elif threshold_reason == "active_children":
                    n_children = len(diag.child_pids) if diag else 0
                    if tree_active is True:
                        parts = [
                            f"⏳ Waiting for child processes ({n_children} children, {mins} min)"
                        ]
                    else:
                        parts = [
                            f"⏳ Child processes idle ({n_children} children, {mins} min)"
                        ]
                else:
                    # Extract tool name from last running action for
                    # actionable stall messages ("Bash command still running"
                    # instead of generic "session may be stuck").
                    if last_action:
                        for _prefix in ("tool:", "note:", "command:"):
                            if last_action.startswith(_prefix):
                                _rest = last_action[len(_prefix) :]
                                _raw = _rest.split(" ", 1)[0].split(":", 1)[0]
                                # Map kind prefix to user-friendly name
                                _tool_name = "Bash" if _prefix == "command:" else _raw
                                break
                    if _tool_name and main_sleeping:
                        if cpu_active is True:
                            parts = [
                                f"⏳ {_tool_name} command still running ({mins} min)"
                            ]
                        else:
                            parts = [
                                f"⏳ {_tool_name} tool may be stuck ({mins} min, no CPU activity)"
                            ]
                    elif cpu_active is True:
                        parts = [f"⏳ Still working ({mins} min, CPU active)"]
                    else:
                        parts = [f"⏳ No progress for {mins} min"]
                if self._stall_warn_count > 1:
                    parts[0] += f" (warned {self._stall_warn_count}x)"
                # "session may be stuck" — only when genuinely stuck
                # (no tool identified, cpu not active, not MCP/frozen,
                # not waiting on a user approval button — #494-C)
                _genuinely_stuck = (
                    not mcp_hung
                    and not frozen_escalate
                    and mcp_server is None
                    and threshold_reason != "active_children"
                    and threshold_reason != "pending_approval"
                    and not (_tool_name and main_sleeping)
                    and cpu_active is not True
                )
                if _genuinely_stuck:
                    parts.append("— session may be stuck.")
                if last_action:
                    _summary = (
                        last_action
                        if len(last_action) <= 80
                        else last_action[:77] + "..."
                    )
                    parts.append(f"Last: {_summary}")
                parts.append("/cancel to stop.")
                text = "\n".join(parts)
                try:
                    await self.transport.send(
                        channel_id=self.channel_id,
                        message=RenderedMessage(text=text),
                        options=SendOptions(
                            thread_id=self.thread_id,
                        ),
                    )
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "progress_edits.stall_notify_failed",
                        exc_info=True,
                    )

    def _bump_heartbeat(self) -> None:
        """Wake the render loop without changing stall counters or last_event_at.

        Used by both existing CPU-active suppression branches (lines 1148-,
        1179-, 1205-) and the new #481 suppression matrix. Idempotent —
        the signal channel is buffer=1; subsequent send_nowait calls hit
        WouldBlock harmlessly because the loop only re-renders if
        rendered_seq != event_seq.
        """
        self.event_seq += 1
        with contextlib.suppress(
            anyio.WouldBlock,
            anyio.BrokenResourceError,
            anyio.ClosedResourceError,
        ):
            self.signal_send.send_nowait(None)

    def _bump_stall_suppression(self, reason: str) -> None:
        """#333 Task 4b: count a suppression event for ``session.summary``.

        ``reason`` is a stable kebab-case label (e.g. ``"post_result"``,
        ``"children_active"``, ``"expected_wait"``). Stored on the
        stream's ``stall_suppression_counts`` dict so the summary line
        in ``session.summary`` (emitted from ``run_runner_with_cancel``)
        can render ``stall_suppressions=expected_wait:N,post_result:N``.
        """
        if self.stream is None:
            return
        counts = getattr(self.stream, "stall_suppression_counts", None)
        if counts is None:
            return
        counts[reason] = counts.get(reason, 0) + 1

    def _is_post_result_idle(self) -> bool:
        """#470: suppression — Claude session is past its `result` event.

        Returns True when ``stream.last_event_type == "result"`` AND
        ``engine_state.result_received_at`` is armed (i.e. the post-result
        idle watchdog is the legitimate owner of the silence). The
        bidirectional CLI keeps stdin open between turns; the watchdog
        will close it after ``post_result_idle_timeout``. Stall warnings
        during that window are pure noise — and the auto-cancel arm would
        otherwise wrongly kill a session that's about to gracefully close.

        Stays engine-agnostic via getattr — engines without engine_state
        no-op gracefully.
        """
        stream = self.stream
        if stream is None:
            return False
        if getattr(stream, "last_event_type", None) != "result":
            return False
        engine_state = getattr(stream, "engine_state", None)
        if engine_state is None:
            return False
        return getattr(engine_state, "result_received_at", None) is not None

    def _post_result_idle_age_seconds(self) -> float | None:
        """#333 Tier 2: seconds since ``result_received_at`` was armed.

        Returns None if not in post-result idle state. Used by the stall
        detector to detect limbo — when the watchdog's post-result
        countdown should have closed the subprocess but didn't.

        Uses ``self.clock()`` (matches the bridge's clock injection) — in
        production this is ``time.monotonic`` which is what claude.py uses
        to set ``result_received_at``; in tests it's the fake clock so
        ages line up with whatever the test driver advances.
        """
        stream = self.stream
        if stream is None:
            return None
        engine_state = getattr(stream, "engine_state", None)
        if engine_state is None:
            return None
        armed_at = getattr(engine_state, "result_received_at", None)
        if armed_at is None:
            return None
        return self.clock() - armed_at

    def _has_pending_wakeup(self) -> tuple[float, int] | None:
        """#481: suppression — ScheduleWakeup with future deadline.

        Returns (soonest_remaining_seconds, count) when at least one entry
        in ``engine_state.live_wakeups`` has a deadline still in the future
        (or 0.0, which means the deadline is unknown but the wakeup is
        armed — still a legitimate wait). Returns None otherwise.

        ScheduleWakeup parks the Claude subprocess waiting for an upstream
        timer fire (#289); during that wait Untether sees no JSONL events
        but the silence is expected. This suppression only fires the
        Telegram notification — the structlog WARN at line 1000 still
        emits, so untether-issue-watcher and ops dashboards stay informed.
        """
        stream = self.stream
        if stream is None:
            return None
        engine_state = getattr(stream, "engine_state", None)
        if engine_state is None:
            return None
        live = getattr(engine_state, "live_wakeups", None)
        if not live:
            return None
        now = self.clock()
        soonest: float | None = None
        for deadline in live.values():
            # 0.0 = unknown deadline (legacy delay_ms fallback path or
            # malformed input); treat as still-armed so we don't suppress
            # the warning forever.
            if deadline == 0.0:
                soonest = 0.0
                continue
            remaining = deadline - now
            if remaining <= 0:
                continue
            if soonest is None or remaining < soonest:
                soonest = remaining
        if soonest is None:
            return None
        return (soonest, len(live))

    def _has_active_monitor(self) -> tuple[float, int] | None:
        """#481: suppression — Monitor handle with future deadline.

        Mirrors ``_has_pending_wakeup`` for ``engine_state.live_monitors``.
        Monitor primitives park the subprocess on a child-process or
        external-event watcher; legitimate silence until the deadline.
        """
        stream = self.stream
        if stream is None:
            return None
        engine_state = getattr(stream, "engine_state", None)
        if engine_state is None:
            return None
        live = getattr(engine_state, "live_monitors", None)
        if not live:
            return None
        now = self.clock()
        soonest: float | None = None
        for deadline in live.values():
            if deadline == 0.0:
                soonest = 0.0
                continue
            remaining = deadline - now
            if remaining <= 0:
                continue
            if soonest is None or remaining < soonest:
                soonest = remaining
        if soonest is None:
            return None
        return (soonest, len(live))

    def _last_action_age(self) -> tuple[str | None, float | None]:
        """Return (tool_name, age_seconds) for the most-recent open action.

        Walks ``tracker._actions`` newest-first (insertion order in the
        dict; the tracker doesn't reorder). Returns (None, None) when no
        open action exists or when ``started_at`` is unset (legacy paths
        without a clock).
        """
        for action_state in reversed(list(self.tracker._actions.values())):
            if action_state.completed:
                return (None, None)
            name = action_state.action.detail.get("name") or action_state.action.title
            tool_name = name if isinstance(name, str) else None
            started_at = action_state.started_at
            if started_at == 0.0:
                return (tool_name, None)
            return (tool_name, self.clock() - started_at)
        return (None, None)

    def _has_recent_bash_action(self, grace_s: float) -> bool:
        """#481: suppression — Bash/BashOutput/KillShell within grace window.

        Returns True when the most recent open action is a Bash-family
        tool and its age is less than ``grace_s``. Covers the "command
        is in its startup phase / first poll cycle" window where the
        chat-side stall warning would be premature.
        """
        tool_name, age = self._last_action_age()
        if tool_name is None or age is None:
            return False
        if tool_name not in ("Bash", "BashOutput", "KillShell"):
            return False
        return age < grace_s

    def _has_fresh_bash_output(self, freshness_s: float) -> bool:
        """#481: suppression — recent BashOutput tool_use within freshness_s.

        BashOutput is Claude Code's mechanism for polling backgrounded
        Bash shells; each call is a fresh tool_use+tool_result cycle. The
        most-recent BashOutput's last_update_at signals "Claude got new
        stdout from this bash recently", which IS the upstream proxy for
        "the command isn't actually frozen". Returns True when any open
        or recently-completed BashOutput action has last_update_at within
        the freshness window.
        """
        now = self.clock()
        for action_state in self.tracker._actions.values():
            name = action_state.action.detail.get("name") or action_state.action.title
            if name != "BashOutput":
                continue
            if action_state.last_update_at == 0.0:
                continue
            if (now - action_state.last_update_at) < freshness_s:
                return True
        return False

    def _has_pending_approval(self) -> bool:
        """Check if the most recent non-completed action is waiting for user approval."""
        for action_state in reversed(list(self.tracker._actions.values())):
            if not action_state.completed:
                return bool(action_state.action.detail.get("inline_keyboard"))
            break  # only check the most recent
        return False

    def _has_running_tool(self) -> bool:
        """Check if any action is still running (e.g. Bash command, TaskOutput)."""
        for action_state in reversed(list(self.tracker._actions.values())):
            if not action_state.completed:
                return True
            break  # only check the most recent
        return False

    def _has_running_mcp_tool(self) -> str | None:
        """Return the MCP server name if the most recent action is a running MCP tool.

        MCP tool names follow the pattern: mcp__<server>__<tool_name>.
        Returns the server name (e.g. 'cloudflare-observability') or None.
        """
        for action_state in reversed(list(self.tracker._actions.values())):
            if not action_state.completed:
                name = (
                    action_state.action.detail.get("name") or action_state.action.title
                )
                if isinstance(name, str) and name.startswith("mcp__"):
                    parts = name.split("__", 2)
                    return parts[1] if len(parts) >= 2 else name
            break  # only check the most recent
        return None

    def _has_active_children(self, diag: Any) -> bool:
        """True if the process has active child processes or elevated TCP.

        Detects Agent subagent work that runs in child processes after the
        tracked action event has completed.  Uses child PIDs and TCP
        connection count as signals.
        """
        if diag is None or not diag.alive:
            return False
        if diag.child_pids:
            return True
        return diag.tcp_total > self._TCP_ACTIVE_THRESHOLD

    def _detect_stuck_after_tool_result(
        self,
        *,
        cpu_active: bool | None,
    ) -> bool:
        """Return True if the "tool_result received, engine silent" pattern matches.

        Engine-agnostic detector for upstream claude-code#39700 / #41086 /
        #38437 and the mcp-remote undici-idle-body wedge root cause
        (geelen/mcp-remote#226, #107).

        Fires only when ALL of:
          1. Feature flag is on
          2. stream.last_tool_result_at > 0 (a tool_result arrived and has not
             been cleared by a subsequent assistant-turn event — the latch)
          3. Elapsed since tool_result >= stuck_after_tool_result_timeout
          4. cpu_active is True (main process burning cycles, not sleeping on
             I/O cleanly — distinguishes Node event-loop spin from a legitimate
             sleeping process waiting on a child subprocess's work)
          5. No pending approval in action tracker (ExitPlanMode-safe)
          6. Ring buffer frozen for >= 3 checks (reuses the existing signal;
             no new stdout activity)
        """
        if not self._stuck_after_tool_result_enabled:
            return False
        stream = self.stream
        if stream is None:
            return False
        last_tr = getattr(stream, "last_tool_result_at", 0.0) or 0.0
        if last_tr <= 0:
            return False
        tr_elapsed = self.clock() - last_tr
        if tr_elapsed < self._stuck_after_tool_result_timeout:
            return False
        if cpu_active is not True:
            return False
        if self._has_pending_approval():
            return False
        # #346: skip the detector when the session has legitimate background
        # work armed (Monitor, Bash run_in_background, ScheduleWakeup, etc.).
        # These primitives emit `result` and then park the subprocess waiting
        # for the background deadline/completion — which *looks* identical to
        # a wedge from the detector's POV. Duck-types against the engine_state
        # so this stays engine-agnostic; Claude populates it via #347. Engines
        # without background-task awareness leave engine_state=None and this
        # check no-ops.
        engine_state = getattr(stream, "engine_state", None)
        if engine_state is not None:
            try:
                from .runners.claude import has_live_background_work
            except ImportError:
                has_live_background_work = None  # type: ignore[assignment]
            if has_live_background_work is not None and has_live_background_work(
                engine_state
            ):
                logger.info(
                    "progress_edits.stuck_after_tool_result.suppressed",
                    reason="live_background_work",
                    tr_elapsed=tr_elapsed,
                )
                return False
        # Reuse the existing frozen-ring-buffer escalation threshold (3) so
        # this detector never fires before the user has seen the generic
        # frozen-ring warning it escalates from.
        return self._frozen_ring_count >= 3

    async def _try_recover_mcp_adapter(self, diag: Any) -> list[int]:
        """SIGTERM known MCP adapter child processes.

        Returns the list of PIDs signalled. Conservative: only targets
        children whose /proc/<pid>/cmdline matches a known MCP adapter
        substring (see _MCP_ADAPTER_CMDLINE_HINTS). SIGTERM (not SIGKILL)
        so the adapter closes its SSE connection cleanly, which is what
        unblocks the parent engine's reader.
        """
        if diag is None or not getattr(diag, "child_pids", None):
            return []
        from .utils.proc_diag import read_cmdline

        victims: list[int] = []
        for child_pid in diag.child_pids:
            cmd = read_cmdline(child_pid)
            if cmd is None:
                continue
            low = cmd.lower()
            if any(h in low for h in _MCP_ADAPTER_CMDLINE_HINTS):
                try:
                    os.kill(child_pid, _signal.SIGTERM)
                    victims.append(child_pid)
                except (ProcessLookupError, PermissionError):
                    continue
        return victims

    async def _handle_stuck_after_tool_result(
        self,
        *,
        diag: Any,
        mcp_server: str | None,
        last_action: str | None,
    ) -> str:
        """Tiered recovery for stuck-after-tool_result.

        Returns:
            "logged"     - Tier 1 only, first detection this episode
            "recovery"   - Tier 2 attempted, waiting to see if engine recovers
            "cancelled"  - Tier 3 fired, cancel_event set, signal_send closed
        """
        now = self.clock()
        state = self._stuck_state
        stream = self.stream
        last_tr = getattr(stream, "last_tool_result_at", 0.0) if stream else 0.0

        # Tier 1: log on first detection
        if state is None:
            state = _StuckAfterToolResultState(first_detected_at=now)
            self._stuck_state = state
            logger.warning(
                "progress_edits.stuck_after_tool_result",
                channel_id=self.channel_id,
                pid=self.pid,
                mcp_server=mcp_server,
                seconds_since_tool_result=round(now - last_tr, 1)
                if last_tr > 0
                else None,
                last_action=last_action,
                last_event_type=(
                    getattr(stream, "last_event_type", None) if stream else None
                ),
                frozen_ring_count=self._frozen_ring_count,
                child_pids=list(diag.child_pids) if diag and diag.child_pids else [],
                tcp_established=diag.tcp_established if diag else None,
                upstream_issue="claude-code#39700",
            )
            return "logged"

        # Tier 2: adapter-kill recovery (once per episode). When recovery is
        # disabled we still mark the attempt so Tier 3 can fire after the
        # configured delay — otherwise the wedge would suppress notifications
        # forever without ever cancelling.
        if not state.recovery_attempted:
            killed = (
                await self._try_recover_mcp_adapter(diag)
                if self._stuck_after_tool_result_recovery_enabled
                else []
            )
            state.recovery_attempted = True
            state.recovery_attempted_at = now
            logger.warning(
                "progress_edits.stuck_after_tool_result.recovery_attempt",
                channel_id=self.channel_id,
                pid=self.pid,
                killed_pids=killed,
                mcp_server=mcp_server,
                recovery_enabled=self._stuck_after_tool_result_recovery_enabled,
            )
            return "recovery"

        # Tier 3: final cancel if recovery did not restore the engine
        since_recovery = now - state.recovery_attempted_at
        if (
            state.recovery_attempted
            and since_recovery >= self._stuck_after_tool_result_recovery_delay
            and not state.cancelled
        ):
            state.cancelled = True
            logger.warning(
                "progress_edits.stuck_after_tool_result.cancel",
                channel_id=self.channel_id,
                pid=self.pid,
                since_recovery_s=round(since_recovery, 1),
                mcp_server=mcp_server,
            )
            if self.cancel_event is not None:
                self.cancel_event.set()
            try:
                await self.transport.send(
                    channel_id=self.channel_id,
                    message=RenderedMessage(
                        text=(
                            "Auto-cancelled: stuck after tool_result "
                            "(see untether#322 / claude-code#39700)."
                        )
                    ),
                    options=SendOptions(thread_id=self.thread_id),
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "progress_edits.stuck_after_tool_result.notify_failed",
                    exc_info=True,
                )
            self.signal_send.close()
            return "cancelled"

        # Still within recovery_delay — wait for next tick.
        return "recovery"

    def _last_action_summary(self) -> str | None:
        """Return a short description of the most recent action."""
        for action_state in reversed(list(self.tracker._actions.values())):
            a = action_state.action
            status = "running" if not action_state.completed else "done"
            return f"{a.kind}:{a.title} ({status})"
        return None

    async def _run_loop(self, bg_tg: anyio.abc.TaskGroup) -> None:
        while True:
            while self.rendered_seq == self.event_seq:
                try:
                    await self.signal_recv.receive()
                except anyio.EndOfStream:
                    return

            # #591: final answer already delivered (or being delivered) —
            # consume the wakeup without repainting so a late progress
            # render can't overwrite the final message.
            if self._finalizing:
                self.rendered_seq = self.event_seq
                continue

            # Debounce: never delay the first render; after that, batch events.
            if self._has_rendered and self._min_render_interval > 0:
                elapsed_since = self.clock() - self._last_render_at
                if elapsed_since < self._min_render_interval:
                    await self._sleep(self._min_render_interval - elapsed_since)

            seq_at_render = self.event_seq
            now = self.clock()
            state = self.tracker.snapshot(
                resume_formatter=self.resume_formatter,
                context_line=self.context_line,
                meta_formatter=format_meta_line,
            )
            rendered = self.presenter.render_progress(
                state,
                elapsed_s=now - self.started_at,
                label=self.label,
                now=now,
            )
            # Detect approval button transitions for push notification
            new_kb = rendered.extra.get("reply_markup", {}).get("inline_keyboard", [])
            old_kb = (
                self.last_rendered.extra.get("reply_markup", {}).get(
                    "inline_keyboard", []
                )
                if self.last_rendered
                else []
            )
            has_approval = len(new_kb) > 1
            had_approval = len(old_kb) > 1
            # Track raw source state before stripping (#163)
            source_has_approval = has_approval

            # When outline has been sent (visible or already cleaned up),
            # strip approval buttons from the progress message — the outline
            # message has the canonical approval buttons.  (#163)
            # Only strip for outline-related approvals (DiscussApproval),
            # not for regular tool approvals (e.g. Write with diff preview).
            _current_is_outline = any(
                a.action.detail.get("request_type") == "DiscussApproval"
                for a in state.actions
                if not a.completed
            )
            if self._outline_sent and has_approval and _current_is_outline:
                cancel_row = new_kb[-1:]  # keep only the cancel row
                rendered = RenderedMessage(
                    text=rendered.text,
                    extra={
                        **rendered.extra,
                        "reply_markup": {"inline_keyboard": cancel_row},
                    },
                )
                new_kb = cancel_row
                has_approval = False
                # Suppress the push notification for the next real approval
                # buttons — the user just interacted with the outline and
                # doesn't need another "Action required" push.
                self._outline_just_resolved = True

            try:
                # Send full outline as separate message(s) when approval buttons appear
                if has_approval and not had_approval and not self._outline_sent:
                    for a in state.actions:
                        outline_text = a.action.detail.get("outline_full_text")
                        if outline_text and isinstance(outline_text, str):
                            self._outline_sent = True
                            # Full keyboard (including cancel) for outline msg (#163)
                            approval_kb = (
                                {"inline_keyboard": new_kb} if len(new_kb) > 1 else None
                            )
                            await self._send_outline(
                                outline_text,
                                bg_tg,
                                approval_keyboard=approval_kb,
                                session_id=(
                                    state.resume.value if state.resume else None
                                ),
                            )
                            # Strip approval from progress this cycle too —
                            # outline message has the canonical buttons (#163)
                            cancel_row = new_kb[-1:]
                            rendered = RenderedMessage(
                                text=rendered.text,
                                extra={
                                    **rendered.extra,
                                    "reply_markup": {"inline_keyboard": cancel_row},
                                },
                            )
                            new_kb = cancel_row
                            has_approval = False
                            break

                if has_approval and not had_approval and not self._approval_notified:
                    self._approval_notified = True
                    # After an outline flow, skip one notification cycle —
                    # the user just approved/denied via outline buttons and
                    # doesn't need a duplicate "Action required" push.
                    if self._outline_just_resolved:
                        self._outline_just_resolved = False
                    else:
                        # Contextual notification text
                        notify_text = "Action required \u2014 approval needed"
                        for a in state.actions:
                            if not a.completed and a.action.detail.get("ask_question"):
                                notify_text = "Question from Claude Code"
                                break

                        async def _send_notify(text: str) -> None:
                            try:
                                self._approval_notify_ref = await self.transport.send(
                                    channel_id=self.channel_id,
                                    message=RenderedMessage(text=text),
                                    options=SendOptions(
                                        notify=True,
                                        reply_to=self.progress_ref,
                                        thread_id=self.thread_id,
                                    ),
                                )
                            except Exception:  # noqa: BLE001
                                logger.debug(
                                    "progress_edits.notify_send_failed",
                                    exc_info=True,
                                )

                        bg_tg.start_soon(_send_notify, notify_text)
                elif had_approval and not has_approval:
                    ref_to_delete = self._approval_notify_ref
                    self._approval_notify_ref = None
                    self._approval_notified = False
                    if ref_to_delete is not None:

                        async def _delete_notify(ref: MessageRef) -> None:
                            try:
                                await self.transport.delete(ref=ref)
                            except Exception:  # noqa: BLE001
                                logger.debug(
                                    "progress_edits.notify_delete_failed",
                                    exc_info=True,
                                )

                        bg_tg.start_soon(_delete_notify, ref_to_delete)

                # Delete outline messages when approval is resolved.
                # Triggers on: buttons disappear (had→!has), OR keyboard
                # content changes (old approval replaced by new one, e.g.
                # ExitPlanMode → Write).
                if self._outline_refs and (
                    (had_approval and not has_approval)
                    or (had_approval and has_approval and new_kb != old_kb)
                ):
                    outline_refs = list(self._outline_refs)
                    self._outline_refs.clear()

                    async def _delete_outlines(
                        refs: list[MessageRef],
                    ) -> None:
                        for ref in refs:
                            try:
                                await self.transport.delete(ref=ref)
                            except Exception:  # noqa: BLE001
                                logger.debug(
                                    "progress_edits.outline_delete_failed",
                                    exc_info=True,
                                )

                    bg_tg.start_soon(_delete_outlines, outline_refs)

                # Reset outline state when source stops providing approval,
                # so future ExitPlanMode can show buttons on progress (#163)
                if self._outline_sent and not source_has_approval:
                    self._outline_sent = False

                if rendered != self.last_rendered:
                    # Log keyboard transitions at info level for #103/#104 diagnostics
                    if has_approval and not had_approval:
                        logger.info(
                            "progress_edits.keyboard_attach",
                            channel_id=self.channel_id,
                            message_id=self.progress_ref.message_id,
                            keyboard_rows=len(new_kb),
                        )
                    logger.debug(
                        "transport.edit_message",
                        channel_id=self.channel_id,
                        message_id=self.progress_ref.message_id,
                        rendered=rendered.text,
                    )
                    edited = await self.transport.edit(
                        ref=self.progress_ref,
                        message=rendered,
                        wait=has_approval and not had_approval,
                    )
                    if edited is not None:
                        self.last_rendered = rendered
                        self._last_render_at = self.clock()
                        self._has_rendered = True
                    elif has_approval:
                        logger.warning(
                            "progress_edits.keyboard_edit_failed",
                            channel_id=self.channel_id,
                            message_id=self.progress_ref.message_id,
                            keyboard_rows=len(new_kb),
                        )
            except Exception:  # noqa: BLE001
                # Transport errors (timeouts, network issues) are best-effort —
                # never crash a run because a progress edit failed to send.
                logger.warning("progress_edits.transport_error", exc_info=True)

            self.rendered_seq = seq_at_render

    _STALL_THRESHOLD_SECONDS: float = 300.0  # 5 minutes
    _STALL_THRESHOLD_TOOL: float = 600.0  # 10 minutes when a tool is actively running
    _STALL_THRESHOLD_MCP_TOOL: float = 900.0  # 15 min for MCP tools (network-bound)
    _STALL_THRESHOLD_SUBAGENT: float = 900.0  # 15 min for child process / subagent work
    # #526 rc20 follow-up: two-tier threshold for approval-pending stalls.
    # First reminder fires at 600 s so users get a reassuring "tap a button
    # above" message within the same window as a normal-tool stall (10 min)
    # — without it, nsd evidence (2026-05-18) showed users ``/cancel``-ing
    # productive sessions after ~13 min of silence. Subsequent reminders
    # fall back to 1800 s (30 min) so the chat doesn't get noisy on long
    # deliberations.
    _STALL_THRESHOLD_APPROVAL_FIRST: float = 600.0
    _STALL_THRESHOLD_APPROVAL: float = 1800.0  # refire threshold after first
    _STALL_MAX_WARNINGS: int = 10  # absolute cap
    _STALL_MAX_WARNINGS_NO_PID: int = 3  # aggressive cap when pid=None + no events
    _TCP_ACTIVE_THRESHOLD: int = 20  # TCP connections above this suggest active work
    # #333 Tier 2: post-result idle limbo threshold. The Claude watchdog
    # (claude.py:_post_result_idle_watchdog + _post_result_subcountdown)
    # closes the subprocess within ``post_result_idle_timeout`` (600 s) +
    # 5 s SIGTERM grace + observation slack. If we're still in post-
    # result idle past this point with no other expected-wait signal,
    # Tier 1 missed an edge case — stop suppressing auto-cancel.
    _POST_RESULT_LIMBO_THRESHOLD_S: float = 660.0

    def note_final(self, evt: UntetherEvent) -> None:
        """#591: record a terminal CompletedEvent WITHOUT scheduling a repaint.

        Used by the early final-answer delivery path: the tracker must see
        the event so the final snapshot renders completed actions/usage, but
        bumping ``event_seq`` would wake ``_run_loop`` into painting one more
        *progress* frame that races the final answer edit on the same
        Telegram message. ``_finalizing`` makes any already-queued wakeup a
        no-op too.
        """
        self.tracker.note_event(evt)
        self._finalizing = True

    async def on_event(self, evt: UntetherEvent) -> None:
        if not self.tracker.note_event(evt):
            return
        if self.progress_ref is None:
            return
        now = self.clock()
        if self._stall_warned:
            elapsed_stall = now - self._last_event_at
            logger.info(
                "progress_edits.stall_recovered",
                channel_id=self.channel_id,
                stall_seconds=round(elapsed_stall, 1),
                stall_warn_count=self._stall_warn_count,
            )
            self._stall_warned = False
            self._stall_warn_count = 0
            # Keep _prev_diag so next stall episode has a CPU baseline
            self._frozen_ring_count = 0
            self._prev_recent_events = None
        # Clear stuck-after-tool_result episode state (#322) on any event —
        # covers both organic recovery and Tier 2 recovery where SIGTERM'ing
        # the adapter lets the engine resume. Outside the stall-recovered
        # branch because a stuck-state can exist without _stall_warned=True
        # when the detector fired before a generic stall warning did.
        if self._stuck_state is not None:
            logger.info(
                "progress_edits.stuck_after_tool_result.recovered",
                channel_id=self.channel_id,
                pid=self.pid,
                seconds_since_first_detected=round(
                    now - self._stuck_state.first_detected_at, 1
                ),
                recovery_was_attempted=self._stuck_state.recovery_attempted,
            )
            self._stuck_state = None
        self._last_event_at = now
        self.event_seq += 1
        try:
            self.signal_send.send_nowait(None)
        except anyio.WouldBlock:
            pass
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            pass

    async def _send_outline(
        self,
        text: str,
        bg_tg: anyio.abc.TaskGroup,
        *,
        approval_keyboard: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> None:
        """Send plan outline as separate ephemeral message(s).

        Splits long outlines across multiple messages to avoid Telegram's
        4096 char limit.  Each chunk is rendered from markdown to Telegram
        entities so headings, bold, code etc. display correctly.  The last
        message gets the approve/deny keyboard so the user doesn't have to
        scroll up.  Refs are tracked in ``_outline_refs`` and registered in
        the module-level ``_OUTLINE_REGISTRY`` so the callback handler can
        delete them on approve/deny.
        """
        # Local import to avoid circular dependency (telegram.bridge → runner_bridge)
        from .telegram.render import render_markdown, split_markdown_body

        max_chars = 3500  # leave room for entities/overhead
        chunks = split_markdown_body(text, max_chars) or [text]

        async def _do_send() -> None:
            last_idx = len(chunks) - 1
            for idx, chunk in enumerate(chunks):
                try:
                    rendered_text, entities = render_markdown(chunk)
                    extra: dict[str, Any] = {"entities": entities}
                    if approval_keyboard and idx == last_idx:
                        extra["reply_markup"] = approval_keyboard
                    ref = await self.transport.send(
                        channel_id=self.channel_id,
                        message=RenderedMessage(text=rendered_text, extra=extra),
                        options=SendOptions(
                            reply_to=self.progress_ref,
                            notify=False,
                            thread_id=self.thread_id,
                        ),
                    )
                    if ref:
                        self._outline_refs.append(ref)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "progress_edits.outline_send_failed",
                        channel_id=self.channel_id,
                        exc_info=True,
                    )
            # Register in module-level registry so callback handler can
            # trigger immediate deletion on approve/deny.
            if session_id and self._outline_refs:
                register_outline_cleanup(session_id, self.transport, self._outline_refs)

        bg_tg.start_soon(_do_send)

    async def delete_ephemeral(self) -> None:
        """Delete any tracked ephemeral notification messages."""
        if self._approval_notify_ref is not None:
            try:
                await self.transport.delete(ref=self._approval_notify_ref)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ephemeral.delete.failed",
                    chat_id=self.channel_id,
                    message_id=self._approval_notify_ref.message_id,
                    error=str(exc),
                    error_type=exc.__class__.__name__,
                )
            self._approval_notify_ref = None
        # Safety-net: delete any outline messages not already cleaned up
        # (e.g. run cancelled while outline is visible).
        # Also remove from the module-level registry to avoid stale entries.
        stale_sessions = [
            sid
            for sid, (_, refs) in _OUTLINE_REGISTRY.items()
            if refs is self._outline_refs
        ]
        for sid in stale_sessions:
            _OUTLINE_REGISTRY.pop(sid, None)
            _OUTLINE_REGISTRY_TS.pop(sid, None)  # #203: keep ts map in sync
        for ref in self._outline_refs:
            try:
                await self.transport.delete(ref=ref)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ephemeral.outline_delete.failed",
                    chat_id=self.channel_id,
                    message_id=ref.message_id,
                    error=str(exc),
                    error_type=exc.__class__.__name__,
                )
        self._outline_refs.clear()
        # Drain messages registered by callback handlers (e.g. approve/deny feedback).
        if self.progress_ref is not None:
            key = (self.channel_id, self.progress_ref.message_id)
            refs = _EPHEMERAL_MSGS.pop(key, [])
            _EPHEMERAL_MSGS_TS.pop(key, None)  # #203: keep ts map in sync
            for ref in refs:
                try:
                    await self.transport.delete(ref=ref)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "ephemeral.delete.failed",
                        chat_id=self.channel_id,
                        message_id=ref.message_id,
                        error=str(exc),
                        error_type=exc.__class__.__name__,
                    )
        # Note: unregister_progress() is called AFTER send_result_message()
        # in handle_message(), not here, to avoid an orphan window.


@dataclass(frozen=True, slots=True)
class ProgressMessageState:
    ref: MessageRef | None
    last_rendered: RenderedMessage | None


async def send_initial_progress(
    cfg: ExecBridgeConfig,
    *,
    channel_id: ChannelId,
    reply_to: MessageRef,
    label: str,
    tracker: ProgressTracker,
    progress_ref: MessageRef | None = None,
    resume_formatter: Callable[[ResumeToken], str] | None = None,
    context_line: str | None = None,
    thread_id: ThreadId | None = None,
) -> ProgressMessageState:
    last_rendered: RenderedMessage | None = None

    state = tracker.snapshot(
        resume_formatter=resume_formatter,
        context_line=context_line,
    )
    initial_rendered = cfg.presenter.render_progress(
        state,
        elapsed_s=0.0,
        label=label,
    )
    sent_ref, _ = await _send_or_edit_message(
        cfg.transport,
        channel_id=channel_id,
        message=initial_rendered,
        edit_ref=progress_ref,
        reply_to=reply_to,
        notify=False,
        replace_ref=progress_ref,
        thread_id=thread_id,
    )
    if sent_ref is not None:
        last_rendered = initial_rendered
        logger.debug(
            "progress.sent",
            channel_id=sent_ref.channel_id,
            message_id=sent_ref.message_id,
        )
        if _PROGRESS_PERSISTENCE_PATH is not None:
            from .telegram.progress_persistence import register_progress

            session_key = f"{channel_id}:{sent_ref.message_id}"
            register_progress(
                _PROGRESS_PERSISTENCE_PATH,
                session_key,
                int(channel_id),
                int(sent_ref.message_id),
            )

    return ProgressMessageState(
        ref=sent_ref,
        last_rendered=last_rendered,
    )


@dataclass(slots=True)
class RunOutcome:
    cancelled: bool = False
    completed: CompletedEvent | None = None
    resume: ResumeToken | None = None


async def run_runner_with_cancel(
    runner: Runner,
    *,
    prompt: str,
    resume_token: ResumeToken | None,
    edits: ProgressEdits,
    running_task: RunningTask | None,
    on_thread_known: Callable[[ResumeToken, anyio.Event], Awaitable[None]] | None,
    channel_id: ChannelId = 0,
    on_completed: Callable[[CompletedEvent, RunOutcome], Awaitable[None]] | None = None,
) -> RunOutcome:
    outcome = RunOutcome()
    start_time = time.monotonic()
    try:
        async with anyio.create_task_group() as tg:

            async def run_runner() -> None:
                events = runner.run(prompt, resume_token)
                try:
                    async for evt in events:
                        _log_runner_event(evt)
                        if isinstance(evt, StartedEvent):
                            outcome.resume = evt.resume
                            bind_run_context(
                                resume=evt.resume.value,
                                session_id=evt.resume.value,
                            )
                            # Thread PID and stream to ProgressEdits
                            if evt.meta:
                                pid = evt.meta.get("pid")
                                if isinstance(pid, int):
                                    edits.pid = pid
                            _cs = getattr(runner, "current_stream", None)
                            if _cs is not None:
                                edits.stream = _cs
                            if running_task is not None and running_task.resume is None:
                                running_task.resume = evt.resume
                                try:
                                    if on_thread_known is not None:
                                        await on_thread_known(
                                            evt.resume, running_task.done
                                        )
                                finally:
                                    running_task.resume_ready.set()
                        elif isinstance(evt, CompletedEvent):
                            first_completed = outcome.completed is None
                            outcome.resume = evt.resume or outcome.resume
                            outcome.completed = evt
                            # #591: deliver the final answer NOW — the run
                            # generator may not return for up to the full
                            # post-result limbo window (MCP children holding
                            # the subprocess open), and the answer already
                            # exists. Only genuine successful results
                            # (ok=True) qualify: synthesized stream-end
                            # errors must keep riding the post-return path
                            # so auto-continue / error formatting still see
                            # them first. Failures here leave the run
                            # untouched — the post-return path retries.
                            if (
                                on_completed is not None
                                and evt.ok is True
                                and first_completed
                            ):
                                edits.note_final(evt)
                                _record_export_event(
                                    evt, outcome.resume, channel_id=channel_id
                                )
                                try:
                                    # #614: shield the delivery — a /cancel
                                    # landing mid-send would otherwise cancel
                                    # this await AFTER the message hit the
                                    # wire but BEFORE final_delivery["sent"]
                                    # was recorded, so handle_message would
                                    # render a spurious "cancelled" message
                                    # on top of the delivered answer. Bounded
                                    # so a wedged transport can't hold the
                                    # cancel hostage. #618: 60s, not less —
                                    # a 4-chunk final under group-chat outbox
                                    # pacing takes 15s+ on its own, and a
                                    # timeout that fires between the last
                                    # chunk and the sent-flag re-creates the
                                    # spurious-cancelled artifact.
                                    with anyio.move_on_after(60, shield=True):
                                        await on_completed(evt, outcome)
                                except Exception:  # noqa: BLE001
                                    logger.warning(
                                        "final.early_delivery_failed",
                                        exc_info=True,
                                    )
                                continue
                        # A3: Record events for /export
                        _record_export_event(evt, outcome.resume, channel_id=channel_id)
                        await edits.on_event(evt)
                finally:
                    # #614: close the runner generator in THIS task. When the
                    # async-for is abandoned mid-body (e.g. /cancel lands
                    # while on_completed is awaiting), the generator is left
                    # suspended at a yield and would be finalized later by
                    # the event loop's async-generator hook in a DIFFERENT
                    # task — and run_impl's anyio task group then raises
                    # "Attempted to exit cancel scope in a different task
                    # than it was entered in" as an unretrieved task
                    # exception. Shielded so the pending bridge-level
                    # cancellation can't interrupt generator teardown
                    # (subprocess kill, registry cleanup); bounded so a
                    # wedged teardown can't hang the bridge.
                    aclose = getattr(events, "aclose", None)
                    if aclose is not None:
                        with anyio.move_on_after(30, shield=True):
                            await aclose()
                    tg.cancel_scope.cancel()

            async def wait_cancel(task: RunningTask) -> None:
                await task.cancel_requested.wait()
                outcome.cancelled = True
                tg.cancel_scope.cancel()

            async def thread_pid() -> None:
                """Poll for early PID from subprocess spawn before StartedEvent."""
                for _ in range(50):  # poll up to 5s
                    pid = getattr(runner, "last_pid", None)
                    if isinstance(pid, int):
                        edits.pid = pid
                        cs = getattr(runner, "current_stream", None)
                        if cs is not None:
                            edits.stream = cs
                        return
                    await anyio.sleep(0.1)

            tg.start_soon(run_runner)
            tg.start_soon(thread_pid)
            if running_task is not None:
                edits.cancel_event = running_task.cancel_requested
                tg.start_soon(wait_cancel, running_task)
    except BaseExceptionGroup as eg:
        # Unwrap ExceptionGroup from anyio TaskGroup so callers' `except Exception`
        # handlers can catch the real error.  Filter out cancellation exceptions
        # (normal TaskGroup shutdown) and re-raise the first real exception.
        cancel_exc = anyio.get_cancelled_exc_class()
        non_cancelled = [
            exc
            for exc in _flatten_exception_group(eg)
            if not isinstance(exc, cancel_exc)
        ]
        if non_cancelled:
            raise non_cancelled[0] from eg

    # Session completion summary
    duration = time.monotonic() - start_time
    event_count = edits.stream.event_count if edits.stream else 0
    # #333 Task 4b: render the per-reason suppression counter as a stable
    # comma-separated string (e.g. ``expected_wait:4,post_result:3``) so
    # log audits can grep without parsing nested JSON.
    suppression_counts = getattr(edits.stream, "stall_suppression_counts", None) or {}
    suppression_summary = ",".join(
        f"{k}:{v}" for k, v in sorted(suppression_counts.items())
    )
    logger.info(
        "session.summary",
        session_id=outcome.resume.value if outcome.resume else None,
        engine=runner.engine,
        duration_seconds=round(duration, 1),
        event_count=event_count,
        stall_warnings=edits._total_stall_warn_count,
        # #494: subprocess-health canary, separate from user-facing stall_warnings
        liveness_stalls=edits.stream.liveness_stalls if edits.stream else 0,
        peak_idle_seconds=round(edits._peak_idle, 1),
        last_event_type=edits.stream.last_event_type if edits.stream else None,
        cancelled=outcome.cancelled,
        ok=outcome.completed.ok if outcome.completed else None,
        stall_suppressions=suppression_summary,
    )
    if event_count == 0 and not outcome.cancelled:
        logger.warning(
            "session.summary.no_events",
            session_id=outcome.resume.value if outcome.resume else None,
            engine=runner.engine,
            duration_seconds=round(duration, 1),
        )

    return outcome


def sync_resume_token(
    tracker: ProgressTracker, resume: ResumeToken | None
) -> ResumeToken | None:
    resume = resume or tracker.resume
    tracker.set_resume(resume)
    return resume


async def send_result_message(
    cfg: ExecBridgeConfig,
    *,
    channel_id: ChannelId,
    reply_to: MessageRef,
    progress_ref: MessageRef | None,
    message: RenderedMessage,
    notify: bool,
    edit_ref: MessageRef | None,
    replace_ref: MessageRef | None = None,
    delete_tag: str = "final",
    thread_id: ThreadId | None = None,
) -> None:
    final_msg, edited = await _send_or_edit_message(
        cfg.transport,
        channel_id=channel_id,
        message=message,
        edit_ref=edit_ref,
        reply_to=reply_to,
        notify=notify,
        replace_ref=replace_ref,
        thread_id=thread_id,
    )
    if final_msg is None:
        return
    if (
        progress_ref is not None
        and (edit_ref is None or not edited)
        and replace_ref is None
    ):
        logger.debug(
            "transport.delete_message",
            channel_id=progress_ref.channel_id,
            message_id=progress_ref.message_id,
            tag=delete_tag,
        )
        await cfg.transport.delete(ref=progress_ref)


async def handle_message(
    cfg: ExecBridgeConfig,
    *,
    runner: Runner,
    incoming: IncomingMessage,
    resume_token: ResumeToken | None,
    context: RunContext | None = None,
    context_line: str | None = None,
    strip_resume_line: Callable[[str], bool] | None = None,
    running_tasks: RunningTasks | None = None,
    on_thread_known: Callable[[ResumeToken, anyio.Event], Awaitable[None]]
    | None = None,
    on_resume_failed: Callable[[ResumeToken], Awaitable[None]] | None = None,
    progress_ref: MessageRef | None = None,
    clock: Callable[[], float] = time.monotonic,
    _auto_continued_count: int = 0,
    _empty_resent_count: int = 0,
) -> None:
    logger.info(
        "handle.incoming",
        channel_id=incoming.channel_id,
        user_msg_id=incoming.message_id,
        resume=resume_token.value if resume_token else None,
        text=incoming.text,
    )

    # #632 (W2): a session marked quarantined (forced teardown after a
    # result — see claude.py's post-result subcountdown) may have a
    # dangling upstream turn and is unsafe to resume. Divert to a fresh
    # session proactively rather than waiting for another empty-result
    # anomaly. Runs on every entry, including recursive auto-resend/
    # auto-continue re-entries — a cheap in-memory dict lookup.
    if resume_token is not None:
        from .session_quarantine import get_quarantine_store

        try:
            _quarantined = get_quarantine_store().is_quarantined(
                runner.engine, resume_token.value
            )
        except Exception:  # noqa: BLE001 — a store failure must never
            # block message handling.
            logger.debug("session.quarantine_check_failed", exc_info=True)
            _quarantined = False
        if _quarantined:
            logger.info(
                "session.resume_diverted_fresh",
                engine=runner.engine,
                session_id=resume_token.value,
                reason="quarantined",
            )
            if on_resume_failed is not None:
                try:
                    await on_resume_failed(resume_token)
                except Exception:  # noqa: BLE001
                    logger.debug("session.clear_failed", exc_info=True)
            resume_token = None

    started_at = clock()
    is_resume_line = runner.is_resume_line
    resume_strip = strip_resume_line or is_resume_line
    runner_text = _strip_resume_lines(incoming.text, is_resume_line=resume_strip)
    runner_text = _apply_preamble(runner_text)

    progress_tracker = ProgressTracker(engine=runner.engine, clock=clock)
    # rc4 (#271): seed trigger source into meta so the footer renders it.
    # The engine's own StartedEvent.meta merges onto this via note_event.
    # rc6 (#271 follow-up): also render `at:<token>` from /at-scheduled runs
    # with the alarm-clock icon — semantically a one-shot delayed cron.
    if context is not None and context.trigger_source:
        icon = (
            "\N{ALARM CLOCK}"
            if context.trigger_source.startswith(("cron:", "at:"))
            else "\N{HIGH VOLTAGE SIGN}"
        )
        progress_tracker.meta = {"trigger": f"{icon} {context.trigger_source}"}

    # #269: refresh progress settings on the default presenter so edits
    # to [progress].max_actions / [progress].verbosity in untether.toml
    # apply on the next run. Per-chat /verbose overrides downstream of
    # _resolve_presenter() construct a fresh formatter from these refreshed
    # values, so the override picks up the new defaults too.
    progress_cfg = _load_progress_settings()
    refresh = getattr(cfg.presenter, "refresh_progress_settings", None)
    if callable(refresh):
        try:
            refresh(progress_cfg)
        except Exception:  # noqa: BLE001
            logger.debug("progress_settings.refresh_failed", exc_info=True)

    # Resolve effective presenter: check for per-chat verbose override
    effective_presenter = _resolve_presenter(cfg.presenter, incoming.channel_id)

    user_ref = MessageRef(
        channel_id=incoming.channel_id,
        message_id=incoming.message_id,
    )
    progress_state = await send_initial_progress(
        cfg,
        channel_id=incoming.channel_id,
        reply_to=user_ref,
        label="starting",
        tracker=progress_tracker,
        progress_ref=progress_ref,
        resume_formatter=runner.format_resume,
        context_line=context_line,
        thread_id=incoming.thread_id,
    )
    progress_ref = progress_state.ref

    edits = ProgressEdits(
        transport=cfg.transport,
        presenter=effective_presenter,
        channel_id=incoming.channel_id,
        progress_ref=progress_ref,
        tracker=progress_tracker,
        started_at=started_at,
        clock=clock,
        last_rendered=progress_state.last_rendered,
        resume_formatter=runner.format_resume,
        context_line=context_line,
        thread_id=incoming.thread_id,
        # #269: read live each run so edits to [progress].min_render_interval
        # apply on the next message without restart. cfg.min_render_interval
        # is the startup snapshot and only used as fallback if the live load
        # fails.
        min_render_interval=progress_cfg.min_render_interval,
    )

    # Apply watchdog settings to runner and edits
    watchdog = _load_watchdog_settings()
    if watchdog is not None:
        edits._stall_repeat_seconds = watchdog.stall_repeat_seconds
        edits._STALL_THRESHOLD_TOOL = watchdog.tool_timeout
        edits._STALL_THRESHOLD_MCP_TOOL = watchdog.mcp_tool_timeout
        edits._STALL_THRESHOLD_SUBAGENT = watchdog.subagent_timeout
        # Stuck-after-tool_result detector (#322)
        edits._stuck_after_tool_result_enabled = watchdog.detect_stuck_after_tool_result
        edits._stuck_after_tool_result_timeout = (
            watchdog.stuck_after_tool_result_timeout
        )
        edits._stuck_after_tool_result_recovery_enabled = (
            watchdog.stuck_after_tool_result_recovery_enabled
        )
        edits._stuck_after_tool_result_recovery_delay = (
            watchdog.stuck_after_tool_result_recovery_delay
        )
        # #481: bash grace window for the stall_bash_grace_suppressed branch.
        edits._bash_grace_seconds = watchdog.bash_grace_seconds
        if hasattr(runner, "_LIVENESS_TIMEOUT_SECONDS"):
            runner._LIVENESS_TIMEOUT_SECONDS = watchdog.liveness_timeout
        if hasattr(runner, "_stall_auto_kill"):
            runner._stall_auto_kill = watchdog.stall_auto_kill
        # #590: post-exit orphan sweep toggle.
        if hasattr(runner, "_reap_orphans"):
            runner._reap_orphans = watchdog.reap_orphans

    # #481: heartbeat tick cadence — drives the long-running-action elapsed
    # tail and the post-result closing-message poller. Read live so config
    # reloads pick up new values on the next message (matches min_render_interval
    # pattern above).
    edits._heartbeat_interval = progress_cfg.heartbeat_interval

    # #591: early final-answer delivery. The answer exists the moment the
    # CompletedEvent arrives, but the run generator may not return for up to
    # the post-result limbo window (MCP children holding the subprocess open
    # — historically up to 600 s of dead wall-clock, and one answer lost
    # entirely to a user /cancel of an already-completed run). This closure
    # performs the final assembly + send. It is invoked from inside
    # run_runner_with_cancel the moment a successful result arrives and —
    # when that early path did not run or failed — from the post-return
    # flow below. ``final_delivery["sent"]`` keeps the two paths idempotent.
    final_delivery = {"sent": False}
    # #596: set by _deliver_final when an empty-result no-op resume is detected
    # and eligible for a single automatic resend. Read by the post-return
    # auto-resend block below (independent of final_delivery["sent"], since the
    # "↻ retrying" notice IS delivered).
    empty_resume = {"pending": False}

    async def _deliver_final(
        completed: CompletedEvent, run_outcome: RunOutcome
    ) -> None:
        # Idempotence: the early path and the post-return path can both
        # reach here; only the first delivery wins.
        if final_delivery["sent"]:
            return
        run_ok = completed.ok
        run_error = completed.error
        elapsed_final = clock() - started_at

        # #510: ``completed.answer`` already has the #508 ExitPlanMode
        # plan-body prepend applied at the runner level (claude.py, on the
        # per-stream path). The previous bridge-side prepend read
        # ``runner.current_stream`` — a shared singleton on the ClaudeRunner
        # — and leaked one chat's plan body into another concurrent chat's
        # final answer.
        final_answer = completed.answer

        # Auto-clear broken session: if a resumed run failed with 0 turns,
        # clear the saved session so the next message starts fresh.
        if (
            run_ok is False
            and resume_token is not None
            and on_resume_failed is not None
        ):
            _num_turns = 0
            if completed.usage:
                _num_turns = completed.usage.get("num_turns", 0) or 0
            if _num_turns == 0:
                try:
                    await on_resume_failed(resume_token)
                    logger.info(
                        "session.auto_cleared",
                        engine=resume_token.engine,
                        resume=resume_token.value,
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("session.auto_clear_failed", exc_info=True)

        if run_ok is False and run_error:
            raw_error = str(run_error)
            hint = _get_error_hint(raw_error)
            if final_answer.strip():
                # Deduplicate: if the answer already starts with the error's
                # first line (common when runner sets both answer and error
                # from the same source, e.g. Claude Code subscription
                # limits), only append the diagnostic context and hint — not
                # the repeated summary.
                error_head = raw_error.split("\n", 1)[0].strip()
                answer_head = final_answer.strip().split("\n", 1)[0].strip()
                if error_head and error_head == answer_head:
                    _, _, remainder = raw_error.partition("\n")
                    parts: list[str] = [final_answer]
                    if hint:
                        parts.append(f"\N{ELECTRIC LIGHT BULB} {hint}")
                    if remainder.strip():
                        parts.append(f"```\n{remainder.strip()}\n```")
                    final_answer = "\n\n".join(parts)
                else:
                    if hint:
                        error_text = (
                            f"\N{ELECTRIC LIGHT BULB} {hint}\n\n```\n{raw_error}\n```"
                        )
                    else:
                        error_text = f"```\n{raw_error}\n```"
                    final_answer = f"{final_answer}\n\n{error_text}"
            else:
                if hint:
                    final_answer = (
                        f"\N{ELECTRIC LIGHT BULB} {hint}\n\n```\n{raw_error}\n```"
                    )
                else:
                    final_answer = f"```\n{raw_error}\n```"

        # #596: a 0-turn / $0 / empty-answer completion with ok=True is a
        # no-op resume (upstream: the resumed session considers itself
        # complete and emits an immediate empty result). Previously this
        # rendered a bare "error"-labelled header with no body — the user
        # got silence and had to re-nudge manually. Surface it explicitly.
        # Missing usage keys default to 1 (non-anomalous) — only an engine
        # that EXPLICITLY reported zero turns and zero API time qualifies;
        # engines without usage reporting never trip this.
        empty_result_anomaly = False
        if (
            run_ok is True
            and not run_outcome.cancelled
            and not final_answer.strip()
            and completed.usage
            and (completed.usage.get("num_turns", 1) or 0) == 0
            and (completed.usage.get("duration_api_ms", 1) or 0) == 0
        ):
            empty_result_anomaly = True
            logger.warning(
                "runner.empty_result",
                engine=runner.engine,
                resume=(completed.resume or run_outcome.resume).value
                if (completed.resume or run_outcome.resume)
                else None,
                was_resume=resume_token is not None,
            )
            # #596: auto-resend the original prompt once (same session)
            # instead of asking the user to re-nudge. Eligible only on a
            # resume with a non-empty original prompt, gated by the
            # single-shot ``_empty_resent_count`` so a retry that is ALSO
            # empty falls through to the manual-resend notice below.
            _resend_settings = _load_auto_continue_settings()
            if (
                _resend_settings.resend_empty_resume
                and resume_token is not None
                and _empty_resent_count < 1
                and bool(incoming.text and incoming.text.strip())
            ):
                empty_resume["pending"] = True
                final_answer = (
                    "\N{ANTICLOCKWISE DOWNWARDS AND UPWARDS OPEN CIRCLE ARROWS} "
                    "engine returned an empty result on resume — retrying your "
                    "message automatically…"
                )
            else:
                final_answer = (
                    "\N{WARNING SIGN} engine returned an empty result "
                    "(0 turns, no API work) — the resumed session may "
                    "consider itself complete. Resend your message, or "
                    "start fresh with /new."
                )

        # #632 (W2): a run that completed with real work proves the session
        # is healthy — clear any forced-teardown quarantine marker for the
        # session id it reports so a reused session id is never stuck
        # fresh-only forever. ``num_turns`` must be explicitly truthy: this
        # naturally excludes the #596 empty_result_anomaly zero-turn case
        # above, and engines that never report usage at all never clear.
        if (
            run_ok is True
            and not run_outcome.cancelled
            and (completed.usage or {}).get("num_turns", 0)
        ):
            _healthy_sid = completed.resume or run_outcome.resume
            if _healthy_sid is not None:
                try:
                    from .session_quarantine import get_quarantine_store

                    get_quarantine_store().clear(runner.engine, _healthy_sid.value)
                except Exception:  # noqa: BLE001 — a store failure must
                    # never break final-message delivery.
                    logger.debug("session.quarantine_clear_failed", exc_info=True)

        status = (
            "error"
            if run_ok is False
            # An auto-resend in flight is a transient retry, not an error.
            else (
                "done"
                if (empty_result_anomaly and empty_resume["pending"])
                else "error"
                if empty_result_anomaly
                else ("done" if final_answer.strip() else "error")
            )
        )
        resume_value = None
        final_resume = completed.resume or run_outcome.resume
        if final_resume is not None:
            resume_value = final_resume.value
        usage_log: dict[str, object] = {}
        if completed.usage:
            for key in ("num_turns", "total_cost_usd", "duration_api_ms"):
                val = completed.usage.get(key)
                if val is not None:
                    usage_log[key] = val
        logger.info(
            "runner.completed",
            ok=run_ok,
            error=run_error,
            answer_len=len(final_answer or ""),
            elapsed_s=round(elapsed_final, 2),
            action_count=progress_tracker.action_count,
            resume=resume_value,
            **usage_log,
        )
        # Record session stats for /stats command
        from .session_stats import record_run as _record_stats_run

        _record_stats_run(
            engine=runner.engine,
            actions=progress_tracker.action_count,
            duration_ms=int(elapsed_final * 1000),
            triggered=bool(context and context.trigger_source),
        )
        sync_resume_token(progress_tracker, final_resume)

        # Post-outline guidance: if the session was outline-pending (user
        # clicked "Pause & Outline Plan" but Claude Code ended the run
        # instead of calling ExitPlanMode), append resume instructions so
        # the user knows how to proceed.
        if runner.engine == "claude" and resume_value:
            from .runners.claude import _OUTLINE_PENDING

            if (
                resume_value in _OUTLINE_PENDING
                and final_answer
                and final_answer.strip()
            ):
                final_answer += (
                    "\n\n---\n"
                    "Plan outline complete. Resume and say "
                    '"approved" to proceed, or send feedback to revise.'
                )

        state = progress_tracker.snapshot(
            resume_formatter=runner.format_resume,
            context_line=context_line,
            meta_formatter=format_meta_line,
        )
        final_rendered = effective_presenter.render_final(
            state,
            elapsed_s=elapsed_final,
            status=status,
            answer=final_answer,
        )

        # Load footer display config (global defaults + per-chat overrides)
        footer_cfg = _load_footer_settings()
        from .runners.run_options import get_run_options

        _footer_run_opts = get_run_options()

        # Append run cost footer with inline budget suffix
        _show_cost = footer_cfg.show_api_cost
        if _footer_run_opts and _footer_run_opts.show_api_cost is not None:
            _show_cost = _footer_run_opts.show_api_cost
        _cost_alert_text, _cost_alert_obj = _check_cost_budget(completed.usage)
        if _show_cost and run_ok is not False:
            cost_line = _format_run_cost(completed.usage)
            if cost_line:
                budget_suffix = (
                    _format_budget_suffix(_cost_alert_obj)
                    if _cost_alert_obj is not None
                    else ""
                )
                final_rendered = RenderedMessage(
                    text=_insert_before_resume(
                        final_rendered.text,
                        f"\n\U0001f4b0{cost_line}{budget_suffix}",
                    ),
                    extra=final_rendered.extra,
                )
        elif _cost_alert_text:
            # Budget exceeded but cost display is off — show standalone alert
            final_rendered = RenderedMessage(
                text=_insert_before_resume(
                    final_rendered.text, f"\n{_cost_alert_text}"
                ),
                extra=final_rendered.extra,
            )

        # Append usage footer for Claude Code engine runs
        if runner.engine == "claude":
            _show_sub = footer_cfg.show_subscription_usage
            if (
                _footer_run_opts
                and _footer_run_opts.show_subscription_usage is not None
            ):
                _show_sub = _footer_run_opts.show_subscription_usage
            final_rendered = await _maybe_append_usage_footer(
                final_rendered, always_show=_show_sub
            )

        logger.debug(
            "handle.final.rendered",
            rendered=final_rendered.text,
            status=status,
        )

        can_edit_final = progress_ref is not None
        edit_ref = None if cfg.final_notify or not can_edit_final else progress_ref

        # #591: stop progress repaints BEFORE the send so a queued render
        # can't overwrite the final message. (The early path already set
        # this via note_final; this covers the post-return path.)
        edits._finalizing = True

        await send_result_message(
            cfg,
            channel_id=incoming.channel_id,
            reply_to=user_ref,
            progress_ref=progress_ref,
            message=final_rendered,
            notify=cfg.final_notify,
            edit_ref=edit_ref,
            replace_ref=progress_ref,
            delete_tag="final",
            thread_id=incoming.thread_id,
        )
        final_delivery["sent"] = True

        # Unregister progress persistence after the final message is sent.
        # Must happen AFTER send_result_message() so a crash between
        # delete_ephemeral() and here still has an orphan cleanup pointer.
        if progress_ref is not None and _PROGRESS_PERSISTENCE_PATH is not None:
            from .telegram.progress_persistence import unregister_progress

            session_key = f"{incoming.channel_id}:{progress_ref.message_id}"
            unregister_progress(_PROGRESS_PERSISTENCE_PATH, session_key)

    running_task: RunningTask | None = None
    if running_tasks is not None and progress_ref is not None:
        running_task = RunningTask(context=context)
        running_tasks[progress_ref] = running_task

    cancel_exc_type = anyio.get_cancelled_exc_class()
    edits_scope = anyio.CancelScope()

    async def run_edits() -> None:
        try:
            with edits_scope:
                await edits.run()
        except cancel_exc_type:
            # Edits are best-effort; cancellation should not bubble into the task group.
            return

    outcome = RunOutcome()
    error: Exception | None = None

    async with anyio.create_task_group() as tg:
        if progress_ref is not None:
            tg.start_soon(run_edits)

        try:
            outcome = await run_runner_with_cancel(
                runner,
                prompt=runner_text,
                resume_token=resume_token,
                edits=edits,
                running_task=running_task,
                on_thread_known=on_thread_known,
                channel_id=incoming.channel_id,
                on_completed=_deliver_final,
            )
        except Exception as exc:
            error = exc
            logger.exception(
                "handle.runner_failed",
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
        finally:
            if running_task is not None and running_tasks is not None:
                running_task.done.set()
                if progress_ref is not None:
                    running_tasks.pop(progress_ref, None)
            if not outcome.cancelled and error is None:
                # Give pending progress edits a chance to flush if they're ready.
                await anyio.lowlevel.checkpoint()
            # Clean up any remaining ephemeral notification messages.
            await edits.delete_ephemeral()
            edits_scope.cancel()

    elapsed = clock() - started_at

    if error is not None and final_delivery["sent"]:
        # #591: the answer was already delivered before the teardown error —
        # don't overwrite the delivered final message with an error render.
        logger.warning(
            "handle.error_after_final_delivery",
            error=str(error),
            error_type=error.__class__.__name__,
            elapsed_s=round(elapsed, 2),
        )
        return

    if error is not None:
        sync_resume_token(progress_tracker, outcome.resume)
        err_body = _format_error(error)
        hint = _get_error_hint(err_body)
        if hint:
            err_body = f"\N{ELECTRIC LIGHT BULB} {hint}\n\n```\n{err_body}\n```"
        else:
            err_body = f"```\n{err_body}\n```"
        state = progress_tracker.snapshot(
            resume_formatter=runner.format_resume,
            context_line=context_line,
            meta_formatter=format_meta_line,
        )
        final_rendered = effective_presenter.render_final(
            state,
            elapsed_s=elapsed,
            status="error",
            answer=err_body,
        )

        # Append usage footer for Claude Code engine runs (even on error)
        if runner.engine == "claude":
            footer_cfg = _load_footer_settings()
            from .runners.run_options import get_run_options

            _err_run_opts = get_run_options()
            _show_sub = footer_cfg.show_subscription_usage
            if _err_run_opts and _err_run_opts.show_subscription_usage is not None:
                _show_sub = _err_run_opts.show_subscription_usage
            final_rendered = await _maybe_append_usage_footer(
                final_rendered, always_show=_show_sub
            )

        logger.debug(
            "handle.error.rendered",
            error=err_body,
            rendered=final_rendered.text,
        )
        await send_result_message(
            cfg,
            channel_id=incoming.channel_id,
            reply_to=user_ref,
            progress_ref=progress_ref,
            message=final_rendered,
            notify=False,
            edit_ref=progress_ref,
            replace_ref=progress_ref,
            delete_tag="error",
            thread_id=incoming.thread_id,
        )
        return

    if outcome.cancelled and final_delivery["sent"]:
        # #591: the run completed and its answer was delivered before the
        # user's /cancel landed (the channelo msg-5815 shape — a cancel of
        # an already-done run). The cancel only tears the subprocess down;
        # the delivered answer must not be replaced by a "cancelled" render.
        logger.info(
            "handle.cancelled_after_delivery",
            resume=outcome.resume.value if outcome.resume else None,
            elapsed_s=elapsed,
        )
        return

    if outcome.cancelled:
        resume = sync_resume_token(progress_tracker, outcome.resume)
        logger.info(
            "handle.cancelled",
            resume=resume.value if resume else None,
            elapsed_s=elapsed,
        )
        state = progress_tracker.snapshot(
            resume_formatter=runner.format_resume,
            context_line=context_line,
            meta_formatter=format_meta_line,
        )
        final_rendered = effective_presenter.render_progress(
            state,
            elapsed_s=elapsed,
            label="`cancelled`",
        )
        await send_result_message(
            cfg,
            channel_id=incoming.channel_id,
            reply_to=user_ref,
            progress_ref=progress_ref,
            message=final_rendered,
            notify=False,
            edit_ref=progress_ref,
            replace_ref=progress_ref,
            delete_tag="cancel",
            thread_id=incoming.thread_id,
        )
        return

    if outcome.completed is None:
        raise RuntimeError("runner finished without a completed event")

    completed = outcome.completed
    run_ok = completed.ok

    # --- Auto-resend: #596 empty-result no-op resume / #631 (W1) quarantine-and-fresh ---
    # _deliver_final already delivered the "↻ retrying automatically…" notice
    # (early, ~5s in). Now that the run generator has fully returned (the
    # empty run's subprocess is done), resend the ORIGINAL prompt once.
    # #631: the resumed session may be POISONED — an upstream dangling turn
    # left over from a forced teardown will keep returning empty 0-turn
    # resumes if resumed again. When empty_resume_fresh is on (default),
    # clear the stored token, quarantine the poisoned session id so it is
    # never resumed again, and re-run the ORIGINAL prompt as a FRESH session
    # (resume=None). When the flag is off, preserve the exact #596
    # same-session resend behaviour. Single-shot via _empty_resent_count;
    # mutually exclusive with auto-continue (that fires only when there was
    # no result at all).
    if empty_resume["pending"] and _empty_resent_count < 1:
        _er_settings = _load_auto_continue_settings()
        # Fall back to the original resume_token so a completion that omits a
        # resume value never silently starts a FRESH session by accident.
        _poison = completed.resume or outcome.resume or resume_token
        if _er_settings.empty_resume_fresh and _poison is not None:
            # #631 W1: clear the stored session token and quarantine the
            # poisoned session id, then retry as a FRESH session.
            if on_resume_failed is not None:
                try:
                    await on_resume_failed(_poison)
                except Exception:  # noqa: BLE001
                    logger.debug("session.clear_failed", exc_info=True)
            from .session_quarantine import get_quarantine_store

            get_quarantine_store().quarantine(
                runner.engine,
                _poison.value,
                reason="empty_zero_turn_resume",
            )
            logger.warning(
                "session.auto_resend_fresh",
                old_session_id=_poison.value,
                engine=runner.engine,
                attempt=_empty_resent_count + 1,
            )
            _er_resume = None
        else:
            # Legacy same-session path (flag off): preserve #596 behaviour
            # byte-for-byte.
            _er_resume = _poison
            logger.warning(
                "session.auto_resend_empty",
                session_id=_er_resume.value if _er_resume else None,
                engine=runner.engine,
                attempt=_empty_resent_count + 1,
            )
        await handle_message(
            cfg,
            runner=runner,
            incoming=IncomingMessage(
                channel_id=incoming.channel_id,
                message_id=incoming.message_id,
                text=incoming.text,
                reply_to=incoming.reply_to,
                thread_id=incoming.thread_id,
            ),
            resume_token=_er_resume,
            context=context,
            context_line=context_line,
            strip_resume_line=strip_resume_line,
            running_tasks=running_tasks,
            on_thread_known=on_thread_known,
            on_resume_failed=on_resume_failed,
            clock=clock,
            # Carry BOTH recovery counters so an alternating empty-resume ↔
            # auto-continue chain can't reset the other guard and loop.
            _auto_continued_count=_auto_continued_count,
            _empty_resent_count=_empty_resent_count + 1,
        )
        return
    # --- End auto-resend ---

    # --- Auto-continue: mitigate Claude Code bug #34142/#30333 ---
    # When Claude Code's turn state machine incorrectly ends a session
    # after receiving tool results (last JSONL event is "user" type),
    # auto-resume so the user doesn't have to manually continue.
    ac_settings = _load_auto_continue_settings()
    _ac_resume = completed.resume or outcome.resume
    _ac_last_event = edits.stream.last_event_type if edits.stream else None
    _ac_proc_rc = edits.stream.proc_returncode if edits.stream else None
    # #591: a run whose answer was already delivered can never need the
    # auto-continue salvage (belt-and-braces — _should_auto_continue already
    # excludes last_event_type == "result").
    if (
        ac_settings.enabled
        and not final_delivery["sent"]
        and _should_auto_continue(
            last_event_type=_ac_last_event,
            engine=runner.engine,
            cancelled=outcome.cancelled,
            resume_value=_ac_resume.value if _ac_resume else None,
            auto_continued_count=_auto_continued_count,
            max_retries=ac_settings.max_retries,
            proc_returncode=_ac_proc_rc,
        )
    ):
        logger.warning(
            "session.auto_continue",
            session_id=_ac_resume.value if _ac_resume else None,
            engine=runner.engine,
            last_event_type=_ac_last_event,
            attempt=_auto_continued_count + 1,
            max_retries=ac_settings.max_retries,
        )

        # #551 Tier 0: deliver outbox files from subprocess 1 BEFORE
        # subprocess 2 spawns. Without this, any files the agent wrote
        # to ``.untether-outbox/`` during the stuck-after-tool-results
        # window are orphaned (subprocess 2 starts fresh and the
        # original outbox is never scanned). ~3.6% silent loss observed
        # on lba-1 before this fix. Failure to deliver must NOT block
        # auto-continue itself \u2014 the recovery is more important than
        # any single batch of files.
        if cfg.send_file is not None and cfg.outbox_config is not None:
            from .telegram.outbox_delivery import deliver_outbox_files
            from .utils.paths import get_run_base_dir

            _run_root = get_run_base_dir()
            if _run_root is not None:
                _oc = cfg.outbox_config
                try:
                    result = await deliver_outbox_files(
                        send_file=cfg.send_file,
                        channel_id=incoming.channel_id,
                        thread_id=incoming.thread_id,
                        reply_to_msg_id=user_ref.message_id,
                        run_root=_run_root,
                        outbox_dir=_oc.outbox_dir,
                        deny_globs=_oc.deny_globs,
                        max_download_bytes=_oc.max_download_bytes,
                        max_files=_oc.outbox_max_files,
                        cleanup=True,  # subprocess 2 starts fresh
                        deliver_directories=getattr(
                            _oc, "outbox_deliver_directories", "off"
                        ),
                    )
                    logger.info(
                        "outbox.delivered_pre_auto_continue",
                        sent=len(result.sent),
                        skipped=len(result.skipped),
                        cleaned=result.cleaned,
                    )
                    # #524 rc20 follow-up: surface skipped items from the
                    # pre-auto-continue scan too. Without this, agents that
                    # write a directory (e.g. ``guides/``) and then hit the
                    # stuck-after-tool-results recovery never tell the user
                    # the deliverable existed — the directory is left in
                    # place for subprocess 2 to re-find, but the user sees
                    # nothing in chat about the first attempt.
                    await _surface_outbox_skipped(
                        cfg,
                        incoming,
                        user_ref,
                        result.skipped,
                        _oc,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "outbox.auto_continue_delivery_failed", exc_info=True
                    )

        # #551 Tier 1: reworded notice signals recovery, not failure.
        # The \ud83d\udd01 prefix distinguishes auto-resume from a fresh start
        # and discourages users from /cancel-ing the salvage.
        notice = _format_auto_continue_notice(_auto_continued_count)
        notice_msg = RenderedMessage(text=notice, extra={})
        await cfg.transport.send(
            channel_id=incoming.channel_id,
            message=notice_msg,
            options=SendOptions(
                reply_to=user_ref,
                notify=True,
                thread_id=incoming.thread_id,
            ),
        )
        await handle_message(
            cfg,
            runner=runner,
            incoming=IncomingMessage(
                channel_id=incoming.channel_id,
                message_id=incoming.message_id,
                text="continue",
                reply_to=incoming.reply_to,
                thread_id=incoming.thread_id,
            ),
            resume_token=_ac_resume,
            context=context,
            context_line=context_line,
            strip_resume_line=strip_resume_line,
            running_tasks=running_tasks,
            on_thread_known=on_thread_known,
            on_resume_failed=on_resume_failed,
            clock=clock,
            _auto_continued_count=_auto_continued_count + 1,
            # Carry the empty-resume guard so it can't be reset by an
            # interleaved auto-continue (see #596 auto-resend).
            _empty_resent_count=_empty_resent_count,
        )
        return
    # --- End auto-continue ---

    # #591: deliver the final answer unless the early path already did.
    if not final_delivery["sent"]:
        await _deliver_final(completed, outcome)

    # Deliver outbox files (agent-initiated file delivery).
    # #524 rc20 follow-up: surface skipped items even when run_ok is False.
    # Delivery of *sent* files still requires a successful run (failures
    # may leave the outbox in a partially-written state), but the user
    # should always learn what the agent intended to send.
    if cfg.send_file is not None and cfg.outbox_config is not None:
        from .telegram.outbox_delivery import (
            OutboxResult,
            deliver_outbox_files,
            scan_outbox,
        )
        from .utils.paths import get_run_base_dir

        _run_root = get_run_base_dir()
        if _run_root is not None:
            _oc = cfg.outbox_config
            _outbox_result: OutboxResult | None = None
            if run_ok is not False:
                try:
                    _outbox_result = await deliver_outbox_files(
                        send_file=cfg.send_file,
                        channel_id=incoming.channel_id,
                        thread_id=incoming.thread_id,
                        reply_to_msg_id=user_ref.message_id,
                        run_root=_run_root,
                        outbox_dir=_oc.outbox_dir,
                        deny_globs=_oc.deny_globs,
                        max_download_bytes=_oc.max_download_bytes,
                        max_files=_oc.outbox_max_files,
                        cleanup=_oc.outbox_cleanup,
                        deliver_directories=getattr(
                            _oc, "outbox_deliver_directories", "off"
                        ),
                    )
                except Exception:  # noqa: BLE001
                    logger.warning("outbox.delivery_failed", exc_info=True)
                    _outbox_result = None
            else:
                # Failed run: skip file delivery but still scan so the user
                # gets the 📎 Outbox skipped notice for any directory or
                # blocked entry the agent left behind.
                try:
                    _, _failed_skipped = scan_outbox(
                        _run_root,
                        outbox_dir=_oc.outbox_dir,
                        deny_globs=_oc.deny_globs,
                        max_download_bytes=_oc.max_download_bytes,
                        max_files=_oc.outbox_max_files,
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("outbox.failed_run_scan_error", exc_info=True)
                    _failed_skipped = []
                _outbox_result = OutboxResult(skipped=_failed_skipped)

            if _outbox_result is not None:
                await _surface_outbox_skipped(
                    cfg,
                    incoming,
                    user_ref,
                    _outbox_result.skipped,
                    _oc,
                )
