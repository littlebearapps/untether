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
from .runner import Runner
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
    "Every response that completes work MUST end with a structured summary:\n"
    "  ## Summary\n"
    "  ### Completed\n"
    "  - [What was done, with specific file paths and line numbers where relevant]\n"
    "  - [Key decisions made and why]\n"
    "  ### Plan/Document Created (if applicable)\n"
    "  - [Path and concise summary of any plan, design doc, or document created — "
    "the user cannot easily open files from Telegram]\n"
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
        from .telegram.commands.usage import (
            _time_until,
            fetch_claude_usage,
            format_usage_compact,
        )

        data = await fetch_claude_usage()

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
    if turns:
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
        # Stuck-after-tool_result detector (#322). Instance overrides of the
        # class-level defaults, populated from WatchdogSettings in
        # handle_message.
        self._stuck_after_tool_result_enabled: bool = False
        self._stuck_after_tool_result_timeout: float = 300.0
        self._stuck_after_tool_result_recovery_enabled: bool = True
        self._stuck_after_tool_result_recovery_delay: float = 60.0
        self._stuck_state: _StuckAfterToolResultState | None = None
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

    async def _stall_monitor(self) -> None:
        """Periodically check for event stalls, log diagnostics, and notify."""
        from .utils.proc_diag import (
            collect_proc_diag,
            is_cpu_active,
            is_tree_cpu_active,
        )

        while True:
            await anyio.sleep(self._stall_check_interval)
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

            last_action = self._last_action_summary()

            recent = list(self.stream.recent_events) if self.stream else []
            stderr_hint = (
                self.stream.stderr_capture[-3:]
                if self.stream and self.stream.stderr_capture
                else None
            )

            logger.warning(
                "progress_edits.stall_detected",
                channel_id=self.channel_id,
                seconds_since_last_event=round(elapsed, 1),
                last_event_seq=self.event_seq,
                stall_warn_count=self._stall_warn_count,
                pid=self.pid,
                last_action=last_action,
                last_event_type=(self.stream.last_event_type if self.stream else None),
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
            )

            # Auto-cancel: dead process, no-PID zombie, or absolute cap
            auto_cancel_reason: str | None = None
            if diag and diag.alive is False:
                auto_cancel_reason = "process_dead"
            elif (
                self.pid is None
                and self.event_seq == 0
                and self._stall_warn_count >= self._STALL_MAX_WARNINGS_NO_PID
            ):
                auto_cancel_reason = "no_pid_no_events"
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

            if cpu_active is True and not frozen_escalate and not main_sleeping:
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
                    _tool_name = None
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
                # (no tool identified, cpu not active, not MCP/frozen)
                _genuinely_stuck = (
                    not mcp_hung
                    and not frozen_escalate
                    and mcp_server is None
                    and threshold_reason != "active_children"
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

        # Tier 2: adapter-kill recovery (once per episode)
        if (
            self._stuck_after_tool_result_recovery_enabled
            and not state.recovery_attempted
        ):
            killed = await self._try_recover_mcp_adapter(diag)
            state.recovery_attempted = True
            state.recovery_attempted_at = now
            logger.warning(
                "progress_edits.stuck_after_tool_result.recovery_attempt",
                channel_id=self.channel_id,
                pid=self.pid,
                killed_pids=killed,
                mcp_server=mcp_server,
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
                state, elapsed_s=now - self.started_at, label=self.label
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
    _STALL_THRESHOLD_APPROVAL: float = 1800.0  # 30 minutes when waiting for approval
    _STALL_MAX_WARNINGS: int = 10  # absolute cap
    _STALL_MAX_WARNINGS_NO_PID: int = 3  # aggressive cap when pid=None + no events
    _TCP_ACTIVE_THRESHOLD: int = 20  # TCP connections above this suggest active work

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
) -> RunOutcome:
    outcome = RunOutcome()
    start_time = time.monotonic()
    try:
        async with anyio.create_task_group() as tg:

            async def run_runner() -> None:
                try:
                    async for evt in runner.run(prompt, resume_token):
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
                            outcome.resume = evt.resume or outcome.resume
                            outcome.completed = evt
                        # A3: Record events for /export
                        _record_export_event(evt, outcome.resume, channel_id=channel_id)
                        await edits.on_event(evt)
                finally:
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
    logger.info(
        "session.summary",
        session_id=outcome.resume.value if outcome.resume else None,
        engine=runner.engine,
        duration_seconds=round(duration, 1),
        event_count=event_count,
        stall_warnings=edits._total_stall_warn_count,
        peak_idle_seconds=round(edits._peak_idle, 1),
        last_event_type=edits.stream.last_event_type if edits.stream else None,
        cancelled=outcome.cancelled,
        ok=outcome.completed.ok if outcome.completed else None,
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
) -> None:
    logger.info(
        "handle.incoming",
        channel_id=incoming.channel_id,
        user_msg_id=incoming.message_id,
        resume=resume_token.value if resume_token else None,
        text=incoming.text,
    )
    started_at = clock()
    is_resume_line = runner.is_resume_line
    resume_strip = strip_resume_line or is_resume_line
    runner_text = _strip_resume_lines(incoming.text, is_resume_line=resume_strip)
    runner_text = _apply_preamble(runner_text)

    progress_tracker = ProgressTracker(engine=runner.engine)
    # rc4 (#271): seed trigger source into meta so the footer renders it.
    # The engine's own StartedEvent.meta merges onto this via note_event.
    if context is not None and context.trigger_source:
        icon = (
            "\N{ALARM CLOCK}"
            if context.trigger_source.startswith("cron:")
            else "\N{HIGH VOLTAGE SIGN}"
        )
        progress_tracker.meta = {"trigger": f"{icon} {context.trigger_source}"}

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
        min_render_interval=cfg.min_render_interval,
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
        if hasattr(runner, "_LIVENESS_TIMEOUT_SECONDS"):
            runner._LIVENESS_TIMEOUT_SECONDS = watchdog.liveness_timeout
        if hasattr(runner, "_stall_auto_kill"):
            runner._stall_auto_kill = watchdog.stall_auto_kill

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
    run_error = completed.error

    # --- Auto-continue: mitigate Claude Code bug #34142/#30333 ---
    # When Claude Code's turn state machine incorrectly ends a session
    # after receiving tool results (last JSONL event is "user" type),
    # auto-resume so the user doesn't have to manually continue.
    ac_settings = _load_auto_continue_settings()
    _ac_resume = completed.resume or outcome.resume
    _ac_last_event = edits.stream.last_event_type if edits.stream else None
    _ac_proc_rc = edits.stream.proc_returncode if edits.stream else None
    if ac_settings.enabled and _should_auto_continue(
        last_event_type=_ac_last_event,
        engine=runner.engine,
        cancelled=outcome.cancelled,
        resume_value=_ac_resume.value if _ac_resume else None,
        auto_continued_count=_auto_continued_count,
        max_retries=ac_settings.max_retries,
        proc_returncode=_ac_proc_rc,
    ):
        logger.warning(
            "session.auto_continue",
            session_id=_ac_resume.value if _ac_resume else None,
            engine=runner.engine,
            last_event_type=_ac_last_event,
            attempt=_auto_continued_count + 1,
            max_retries=ac_settings.max_retries,
        )
        notice = (
            "\u26a0\ufe0f Auto-continuing \u2014 "
            "Claude stopped before processing tool results"
        )
        if _auto_continued_count > 0:
            notice += f" (attempt {_auto_continued_count + 1})"
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
        )
        return
    # --- End auto-continue ---

    final_answer = completed.answer

    # If there's a plan outline stored in a synthetic warning action,
    # prepend it to the final answer so the user can read it.
    # (The progress message that showed the outline gets replaced by
    # the final message, so the outline would otherwise be lost.)
    _outline_prefix = "Plan outline:\n"
    for _action_state in progress_tracker.snapshot(
        resume_formatter=runner.format_resume,
        context_line=None,
    ).actions:
        _title = _action_state.action.title or ""
        if _action_state.action.kind == "warning" and _title.startswith(
            _outline_prefix
        ):
            _outline_body = _title[len(_outline_prefix) :]
            if _outline_body.strip():
                final_answer = f"{_outline_body}\n\n{final_answer}"
            break

    # Auto-clear broken session: if a resumed run failed with 0 turns,
    # clear the saved session so the next message starts fresh.
    if run_ok is False and resume_token is not None and on_resume_failed is not None:
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
            # Deduplicate: if the answer already starts with the error's first
            # line (common when runner sets both answer and error from the same
            # source, e.g. Claude Code subscription limits), only append the
            # diagnostic context and hint — not the repeated summary.
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

    status = (
        "error" if run_ok is False else ("done" if final_answer.strip() else "error")
    )
    resume_value = None
    resume_token = completed.resume or outcome.resume
    if resume_token is not None:
        resume_value = resume_token.value
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
        elapsed_s=round(elapsed, 2),
        action_count=progress_tracker.action_count,
        resume=resume_value,
        **usage_log,
    )
    # Record session stats for /stats command
    from .session_stats import record_run as _record_stats_run

    _record_stats_run(
        engine=runner.engine,
        actions=progress_tracker.action_count,
        duration_ms=int(elapsed * 1000),
    )
    sync_resume_token(progress_tracker, completed.resume or outcome.resume)

    # Post-outline guidance: if the session was outline-pending (user clicked
    # "Pause & Outline Plan" but Claude Code ended the run instead of calling
    # ExitPlanMode), append resume instructions so the user knows how to proceed.
    if runner.engine == "claude" and resume_value:
        from .runners.claude import _OUTLINE_PENDING

        if resume_value in _OUTLINE_PENDING and final_answer and final_answer.strip():
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
        elapsed_s=elapsed,
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
            text=_insert_before_resume(final_rendered.text, f"\n{_cost_alert_text}"),
            extra=final_rendered.extra,
        )

    # Append usage footer for Claude Code engine runs
    if runner.engine == "claude":
        _show_sub = footer_cfg.show_subscription_usage
        if _footer_run_opts and _footer_run_opts.show_subscription_usage is not None:
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

    # Unregister progress persistence after the final message is sent.
    # Must happen AFTER send_result_message() so a crash between
    # delete_ephemeral() and here still has an orphan cleanup pointer.
    if progress_ref is not None and _PROGRESS_PERSISTENCE_PATH is not None:
        from .telegram.progress_persistence import unregister_progress

        session_key = f"{incoming.channel_id}:{progress_ref.message_id}"
        unregister_progress(_PROGRESS_PERSISTENCE_PATH, session_key)

    # Deliver outbox files (agent-initiated file delivery)
    if (
        cfg.send_file is not None
        and cfg.outbox_config is not None
        and run_ok is not False
    ):
        from .telegram.outbox_delivery import deliver_outbox_files
        from .utils.paths import get_run_base_dir

        _run_root = get_run_base_dir()
        if _run_root is not None:
            _oc = cfg.outbox_config
            try:
                await deliver_outbox_files(
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
                )
            except Exception:  # noqa: BLE001
                logger.warning("outbox.delivery_failed", exc_info=True)
