"""
Updated ClaudeRunner with PTY support for control channel.

This replaces the existing claude.py with PTY-based stdin handling
to prevent deadlock when keeping stdin open for control responses.
"""

from __future__ import annotations

import contextlib
import json
import os
import pty
import re
import shutil
import subprocess as subprocess_module
import time
import tty
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

import anyio
import msgspec

from ..backends import EngineBackend, EngineConfig
from ..config import ConfigError
from ..events import EventFactory
from ..logging import get_logger
from ..model import (
    Action,
    ActionKind,
    CompletedEvent,
    EngineId,
    ResumeToken,
    StartedEvent,
    UntetherEvent,
)
from ..runner import (
    JsonlStreamState,
    JsonlSubprocessRunner,
    ResumeTokenMixin,
    Runner,
    _rc_label,
    _session_label,
    _stderr_excerpt,
)
from ..schemas import claude as claude_schema
from ..settings import load_settings_if_exists
from ..utils.env_audit import audit_proc_env
from ..utils.paths import get_run_base_dir
from ..utils.streams import drain_stderr
from ..utils.subprocess import manage_subprocess, redact_env_i_args, wrap_with_env_i
from .run_options import get_run_options
from .tool_actions import tool_input_path, tool_kind_and_title

logger = get_logger(__name__)

ENGINE: EngineId = "claude"
DEFAULT_ALLOWED_TOOLS = ["Bash", "Read", "Edit", "Write"]

_RESUME_RE = re.compile(
    r"(?im)^\s*`?claude\s+(?:--resume|-r)\s+(?P<token>[^`\s]+)`?\s*$"
)

# Flags that Untether sets on every spawn (stream-json I/O, resume tokens,
# permission wiring). A user-supplied copy in `[claude].extra_args` would
# either duplicate the arg or collide with Untether's expected value, so
# `build_runner` rejects any entry matching this set or one of the equivalent
# `key=value` prefixes below. Mirrors `codex._EXEC_ONLY_FLAGS` (#407).
_RESERVED_FLAGS: frozenset[str] = frozenset(
    {
        "-p",
        "--print",
        "--output-format",
        "--input-format",
        "--resume",
        "-r",
        "--continue",
        "-c",
        "--permission-mode",
        "--permission-prompt-tool",
    }
)
_RESERVED_PREFIXES: tuple[str, ...] = (
    "--output-format=",
    "--input-format=",
    "--resume=",
    "--permission-mode=",
    "--permission-prompt-tool=",
)


def _find_reserved_flag(extra_args: list[str]) -> str | None:
    for arg in extra_args:
        if arg in _RESERVED_FLAGS:
            return arg
        for prefix in _RESERVED_PREFIXES:
            if arg.startswith(prefix):
                return arg
    return None


def _load_env_extras() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """#409: read [security] env_extra_allow / env_extra_prefix_allow.

    Best-effort — config errors must never block a run, so we swallow
    them and fall back to the built-in defaults. Returns
    ``(extra_exact, extra_prefix)``.
    """
    from ..settings import load_settings_if_exists

    try:
        result = load_settings_if_exists()
        if result is None:
            return ((), ())
        settings, _ = result
        return (
            tuple(settings.security.env_extra_allow),
            tuple(settings.security.env_extra_prefix_allow),
        )
    except Exception:  # noqa: BLE001 — never let config errors block a run
        return ((), ())


# Phase 2: Global registry for active ClaudeRunner instances
# Keyed by session_id, stores (runner_instance, timestamp)
_ACTIVE_RUNNERS: dict[str, tuple[ClaudeRunner, float]] = {}

# Phase 2: Global registry mapping session_id -> process stdin
# Stored separately from _ACTIVE_RUNNERS to support concurrent sessions
# on the same runner instance (runner._proc_stdin would be overwritten).
_SESSION_STDIN: dict[str, Any] = {}

# Phase 2: Global registry mapping request_id -> session_id
# This allows callbacks to find the right runner instance
_REQUEST_TO_SESSION: dict[str, str] = {}

# Phase 2: Global registry mapping request_id -> original tool input
# Claude Code CLI requires updatedInput in can_use_tool responses
_REQUEST_TO_INPUT: dict[str, dict[str, Any]] = {}

# Phase 2: Global registry mapping request_id -> tool_name
# Used by claude_control.py to send tool-specific deny messages
_REQUEST_TO_TOOL_NAME: dict[str, str] = {}

# Recently handled request_ids (prevents duplicate callback warnings).
# #197: previously a plain set cleared wholesale when len > 100, which opened
# a small window where duplicate callbacks could slip through as "not found"
# rather than being recognised as duplicates.  Now an LRU OrderedDict that
# evicts oldest-first at _HANDLED_REQUESTS_MAX entries.
_HANDLED_REQUESTS_MAX = 200
_HANDLED_REQUESTS: OrderedDict[str, None] = OrderedDict()

# Discuss cooldown: session_id -> (timestamp, deny_count)
# When user clicks "Pause & Outline Plan", this tracks when the denial was sent
# so rapid ExitPlanMode retries can be auto-denied with escalating messages.
_DISCUSS_COOLDOWN: dict[str, tuple[float, int]] = {}

# Discuss approval: session_ids where user approved the plan via post-outline buttons.
# When Claude Code next calls ExitPlanMode, it will be auto-approved.
_DISCUSS_APPROVED: set[str] = set()

# Plan-bypass set: session_ids where the user has approved at least one
# plan-gated tool (ExitPlanMode, Edit, Write, or Bash). After the first
# approval, subsequent diff_preview tools auto-approve instead of re-prompting
# — the user has already reviewed code for this session (#283, #369).
_PLAN_EXIT_APPROVED: set[str] = set()

# Tools guarded by the diff_preview approval gate. Mirrors the tools an
# approved plan unlocks: approving any of these populates _PLAN_EXIT_APPROVED
# for the session so subsequent diff_preview tools auto-approve (#369).
_DIFF_PREVIEW_TOOLS: frozenset[str] = frozenset({"Edit", "Write", "Bash"})

# Sessions where "Pause & Outline Plan" was clicked and we're waiting for outline text.
# StreamTextBlock handler checks this to emit visible note events in the progress message.
_OUTLINE_PENDING: set[str] = set()

# Minimum characters for an outline to be considered "substantial".
_OUTLINE_MIN_CHARS = 200

# A1: Pending AskUserQuestion requests: request_id -> (channel_id, question text)
# When Claude Code asks a question, the user can reply via Telegram text.
# Scoped by channel_id to prevent cross-chat message stealing (#144).
_PENDING_ASK_REQUESTS: dict[str, tuple[int, str]] = {}


def is_session_alive(session_id: str) -> bool:
    """Return True if a Claude subprocess for ``session_id`` is currently
    running and has an open stdin (registered in :data:`_SESSION_STDIN`).

    Used by :mod:`untether.loop_scheduler` (#289) before firing a loop
    iteration, to avoid racing a still-live subprocess that may be parked
    on a control_request awaiting Telegram button input.  Once the
    subprocess exits its registry entry is cleared in :class:`ClaudeRunner`'s
    ``run_impl`` finally block.
    """
    return session_id in _SESSION_STDIN


@dataclass(slots=True)
class AskQuestionState:
    """Tracks multi-question AskUserQuestion flow state."""

    request_id: str
    channel_id: int
    questions: list[dict[str, Any]]
    current_index: int = 0
    answers: dict[str, str] = field(default_factory=dict)
    awaiting_text: bool = False  # True when "Other" was clicked


# Active AskUserQuestion flows: request_id -> AskQuestionState
_ASK_QUESTION_FLOWS: dict[str, AskQuestionState] = {}
CONTROL_REQUEST_TIMEOUT_SECONDS: float = 300.0  # 5 minutes
DISCUSS_COOLDOWN_BASE_SECONDS: float = 30.0
DISCUSS_COOLDOWN_MAX_SECONDS: float = 120.0

_DISCUSS_ESCALATION_MESSAGE = (
    "REJECTED — your ExitPlanMode call was automatically blocked because you have not "
    "written enough visible text yet.\n\n"
    "The user is waiting to read your plan outline on their phone. Write it NOW as your "
    "next assistant message — at least 15 lines of visible text covering files, changes, "
    "order, and key decisions.\n\n"
    "Do NOT call ExitPlanMode again until you have written the outline. "
    "Any further calls without visible outline text will also be rejected."
)


@dataclass(slots=True)
class ClaudeStreamState:
    factory: EventFactory = field(default_factory=lambda: EventFactory(ENGINE))
    pending_actions: dict[str, Action] = field(default_factory=dict)
    last_assistant_text: str | None = None
    note_seq: int = 0
    # Phase 2: Control request tracking
    pending_control_requests: dict[
        str, tuple[claude_schema.StreamControlRequest, float]
    ] = field(default_factory=dict)
    # Auto-approve queue: request IDs that should be approved without user interaction
    auto_approve_queue: list[str] = field(default_factory=list)
    # Auto-deny queue: (request_id, message) pairs for rate-limited denials
    auto_deny_queue: list[tuple[str, str]] = field(default_factory=list)
    # Whether the control channel initialization handshake has been sent
    control_init_sent: bool = False
    # Track last tool_use_id for mapping control requests to tool actions
    last_tool_use_id: str | None = None
    # Map tool_use_id -> control action_id for completing control actions on tool result
    control_action_for_tool: dict[str, str] = field(default_factory=dict)
    # Map request_id -> action_id for reconciling callback-handled requests (#229)
    request_to_action: dict[str, str] = field(default_factory=dict)
    # Auto-approve ExitPlanMode when permission_mode is "auto"
    auto_approve_exit_plan_mode: bool = False
    # Whether this run is a resume (for error diagnostics)
    resumed: bool = False
    # Track max text block length seen (for cooldown bypass — survives overwrites)
    max_text_len_since_cooldown: int = 0
    # Store outline text for embedding in synthetic approve/deny action
    outline_text: str | None = None
    # #508 ExitPlanMode plan body — captured from the tool_use input on
    # every ExitPlanMode call so the bridge can re-emit it as part of the
    # final answer when the post-approval result is brief or empty
    # (research/audit tasks where Claude has nothing left to say after
    # the user approves).  Plan messages on Telegram are deleted on
    # approve, so this is the only path to retain the body.
    last_exitplanmode_plan: str | None = None
    # Cumulative seconds the session spent in Anthropic-side rate-limit waits (#349).
    # Sum of every rate_limit_event's retry_after_ms, so the cost footer can annotate
    # "(incl. Xm Ys rate-limited)" when a run finishes after one or more throttles.
    rate_limit_total_s: float = 0.0
    # Count of rate_limit_event emissions in this session — feeds a unit-test hook
    # and future /stats surfacing (#349 v2).
    rate_limit_count: int = 0

    # #347 per-session background-task tracking. Claude Code v2.1.72+ has
    # primitives that arm long-running work and return the subprocess to
    # "ready" state while the primitive continues in the background:
    # `Monitor`, `Bash run_in_background=true`, `Agent run_in_background=true`,
    # `ScheduleWakeup`, `RemoteTrigger`. Untether tracks them so (a) #346's
    # wedge detector can gate SIGTERM on "do we still have armed work?",
    # (b) progress footers can show "⏳ N watchers · M bg tasks", and (c)
    # a future `/background` command can enumerate the handles.
    #
    # Each dict keys on `tool_use_id` → deadline `time.monotonic()` seconds;
    # sets hold tool_use_ids without deadlines. Entries are cleared either
    # when the matching `tool_result` arrives (explicit completion) or, for
    # Monitor/ScheduleWakeup, when the deadline passes.
    live_monitors: dict[str, float] = field(default_factory=dict)
    live_bg_bashes: set[str] = field(default_factory=set)
    live_bg_agents: set[str] = field(default_factory=set)
    live_wakeups: dict[str, float] = field(default_factory=dict)
    # #507 arm-time `delaySeconds` per ScheduleWakeup tool_use_id, captured
    # parallel to ``live_wakeups``. ``live_wakeups`` stores future deadlines
    # which are hard to invert after they pass, so the post-result idle
    # watchdog reads this dict to shorten its timeout to ``max_delay + 60s``
    # when /loop is OFF (the wakeup is then a silent no-op upstream).
    live_wakeups_arm_delay: dict[str, float] = field(default_factory=dict)
    live_remote_triggers: set[str] = field(default_factory=set)

    # #289 — first user message text for the run.  Populated by ``new_state``
    # from the prompt arg.  Used as the fallback for the
    # ``<<autonomous-loop-dynamic>>`` sentinel when ScheduleWakeup is
    # observed without an explicit ``prompt`` field (Probe 3 result).
    first_user_message_text: str | None = None

    # #361 env-leak audit: pid populated by ClaudeRunner.run_impl after
    # spawn so translate_claude_event can sample /proc/<pid>/environ in
    # the system.init handler. audited flips to True after the first
    # sample; audited_leaks dedups warnings per (session, leaked_name).
    pid: int | None = None
    audited: bool = False
    audited_leaks: set[str] = field(default_factory=set)

    # #365 MCP catalog observability + proactive refresh. Settings
    # populated by ClaudeRunner.new_state() from WatchdogSettings so
    # translate_claude_event() can gate its behaviour without re-reading
    # config per-line. ``detect_catalog_staleness`` gates the
    # ``catalog_staleness.detected`` WARNING emitted from the system.init
    # handler when any configured MCP server reports a non-"connected"
    # status; ``notify_catalog_refresh`` gates the fire-and-forget
    # ``mcp_status`` control_request appended to
    # ``pending_catalog_refresh_ids`` after every tool_result and drained
    # on the runner's stdin by _drain_catalog_refresh().
    detect_catalog_staleness: bool = True
    notify_catalog_refresh: bool = False
    # Snapshot of ``mcp_servers`` from the session's first system.init
    # event: list of ``{name, status}`` dicts. Used only for the
    # init-time staleness log today; could feed mid-session comparison
    # in a future follow-up.
    initial_mcp_servers: list[Any] | None = None
    # Dedup set for catalog_staleness warnings — holds
    # (session_id, server_name, status) tuples so re-fired init events
    # (rare: only on Claude Code internal resume) don't spam the log.
    catalog_staleness_logged: set[tuple[str, str, str]] = field(default_factory=set)
    # Pending mcp_status control_request IDs queued by tool_result,
    # drained on stdin by ClaudeRunner._drain_catalog_refresh. Names
    # allocated as ``ut_catalog_refresh_<session_id>_<seq>`` to avoid
    # colliding with Claude Code's own ``req_*`` namespace.
    pending_catalog_refresh_ids: list[str] = field(default_factory=list)
    catalog_refresh_seq: int = 0
    # #497: debounce gate. Holds the ``time.monotonic()`` timestamp of the
    # last enqueued refresh; the translate path skips re-enqueue while
    # ``(now - last) < catalog_refresh_min_interval_s``. None until the
    # first fire so the very first tool_result batch always queues.
    last_catalog_refresh_queued_at: float | None = None
    # Configured per-session interval mirrored from
    # ``WatchdogSettings.catalog_refresh_min_interval_s`` at session init
    # so translate() doesn't reach back into settings on every event.
    catalog_refresh_min_interval_s: float = 5.0

    # #333: monotonic timestamp of the most recent ``result`` event. The
    # post-result idle watchdog (``ClaudeRunner._post_result_idle_watchdog``)
    # polls this to decide when to close stdin. None until the first
    # result lands; reset on each subsequent result so that a multi-turn
    # bidirectional session re-arms the timer on every turn boundary.
    result_received_at: float | None = None

    # #470: cross-layer signals from _post_result_idle_watchdog → bridge.
    # The watchdog stamps ``post_result_closed_at`` (monotonic) and
    # ``post_result_idle_minutes`` immediately before closing stdin.
    # ``ProgressEdits._stall_monitor`` polls these via engine_state
    # duck-typing (mirrors the pattern at runner_bridge.py:1426 for
    # ``has_live_background_work``) and fires a one-shot Telegram closing
    # message with the elapsed-minutes wording, then sets
    # ``post_result_closing_sent`` so subsequent ticks no-op (idempotent).
    post_result_closed_at: float | None = None
    post_result_idle_minutes: float = 0.0
    post_result_closing_sent: bool = False


def _derive_retry_after_s(info: claude_schema.RateLimitInfo | None) -> float | None:
    """#518: when `rate_limit_event` omits `retry_after_ms`, fall back to the
    earlier of `requests_reset` / `tokens_reset` ISO timestamps.

    Returns the seconds-until-reset (clamped ≥ 0) so the chat can show
    "retrying in N s" and `state.rate_limit_total_s` accumulates correctly,
    even when upstream sends only the reset-window form documented in
    `docs/reference/runners/claude/stream-json-cheatsheet.md`. Returns None
    if no parseable timestamp is present, in which case the caller continues
    to render the generic "waiting to retry" copy.
    """
    if info is None:
        return None
    from datetime import datetime

    candidates: list[float] = []
    for raw in (info.requests_reset, info.tokens_reset):
        if not isinstance(raw, str) or not raw:
            continue
        try:
            # `fromisoformat` (3.11+) handles "Z" suffix natively, but to keep
            # parsing forgiving across CLI versions accept both spellings.
            normalised = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            dt = datetime.fromisoformat(normalised)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        delta = (dt - datetime.now(UTC)).total_seconds()
        candidates.append(max(0.0, delta))
    if not candidates:
        return None
    # Choose the EARLIER reset (smaller delta) — the rate limit lifts as
    # soon as one of the two budgets refills.
    return min(candidates)


def _normalize_tool_result(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
    return str(content)


def _coerce_comma_list(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        parts = [str(item) for item in value if item is not None]
        joined = ",".join(part for part in parts if part)
        return joined or None
    text = str(value)
    return text or None


def _tool_kind_and_title(
    name: str, tool_input: dict[str, Any]
) -> tuple[ActionKind, str]:
    return tool_kind_and_title(name, tool_input, path_keys=("file_path", "path"))


def _tool_action(
    content: claude_schema.StreamToolUseBlock,
    *,
    parent_tool_use_id: str | None,
) -> Action:
    tool_id = content.id
    tool_name = str(content.name or "tool")
    tool_input = content.input

    kind, title = _tool_kind_and_title(tool_name, tool_input)

    detail: dict[str, Any] = {
        "name": tool_name,
        "input": tool_input,
    }
    if parent_tool_use_id:
        detail["parent_tool_use_id"] = parent_tool_use_id

    if kind == "file_change":
        path = tool_input_path(tool_input, path_keys=("file_path", "path"))
        if path:
            detail["changes"] = [{"path": path, "kind": "update"}]

    return Action(id=tool_id, kind=kind, title=title, detail=detail)


def _register_background_handle(
    state: ClaudeStreamState,
    content: claude_schema.StreamToolUseBlock,
) -> None:
    """Track long-running primitives that outlive the tool_result (#347).

    Monitor / Bash-bg / Agent-bg / ScheduleWakeup / RemoteTrigger can arm
    work that continues after Claude Code emits `result`. Untether records
    the handle so downstream consumers (#346 wedge detector, progress
    footer, `/background` command) know the subprocess is legitimately
    parked rather than hung. Entries are removed in
    `_clear_background_handle` when the matching tool_result arrives.

    Deliberately lenient with the `input` shape — Claude Code's schema
    forbids unknown fields at the outer level but the tool-specific `input`
    is free-form, so we defensively coerce to dict.
    """
    tool_name = str(content.name or "")
    tool_id = content.id
    raw_input = content.input if isinstance(content.input, dict) else {}

    if tool_name == "Monitor":
        timeout_ms = raw_input.get("timeout_ms")
        if isinstance(timeout_ms, (int, float)) and timeout_ms > 0:
            state.live_monitors[tool_id] = time.monotonic() + (timeout_ms / 1000.0)
        else:
            # Unknown deadline → store 0.0 so membership tests still work
            state.live_monitors[tool_id] = 0.0
    elif tool_name == "Bash" and bool(raw_input.get("run_in_background")):
        state.live_bg_bashes.add(tool_id)
    elif tool_name == "Agent" and bool(raw_input.get("run_in_background")):
        state.live_bg_agents.add(tool_id)
    elif tool_name == "ScheduleWakeup":
        # #481: the actual Claude Code ScheduleWakeup tool schema (per
        # #289 / claude-agent-sdk-python) emits ``delaySeconds`` as the
        # canonical field. Earlier versions of this code read
        # ``delay_ms``/``timeout_ms`` only, which always missed in
        # production (live_wakeups[tool_id] fell to 0.0 → countdown
        # rendering broken, though membership-only suppression still
        # worked). Read delaySeconds first; keep the legacy fallbacks so
        # existing test fixtures parameterised on delay_ms still work.
        delay_seconds_raw = raw_input.get("delaySeconds")
        if isinstance(delay_seconds_raw, (int, float)) and delay_seconds_raw > 0:
            state.live_wakeups[tool_id] = time.monotonic() + float(delay_seconds_raw)
            state.live_wakeups_arm_delay[tool_id] = float(delay_seconds_raw)
        else:
            delay_ms = raw_input.get("delay_ms") or raw_input.get("timeout_ms")
            if isinstance(delay_ms, (int, float)) and delay_ms > 0:
                state.live_wakeups[tool_id] = time.monotonic() + (delay_ms / 1000.0)
                state.live_wakeups_arm_delay[tool_id] = delay_ms / 1000.0
            else:
                state.live_wakeups[tool_id] = 0.0
                state.live_wakeups_arm_delay[tool_id] = 0.0
    elif tool_name == "RemoteTrigger":
        state.live_remote_triggers.add(tool_id)


def _clear_background_handle(state: ClaudeStreamState, tool_use_id: str) -> None:
    """Remove a background-task entry when its tool_result arrives (#347)."""
    state.live_monitors.pop(tool_use_id, None)
    state.live_bg_bashes.discard(tool_use_id)
    state.live_bg_agents.discard(tool_use_id)
    state.live_wakeups.pop(tool_use_id, None)
    state.live_wakeups_arm_delay.pop(tool_use_id, None)
    state.live_remote_triggers.discard(tool_use_id)


# ── /loop and ScheduleWakeup observation (#289) ─────────────────────────


# Result-text patterns extracted in ``_observe_loop_tool_result``.
# CronCreate / CronDelete share the ``\bjob ([0-9a-f]{8})\b`` form (Probe 5).
_LOOP_CRON_ID_RE = re.compile(r"\bjob ([0-9a-f]{8})\b")
# ScheduleWakeup result text reports the runtime-clamped delay as ``(in Ns)``.
_LOOP_WAKEUP_DELAY_RE = re.compile(r"\(in (\d+)s\)")


def _loop_enabled_for_chat(chat_id: int | None) -> bool:
    """Resolve the /loop master toggle for a chat.

    Resolution order (matches the design doc §5.0):

    1. Per-chat override via ``EngineRunOptions.loop_enabled`` (set by
       ``/config → 🔁 Loop mode``).  ``None`` means "follow global".
    2. Global ``[loop] enabled`` from ``untether.toml``.
    3. Hard fallback: ``False`` so a config error never accidentally
       turns Loop mode on.

    ``chat_id`` is currently advisory — the per-chat override lives in
    the run-options contextvar set by ``executor.handle_engine_run``,
    which is already chat-scoped.  We accept it so the call site reads
    cleanly and so a future per-chat resolver can be wired in without
    changing observer signatures.
    """
    options = get_run_options()
    if options is not None and options.loop_enabled is not None:
        return bool(options.loop_enabled)
    try:
        result = load_settings_if_exists()
        if result is None:
            return False
        settings, _ = result
        return bool(settings.loop.enabled)
    except Exception:  # noqa: BLE001 — never let config errors turn loop ON
        return False


def _observe_loop_tool_use(
    state: ClaudeStreamState,
    content: claude_schema.StreamToolUseBlock,
) -> None:
    """Observe ``CronCreate`` / ``ScheduleWakeup`` / ``CronDelete``
    ``tool_use`` events and register Untether-side loop entries (#289).

    Sibling of :func:`_register_background_handle` — does NOT mutate
    ``state.live_*`` registries.  Called after
    :func:`_register_background_handle` so the rc8 ScheduleWakeup
    countdown still works for short waits when Loop mode is OFF.
    """
    from ..utils.paths import get_run_channel_id

    chat_id = get_run_channel_id()
    if chat_id is None:
        return  # not in a chat-scoped run (probes, ad-hoc spawns)
    if not _loop_enabled_for_chat(chat_id):
        return  # master toggle off → behave as today
    tool_name = str(content.name or "")
    tool_id = content.id
    raw_input = content.input if isinstance(content.input, dict) else {}
    session_id = state.factory.resume.value if state.factory.resume else None
    if not session_id:
        return  # session_id only known after system.init; tool_use shouldn't
        # arrive before that, but guard defensively

    from .. import loop_scheduler

    if tool_name == "CronCreate":
        # Probe 5: input field is `cron`, NOT `cron_expression`.  Lenient
        # fallback to `cron_expression`/`schedule` in case the upstream
        # schema gains aliases later.
        cron_expr = (
            raw_input.get("cron")
            or raw_input.get("cron_expression")
            or raw_input.get("schedule")
        )
        prompt = raw_input.get("prompt") or raw_input.get("text") or ""
        recurring = bool(raw_input.get("recurring", True))
        if not cron_expr or not prompt:
            return
        try:
            loop_scheduler.register_pending_cron(
                session_id=session_id,
                tool_use_id=tool_id,
                cron_expression=str(cron_expr),
                prompt=str(prompt),
                recurring=recurring,
                chat_id=int(chat_id),
                fallback_first_user_message=state.first_user_message_text,
            )
        except loop_scheduler.LoopSchedulerError as exc:
            logger.warning(
                "loop.observe.cron_register_failed",
                session=session_id,
                error=str(exc),
            )
    elif tool_name == "ScheduleWakeup":
        # Probe 5: minimum delaySeconds = 60 (runtime clamps shorter values).
        delay_seconds_raw = raw_input.get("delaySeconds")
        if not isinstance(delay_seconds_raw, (int, float)) or delay_seconds_raw <= 0:
            return
        # Inline threshold — short waits stay rendered live by the
        # rc8 countdown without an Untether-side timer (post-result
        # watchdog won't reach them).
        try:
            settings_result = load_settings_if_exists()
            inline_threshold = (
                settings_result[0].loop.inline_threshold_seconds
                if settings_result is not None
                else 300
            )
        except Exception:  # noqa: BLE001
            inline_threshold = 300
        if delay_seconds_raw <= inline_threshold:
            return
        prompt = raw_input.get("prompt") or "<<autonomous-loop-dynamic>>"
        try:
            loop_scheduler.register_pending_wakeup(
                session_id=session_id,
                tool_use_id=tool_id,
                delay_seconds=float(delay_seconds_raw),
                prompt=str(prompt),
                chat_id=int(chat_id),
                fallback_first_user_message=state.first_user_message_text,
            )
        except loop_scheduler.LoopSchedulerError as exc:
            logger.warning(
                "loop.observe.wakeup_register_failed",
                session=session_id,
                error=str(exc),
            )
    elif tool_name == "CronDelete":
        # Probe 5: input field is `id`, NOT `taskId`/`cronId`.
        upstream_id = raw_input.get("id") or raw_input.get("taskId")
        if upstream_id:
            loop_scheduler.cancel_by_upstream_id(str(upstream_id))


def _observe_loop_tool_result(
    state: ClaudeStreamState,
    tool_use_id: str,
    result_content: object,
) -> None:
    """Observe ``CronCreate`` ``tool_result`` events and bind the upstream
    8-character cron ID to the matching pending entry (#289).

    Sibling of :func:`_clear_background_handle`.  Does nothing if no
    matching entry exists (e.g. master toggle was off when tool_use was
    observed).  Idempotent — bind_upstream_id is a no-op for unknown
    tool_use_ids.
    """
    if not isinstance(result_content, str):
        # tool_result.content can be list[dict] for multi-block results.
        # CronCreate / ScheduleWakeup return free-form strings, so anything
        # else is irrelevant.
        return
    from .. import loop_scheduler

    match = _LOOP_CRON_ID_RE.search(result_content)
    if match is None:
        return
    upstream_id = match.group(1)
    loop_scheduler.bind_upstream_id(tool_use_id, upstream_id)


def has_live_background_work(state: ClaudeStreamState) -> bool:
    """Return True when the session has any background handle whose deadline
    (if any) is still in the future (#346 gate).

    Monitors + wakeups with expired deadlines are treated as "no longer
    live" — the primitive should have fired and emitted its result by then.
    Sets (bg bashes, bg agents, remote triggers) have no deadline so any
    entry counts as live.
    """
    now = time.monotonic()
    for deadline in state.live_monitors.values():
        if deadline == 0.0 or deadline > now:
            return True
    for deadline in state.live_wakeups.values():
        if deadline == 0.0 or deadline > now:
            return True
    return bool(
        state.live_bg_bashes or state.live_bg_agents or state.live_remote_triggers
    )


def background_task_summary(state: ClaudeStreamState) -> str | None:
    """Return a compact "⏳ 2 watchers · 1 bg task" summary or None if empty.

    Used by progress footer rendering (#347 v2) and the `/background`
    command. v1 of this PR only computes it; the footer wiring lands in
    a follow-up once meta-threading from ClaudeStreamState to
    `ProgressTracker.meta` is confirmed safe for the other 5 engines.
    """
    watchers = len(state.live_monitors) + len(state.live_wakeups)
    bg_tasks = (
        len(state.live_bg_bashes)
        + len(state.live_bg_agents)
        + len(state.live_remote_triggers)
    )
    if watchers == 0 and bg_tasks == 0:
        return None
    parts: list[str] = []
    if watchers:
        parts.append(f"{watchers} watcher{'s' if watchers != 1 else ''}")
    if bg_tasks:
        parts.append(f"{bg_tasks} bg task{'s' if bg_tasks != 1 else ''}")
    return "⏳ " + " · ".join(parts)


def _tool_result_event(
    content: claude_schema.StreamToolResultBlock,
    *,
    action: Action,
    factory: EventFactory,
) -> UntetherEvent:
    is_error = content.is_error is True
    raw_result = content.content
    normalized = _normalize_tool_result(raw_result)
    preview = normalized

    detail = action.detail | {
        "tool_use_id": content.tool_use_id,
        "result_preview": preview,
        "result_len": len(normalized),
        "is_error": is_error,
    }
    return factory.action_completed(
        action_id=action.id,
        kind=action.kind,
        title=action.title,
        ok=not is_error,
        detail=detail,
    )


def _format_diff_preview(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Format a compact diff preview for Edit/Write tool approval messages."""
    max_preview_lines = 8
    max_line_len = 60

    def _truncate(text: str, max_len: int) -> str:
        if len(text) > max_len:
            return text[: max_len - 1] + "…"
        return text

    if tool_name == "Edit":
        file_path = tool_input.get("file_path", "")
        old_string = tool_input.get("old_string", "")
        new_string = tool_input.get("new_string", "")
        if not old_string and not new_string:
            return ""
        lines: list[str] = []
        if file_path:
            from ..utils.paths import relativize_path

            lines.append(f"📝 {relativize_path(file_path)}")
        old_lines = old_string.splitlines()
        new_lines = new_string.splitlines()
        # Show removed/added lines
        half = max_preview_lines // 2
        lines.extend(f"- {_truncate(line, max_line_len)}" for line in old_lines[:half])
        if len(old_lines) > half:
            lines.append(f"  …({len(old_lines) - half} more removed)")
        lines.extend(f"+ {_truncate(line, max_line_len)}" for line in new_lines[:half])
        if len(new_lines) > half:
            lines.append(f"  …({len(new_lines) - half} more added)")
        return "\n".join(lines)

    if tool_name == "Write":
        file_path = tool_input.get("file_path", "")
        content = tool_input.get("content", "")
        if not content:
            return ""
        lines = []
        if file_path:
            from ..utils.paths import relativize_path

            lines.append(f"📝 {relativize_path(file_path)}")
        content_lines = content.splitlines()
        line_count = len(content_lines)
        for line in content_lines[:max_preview_lines]:
            lines.append(f"+ {_truncate(line, max_line_len)}")
        if line_count > max_preview_lines:
            lines.append(f"  …({line_count - max_preview_lines} more lines)")
        return "\n".join(lines)

    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if command:
            return f"$ {_truncate(command, 200)}"
        return ""

    return ""


# #438: classify Stream idle timeout failures so the user sees actionable
# context instead of just "API Error: Stream idle timeout - partial response
# received". Two distinct upstream Anthropic API failure modes:
#
# - Type A — mid-generation stall: the model emitted some output, then went
#   silent for >CLAUDE_STREAM_IDLE_TIMEOUT_MS. ``num_turns >= 1`` and
#   ``duration_api_ms > 0``. Often legitimate long opus 4.7 1M plan-mode
#   reasoning that exceeded the watchdog; raising the timeout helps.
#
# - Type B — cold-start zero-byte stall: zero bytes ever arrived. ``num_turns
#   <= 1`` and ``duration_api_ms == 0``. The watchdog correctly detected an
#   API outage from the client's perspective; raising the timeout does NOT
#   help. Likely Anthropic API queueing / availability under load.
#
# See #438 for upstream tracking (consolidated `claude-code` issues
# 2026-04-17→26).
_STREAM_IDLE_TIMEOUT_PATTERN = "Stream idle timeout"


def _classify_stream_idle_timeout(
    event: claude_schema.StreamResultMessage,
) -> str | None:
    """Return a short Type-A / Type-B annotation, or None if not a stall."""
    result = event.result if isinstance(event.result, str) else ""
    if _STREAM_IDLE_TIMEOUT_PATTERN not in result:
        return None
    if event.num_turns <= 1 and (
        event.duration_api_ms is None or event.duration_api_ms == 0
    ):
        # Type B — cold-start zero-byte stall. No bytes from API.
        return (
            "🌐 Cold-start API stall (Type B): Anthropic API returned no "
            "bytes within the watchdog window. Likely upstream API "
            "queueing/availability — raising CLAUDE_STREAM_IDLE_TIMEOUT_MS "
            "will NOT help. Retry shortly."
        )
    # Type A — mid-generation stall. Model emitted output then went silent.
    return (
        "⏳ Mid-generation API stall (Type A): SSE stream went silent after "
        "partial output. Often legitimate long reasoning that exceeded the "
        "watchdog — consider raising [watchdog] claude_stream_idle_timeout_ms "
        "in untether.toml."
    )


def _extract_error(
    event: claude_schema.StreamResultMessage,
    *,
    resumed: bool = False,
) -> str | None:
    if not event.is_error:
        return None
    # First line: error summary
    if isinstance(event.result, str) and event.result:
        first = event.result
    elif event.subtype:
        first = f"Claude Code run failed ({event.subtype})"
    else:
        first = "Claude Code run failed"

    # #438: append a Type-A / Type-B annotation when the failure is a
    # Stream idle timeout, so the operator can tell the two failure modes
    # apart from the visible message alone.
    classification = _classify_stream_idle_timeout(event)

    # Second line: diagnostic context
    parts: list[str] = []
    sid = event.session_id[:8] if event.session_id else None
    if sid:
        parts.append(f"session: {sid}")
    parts.append("resumed" if resumed else "new")
    parts.append(f"turns: {event.num_turns}")
    cost = event.total_cost_usd
    if cost is not None:
        parts.append(f"cost: ${cost:.2f}")
    if event.duration_api_ms:
        parts.append(f"api: {event.duration_api_ms}ms")

    diagnostics = " · ".join(parts)
    if classification is not None:
        return f"{first}\n{diagnostics}\n\n{classification}"
    return f"{first}\n{diagnostics}"


_PREPEND_LENGTH_GATE = 600
_PREPEND_BODY_CAP = 1500
_PREPEND_BODY_TRUNC_SUFFIX = "\n\n…\n\n(plan truncated — shown in full during approval)"


def _prepend_exitplanmode_plan(final_answer: str | None, plan_body: str | None) -> str:
    """#508 Re-emit ExitPlanMode plan body in the final answer.

    Called from the per-stream ``StreamResultMessage`` translation path
    (#510) using ``state.last_exitplanmode_plan`` — correctly scoped to
    this run's stream, not the shared ``runner.current_stream`` singleton.

    #515 length-gate tuning (rc13). The original substring check
    (``body in final_answer``) failed in practice because the rc11
    preamble told Claude to *paraphrase* the plan post-approval rather
    than literal-copy it, so the skip never triggered and Layer E
    concatenated the full plan body in front of every well-behaved run
    (42k-char Telegram messages on staging). The new preamble asks for a
    brief CLI-style summary post-approval — when Claude obeys, the
    answer is >600 chars and we skip the prepend; when Claude exits with
    nothing substantive (the original #508 repro at 584 chars), the
    length gate falls through and we prepend a capped plan body.

    Skip rules (in order):
    1. ``plan_body`` empty/whitespace → return final answer as-is.
    2. ``final_answer`` already substantive (≥ ``_PREPEND_LENGTH_GATE``)
       → skip prepend, post-approval text is doing the job.
    3. Exact substring match → skip prepend (cheap belt-and-braces).
    4. Otherwise prepend, truncating ``plan_body`` to
       ``_PREPEND_BODY_CAP`` chars so a runaway plan body doesn't ship
       a 30k-char final.
    """
    if not plan_body or not plan_body.strip():
        return final_answer or ""
    final = final_answer or ""
    if len(final) >= _PREPEND_LENGTH_GATE:
        return final
    body = plan_body.strip()
    if body in final:
        return final
    if len(body) > _PREPEND_BODY_CAP:
        body = body[:_PREPEND_BODY_CAP].rstrip() + _PREPEND_BODY_TRUNC_SUFFIX
    if final:
        return f"📋 Plan (approved):\n\n{body}\n\n---\n\n{final}"
    return f"📋 Plan (approved):\n\n{body}"


def _maybe_audit_env(state: ClaudeStreamState, session_id: str) -> None:
    """One-shot ``/proc/<pid>/environ`` audit on first system.init (#361).

    Best-effort: skips silently when no PID is recorded, when audit is
    disabled in config, when settings can't be loaded, or when /proc is
    unreadable. Emits one ``claude.env_audit.leaked_var`` warning per
    (session, leaked_name).
    """
    if state.audited or state.pid is None:
        return
    state.audited = True

    enabled = True
    try:
        result = load_settings_if_exists()
        if result is not None:
            settings, _ = result
            enabled = settings.security.env_audit
    except Exception:  # noqa: BLE001 — never let config errors block a run
        enabled = True
    if not enabled:
        return

    # #409: pass user extras through so the audit doesn't flag names the
    # operator explicitly opted into via [security] env_extra_allow.
    user_exact, user_prefix = _load_env_extras()
    leaked = audit_proc_env(
        state.pid,
        expected_extras=("UNTETHER_SESSION",),
        user_extra_exact=user_exact,
        user_extra_prefix=user_prefix,
    )
    for name in leaked:
        if name in state.audited_leaks:
            continue
        state.audited_leaks.add(name)
        logger.warning(
            "claude.env_audit.leaked_var",
            session_id=session_id,
            pid=state.pid,
            name=name,
        )


def _capture_mcp_catalog(
    state: ClaudeStreamState,
    session_id: str,
    mcp_servers: list[Any] | None,
) -> None:
    """Snapshot ``mcp_servers`` from system.init and log init-time staleness (#365).

    Claude Code's ``system.init`` event reports each configured MCP
    server as ``{"name": "...", "status": "connected"|"pending"|"error"|"failed"}``.
    A non-``connected`` status at init time is the clearest indicator we
    have that the MCP catalog is stale from the user's perspective —
    without waiting for a mid-session reminder from Claude.

    Gated by ``WatchdogSettings.detect_catalog_staleness`` (default on;
    observability only — no recovery action). Logs once per
    (session, server, status) tuple so re-fired init events don't spam.
    """
    if not mcp_servers:
        return
    # Preserve the raw list for downstream tooling (future follow-ups may
    # compare mid-session state against this snapshot).
    if state.initial_mcp_servers is None:
        state.initial_mcp_servers = list(mcp_servers)
    if not state.detect_catalog_staleness:
        return
    for server in mcp_servers:
        if not isinstance(server, dict):
            continue
        name = server.get("name")
        status = server.get("status")
        if not isinstance(name, str) or not isinstance(status, str):
            continue
        if status == "connected":
            continue
        key = (session_id, name, status)
        if key in state.catalog_staleness_logged:
            continue
        state.catalog_staleness_logged.add(key)
        logger.warning(
            "catalog_staleness.detected",
            session_id=session_id,
            pid=state.pid,
            server=name,
            status=status,
            source="system.init",
        )


def _usage_payload(event: claude_schema.StreamResultMessage) -> dict[str, Any]:
    usage: dict[str, Any] = {}
    for key in (
        "total_cost_usd",
        "duration_ms",
        "duration_api_ms",
        "num_turns",
    ):
        value = getattr(event, key, None)
        if value is not None:
            usage[key] = value
    if event.usage is not None:
        usage["usage"] = event.usage
    return usage


def translate_claude_event(
    event: claude_schema.StreamJsonMessage,
    *,
    title: str,
    state: ClaudeStreamState,
    factory: EventFactory,
) -> list[UntetherEvent]:
    match event:
        case claude_schema.StreamSystemMessage(subtype=subtype):
            if subtype != "init":
                logger.debug(
                    "claude.system_event.non_init",
                    subtype=subtype,
                    session_id=event.session_id,
                )
                return []
            session_id = event.session_id
            if not session_id:
                return []
            # #361 sample child env on first init; no-op if PID missing,
            # audit disabled, /proc unreadable, or non-Linux.
            _maybe_audit_env(state, session_id)
            # #365 capture MCP catalog snapshot + log init-time staleness.
            _capture_mcp_catalog(state, session_id, event.mcp_servers)
            meta: dict[str, Any] = {}
            for key in (
                "cwd",
                "model",
                "tools",
                "permissionMode",
                "output_style",
                "apiKeySource",
                "mcp_servers",
            ):
                value = getattr(event, key, None)
                if value is not None:
                    meta[key] = value
            run_options = get_run_options()
            if run_options is not None and run_options.reasoning:
                meta["effort"] = run_options.reasoning
            model = event.model
            token = ResumeToken(engine=ENGINE, value=session_id)
            event_title = str(model) if isinstance(model, str) and model else title
            return [factory.started(token, title=event_title, meta=meta or None)]
        case claude_schema.StreamAssistantMessage(
            message=message, parent_tool_use_id=parent_tool_use_id
        ):
            out: list[UntetherEvent] = []
            for content in message.content:
                match content:
                    case (
                        claude_schema.StreamToolUseBlock()
                        | claude_schema.StreamServerToolUseBlock()
                    ):
                        # #489 server_tool_use shares the tool_use translation —
                        # _register_background_handle / _observe_loop_tool_use
                        # filter on tool name and no-op for unrecognised server
                        # tools (web_search, code_execution, computer_use, …).
                        action = _tool_action(
                            content,
                            parent_tool_use_id=parent_tool_use_id,
                        )
                        state.pending_actions[action.id] = action
                        state.last_tool_use_id = content.id
                        # #347 track long-running primitives that outlive
                        # this tool_use → tool_result cycle
                        _register_background_handle(state, content)
                        # #289 observe /loop and ScheduleWakeup tool calls
                        # so Untether can re-fire after the subprocess exits
                        # (master toggle gate inside).  Sibling of, not
                        # replacement for, _register_background_handle.
                        _observe_loop_tool_use(state, content)
                        # #508 capture ExitPlanMode plan body so the bridge
                        # can re-emit it in the final answer when the
                        # post-approval result is brief/empty (research
                        # tasks).  Only captures from the regular Approve
                        # flow — Pause-and-Outline outlines go via
                        # state.outline_text and a different code path.
                        if str(content.name or "") == "ExitPlanMode":
                            _epm_input = (
                                content.input if isinstance(content.input, dict) else {}
                            )
                            _plan_body = _epm_input.get("plan")
                            if isinstance(_plan_body, str) and _plan_body.strip():
                                state.last_exitplanmode_plan = _plan_body
                        out.append(
                            factory.action_started(
                                action_id=action.id,
                                kind=action.kind,
                                title=action.title,
                                detail=action.detail,
                            )
                        )
                    case claude_schema.StreamThinkingBlock(
                        thinking=thinking, signature=signature
                    ):
                        if not thinking:
                            continue
                        state.note_seq += 1
                        action_id = f"claude.thinking.{state.note_seq}"
                        detail: dict[str, Any] = {}
                        if parent_tool_use_id:
                            detail["parent_tool_use_id"] = parent_tool_use_id
                        if signature:
                            detail["signature"] = signature
                        out.append(
                            factory.action_completed(
                                action_id=action_id,
                                kind="note",
                                title=thinking,
                                ok=True,
                                detail=detail,
                            )
                        )
                    case claude_schema.StreamTextBlock(text=text):
                        if text:
                            state.last_assistant_text = text
                            if len(text) > state.max_text_len_since_cooldown:
                                state.max_text_len_since_cooldown = len(text)
                            # When outline is pending (user clicked "Pause & Outline Plan"),
                            # store the outline text so it can be embedded in the synthetic
                            # approve/deny action that follows (separate note actions get
                            # scrolled off by the max_actions window).
                            if (
                                factory.resume
                                and factory.resume.value in _OUTLINE_PENDING
                                and len(text) >= _OUTLINE_MIN_CHARS
                            ):
                                state.outline_text = text
                    case _:
                        continue
            return out
        case claude_schema.StreamUserMessage(message=message):
            if not isinstance(message.content, list):
                return []
            out: list[UntetherEvent] = []
            saw_tool_result = False
            for content in message.content:
                # #489 advisor_tool_result shares the tool_result translation.
                if not isinstance(
                    content,
                    (
                        claude_schema.StreamToolResultBlock,
                        claude_schema.StreamAdvisorToolResultBlock,
                    ),
                ):
                    continue
                saw_tool_result = True
                tool_use_id = content.tool_use_id
                # #347 clear any background-task entry for this tool_use_id
                _clear_background_handle(state, tool_use_id)
                # #289 bind upstream cron ID so CronDelete observations
                # later in the session can target the right loop entry.
                _observe_loop_tool_result(state, tool_use_id, content.content)
                action = state.pending_actions.pop(tool_use_id, None)
                if action is None:
                    action = Action(
                        id=tool_use_id,
                        kind="tool",
                        title="tool result",
                        detail={},
                    )
                out.append(
                    _tool_result_event(
                        content,
                        action=action,
                        factory=factory,
                    )
                )
                # Complete any associated control action (e.g. permission approval)
                control_action_id = state.control_action_for_tool.pop(tool_use_id, None)
                if control_action_id:
                    out.append(
                        factory.action_completed(
                            action_id=control_action_id,
                            kind="warning",
                            title="Permission resolved",
                            ok=True,
                        )
                    )
            # #365 queue a proactive mcp_status nudge once per tool_result
            # batch. Opt-in via WatchdogSettings.notify_catalog_refresh.
            # Drained from stdin by ClaudeRunner._drain_catalog_refresh so
            # the send is fire-and-forget and cannot block translate().
            # #497 debounce: skip the enqueue while the previous fire is
            # within ``catalog_refresh_min_interval_s``. Set to 0 to disable.
            if saw_tool_result and state.notify_catalog_refresh:
                resume_val = factory.resume.value if factory.resume else None
                if resume_val:
                    now = time.monotonic()
                    last = state.last_catalog_refresh_queued_at
                    interval = state.catalog_refresh_min_interval_s
                    if last is None or interval <= 0 or (now - last) >= interval:
                        state.catalog_refresh_seq += 1
                        request_id = (
                            f"ut_catalog_refresh_{resume_val}_"
                            f"{state.catalog_refresh_seq}"
                        )
                        state.pending_catalog_refresh_ids.append(request_id)
                        state.last_catalog_refresh_queued_at = now
            return out
        case claude_schema.StreamResultMessage():
            ok = not event.is_error
            result_text = event.result or ""
            if ok and not result_text and state.last_assistant_text:
                result_text = state.last_assistant_text

            # #510 / #508: re-emit the ExitPlanMode plan body when the
            # post-approval final answer is brief/empty. Done HERE on the
            # per-stream path (state is per-run, correctly scoped) rather
            # than in runner_bridge.handle_message against the shared
            # runner.current_stream singleton — which raced across
            # concurrent Claude chats and leaked plan bodies cross-chat.
            if ok:
                result_text = _prepend_exitplanmode_plan(
                    result_text, state.last_exitplanmode_plan
                )

            resume = ResumeToken(engine=ENGINE, value=event.session_id)
            error = None if ok else _extract_error(event, resumed=state.resumed)
            usage = _usage_payload(event)

            # #333: arm the post-result idle watchdog. Reset on every
            # result (multi-turn re-arms the timer per turn boundary).
            state.result_received_at = time.monotonic()

            events_out: list[UntetherEvent] = []
            # #333 UX signal #1: append "✓ turn complete" to the meta
            # footer so the user immediately sees the turn is done and
            # the session is now waiting for the next prompt. A
            # supplementary StartedEvent with new meta is the supported
            # pattern for late-arriving metadata (see
            # .claude/rules/runner-development.md).
            if ok:
                events_out.append(
                    factory.started(
                        resume,
                        title=None,
                        meta={"complete": "✓ turn complete"},
                    )
                )
            events_out.append(
                factory.completed(
                    ok=ok,
                    answer=result_text,
                    resume=resume,
                    error=error,
                    usage=usage or None,
                )
            )
            return events_out
        case claude_schema.StreamControlRequest(request_id=request_id, request=request):
            # Auto-approve non-user-facing control requests.
            #
            # #380 — security audit (2026-04-27) verified the safety invariant
            # for the two subtypes that look superficially scary:
            #
            # * `ControlMcpMessageRequest` (subtype=mcp_message). Carries
            #   `server_name: str` + `message: Any`. Untether NEVER inspects
            #   or executes the `message` payload — it auto-acknowledges and
            #   the payload flows through Claude Code to the model, where
            #   model-initiated tool calls still pass through the standard
            #   `ControlCanUseToolRequest` gate (and ExitPlanMode / interactive
            #   approval where applicable). A compromised MCP server CAN send
            #   tainted prompts via this channel, but that's the inherent
            #   threat model of any MCP server — not specific to auto-approve.
            #   Routing this through Telegram approval would not block the
            #   payload (it's already in-flight) — it would just delay the
            #   acknowledgement, with no security gain.
            #
            # * `ControlRewindFilesRequest` (subtype=rewind_files). Carries
            #   `user_message_id: str`. Rewind is initiated by the user via
            #   the Claude CLI's `/rewind` slash command (or programmatic
            #   equivalent) — the model cannot autonomously trigger rewind
            #   in upstream Claude Code 2.1.x. Untether currently has no UI
            #   that issues `/rewind`, so this control_request only fires
            #   when the user types `/rewind` themselves in a chat; the user
            #   has already consented. If a future release exposes rewind
            #   via Telegram UI, that UI's command handler should provide
            #   the gate, not this control-channel layer. The denial state
            #   that drove a prior approval/deny decision lives on the
            #   parent (Untether) side in `_HANDLED_REQUESTS` /
            #   `_PLAN_EXIT_APPROVED` — those are NOT mutated by rewind.
            #
            # The other three (initialize, hook_callback, interrupt) are
            # protocol housekeeping with no payload that Untether interprets.
            #
            # Acceptance: changes to either subtype's semantics in upstream
            # Claude Code MUST trigger a re-audit. Tests in
            # tests/test_claude_control.py::TestAutoApproveSafetyInvariant
            # lock in the expectation that auto-approve runs without
            # invoking any callback that observes the payload.
            _AUTO_APPROVE_TYPES = (
                claude_schema.ControlInitializeRequest,
                claude_schema.ControlHookCallbackRequest,
                claude_schema.ControlMcpMessageRequest,
                claude_schema.ControlRewindFilesRequest,
                claude_schema.ControlInterruptRequest,
            )
            if isinstance(request, _AUTO_APPROVE_TYPES):
                request_type = (
                    type(request).__name__.replace("Control", "").replace("Request", "")
                )
                logger.debug(
                    "control_request.auto_approve",
                    request_id=request_id,
                    request_type=request_type,
                )
                _REQUEST_TO_INPUT[request_id] = getattr(request, "input", {})
                state.auto_approve_queue.append(request_id)
                return []

            # Auto-approve tool requests that don't need user interaction.
            # _DIFF_PREVIEW_TOOLS is module-scoped — see top of file.
            _TOOLS_REQUIRING_APPROVAL = {"ExitPlanMode", "AskUserQuestion"}
            if isinstance(request, claude_schema.ControlCanUseToolRequest):
                tool_name = getattr(request, "tool_name", "unknown")
                if tool_name not in _TOOLS_REQUIRING_APPROVAL:
                    # When diff_preview is enabled, route previewable tools
                    # through interactive approval so users see the diff.
                    # Bypass after ExitPlanMode approval — the user already
                    # reviewed the plan, per-tool approval is redundant (#283).
                    run_opts = get_run_options()
                    session_id = factory.resume.value if factory.resume else None
                    plan_approved = (
                        session_id is not None and session_id in _PLAN_EXIT_APPROVED
                    )
                    if (
                        run_opts
                        and run_opts.diff_preview is True
                        and tool_name in _DIFF_PREVIEW_TOOLS
                        and not plan_approved
                    ):
                        logger.debug(
                            "control_request.diff_preview_gate",
                            request_id=request_id,
                            tool_name=tool_name,
                        )
                    else:
                        logger.debug(
                            "control_request.auto_approve_tool",
                            request_id=request_id,
                            tool_name=tool_name,
                        )
                        _REQUEST_TO_INPUT[request_id] = getattr(request, "input", {})
                        state.auto_approve_queue.append(request_id)
                        return []

            # Auto-deny AskUserQuestion when ask_questions toggle is OFF
            if isinstance(request, claude_schema.ControlCanUseToolRequest):
                tool_name = getattr(request, "tool_name", "")
                if tool_name == "AskUserQuestion":
                    run_opts = get_run_options()
                    if run_opts and run_opts.ask_questions is False:
                        logger.info(
                            "control_request.ask_questions_disabled",
                            request_id=request_id,
                        )
                        _REQUEST_TO_INPUT.pop(request_id, None)
                        _REQUEST_TO_TOOL_NAME.pop(request_id, None)
                        state.auto_deny_queue.append(
                            (
                                request_id,
                                "AskUserQuestion is disabled. Proceed with reasonable "
                                "defaults and state your assumptions.",
                            )
                        )
                        return []

            # Auto-approve ExitPlanMode in "auto" permission mode
            if isinstance(request, claude_schema.ControlCanUseToolRequest):
                tool_name = getattr(request, "tool_name", "")
                if tool_name == "ExitPlanMode" and state.auto_approve_exit_plan_mode:
                    logger.debug(
                        "control_request.auto_approve_exit_plan_mode",
                        request_id=request_id,
                    )
                    # #283: also bypass diff_preview gate for subsequent tools
                    # — same as interactive approval. Without this, users in
                    # auto permission mode + diff_preview enabled still see
                    # individual tool gates after plan approval (#309).
                    auto_session = factory.resume.value if factory.resume else None
                    if auto_session is not None:
                        _PLAN_EXIT_APPROVED.add(auto_session)
                    _REQUEST_TO_INPUT[request_id] = getattr(request, "input", {})
                    state.auto_approve_queue.append(request_id)
                    return []

            # Auto-approve ExitPlanMode after user approved via post-outline buttons
            if isinstance(request, claude_schema.ControlCanUseToolRequest):
                tool_name = getattr(request, "tool_name", "")
                if tool_name == "ExitPlanMode" and factory.resume:
                    session_id = factory.resume.value
                    if session_id in _DISCUSS_APPROVED:
                        _DISCUSS_APPROVED.discard(session_id)
                        _OUTLINE_PENDING.discard(session_id)
                        clear_discuss_cooldown(session_id)
                        # #283: bypass diff_preview gate for subsequent tools
                        # in this session (#309).
                        _PLAN_EXIT_APPROVED.add(session_id)
                        logger.info(
                            "control_request.discuss_approved",
                            request_id=request_id,
                            session_id=session_id,
                        )
                        _REQUEST_TO_INPUT[request_id] = getattr(request, "input", {})
                        state.auto_approve_queue.append(request_id)
                        return []

            # Rate-limit ExitPlanMode after a discuss denial.
            # Both paths (outline written / not written) auto-deny and show
            # synthetic 2-button Approve/Deny.  The old "fall through to normal
            # 3-button flow" caused a confusing loop where the user kept seeing
            # the same Pause & Outline Plan button.
            if isinstance(request, claude_schema.ControlCanUseToolRequest):
                tool_name = getattr(request, "tool_name", "")
                if tool_name == "ExitPlanMode" and factory.resume:
                    session_id = factory.resume.value
                    text_len = state.max_text_len_since_cooldown

                    # Guard: if outline is pending but Claude hasn't written
                    # enough visible text, block ExitPlanMode regardless of
                    # whether the time-based cooldown has expired.
                    outline_guard = (
                        session_id in _OUTLINE_PENDING and text_len < _OUTLINE_MIN_CHARS
                    )
                    # Catch outline-pending sessions even after cooldown expires.
                    # Without this, expired cooldown + written outline would skip
                    # the synthetic 2-button flow and fall through to normal
                    # 3-button ExitPlanMode (showing "Pause & Outline" again).
                    outline_ready = (
                        session_id in _OUTLINE_PENDING
                        and text_len >= _OUTLINE_MIN_CHARS
                    )

                    escalation_msg = check_discuss_cooldown(session_id)
                    if outline_guard or escalation_msg is not None or outline_ready:
                        if text_len >= _OUTLINE_MIN_CHARS:
                            # Outline was written — hold the request open.
                            # Don't auto-deny; keep the control request pending
                            # so Claude blocks on stdin until the user clicks
                            # Approve/Deny in Telegram.
                            logger.info(
                                "control_request.discuss_outline_hold_open",
                                request_id=request_id,
                                session_id=session_id,
                                text_chars=text_len,
                            )
                            _OUTLINE_PENDING.discard(session_id)
                            state.max_text_len_since_cooldown = 0
                            # Store as pending so the 5-min timeout safety net
                            # applies.  Register session/input/tool-name mappings
                            # here because the early return below skips the normal
                            # registration at line ~779.
                            state.pending_control_requests[request_id] = (
                                event,
                                time.time(),
                            )
                            _REQUEST_TO_SESSION[request_id] = session_id
                            _REQUEST_TO_INPUT[request_id] = getattr(
                                request, "input", {}
                            )
                            _REQUEST_TO_TOOL_NAME[request_id] = getattr(
                                request, "tool_name", ""
                            )
                        else:
                            # Retry without outline — auto-deny with escalation.
                            # outline_guard catches expired-cooldown retries too.
                            logger.info(
                                "control_request.discuss_cooldown_deny",
                                request_id=request_id,
                                session_id=session_id,
                                outline_guard=outline_guard,
                            )
                            deny_msg = escalation_msg or _DISCUSS_ESCALATION_MESSAGE
                            _REQUEST_TO_INPUT.pop(request_id, None)
                            _REQUEST_TO_TOOL_NAME.pop(request_id, None)
                            state.auto_deny_queue.append((request_id, deny_msg))

                        # Show synthetic Approve/Deny buttons (no "Pause" option).
                        # For outline-ready: uses the REAL request_id so the
                        # normal approve/deny flow in claude_control.py responds
                        # directly to the held-open control request.
                        # For escalation: uses da: prefix (discuss-approve) since
                        # the request was already auto-denied.
                        state.note_seq += 1
                        synth_action_id = f"claude.discuss_approve.{state.note_seq}"
                        if text_len >= _OUTLINE_MIN_CHARS:
                            button_request_id = request_id
                        else:
                            button_request_id = f"da:{session_id}"
                            _REQUEST_TO_SESSION[button_request_id] = session_id

                        # Send full outline as a separate ephemeral message
                        # (progress message is limited to 4096 chars and truncates).
                        # The outline_full_text in detail triggers ProgressEdits
                        # to send it as a standalone message.
                        outline_detail: dict[str, object] = {}
                        if state.outline_text:
                            synth_title = "📋 Plan outline (see above)"
                            outline_detail["outline_full_text"] = state.outline_text
                            state.outline_text = None
                        else:
                            synth_title = "Plan outlined — approve to proceed"

                        return [
                            state.factory.action_started(
                                action_id=synth_action_id,
                                kind="warning",
                                title=synth_title,
                                detail={
                                    **outline_detail,
                                    "request_id": button_request_id,
                                    "request_type": "DiscussApproval",
                                    "inline_keyboard": {
                                        "buttons": [
                                            [
                                                {
                                                    "text": "✅ Approve Plan",
                                                    "callback_data": f"claude_control:approve:{button_request_id}",
                                                },
                                                {
                                                    "text": "❌ Deny",
                                                    "callback_data": f"claude_control:deny:{button_request_id}",
                                                },
                                            ],
                                            [
                                                {
                                                    "text": "💬 Let's discuss",
                                                    "callback_data": f"claude_control:chat:{button_request_id}",
                                                },
                                            ],
                                        ]
                                    },
                                },
                            ),
                        ]

            # Phase 2: Interactive control request with inline keyboard
            request_type = (
                type(request).__name__.replace("Control", "").replace("Request", "")
            )

            # Extract details based on request type
            details = ""
            diff_preview = ""
            if isinstance(request, claude_schema.ControlCanUseToolRequest):
                tool_name = getattr(request, "tool_name", "unknown")
                tool_input = getattr(request, "input", {})
                details = f"tool: {tool_name}"
                # Include key input parameters if available
                if tool_input:
                    key_params = []
                    for key in ["file_path", "path", "command", "pattern"]:
                        if key in tool_input:
                            value = str(tool_input[key])
                            if len(value) > 50:
                                value = value[:47] + "..."
                            key_params.append(f"{key}={value}")
                    if key_params:
                        details += f" ({', '.join(key_params)})"
                # CC4: Diff preview for Edit/Write tools (gated on per-chat setting)
                run_opts = get_run_options()
                if run_opts is None or run_opts.diff_preview is not False:
                    diff_preview = _format_diff_preview(tool_name, tool_input)
            elif isinstance(request, claude_schema.ControlSetPermissionModeRequest):
                mode = getattr(request, "mode", "unknown")
                details = f"mode: {mode}"
            elif isinstance(request, claude_schema.ControlHookCallbackRequest):
                callback_id = getattr(request, "callback_id", "unknown")
                details = f"callback: {callback_id}"

            warning_text = f"Permission Request [{request_type}]"
            if details:
                warning_text += f" - {details}"
            if diff_preview:
                warning_text += f"\n{diff_preview}"

            # Store in pending requests with timestamp
            state.pending_control_requests[request_id] = (event, time.time())

            # Phase 2: Register request_id -> session_id mapping for callback routing
            if factory.resume:
                session_id = factory.resume.value
                _REQUEST_TO_SESSION[request_id] = session_id
                # Store original tool input and tool name for response handling
                if isinstance(request, claude_schema.ControlCanUseToolRequest):
                    _REQUEST_TO_INPUT[request_id] = getattr(request, "input", {})
                    _REQUEST_TO_TOOL_NAME[request_id] = getattr(
                        request, "tool_name", ""
                    )
                logger.debug(
                    "control_request.registered",
                    request_id=request_id,
                    session_id=session_id,
                )

            # Reconcile requests that were handled via Telegram callback.
            # send_claude_control_response() can't access state, so it marks
            # handled requests in _HANDLED_REQUESTS.  We reconcile here to:
            # 1. Remove from pending (prevents spurious expired_auto_deny)
            # 2. Emit action_completed to clear stale inline keyboards
            # See: https://github.com/littlebearapps/untether/issues/229
            reconciled_events: list[UntetherEvent] = []
            callback_handled = [
                rid
                for rid in state.pending_control_requests
                if rid in _HANDLED_REQUESTS
            ]
            for rid in callback_handled:
                del state.pending_control_requests[rid]
                action_id_for_req = state.request_to_action.pop(rid, None)
                if action_id_for_req:
                    # Remove from control_action_for_tool so tool_result
                    # doesn't try to complete it again
                    state.control_action_for_tool = {
                        k: v
                        for k, v in state.control_action_for_tool.items()
                        if v != action_id_for_req
                    }
                    reconciled_events.append(
                        factory.action_completed(
                            action_id=action_id_for_req,
                            kind="warning",
                            title="Permission resolved",
                            ok=True,
                        )
                    )
                logger.debug(
                    "control_request.reconciled",
                    request_id=rid,
                    action_id=action_id_for_req,
                )

            # Clean up expired requests (older than timeout).
            # Send auto-deny to unblock the subprocess — without this,
            # Claude Code blocks forever waiting for a response that never comes.
            # See: https://github.com/banteg/takopi/issues/215
            current_time = time.time()
            expired = [
                rid
                for rid, (_, timestamp) in state.pending_control_requests.items()
                if current_time - timestamp > CONTROL_REQUEST_TIMEOUT_SECONDS
                and rid not in _HANDLED_REQUESTS  # belt-and-suspenders (#229)
            ]
            for rid in expired:
                del state.pending_control_requests[rid]
                _REQUEST_TO_INPUT.pop(rid, None)
                _REQUEST_TO_TOOL_NAME.pop(rid, None)
                state.request_to_action.pop(rid, None)
                state.auto_deny_queue.append(
                    (rid, "Request timed out — no response from user within 5 minutes.")
                )
                logger.warning("control_request.expired_auto_deny", request_id=rid)

            # Check max pending limit
            if len(state.pending_control_requests) > 100:
                logger.warning(
                    "control_request.max_pending",
                    count=len(state.pending_control_requests),
                )

            state.note_seq += 1
            action_id = f"claude.control.{state.note_seq}"

            # Map the preceding tool_use_id to this control action for cleanup
            if state.last_tool_use_id:
                state.control_action_for_tool[state.last_tool_use_id] = action_id
            # Map request_id -> action_id for reconciling callback-handled requests (#229)
            state.request_to_action[request_id] = action_id

            # Include inline keyboard data in detail
            button_rows: list[list[dict[str, str]]] = [
                [
                    {
                        "text": "✅ Approve",
                        "callback_data": f"claude_control:approve:{request_id}",
                    },
                    {
                        "text": "❌ Deny",
                        "callback_data": f"claude_control:deny:{request_id}",
                    },
                ],
            ]
            # ExitPlanMode gets an extra "Outline Plan" button
            if isinstance(request, claude_schema.ControlCanUseToolRequest):
                tool_name = getattr(request, "tool_name", "")
                if tool_name == "ExitPlanMode":
                    button_rows.append(
                        [
                            {
                                "text": "📋 Pause & Outline Plan",
                                "callback_data": f"claude_control:discuss:{request_id}",
                            },
                        ]
                    )

            # A1: AskUserQuestion — extract questions and render option buttons
            ask_question: str | None = None
            if isinstance(request, claude_schema.ControlCanUseToolRequest):
                tool_name = getattr(request, "tool_name", "")
                if tool_name == "AskUserQuestion":
                    from ..utils.paths import get_run_channel_id

                    _ask_channel = get_run_channel_id() or 0
                    # Parse the full questions array
                    questions_list: list[dict[str, Any]] = []
                    if tool_input:
                        raw_questions = tool_input.get("questions", [])
                        if raw_questions and isinstance(raw_questions, list):
                            questions_list = [
                                q for q in raw_questions if isinstance(q, dict)
                            ]
                        # Fallback: single "question" key without options
                        if not questions_list:
                            single_q = tool_input.get("question", "")
                            if single_q:
                                questions_list = [{"question": single_q}]

                    if questions_list:
                        first_q = questions_list[0]
                        ask_question = first_q.get("question", "")
                        options = first_q.get("options", [])
                        total = len(questions_list)

                        # Build question header with counter
                        if total > 1:
                            warning_text = f"❓ Question 1 of {total}: {ask_question}"
                        else:
                            warning_text = f"❓ {ask_question}"

                        # Create flow state and option buttons
                        if options and isinstance(options, list):
                            flow = AskQuestionState(
                                request_id=request_id,
                                channel_id=_ask_channel,
                                questions=questions_list,
                            )
                            _ASK_QUESTION_FLOWS[request_id] = flow
                            # Replace Approve/Deny with option buttons
                            button_rows.clear()
                            for i, opt in enumerate(options[:4]):
                                label = opt.get("label", f"Option {i + 1}")
                                # Truncate label to fit 64-byte callback limit
                                # Format: aq:opt:N — very compact
                                button_rows.append(
                                    [
                                        {
                                            "text": label,
                                            "callback_data": f"aq:opt:{i}",
                                        }
                                    ]
                                )
                            # Add "Other" button for free text
                            button_rows.append(
                                [
                                    {
                                        "text": "Other (type reply)",
                                        "callback_data": "aq:other",
                                    }
                                ]
                            )
                        else:
                            # No options — keep Approve/Deny for text reply
                            pass

                    else:
                        session_id = factory.resume.value if factory.resume else None
                        logger.warning(
                            "ask_question.extraction_failed",
                            request_id=request_id,
                            session_id=session_id,
                            tool_input_keys=list(tool_input.keys())
                            if tool_input
                            else [],
                        )
                        ask_question = ""

                    # Register this request for reply handling (scoped by channel)
                    _PENDING_ASK_REQUESTS[request_id] = (
                        _ask_channel,
                        ask_question or "",
                    )

            detail: dict[str, Any] = {
                "request_id": request_id,
                "request_type": request_type,
                "inline_keyboard": {
                    "buttons": button_rows,
                },
            }
            if ask_question:
                detail["ask_question"] = ask_question

            return [
                *reconciled_events,
                factory.action_started(
                    action_id=action_id,
                    kind="warning",  # Use warning kind for visibility
                    title=warning_text,
                    detail=detail,
                ),
            ]
        case claude_schema.StreamRateLimitMessage(rate_limit_info=info):
            # #349: surface rate_limit_event as a visible "waiting for API" note
            # so the user sees a clear "Anthropic is throttling us, we're waiting"
            # status instead of silent inactivity + eventual mystery cancel.
            retry_ms = info.retry_after_ms if info is not None else None
            retry_s = retry_ms / 1000.0 if retry_ms is not None else None
            # #518: when retry_after_ms is missing, derive retry_after_s from
            # the requests_reset / tokens_reset ISO timestamps so subscription-
            # cap throttles (which the rc13 audit showed always emit "bare"
            # rate_limit_events) still surface an actionable wait time and
            # accumulate into cumulative_s.
            retry_s_source = "retry_after_ms"
            if retry_s is None:
                derived = _derive_retry_after_s(info)
                if derived is not None:
                    retry_s = derived
                    retry_s_source = "reset_ts"
            if retry_s is not None:
                state.rate_limit_total_s += retry_s
            state.rate_limit_count += 1
            state.note_seq += 1
            action_id = f"rate_limit_{state.note_seq}"
            if retry_s is not None:
                # Round to nearest second for display but show fractional when < 1s
                display_s = int(retry_s) if retry_s >= 1 else f"{retry_s:.1f}"
                title = f"⏳ Rate limited — retrying in {display_s}s"
            else:
                title = "⏳ Rate limited — waiting to retry"
            detail: dict[str, Any] = {}
            if info is not None:
                if info.tokens_remaining is not None:
                    detail["tokens_remaining"] = info.tokens_remaining
                if info.requests_remaining is not None:
                    detail["requests_remaining"] = info.requests_remaining
                if retry_ms is not None:
                    detail["retry_after_ms"] = retry_ms
            # #518: log all RateLimitInfo fields when present so future audits
            # can see what upstream actually sent, instead of having to back-
            # infer from the single-field log line that was here before.
            info_payload: dict[str, Any] = {}
            if info is not None:
                for field_name in (
                    "requests_limit",
                    "requests_remaining",
                    "requests_reset",
                    "tokens_limit",
                    "tokens_remaining",
                    "tokens_reset",
                    "retry_after_ms",
                ):
                    value = getattr(info, field_name, None)
                    if value is not None:
                        info_payload[field_name] = value
            logger.info(
                "claude.rate_limit_event",
                retry_after_s=retry_s,
                retry_after_source=retry_s_source if retry_s is not None else None,
                count=state.rate_limit_count,
                cumulative_s=state.rate_limit_total_s,
                info=info_payload or None,
            )
            return [
                factory.action_started(
                    action_id=action_id,
                    kind="note",
                    title=title,
                    detail=detail,
                ),
                factory.action_completed(
                    action_id=action_id,
                    kind="note",
                    title=title,
                    ok=True,
                    level="info",
                    detail=detail,
                ),
            ]
        case _:
            logger.debug(
                "claude.event.unrecognised",
                event_type=type(event).__name__,
            )
            return []


@dataclass(slots=True)
class ClaudeRunner(ResumeTokenMixin, JsonlSubprocessRunner):
    engine: EngineId = ENGINE
    resume_re: re.Pattern[str] = _RESUME_RE

    claude_cmd: str = "claude"
    model: str | None = None
    permission_mode: str | None = None
    allowed_tools: list[str] | None = None
    extra_args: list[str] = field(default_factory=list)
    dangerously_skip_permissions: bool = False
    use_api_billing: bool = False
    session_title: str = "claude"
    logger = logger

    # Phase 2: Control channel support
    supports_control_channel: bool = True
    _pty_master_fd: int | None = None  # legacy PTY approach (non-permission mode)
    _proc_stdin: Any | None = None  # PIPE stdin for control channel (permission mode)
    _control_timeout_seconds: float = CONTROL_REQUEST_TIMEOUT_SECONDS
    _max_pending_control_requests: int = 100

    def format_resume(self, token: ResumeToken) -> str:
        if token.engine != ENGINE:
            raise RuntimeError(f"resume token is for engine {token.engine!r}")
        return f"`claude --resume {token.value}`"

    def _effective_permission_mode(self) -> str | None:
        """Resolve effective permission mode from per-chat override or engine config."""
        run_options = get_run_options()
        return (
            run_options.permission_mode if run_options else None
        ) or self.permission_mode

    async def write_control_response(
        self, request_id: str, approved: bool, *, deny_message: str | None = None
    ) -> bool:
        """Write a control response to the Claude Code process via PIPE or PTY.

        Uses _SESSION_STDIN to find the correct stdin for the session,
        supporting concurrent sessions on the same runner instance.
        """
        if approved:
            inner: dict[str, Any] = {"behavior": "allow"}
            # Claude Code CLI requires updatedInput for can_use_tool responses
            if request_id in _REQUEST_TO_INPUT:
                inner["updatedInput"] = _REQUEST_TO_INPUT.pop(request_id)
            tool_name = _REQUEST_TO_TOOL_NAME.pop(request_id, None)
            # After approving any plan-gated tool, bypass the diff_preview
            # gate for subsequent tools in the same session — the user has
            # already reviewed code, repeating the prompt per-tool is
            # redundant (#283 for ExitPlanMode; #369 extended to diff_preview
            # tools so plan-mode sessions that skip ExitPlanMode also bypass).
            session_id_for_plan = _REQUEST_TO_SESSION.get(request_id)
            if session_id_for_plan and (
                tool_name == "ExitPlanMode" or tool_name in _DIFF_PREVIEW_TOOLS
            ):
                _PLAN_EXIT_APPROVED.add(session_id_for_plan)
        else:
            inner = {"behavior": "deny", "message": deny_message or "User denied"}
            # Clean up stored input on denial too
            _REQUEST_TO_INPUT.pop(request_id, None)
            _REQUEST_TO_TOOL_NAME.pop(request_id, None)
        response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": inner,
            },
        }

        jsonl_line = json.dumps(response) + "\n"

        # Look up the session-specific stdin from _SESSION_STDIN
        session_id = _REQUEST_TO_SESSION.get(request_id)
        session_stdin = _SESSION_STDIN.get(session_id) if session_id else None

        # Prefer session-specific stdin, fall back to instance stdin, then PTY
        stdin_to_use = session_stdin or self._proc_stdin
        if stdin_to_use is not None:
            try:
                await stdin_to_use.send(jsonl_line.encode())
                logger.info(
                    "control_response.sent",
                    request_id=request_id,
                    approved=approved,
                    session_id=session_id,
                    channel="pipe",
                )
                return True
            except (OSError, anyio.ClosedResourceError) as e:
                logger.warning(
                    "control_response.pipe_closed",
                    request_id=request_id,
                    approved=approved,
                    session_id=session_id,
                    error=str(e),
                    error_type=e.__class__.__name__,
                    channel="pipe",
                )
                return False
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "control_response.write_failed",
                    request_id=request_id,
                    approved=approved,
                    session_id=session_id,
                    error=str(e),
                    error_type=e.__class__.__name__,
                    channel="pipe",
                )
                return False
        elif self._pty_master_fd is not None:
            try:
                os.write(self._pty_master_fd, jsonl_line.encode())
                logger.info(
                    "control_response.sent",
                    request_id=request_id,
                    approved=approved,
                    session_id=session_id,
                    channel="pty",
                )
                return True
            except OSError as e:
                logger.warning(
                    "control_response.pipe_closed",
                    request_id=request_id,
                    approved=approved,
                    session_id=session_id,
                    error=str(e),
                    error_type=e.__class__.__name__,
                    channel="pty",
                )
                return False
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "control_response.write_failed",
                    request_id=request_id,
                    approved=approved,
                    session_id=session_id,
                    error=str(e),
                    error_type=e.__class__.__name__,
                    channel="pty",
                )
                return False
        else:
            logger.warning(
                "control_response.no_channel",
                request_id=request_id,
                approved=approved,
                session_id=session_id,
            )
            return False

    def _build_args(self, prompt: str, resume: ResumeToken | None) -> list[str]:
        run_options = get_run_options()
        effective_mode = self._effective_permission_mode()

        # When using permission mode with control channel, don't use -p mode.
        # The SDK-style streaming protocol requires bidirectional stdin/stdout
        # without -p. The prompt is sent as a JSON user message on stdin.
        if effective_mode is not None:
            args: list[str] = [
                "--output-format",
                "stream-json",
                "--input-format",
                "stream-json",
                "--verbose",
            ]
        else:
            args = [
                "-p",
                "--output-format",
                "stream-json",
                "--input-format",
                "stream-json",
                "--verbose",
            ]

        # User-supplied CLI flags (e.g. `--chrome` to opt into Claude-in-Chrome).
        # Must sit after the Untether-managed I/O prelude but before
        # resume / model / effort / allowed-tools / permission so the final
        # prompt position (after `--`) is never displaced (#407).
        args.extend(self.extra_args)

        if resume is not None:
            if resume.is_continue:
                args.append("--continue")
            else:
                args.extend(["--resume", resume.value])
        model = self.model
        if run_options is not None and run_options.model:
            model = run_options.model
        if model is not None:
            args.extend(["--model", str(model)])
        reasoning = None
        if run_options is not None and run_options.reasoning:
            reasoning = run_options.reasoning
        if reasoning is not None:
            args.extend(["--effort", reasoning])
        allowed_tools = _coerce_comma_list(self.allowed_tools)
        if allowed_tools is not None:
            args.extend(["--allowedTools", allowed_tools])
        if self.dangerously_skip_permissions is True:
            args.append("--dangerously-skip-permissions")

        if effective_mode is not None:
            cli_mode = "plan" if effective_mode == "auto" else effective_mode
            args.extend(["--permission-mode", cli_mode])
            args.extend(["--permission-prompt-tool", "stdio"])
            # Prompt sent via stdin as JSON, not as CLI arg
        else:
            args.append("--")
            args.append(prompt)

        return args

    def command(self) -> str:
        return self.claude_cmd

    def build_args(
        self,
        prompt: str,
        resume: ResumeToken | None,
        *,
        state: Any,
    ) -> list[str]:
        return self._build_args(prompt, resume)

    def stdin_payload(
        self,
        prompt: str,
        resume: ResumeToken | None,
        *,
        state: Any,
    ) -> bytes | None:
        effective_mode = self._effective_permission_mode()
        if effective_mode is not None:
            # SDK-style control channel: send init handshake + user message.
            # The CLI reads both from stdin (no -p mode).
            init_request = {
                "type": "control_request",
                "request_id": f"init_{id(self)}",
                "request": {"subtype": "initialize", "hooks": None},
            }
            user_message = {
                "type": "user",
                "session_id": resume.value if resume else "",
                "message": {
                    "role": "user",
                    "content": prompt,
                },
                "parent_tool_use_id": None,
            }
            payload = json.dumps(init_request) + "\n" + json.dumps(user_message) + "\n"
            return payload.encode()
        return None

    def env(self, *, state: Any) -> dict[str, str] | None:
        # #198: allowlist filter — Claude subprocess no longer inherits the
        # parent's full environment. Only vars recognised by
        # `utils.env_policy` (basic OS, AI/cloud provider keys, Claude /
        # MCP namespaces, etc.) flow through. See env_policy.py for the
        # canonical list + how to extend it when a new MCP or engine needs
        # an unfamiliar variable.
        from ..utils.env_policy import filtered_env, log_user_extensions_once

        # #409: thread per-deployment extras from
        # [security] env_extra_allow / env_extra_prefix_allow.
        extra_exact, extra_prefix = _load_env_extras()
        log_user_extensions_once(extra_exact, extra_prefix)
        env = filtered_env(extra_allow=extra_exact, extra_prefix=extra_prefix)
        # Let Claude Code hooks detect Untether sessions (e.g. PitchDocs
        # context-guard skips blocking Stop hooks in Telegram).
        env["UNTETHER_SESSION"] = "1"
        # Reinforcements for upstream claude-code#39700 / #41086 / #38437 —
        # stream-json mode hangs after MCP tool_result. Shell env is honoured
        # by Claude Code 2.1.110+ for the sdk-cli stdio path. Use setdefault
        # so user overrides (shell rc, per-project env) always win. See #322.
        env.setdefault("CLAUDE_ENABLE_STREAM_WATCHDOG", "1")
        # #342: opus on `max` reasoning can legitimately idle its SSE stream
        # for 60-120s while chain-of-thought expands between output deltas; a
        # 60s watchdog trips and aborts the run mid-reasoning ("API Error:
        # Stream idle timeout - partial response received"). 300000ms (5 min)
        # matches the undici idle-body timeout that motivated #322 *and*
        # Untether's own `stuck_after_tool_result_timeout` default, so the
        # upstream CLI watchdog and our detector fire in the same window.
        # #438: now user-configurable via [watchdog] claude_stream_idle_timeout_ms
        # so deployments hitting upstream Anthropic API stalls can ride out
        # longer silences. setdefault still respects shell-set overrides.
        idle_timeout_default = "300000"
        try:
            result = load_settings_if_exists()
            if result is not None:
                settings, _ = result
                idle_timeout_default = str(
                    settings.watchdog.claude_stream_idle_timeout_ms
                )
        except Exception:  # noqa: BLE001 — settings errors must not block a run
            logger.debug(
                "claude_stream_idle_timeout.settings_load_failed", exc_info=True
            )
        env.setdefault("CLAUDE_STREAM_IDLE_TIMEOUT_MS", idle_timeout_default)
        env.setdefault("MCP_TOOL_TIMEOUT", "120000")
        env.setdefault("MAX_MCP_OUTPUT_TOKENS", "12000")
        if self.use_api_billing is not True:
            env.pop("ANTHROPIC_API_KEY", None)
        return env

    def new_state(self, prompt: str, resume: ResumeToken | None) -> ClaudeStreamState:
        state = ClaudeStreamState()
        state.auto_approve_exit_plan_mode = self._effective_permission_mode() == "auto"
        state.resumed = resume is not None
        # #289 capture the first user message so loop observers can fall back
        # to it when ScheduleWakeup uses the <<autonomous-loop-dynamic>>
        # sentinel.  For resumed runs this is the resume prompt (still better
        # than letting the sentinel reach Claude verbatim).
        state.first_user_message_text = prompt
        # #365 propagate MCP catalog observability knobs from WatchdogSettings.
        # Defaults on the dataclass already mirror WatchdogSettings defaults,
        # so a load failure is a safe no-op.
        try:
            result = load_settings_if_exists()
            if result is not None:
                settings, _ = result
                state.detect_catalog_staleness = (
                    settings.watchdog.detect_catalog_staleness
                )
                state.notify_catalog_refresh = settings.watchdog.notify_catalog_refresh
                state.catalog_refresh_min_interval_s = (
                    settings.watchdog.catalog_refresh_min_interval_s
                )
        except Exception:  # noqa: BLE001 — settings errors must not block a run
            logger.warning("catalog_settings.load_failed", exc_info=True)
        return state

    def start_run(
        self,
        prompt: str,
        resume: ResumeToken | None,
        *,
        state: ClaudeStreamState,
    ) -> None:
        # Phase 2: Register this runner for control responses
        if (
            resume is not None
            and not resume.is_continue
            and self.supports_control_channel
        ):
            _ACTIVE_RUNNERS[resume.value] = (self, time.time())
            logger.info(
                "claude_runner.registered",
                session_id=resume.value,
                registries=["active_runners"],
            )

    def decode_jsonl(
        self,
        *,
        line: bytes,
    ) -> claude_schema.StreamJsonMessage:
        return claude_schema.decode_stream_json_line(line)

    def decode_error_events(
        self,
        *,
        raw: str,
        line: str,
        error: Exception,
        state: ClaudeStreamState,
    ) -> list[UntetherEvent]:
        if isinstance(error, msgspec.DecodeError):
            self.get_logger().warning(
                "jsonl.msgspec.invalid",
                tag=self.tag(),
                error=str(error),
                error_type=error.__class__.__name__,
            )
            return []
        return super().decode_error_events(
            raw=raw,
            line=line,
            error=error,
            state=state,
        )

    def invalid_json_events(
        self,
        *,
        raw: str,
        line: str,
        state: ClaudeStreamState,
    ) -> list[UntetherEvent]:
        return []

    async def _iter_jsonl_events(
        self,
        *,
        stdout: Any,
        stream: JsonlStreamState,
        state: ClaudeStreamState,
        resume: ResumeToken | None,
        logger: Any,
        pid: int,
        session_stdin: Any = None,
    ) -> AsyncIterator[UntetherEvent]:
        """Override to drain auto-approve queue after every line, not just after yielded events.

        The base class only drains auto-approves in run_impl after `yield evt`.
        If a line produces no events (e.g. auto-approved control requests), the drain
        never runs, causing a deadlock when Claude Code blocks waiting for the response.

        session_stdin is passed from run_impl to avoid using self._proc_stdin
        which may be overwritten by a concurrent session on the same runner.
        """
        registered_session_id: str | None = None
        async for raw_line in self.iter_json_lines(stdout):
            for evt in self._handle_jsonl_line(
                raw_line=raw_line,
                stream=stream,
                state=state,
                resume=resume,
                logger=logger,
                pid=pid,
            ):
                # Register _SESSION_STDIN here (not in translate) because we
                # have the correct captured stdin.  translate() would use the
                # stale self._proc_stdin which may have been overwritten by a
                # concurrent session on the same runner.
                if (
                    not registered_session_id
                    and isinstance(evt, StartedEvent)
                    and evt.resume
                ):
                    registered_session_id = evt.resume.value
                    _SESSION_STDIN[registered_session_id] = session_stdin
                    logger.info(
                        "session_stdin.registered",
                        session_id=registered_session_id,
                        pid=pid,
                    )
                yield evt
            # Drain auto-approve and auto-deny queues after EVERY line, even if no events
            # were yielded.  This prevents deadlock when auto-handled requests produce no events.
            await self._drain_auto_approve(state, stdin=session_stdin)
            await self._drain_auto_deny(state, stdin=session_stdin)
            # #365 fire-and-forget mcp_status control_requests queued by
            # translate_claude_event on tool_result. Drain last so the
            # response (if any) arrives after Claude has processed the
            # tool_result itself.
            await self._drain_catalog_refresh(state, stdin=session_stdin)
            # After CompletedEvent, stop reading stdout immediately.
            # Claude Code's MCP server child processes may inherit the stdout pipe FD,
            # keeping it open even after Claude Code exits. Without this break,
            # we'd block forever waiting for EOF that never comes.
            if stream.did_emit_completed:
                break

    async def _drain_auto_approve(
        self, state: ClaudeStreamState, *, stdin: Any = None
    ) -> None:
        """Drain the auto-approve queue, writing responses to the control channel."""
        if not state.auto_approve_queue:
            return

        # Use provided stdin (session-specific) or fall back to instance
        pipe = stdin or self._proc_stdin
        for req_id in state.auto_approve_queue:
            inner: dict[str, Any] = {"behavior": "allow"}
            if req_id in _REQUEST_TO_INPUT:
                inner["updatedInput"] = _REQUEST_TO_INPUT.pop(req_id)
            response = {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": req_id,
                    "response": inner,
                },
            }
            payload = (json.dumps(response) + "\n").encode()
            try:
                if pipe is not None:
                    await pipe.send(payload)
                    logger.info(
                        "control_response.auto_approved",
                        request_id=req_id,
                        channel="pipe",
                    )
                elif self._pty_master_fd is not None:
                    os.write(self._pty_master_fd, payload)
                    logger.info(
                        "control_response.auto_approved",
                        request_id=req_id,
                        channel="pty",
                    )
                else:
                    logger.warning(
                        "control_response.auto_approve_failed", request_id=req_id
                    )
            except (OSError, anyio.ClosedResourceError) as e:
                logger.warning(
                    "control_response.auto_approve_failed",
                    request_id=req_id,
                    error=str(e),
                )
        state.auto_approve_queue.clear()

    async def _drain_auto_deny(
        self, state: ClaudeStreamState, *, stdin: Any = None
    ) -> None:
        """Drain the auto-deny queue, writing deny responses to the control channel."""
        if not state.auto_deny_queue:
            return

        pipe = stdin or self._proc_stdin
        for req_id, message in state.auto_deny_queue:
            response = {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": req_id,
                    "response": {"behavior": "deny", "message": message},
                },
            }
            payload = (json.dumps(response) + "\n").encode()
            try:
                if pipe is not None:
                    await pipe.send(payload)
                    logger.info(
                        "control_response.auto_denied",
                        request_id=req_id,
                        channel="pipe",
                    )
                elif self._pty_master_fd is not None:
                    os.write(self._pty_master_fd, payload)
                    logger.info(
                        "control_response.auto_denied", request_id=req_id, channel="pty"
                    )
                else:
                    logger.warning(
                        "control_response.auto_deny_failed", request_id=req_id
                    )
            except (OSError, anyio.ClosedResourceError) as e:
                logger.warning(
                    "control_response.auto_deny_failed",
                    request_id=req_id,
                    error=str(e),
                )
        state.auto_deny_queue.clear()

    async def _drain_catalog_refresh(
        self, state: ClaudeStreamState, *, stdin: Any = None
    ) -> None:
        """Send queued mcp_status control_requests to Claude Code (#365).

        Fire-and-forget: Untether does not register a pending response for
        these IDs and does not wait on the eventual ``control_response``
        (Claude Code will emit one with ``request_id`` matching; our
        existing JSONL decoder treats unknown control_response events as
        a no-op at present). The goal is to nudge Claude Code's MCP
        catalog state, per P0#1 of #365.

        Logs ``catalog.refresh_sent`` per request on success and
        ``catalog.refresh_failed`` on write errors so staging can observe
        frequency + failure modes independently.
        """
        if not state.pending_catalog_refresh_ids:
            return
        pipe = stdin or self._proc_stdin
        for req_id in state.pending_catalog_refresh_ids:
            request = {
                "type": "control_request",
                "request_id": req_id,
                "request": {"subtype": "mcp_status"},
            }
            payload = (json.dumps(request) + "\n").encode()
            try:
                if pipe is not None:
                    await pipe.send(payload)
                    logger.info(
                        "catalog.refresh_sent",
                        request_id=req_id,
                        channel="pipe",
                    )
                elif self._pty_master_fd is not None:
                    os.write(self._pty_master_fd, payload)
                    logger.info(
                        "catalog.refresh_sent",
                        request_id=req_id,
                        channel="pty",
                    )
                else:
                    logger.warning(
                        "catalog.refresh_failed",
                        request_id=req_id,
                        reason="no_channel",
                    )
            except (OSError, anyio.ClosedResourceError) as e:
                logger.warning(
                    "catalog.refresh_failed",
                    request_id=req_id,
                    error=str(e),
                    error_type=e.__class__.__name__,
                )
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "catalog.refresh_failed",
                    request_id=req_id,
                    error=str(e),
                    error_type=e.__class__.__name__,
                )
        state.pending_catalog_refresh_ids.clear()

    async def _post_result_idle_watchdog(
        self,
        state: ClaudeStreamState,
        this_proc_stdin: Any,
        reader_done: anyio.Event,
        run_logger: Any,
        timeout_s: float,
    ) -> None:
        """Close stdin once the bidirectional CLI has been idle past the result.

        After ``StreamResultMessage`` the Claude CLI stays alive in the
        bidirectional/permission-mode protocol so multi-turn sessions don't
        re-spawn. In practice (#333) this leaves a 400 MB RSS subprocess
        plus ~200 TCP sockets idling for 30+ minutes between user prompts.

        Mechanism: poll ``state.result_received_at``. When elapsed exceeds
        ``timeout_s`` and no approval-state references the session, close
        ``this_proc_stdin`` (same call as the normal-flow exit on line
        2412). The CLI hits stdin EOF and exits gracefully (rc=0). The
        auto-continue safety gate excludes ``last_event_type == "result"``
        so the clean exit will not phantom-resume the session
        (test_skips_result_event_type in test_exec_bridge.py locks this).

        Approval-state guard: ``_REQUEST_TO_SESSION`` and
        ``_PENDING_ASK_REQUESTS`` track in-flight callback responses. If
        either has live entries for this session we re-arm the timer
        rather than orphaning a button-click control_response that's
        mid-flight.
        """
        # Poll often enough to react within a few seconds of the deadline,
        # but not so often that we burn CPU on a fully idle session.
        poll_interval = max(5.0, min(timeout_s / 20.0, 30.0))
        while not reader_done.is_set():
            await anyio.sleep(poll_interval)
            if reader_done.is_set():
                return
            armed_at = state.result_received_at
            if armed_at is None:
                continue
            elapsed = time.monotonic() - armed_at

            # #507: dead-ScheduleWakeup shortcut. ScheduleWakeup outside
            # ``/loop dynamic mode`` is a silent no-op upstream — the
            # wakeup never fires, the agent's turn ended, and we'd otherwise
            # wait the full ``timeout_s`` (default 600 s) before closing
            # stdin. Detect the case via the live_wakeups registry and the
            # /loop master toggle for this chat; cut the effective timeout
            # to ``max_armed_delay + 60s grace`` so the session closes
            # within ~delay+grace instead of 10 minutes.
            effective_timeout = timeout_s
            dead_wakeup = False
            if state.live_wakeups_arm_delay:
                from ..utils.paths import get_run_channel_id

                _chat_id = get_run_channel_id()
                if _chat_id is not None and not _loop_enabled_for_chat(_chat_id):
                    _max_delay = max(state.live_wakeups_arm_delay.values(), default=0.0)
                    effective_timeout = min(timeout_s, _max_delay + 60.0)
                    dead_wakeup = True
            if elapsed < effective_timeout:
                continue

            # Locate the session id for the approval-state guard. The
            # Claude factory's resume token is set during the very first
            # StartedEvent, so by the time a result lands we always have
            # one — but defend against the rare race where the watchdog
            # ticks before that first started event.
            sid = (
                state.factory.resume.value if state.factory.resume is not None else None
            )
            pending_requests = (
                [k for k, v in _REQUEST_TO_SESSION.items() if v == sid] if sid else []
            )
            pending_asks = (
                [k for k in _PENDING_ASK_REQUESTS if _REQUEST_TO_SESSION.get(k) == sid]
                if sid
                else []
            )
            if pending_requests or pending_asks:
                run_logger.info(
                    "claude.post_result_idle.deferred",
                    session_id=sid,
                    pending_requests=len(pending_requests),
                    pending_asks=len(pending_asks),
                    elapsed_s=round(elapsed, 1),
                    timeout_s=timeout_s,
                )
                # Re-arm: push the deadline forward by one full interval.
                state.result_received_at = time.monotonic()
                continue

            run_logger.info(
                "claude.post_result_idle.closing_stdin",
                session_id=sid,
                elapsed_s=round(elapsed, 1),
                timeout_s=timeout_s,
                effective_timeout_s=round(effective_timeout, 1),
                dead_wakeup=dead_wakeup,
            )
            # #470: stamp closed-at signals BEFORE the actual stdin close
            # so the bridge's heartbeat tick (which polls engine_state via
            # duck-typing) can fire the one-shot closing Telegram message.
            # ``post_result_closing_sent`` stays False — the bridge sets
            # it after the message is sent (idempotency).
            state.post_result_closed_at = time.monotonic()
            state.post_result_idle_minutes = elapsed / 60.0
            with contextlib.suppress(Exception):
                await this_proc_stdin.aclose()
            return

    def translate(
        self,
        data: claude_schema.StreamJsonMessage,
        *,
        state: ClaudeStreamState,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
    ) -> list[UntetherEvent]:
        events = translate_claude_event(
            data,
            title=self.session_title,
            state=state,
            factory=state.factory,
        )

        # Phase 2: Register runner when we get a session_id
        # NOTE: _SESSION_STDIN is registered in _iter_jsonl_events (not here)
        # because self._proc_stdin may be stale if another session has started
        # concurrently on the same runner instance.
        if self.supports_control_channel:
            for evt in events:
                if isinstance(evt, StartedEvent) and evt.resume:
                    session_id = evt.resume.value
                    _ACTIVE_RUNNERS[session_id] = (self, time.time())
                    logger.debug(
                        "claude_runner.registered",
                        session_id=session_id,
                    )

        # Auto-approve queue is drained asynchronously in run_impl
        # after events are yielded (see _drain_auto_approve)

        return events

    def process_error_events(
        self,
        rc: int,
        *,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
        state: ClaudeStreamState,
        stderr_lines: list[str] | None = None,
    ) -> list[UntetherEvent]:
        # Phase 2: Cleanup runner registration on error
        session_id = (
            found_session.value if found_session else (resume.value if resume else None)
        )
        if session_id:
            _cleanup_session_registries(session_id)

        parts = [f"Claude Code failed ({_rc_label(rc)})."]
        session = _session_label(found_session, resume)
        if session:
            parts.append(f"session: {session}")
        excerpt = _stderr_excerpt(stderr_lines)
        if excerpt:
            parts.append(excerpt)
        message = "\n".join(parts)
        resume_for_completed = found_session or resume
        return [
            self.note_event(message, state=state, ok=False),
            state.factory.completed_error(
                error=message,
                resume=resume_for_completed,
            ),
        ]

    def stream_end_events(
        self,
        *,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
        state: ClaudeStreamState,
    ) -> list[UntetherEvent]:
        # Phase 2: Cleanup runner registration
        session_id = (
            found_session.value if found_session else (resume.value if resume else None)
        )
        if session_id:
            _cleanup_session_registries(session_id)

        if not found_session:
            parts = ["Claude Code finished but no session_id was captured"]
            session = _session_label(None, resume)
            if session:
                parts.append(f"session: {session}")
            message = "\n".join(parts)
            resume_for_completed = resume
            return [
                state.factory.completed_error(
                    error=message,
                    resume=resume_for_completed,
                )
            ]

        parts = ["Claude Code finished without a result event"]
        session = _session_label(found_session, resume)
        if session:
            parts.append(f"session: {session}")
        message = "\n".join(parts)
        return [
            state.factory.completed_error(
                error=message,
                answer=state.last_assistant_text or "",
                resume=found_session,
            )
        ]

    async def run_impl(
        self, prompt: str, resume: ResumeToken | None
    ) -> AsyncIterator[UntetherEvent]:
        """
        Override run_impl to support two modes:

        1. Permission mode (SDK-style): No -p flag. Stdin stays open for
           bidirectional control protocol. Init handshake + user message
           sent on stdin; control_request/response flow over stdin/stdout.

        2. Legacy mode: -p flag with PTY stdin. Prompt passed as CLI arg.
           Stdin used only for initial payload, then kept open via PTY.
        """
        state = self.new_state(prompt, resume)
        self.start_run(prompt, resume, state=state)

        tag = self.tag()
        run_logger = self.get_logger()
        cmd = [self.command(), *self.build_args(prompt, resume, state=state)]
        payload = self.stdin_payload(prompt, resume, state=state)
        env = self.env(state=state)
        # #361 wrap with `env -i KEY=VAL ...` so Claude exec resolves with
        # exactly the allowlisted env. Blocks re-introduction from upstream
        # rc-file sourcing, /etc/environment, or wrapper scripts that the
        # filtered env passed to manage_subprocess can't prevent post-exec.
        # Pass env=None to subprocess so we don't double-set.
        if env is not None:
            cmd = wrap_with_env_i(cmd, env)
            env = None
        # #205 / #478: redact two flavours of secret material before logging
        # ``args`` at INFO:
        #   1. ``env -i KEY=VAL`` pairs from wrap_with_env_i embed live
        #      credentials (bot tokens, API keys, BWS access token, ...)
        #      — handled by ``redact_env_i_args`` (#361).
        #   2. In legacy mode ``build_args`` ends with ``-- <prompt>`` so the
        #      whole prompt sits as the last argv element. Truncate at the
        #      ``--`` boundary so prompt content never reaches INFO logs.
        logged_args = redact_env_i_args(cmd)[1:]
        if "--" in logged_args:
            sep = logged_args.index("--")
            logged_args = [*logged_args[:sep], "--", "<prompt redacted>"]
        run_logger.info(
            "runner.start",
            engine=self.engine,
            resume=resume.value if resume else None,
            prompt_len=len(prompt),
            args=logged_args,
        )
        # #205 / #478: prompt content may carry credentials/PII; keep at DEBUG
        # so it only surfaces with explicit operator opt-in. Mirrors the
        # base ``runner.run_impl`` companion log so behaviour is consistent
        # across all engines.
        run_logger.debug(
            "runner.start_prompt",
            engine=self.engine,
            prompt_preview=prompt[:100] + "…" if len(prompt) > 100 else prompt,
        )

        cwd = get_run_base_dir()
        effective_mode = self._effective_permission_mode()
        use_control_channel = effective_mode is not None

        # PTY setup only for legacy (non-permission) mode
        pty_master_fd: int | None = None
        pty_slave_fd: int | None = None
        this_proc_stdin: Any = None

        try:
            if use_control_channel:
                # SDK-style: use PIPE stdin, keep it open for control responses
                stdin_arg = subprocess_module.PIPE
            elif self.supports_control_channel and os.name == "posix":
                # Legacy: use PTY for stdin
                pty_master_fd, pty_slave_fd = pty.openpty()
                run_logger.debug(
                    "pty.opened", master_fd=pty_master_fd, slave_fd=pty_slave_fd
                )
                try:
                    tty.setraw(pty_master_fd)
                except OSError:
                    run_logger.debug(
                        "pty.setraw_failed", fd=pty_master_fd, exc_info=True
                    )
                self._pty_master_fd = pty_master_fd
                stdin_arg = pty_slave_fd
            else:
                stdin_arg = subprocess_module.PIPE

            async with manage_subprocess(
                cmd,
                stdin=stdin_arg,
                stdout=subprocess_module.PIPE,
                stderr=subprocess_module.PIPE,
                env=env,
                cwd=cwd,
            ) as proc:
                # Close slave fd in parent after subprocess starts (PTY mode)
                if pty_slave_fd is not None:
                    os.close(pty_slave_fd)
                    run_logger.debug("pty.slave_closed", fd=pty_slave_fd)
                    pty_slave_fd = None

                if proc.stdout is None or proc.stderr is None:
                    raise RuntimeError(self.pipes_error_message())

                # #361: redact env -i KEY=VAL pairs so secrets passed via
                # the env-wrap don't leak into journald.
                logged_args = redact_env_i_args(cmd)[1:]
                run_logger.info(
                    "subprocess.spawn",
                    cmd=cmd[0] if cmd else None,
                    args=logged_args,
                    pid=proc.pid,
                    use_control_channel=use_control_channel,
                )

                if use_control_channel and proc.stdin is not None:
                    # SDK-style: send payload but keep stdin open
                    if payload is not None:
                        await proc.stdin.send(payload)
                        run_logger.info(
                            "subprocess.stdin.payload_sent",
                            pid=proc.pid,
                            payload_len=len(payload),
                        )
                    # Store stdin for writing control responses later.
                    # Keep a local copy too - self._proc_stdin may be
                    # overwritten by a concurrent session on the same runner.
                    self._proc_stdin = proc.stdin
                    this_proc_stdin = proc.stdin
                elif payload is not None and self._pty_master_fd is not None:
                    # Legacy PTY: write to master
                    os.write(self._pty_master_fd, payload)
                    run_logger.info(
                        "subprocess.pty.payload_sent",
                        pid=proc.pid,
                        payload_len=len(payload),
                    )
                elif payload is not None and proc.stdin is not None:
                    # Legacy PIPE fallback: send and close
                    await proc.stdin.send(payload)
                    await proc.stdin.aclose()

                stream = JsonlStreamState(expected_session=resume)
                # #346 thread the ClaudeStreamState into the generic stream
                # so the wedge detector in runner_bridge can duck-type against
                # background-task helpers without importing claude-specific code.
                stream.engine_state = state
                # #361 stash PID so the env audit in translate_claude_event
                # can sample /proc/<pid>/environ on system.init.
                state.pid = proc.pid
                self.current_stream = stream
                reader_done = anyio.Event()

                # #333: load post-result idle settings before the task group
                # so the watchdog gets a snapshot. A load failure leaves the
                # legacy "stay alive forever" behaviour in place.
                post_result_idle_enabled = True
                post_result_idle_timeout_s = 600.0
                try:
                    result = load_settings_if_exists()
                    if result is not None:
                        settings_obj, _ = result
                        post_result_idle_enabled = (
                            settings_obj.watchdog.post_result_idle_enabled
                        )
                        post_result_idle_timeout_s = float(
                            settings_obj.watchdog.post_result_idle_timeout
                        )
                except Exception:  # noqa: BLE001 — settings errors must not block a run
                    run_logger.debug(
                        "post_result_idle.settings_load_failed", exc_info=True
                    )

                async with anyio.create_task_group() as tg:
                    tg.start_soon(
                        drain_stderr,
                        proc.stderr,
                        run_logger,
                        tag,
                        stream.stderr_capture,
                    )
                    tg.start_soon(
                        self._subprocess_watchdog,
                        proc,
                        stream,
                        reader_done,
                        run_logger,
                        proc.pid,
                    )
                    if (
                        use_control_channel
                        and this_proc_stdin is not None
                        and post_result_idle_enabled
                    ):
                        tg.start_soon(
                            self._post_result_idle_watchdog,
                            state,
                            this_proc_stdin,
                            reader_done,
                            run_logger,
                            post_result_idle_timeout_s,
                        )
                    async for evt in self._iter_jsonl_events(
                        stdout=proc.stdout,
                        stream=stream,
                        state=state,
                        resume=resume,
                        logger=run_logger,
                        pid=proc.pid,
                        session_stdin=this_proc_stdin if use_control_channel else None,
                    ):
                        yield evt
                    reader_done.set()

                    # Close stdin after all events to let CLI exit.
                    # Use this_proc_stdin (local) not self._proc_stdin (may
                    # have been overwritten by a concurrent session).
                    if use_control_channel and this_proc_stdin is not None:
                        with contextlib.suppress(Exception):
                            await this_proc_stdin.aclose()
                    # #502 — Close our read end of stderr so drain_stderr
                    # exits even when a child (e.g. an MCP server) inherited
                    # the stderr fd and is keeping it open. Without this the
                    # task group blocks forever waiting on drain_stderr and
                    # `proc.wait()` below is never reached.
                    with contextlib.suppress(Exception):
                        await proc.stderr.aclose()

                rc = await proc.wait()
                run_logger.info("subprocess.exit", pid=proc.pid, rc=rc)
                if stream.did_emit_completed:
                    return
                found_session = stream.found_session
                if rc != 0:
                    events = self.process_error_events(
                        rc,
                        resume=resume,
                        found_session=found_session,
                        state=state,
                        stderr_lines=stream.stderr_capture or None,
                    )
                    for evt in events:
                        if isinstance(evt, CompletedEvent):
                            self._log_completed_event(
                                logger=run_logger,
                                pid=proc.pid,
                                event=evt,
                                source="process_error",
                            )
                        yield evt
                    return

                events = self.stream_end_events(
                    resume=resume,
                    found_session=found_session,
                    state=state,
                )
                for evt in events:
                    if isinstance(evt, CompletedEvent):
                        self._log_completed_event(
                            logger=run_logger,
                            pid=proc.pid,
                            event=evt,
                            source="stream_end",
                        )
                    yield evt

        finally:
            # Clean up global registries on ANY exit (cancel, error, normal).
            # process_error_events/stream_end_events handle normal paths but
            # cancellation skips both, leaving stale outline_guard/cooldown state.
            _sid = resume.value if resume else None
            if _sid is None:
                try:
                    if stream.found_session is not None:
                        _sid = stream.found_session.value
                except (NameError, AttributeError):
                    pass
            if _sid:
                try:
                    _cleanup_session_registries(_sid)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "session.registry.cleanup_failed",
                        session_id=_sid,
                        error=str(e),
                        error_type=e.__class__.__name__,
                    )
            # Cleanup - close the local stdin if it wasn't already closed
            if this_proc_stdin is not None:
                with contextlib.suppress(Exception):
                    await this_proc_stdin.aclose()
            if pty_slave_fd is not None:
                try:
                    os.close(pty_slave_fd)
                except OSError:
                    logger.debug(
                        "pty.slave_close_failed", fd=pty_slave_fd, exc_info=True
                    )
            if pty_master_fd is not None:
                try:
                    os.close(pty_master_fd)
                except OSError:
                    logger.debug(
                        "pty.master_close_failed", fd=pty_master_fd, exc_info=True
                    )
            self._pty_master_fd = None


def build_runner(config: EngineConfig, config_path: Path) -> Runner:
    claude_cmd = shutil.which("claude") or "claude"

    model = config.get("model")
    if "allowed_tools" in config:
        allowed_tools = config.get("allowed_tools")
    else:
        allowed_tools = DEFAULT_ALLOWED_TOOLS
    dangerously_skip_permissions = config.get("dangerously_skip_permissions") is True
    use_api_billing = config.get("use_api_billing") is True
    permission_mode = config.get("permission_mode")
    title = str(model) if model is not None else "claude"

    extra_args_value = config.get("extra_args")
    if extra_args_value is None:
        extra_args: list[str] = []
    elif isinstance(extra_args_value, list) and all(
        isinstance(item, str) for item in extra_args_value
    ):
        extra_args = list(extra_args_value)
    else:
        logger.warning(
            "claude.config.invalid",
            error="extra_args must be a list of strings",
            config_path=str(config_path),
        )
        raise ConfigError(
            f"Invalid `claude.extra_args` in {config_path}; expected a list of strings."
        )

    reserved_flag = _find_reserved_flag(extra_args)
    if reserved_flag:
        logger.warning(
            "claude.config.invalid",
            error=f"reserved flag {reserved_flag!r} is managed by Untether",
            config_path=str(config_path),
        )
        raise ConfigError(
            f"Invalid `claude.extra_args` in {config_path}; flag {reserved_flag!r} "
            f"is managed by Untether and cannot be overridden."
        )

    return ClaudeRunner(
        claude_cmd=claude_cmd,
        model=model,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
        extra_args=extra_args,
        dangerously_skip_permissions=dangerously_skip_permissions,
        use_api_billing=use_api_billing,
        session_title=title,
    )


BACKEND = EngineBackend(
    id="claude",
    build_runner=build_runner,
    install_cmd="npm install -g @anthropic-ai/claude-code",
)


# Phase 2: Public API for sending control responses
async def send_claude_control_response(
    request_id: str, approved: bool, *, deny_message: str | None = None
) -> bool:
    """Send a control response to an active Claude Code session.

    Args:
        request_id: The control request ID
        approved: Whether to approve (True) or deny (False) the request
        deny_message: Custom denial message (used when approved=False)

    Returns:
        True if the response was sent successfully, False if the request is not found
    """
    # Look up session_id from request_id
    if request_id not in _REQUEST_TO_SESSION:
        # Duplicate callback (Telegram long-polling can deliver the same update twice)
        if request_id in _HANDLED_REQUESTS:
            logger.debug("control_response.duplicate", request_id=request_id)
            return True
        logger.warning(
            "control_response.request_not_found",
            request_id=request_id,
        )
        return False

    session_id = _REQUEST_TO_SESSION[request_id]

    if session_id not in _ACTIVE_RUNNERS:
        logger.warning(
            "control_response.no_active_session",
            session_id=session_id,
            request_id=request_id,
        )
        # Clean up stale mappings
        del _REQUEST_TO_SESSION[request_id]
        _REQUEST_TO_INPUT.pop(request_id, None)
        _REQUEST_TO_TOOL_NAME.pop(request_id, None)
        return False

    runner, _ = _ACTIVE_RUNNERS[session_id]
    success = await runner.write_control_response(
        request_id, approved, deny_message=deny_message
    )

    # Clean up the mapping after use
    del _REQUEST_TO_SESSION[request_id]
    # #197: LRU-evict oldest entries instead of clear()-ing the whole set.
    _HANDLED_REQUESTS[request_id] = None
    _HANDLED_REQUESTS.move_to_end(request_id)
    while len(_HANDLED_REQUESTS) > _HANDLED_REQUESTS_MAX:
        _HANDLED_REQUESTS.popitem(last=False)

    return success


def _cooldown_seconds(count: int) -> float:
    """Progressive cooldown: 30s, 60s, 90s, 120s (capped)."""
    return min(DISCUSS_COOLDOWN_BASE_SECONDS * count, DISCUSS_COOLDOWN_MAX_SECONDS)


def set_discuss_cooldown(session_id: str) -> None:
    """Record that a discuss denial was sent for this session.

    Called by claude_control when the user clicks 'Pause & Outline Plan'.
    Subsequent ExitPlanMode requests within the cooldown window will
    be auto-denied with an escalating message. The cooldown window
    grows with each click: 30s, 60s, 90s, 120s (capped).
    """
    existing = _DISCUSS_COOLDOWN.get(session_id)
    count = (existing[1] + 1) if existing else 1
    _DISCUSS_COOLDOWN[session_id] = (time.time(), count)
    cooldown = _cooldown_seconds(count)
    _OUTLINE_PENDING.add(session_id)
    logger.info(
        "discuss_cooldown.set",
        session_id=session_id,
        deny_count=count,
        cooldown_seconds=cooldown,
    )


def check_discuss_cooldown(session_id: str) -> str | None:
    """Check if an ExitPlanMode request should be auto-denied due to discuss cooldown.

    Returns an escalation deny message (with cooldown duration) if within
    cooldown, or None if clear. Uses progressive timing based on deny count.
    """
    entry = _DISCUSS_COOLDOWN.get(session_id)
    if entry is None:
        return None
    ts, count = entry
    cooldown = _cooldown_seconds(count)
    if time.time() - ts > cooldown:
        # Cooldown expired — keep the count so next click escalates further
        # Only clear the timestamp so the next ExitPlanMode gets through
        # but set_discuss_cooldown will use count+1 for the next window
        _DISCUSS_COOLDOWN[session_id] = (0.0, count)
        return None
    return _DISCUSS_ESCALATION_MESSAGE


def clear_discuss_cooldown(session_id: str) -> None:
    """Clear the discuss cooldown for a session (e.g. on approve/deny)."""
    _DISCUSS_COOLDOWN.pop(session_id, None)


def _cleanup_session_registries(session_id: str) -> None:
    """Clean up all global registries for a session.

    Called from run_impl finally (covers cancel), process_error_events,
    and stream_end_events. All operations are idempotent.
    """
    cleaned: list[str] = []
    if _ACTIVE_RUNNERS.pop(session_id, None) is not None:
        cleaned.append("active_runners")
    if _SESSION_STDIN.pop(session_id, None) is not None:
        cleaned.append("session_stdin")
    if session_id in _DISCUSS_COOLDOWN:
        cleaned.append("discuss_cooldown")
    clear_discuss_cooldown(session_id)
    if session_id in _DISCUSS_APPROVED:
        cleaned.append("discuss_approved")
    _DISCUSS_APPROVED.discard(session_id)
    if session_id in _PLAN_EXIT_APPROVED:
        cleaned.append("plan_exit_approved")
    _PLAN_EXIT_APPROVED.discard(session_id)
    if session_id in _OUTLINE_PENDING:
        cleaned.append("outline_pending")
    _OUTLINE_PENDING.discard(session_id)
    # Clean up discuss feedback ref (post-outline edit-instead-of-send tracking)
    from ..telegram.commands.claude_control import _DISCUSS_FEEDBACK_REFS

    if _DISCUSS_FEEDBACK_REFS.pop(session_id, None) is not None:
        cleaned.append("discuss_feedback_ref")
    stale = [k for k, v in _REQUEST_TO_SESSION.items() if v == session_id]
    if stale:
        cleaned.append(f"requests({len(stale)})")
    for k in stale:
        del _REQUEST_TO_SESSION[k]
        # Also clean up any pending ask requests and flows for stale requests
        _PENDING_ASK_REQUESTS.pop(k, None)
        _ASK_QUESTION_FLOWS.pop(k, None)
    logger.info(
        "claude_runner.session_cleanup",
        session_id=session_id,
        cleaned=cleaned,
    )


def get_pending_ask_request(
    channel_id: int | None = None,
) -> tuple[str, str] | None:
    """Return the oldest pending AskUserQuestion for *channel_id*, or None.

    When *channel_id* is provided, only requests from that channel are
    returned — preventing cross-chat message stealing (#144).
    """
    for request_id, (ch, question) in _PENDING_ASK_REQUESTS.items():
        if channel_id is not None and ch != channel_id:
            continue
        return request_id, question
    return None


async def answer_ask_question(request_id: str, answer: str) -> bool:
    """Answer a pending AskUserQuestion by denying with the user's response.

    The deny message contains the user's answer so Claude Code reads it and
    continues with that information.
    """
    _PENDING_ASK_REQUESTS.pop(request_id, None)
    deny_message = (
        f"The user answered your question via Telegram:\n\n"
        f'"{answer}"\n\n'
        f"Use this answer and continue. Do not call AskUserQuestion again "
        f"for this same question."
    )
    return await send_claude_control_response(
        request_id, approved=False, deny_message=deny_message
    )


def get_ask_question_flow(
    channel_id: int | None = None,
) -> AskQuestionState | None:
    """Return the active AskUserQuestion flow for *channel_id*, or None."""
    for flow in _ASK_QUESTION_FLOWS.values():
        if channel_id is not None and flow.channel_id != channel_id:
            continue
        return flow
    return None


def get_ask_question_flow_by_id(request_id: str) -> AskQuestionState | None:
    """Return a specific AskUserQuestion flow, or None."""
    return _ASK_QUESTION_FLOWS.get(request_id)


async def answer_ask_question_with_options(request_id: str) -> bool:
    """Send a structured answer for an AskUserQuestion flow with collected answers.

    Approves the request with updatedInput containing the answers dict.
    """
    flow = _ASK_QUESTION_FLOWS.pop(request_id, None)
    _PENDING_ASK_REQUESTS.pop(request_id, None)
    if flow is None:
        return False

    # Update the stored input to include answers
    stored_input = _REQUEST_TO_INPUT.get(request_id)
    if stored_input is not None:
        stored_input["answers"] = flow.answers

    return await send_claude_control_response(request_id, approved=True)


def format_question_message(flow: AskQuestionState) -> str:
    """Format the current question in a flow as a display string."""
    q = flow.questions[flow.current_index]
    question_text = q.get("question", "")
    total = len(flow.questions)
    if total > 1:
        return f"❓ Question {flow.current_index + 1} of {total}: {question_text}"
    return f"❓ {question_text}"


def get_question_option_buttons(flow: AskQuestionState) -> list[list[dict[str, str]]]:
    """Build inline keyboard buttons for the current question's options."""
    q = flow.questions[flow.current_index]
    options = q.get("options", [])
    buttons: list[list[dict[str, str]]] = []
    for i, opt in enumerate(options[:4]):
        label = opt.get("label", f"Option {i + 1}")
        buttons.append([{"text": label, "callback_data": f"aq:opt:{i}"}])
    buttons.append([{"text": "Other (type reply)", "callback_data": "aq:other"}])
    return buttons


def get_active_claude_sessions() -> list[str]:
    """Get list of active Claude Code session IDs."""
    return list(_ACTIVE_RUNNERS.keys())


def cleanup_expired_sessions(max_age_seconds: float = 3600.0) -> int:
    """Clean up stale session registrations.

    Args:
        max_age_seconds: Maximum age of a session before cleanup (default: 1 hour)

    Returns:
        Number of sessions cleaned up
    """
    current_time = time.time()
    expired = [
        session_id
        for session_id, (_, timestamp) in _ACTIVE_RUNNERS.items()
        if current_time - timestamp > max_age_seconds
    ]
    for session_id in expired:
        del _ACTIVE_RUNNERS[session_id]
        logger.info("claude_runner.expired_cleanup", session_id=session_id)
    return len(expired)
