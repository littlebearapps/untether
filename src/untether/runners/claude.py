"""
Updated ClaudeRunner with PTY support for control channel.

This replaces the existing claude.py with PTY-based stdin handling
to prevent deadlock when keeping stdin open for control responses.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import html
import itertools
import json
import os
import pty
import re
import shutil
import signal
import subprocess as subprocess_module
import time
import tty
import weakref
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, NamedTuple, Protocol

import anyio
import msgspec

from ..backends import EngineBackend, EngineConfig
from ..background_status import format_tokens
from ..config import ConfigError
from ..events import EventFactory
from ..logging import get_logger
from ..markdown import _short_model_name, backtick_fence, inline_code
from ..model import (
    TURN_COMPLETE_MARKER,
    Action,
    ActionEvent,
    ActionKind,
    CompletedEvent,
    EngineId,
    ResumeToken,
    StartedEvent,
    TurnEvent,
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
    publish_run_stream,
)
from ..schemas import claude as claude_schema
from ..session_quarantine import get_quarantine_store
from ..settings import load_settings_if_exists
from ..utils.env_audit import audit_proc_env
from ..utils.paths import get_run_base_dir, get_run_channel_id
from ..utils.proc_diag import CliScan, hook_clock
from ..utils.streams import drain_stderr
from ..utils.subprocess import (
    manage_subprocess,
    redact_env_i_args,
    signal_pid_group,
    wrap_with_env_i,
)
from .extra_args_guard import (
    SECURITY_DOC_REF,
    BlockedArg,
    BlockedCategory,
    BlockedExtraArgsError,
    dedupe_hits,
    format_blocked,
    iter_option_tokens,
)
from .run_options import (
    VALID_PERMISSION_MODES_BY_ENGINE,
    claude_cli_permission_mode,
    get_run_options,
    is_claude_plan_auto,
    is_claude_prompting_mode,
    normalise_claude_cli_mode,
)
from .tool_actions import tool_input_path, tool_kind_and_title

logger = get_logger(__name__)

ENGINE: EngineId = "claude"
DEFAULT_ALLOWED_TOOLS = ["Bash", "Read", "Edit", "Write"]

_RESUME_RE = re.compile(
    r"(?im)^\s*`?claude\s+(?:--resume|-r)\s+(?P<token>[^`\s]+)`?\s*$"
)

# `[claude] extra_args` deny-list (#407, #209). Matched through the shared
# tokeniser in `extra_args_guard`, so every spelling is caught: `--flag=value`,
# short clusters (`-pc`, commander expands them) and a bare `--`.
#
# Managed: flags Untether sets on every spawn (stream-json I/O, resume tokens,
# permission wiring). A user copy would duplicate or collide with Untether's
# value. Mirrors `codex.find_blocked_codex_args`.
_RESERVED_FLAGS: frozenset[str] = frozenset(
    {
        "--print",
        "--output-format",
        "--input-format",
        "--resume",
        "--continue",
        "--permission-mode",
        "--permission-prompt-tool",
    }
)
_MANAGED_HINT = "is managed by Untether and cannot be overridden"
# #209 (Claude findings Q3): `--dangerously-skip-permissions` outranks
# `--permission-mode` (a probe shows plan + skip → init `bypassPermissions`),
# and `--allow-dangerously-skip-permissions` makes bypass reachable later.
# Either one voids every Telegram approval, #749 and `/planmode`. The hint is
# deliberately neutral: it never names the explicit opt-in key.
_BYPASS_HINT = (
    "bypasses Untether's Telegram approvals and is not accepted in"
    f" `extra_args`; see {SECURITY_DOC_REF}"
)
_CLAUDE_BLOCKED: dict[str, tuple[BlockedCategory, str]] = {
    **dict.fromkeys(_RESERVED_FLAGS, ("managed", _MANAGED_HINT)),
    "--permission-prompts": (
        "managed",
        "is managed by Untether: Untether answers permission prompts itself",
    ),
    "--allowedTools": (
        "managed",
        "is managed by Untether: use `[claude] allowed_tools` — it is"
        " permission-mode aware (#749)",
    ),
    "--allowed-tools": (
        "managed",
        "is managed by Untether: use `[claude] allowed_tools` — it is"
        " permission-mode aware (#749)",
    ),
    "--dangerously-skip-permissions": ("bypass", _BYPASS_HINT),
    "--allow-dangerously-skip-permissions": ("bypass", _BYPASS_HINT),
    "--": (
        "separator",
        "is not accepted: a bare `--` turns Untether's own flags into prompt text",
    ),
}
# commander short options per `claude --help` (2.1.285); d/n/r/w take a value
# (optional or required), so the rest of a cluster after them is that value.
_CLAUDE_SHORT_ALIASES: dict[str, str] = {
    "p": "--print",
    "r": "--resume",
    "c": "--continue",
    "d": "--debug",
    "n": "--name",
    "w": "--worktree",
    "v": "--version",
    "h": "--help",
}
_CLAUDE_SHORT_VALUE_FLAGS: frozenset[str] = frozenset({"d", "n", "r", "w"})


def find_blocked_claude_args(extra_args: list[str]) -> list[BlockedArg]:
    """Every blocked flag in *extra_args* (#209), deduped, in order."""
    hits: list[BlockedArg] = []
    for tok in iter_option_tokens(
        extra_args,
        short_aliases=_CLAUDE_SHORT_ALIASES,
        short_value_flags=_CLAUDE_SHORT_VALUE_FLAGS,
    ):
        rule = _CLAUDE_BLOCKED.get(tok.flag)
        if rule is not None:
            category, hint = rule
            hits.append(BlockedArg(flag=tok.flag, category=category, hint=hint))
    return dedupe_hits(hits)


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


# #812: ``--include-hook-events`` (listed in ``claude --help`` on 2.1.284,
# findings §A2). Not in ``_RESERVED_FLAGS`` / the #209 deny-list (D-2): a
# config that already
# passes it must keep working, so ``_build_args`` dedupes instead.
_HOOK_EVENTS_FLAG = "--include-hook-events"
_CLI_HELP_TIMEOUT_S = 15.0
# (resolved cmd path, mtime) -> does ``--help`` list the flag. Unknown (probe
# failed) is cached as False until the binary changes.
_HOOK_EVENTS_SUPPORT: dict[tuple[str, float], bool] = {}


def _probe_cli_help(path: str) -> str | None:
    """Run ``<claude> --help`` (zero-token, no session). None on failure.
    Tests stub this (see ``tests/conftest.py``)."""
    try:
        proc = subprocess_module.run(  # nosec B603 — fixed argv, no shell
            [path, "--help"],
            capture_output=True,
            text=True,
            timeout=_CLI_HELP_TIMEOUT_S,
            stdin=subprocess_module.DEVNULL,
            check=False,
        )
    except (OSError, subprocess_module.SubprocessError):
        return None
    return (proc.stdout or "") + (proc.stderr or "")


def cli_supports_hook_events(cmd: str) -> bool:
    """#812: does the installed Claude CLI accept ``--include-hook-events``?

    One ``--help`` probe per (binary path, mtime) — a CLI upgrade re-probes.
    Unknown (unresolvable command, probe failure) → False: the flag is never
    passed to a CLI that might reject it.
    """
    path = shutil.which(cmd) or (cmd if os.path.isabs(cmd) else None)
    if path is None:
        return False
    try:
        real = os.path.realpath(path)
        key = (real, os.stat(real).st_mtime)
    except OSError:
        return False
    cached = _HOOK_EVENTS_SUPPORT.get(key)
    if cached is not None:
        return cached
    text = _probe_cli_help(real)
    supported = text is not None and _HOOK_EVENTS_FLAG in text
    _HOOK_EVENTS_SUPPORT[key] = supported
    logger.info(
        "claude.hook_events.probe",
        cli_path=real,
        supported=supported,
        probe_ok=text is not None,
    )
    return supported


def _load_hook_hold_settings() -> tuple[bool, float]:
    """#812: ``[watchdog] hold_for_async_hooks`` / ``async_hook_max_hold``.

    Best-effort: a config error falls back to the defaults (hold on, 630 s).
    """
    try:
        result = load_settings_if_exists()
        if result is not None:
            settings, _ = result
            return (
                bool(getattr(settings.watchdog, "hold_for_async_hooks", True)),
                float(getattr(settings.watchdog, "async_hook_max_hold", 630.0)),
            )
    except Exception:  # noqa: BLE001 — config errors must never block a run
        logger.debug("claude.hook_hold.settings_load_failed", exc_info=True)
    return (True, 630.0)


def _load_quarantine_on_forced_teardown() -> bool:
    """#632 (W2): read ``[auto_continue].quarantine_on_forced_teardown``.

    Best-effort — config errors must never block subprocess teardown, so we
    swallow them and default to True (the safety net stays armed even when
    config can't be read).
    """
    try:
        result = load_settings_if_exists()
        if result is None:
            return True
        settings, _ = result
        return settings.auto_continue.quarantine_on_forced_teardown
    except Exception:  # noqa: BLE001 — never let config errors block teardown
        return True


# Phase 2: Global registry for active ClaudeRunner instances
# Keyed by session_id, stores (runner_instance, timestamp)
_ACTIVE_RUNNERS: dict[str, tuple[ClaudeRunner, float]] = {}

# Phase 2: Global registry mapping session_id -> process stdin
# Stored separately from _ACTIVE_RUNNERS to support concurrent sessions
# on the same runner instance (runner._proc_stdin would be overwritten).
_SESSION_STDIN: dict[str, Any] = {}

# #647: session_id -> live ClaudeStreamState for the owning run. Lets the
# bridge's handoff wait (`handle_message`) ask "does the still-alive prior
# owner have live background work?" without reaching into runner internals.
# Registered alongside _SESSION_STDIN in _iter_jsonl_events; cleared in
# _cleanup_session_registries (run_impl finally).
_SESSION_BG_STATE: dict[str, ClaudeStreamState] = {}

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


class ControlRequestStatus(StrEnum):
    """Where a control request stands when a button is tapped (#685).

    The user-facing grouping is three-way: **sent** (a ``PENDING`` request we
    answered) / **already handled** (``IN_FLIGHT``, ``ALREADY_HANDLED``,
    ``CANCELLED``) / **not found**. The CLI gives no feedback on a duplicate
    answer, so this status comes from Untether's own registries.
    """

    PENDING = "pending"  # in _REQUEST_TO_SESSION, not claimed
    IN_FLIGHT = "in_flight"  # another tap is writing its answer right now
    ALREADY_HANDLED = "already_handled"  # answered earlier (Telegram / auto)
    CANCELLED = "cancelled"  # the CLI withdrew it (control_cancel_request)
    NOT_FOUND = "not_found"  # unknown: restart, LRU-evicted, session gone, other chat


@dataclass(frozen=True, slots=True)
class HandledControl:
    """How and where a control request was resolved (#685).

    ``action``: approve | deny | discuss | chat | auto | cancelled | timeout |
    superseded (``None`` when unknown). ``outcome``: answered | cancelled |
    expired. ``channel_id``: the chat that resolved it (scopes the memo, #715).
    """

    action: str | None
    outcome: str
    channel_id: int | None
    at: float


# Outcomes that record a request Untether can no longer answer; a later tap
# never overwrites them with "answered" (#685).
_TERMINAL_CONTROL_OUTCOMES = frozenset({"cancelled", "expired"})

# Values are ``HandledControl`` records; ``None`` is tolerated (legacy/tests)
# and reads as "answered, details unknown".
_HANDLED_REQUESTS: OrderedDict[str, HandledControl | None] = OrderedDict()

# #388: request_id -> the chat its buttons were posted in (the run's chat,
# ``get_run_channel_id()`` at registration). A tap from any other chat on a
# pending / in-flight request reads ``NOT_FOUND`` (``channel_mismatch``) and
# writes nothing. Only consulted while the id is still pending or claimed, so
# an entry left behind by any of the deletion paths is inert. Bounded by
# liveness pruning, never by evicting a live id (no fail-open under load).
_REQUEST_TO_CHANNEL: dict[str, int] = {}
_REQUEST_TO_CHANNEL_MAX = 512


@dataclass(slots=True)
class InflightClaim:
    """A tap that is answering a request right now (#685)."""

    action: str
    owner: str
    channel_id: int | None
    at: float


# #685 §2.4: request_id -> the claim of the tap currently answering it.
# Callbacks are dispatched concurrently, and the gap between "is it pending?"
# and the registry cleanup spans an ``await`` (the stdin lock), so without a
# synchronous claim two taps could both write — and the loser's ``del`` raised
# KeyError. The dispatcher reserves the claim before its early answer, so the
# toast reflects it too. Every writer respects it: the 5-min sweep skips a
# claimed id, and ``_cleanup_session_registries`` drops the session's claims.
_INFLIGHT_CONTROL_RESPONSES: dict[str, InflightClaim] = {}
_DIRECT_CLAIM_SEQ = itertools.count(1)

# #684: request ids the CLI withdrew (``control_cancel_request``) while a tap
# was writing its answer. The cancel handler leaves the ``_REQUEST_TO_*``
# entries to that writer (it still reads them) and parks the id here;
# ``respond_to_control_request`` consumes it in its ``finally`` and records
# ``outcome="cancelled"``. Bounded: the writer always discards its id, and
# session cleanup drops any leftover.
_CANCELLED_DURING_WRITE: set[str] = set()

# NOTE (#570): the time-based progressive discuss cooldown (_DISCUSS_COOLDOWN,
# 30/60/90/120s escalation) that lived here was a workaround for Claude Code
# v2.1.72-2.1.74 re-issuing ExitPlanMode immediately after a denial (#126
# lineage). Verified fixed on CLI 2.1.215 (2026-07-20: denied ExitPlanMode →
# clean text turn, no re-issue) and removed. The TEXT-based outline gate
# (_OUTLINE_PENDING + _OUTLINE_MIN_CHARS) below is NOT part of that workaround
# — it enforces the Pause-&-Outline flow and stays.

# Discuss approval: session_ids where user approved the plan via post-outline buttons.
# When Claude Code next calls ExitPlanMode, it will be auto-approved.
_DISCUSS_APPROVED: set[str] = set()

# #383: post-outline approvals that have already survived one live-session turn
# boundary unconsumed. An approval tapped after Claude wrote the outline and
# ended its reply must cover the user's next message ("go ahead"), so it is
# carried across exactly ONE boundary; the second boundary clears it.
_DISCUSS_CARRY: set[str] = set()

# Plan-bypass set: session_ids where the user has approved at least one
# plan-gated tool (ExitPlanMode, Edit, Write, or Bash). After the first
# approval, subsequent diff_preview tools auto-approve instead of re-prompting
# — the user has already reviewed code for this reply (#283, #369). #383: the
# approval is turn-scoped — every live-session turn open clears it, so one
# approval can no longer cover the follow-up and wake turns of a live session.
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


# #776: stdin writes now come from several tasks (the reader's drains, the
# control-response path, follow-up injection, the live-session lifecycle's
# close), so every writer serialises on a per-pipe lock. Keyed weakly on the
# stream object itself so the entry dies with the process's pipe.
_STDIN_LOCKS: weakref.WeakKeyDictionary[Any, anyio.Lock] = weakref.WeakKeyDictionary()


def _stdin_lock(stdin: Any) -> anyio.Lock | None:
    try:
        lock = _STDIN_LOCKS.get(stdin)
        if lock is None:
            lock = anyio.Lock()
            _STDIN_LOCKS[stdin] = lock
        return lock
    except TypeError:  # not weak-referenceable (exotic test doubles)
        return None


async def _locked_send(stdin: Any, data: bytes) -> None:
    lock = _stdin_lock(stdin)
    if lock is None:
        await stdin.send(data)
        return
    async with lock:
        await stdin.send(data)


async def write_user_message(session_id: str, text: str, *, command_uuid: str) -> bool:
    """Write a user turn into the live Claude process that owns ``session_id``.

    #776: the stream-json input shape is the one ``stdin_payload`` sends at
    spawn, plus ``uuid`` — the CLI echoes it back as
    ``command_lifecycle.command_uuid`` (F7), which is how the next turn is
    attributed to this message. Written while the session is idle the CLI
    runs it as its own turn (F6); written mid-turn it is folded into the
    running turn (F5 — steer semantics, rc12), so callers decide *when*.
    Returns False when there is no live stdin or the pipe is closed.
    """
    stdin = _SESSION_STDIN.get(session_id)
    if stdin is None:
        return False
    state = _SESSION_BG_STATE.get(session_id)
    if state is not None:
        # Record before writing: command_lifecycle can race the send.
        state.injected_commands[command_uuid] = time.monotonic()
        state.awaiting_injected[command_uuid] = time.monotonic()
    payload = {
        "type": "user",
        "uuid": command_uuid,
        "session_id": session_id,
        "message": {"role": "user", "content": text},
        "parent_tool_use_id": None,
    }
    try:
        await _locked_send(stdin, (json.dumps(payload) + "\n").encode())
    except (OSError, anyio.ClosedResourceError, anyio.BrokenResourceError) as exc:
        if state is not None:
            state.injected_commands.pop(command_uuid, None)
            state.awaiting_injected.pop(command_uuid, None)
        logger.warning(
            "claude.live_session.write_failed",
            session_id=session_id,
            command_uuid=command_uuid,
            error_type=exc.__class__.__name__,
        )
        return False
    logger.info(
        "claude.live_session.user_message_written",
        session_id=session_id,
        command_uuid=command_uuid,
        text_len=len(text),
    )
    return True


@dataclass(slots=True)
class LiveSession:
    """A Claude process kept live after its reply (#776).

    The session is *accepting input* until the lifecycle (or /cancel, /new,
    drain) starts closing it; ``lock`` serialises that transition against
    follow-up injection so a message can never be written into a pipe that is
    about to close (the #775 race guard, built here).
    """

    session_id: str
    state: ClaudeStreamState
    stdin: Any
    pid: int | None = None
    lock: anyio.Lock = field(default_factory=anyio.Lock)
    spawned_at: float = field(default_factory=time.monotonic)
    idle_since: float | None = None
    hold_started: float | None = None
    had_live_work: bool = False
    closing: bool = False
    close_reason: str | None = None
    # #791: set when Untether closed stdin on an idle session (turn closed,
    # no injected line pending) with no live background work — the
    # transcript is complete, so a close that overruns its grace must not
    # quarantine it.
    closed_idle_clean: bool = False
    # #829: the parent's turn was closed when stdin was closed (idle, no
    # injected line pending) — ``closed_idle_clean`` minus the "nothing
    # live" part. A close that stopped live tasks from such a turn, asked
    # for by Untether and exiting rc 0 on SIGINT, stopped cleanly (B2).
    closed_turn_idle: bool = False
    # #829: the tasks named when stdin was closed, and whether the
    # lifecycle has emitted its ``"closed"`` notice (once).
    close_tasks: list[str] = field(default_factory=list)
    closed_notified: bool = False
    # #820: the last escalation ``_await_live_exit_or_force`` started
    # (``sigint`` / ``sigterm`` / ``sigkill``), written *before* each signal so
    # the lifecycle can say how the CLI ended even when the run's teardown
    # cancels the await that would have returned it.
    exit_stage: str | None = None
    # #829: when the current idle period began (the turn ended), and the
    # rate limit of ``claude.live_session.hold_rearmed`` (first re-arm of an
    # idle period, then at most every ``_hold_rearm_log_every_s``).
    idle_period_started: float | None = None
    rearm_logged_at: float | None = None
    # #872: ``(source, task_id)`` keys already logged as
    # ``claude.live_session.hold_extended`` this idle period (once each).
    hold_extended_logged: set[str] = field(default_factory=set)
    # #872: a declared wait held this idle period past the quiet-time limit.
    # When it ends (task ended / deadline passed) the hold clock restarts, so
    # the wake turn it was waiting for — and the hooks its prompt fires —
    # get a fresh ``max_hold`` window instead of the long-expired one.
    declared_wait_holding: bool = False
    # #812: background hooks still unpaired when stdin was closed (the
    # stream's view — *candidates*: frames carry no pid, so which of them is
    # still running can't be told); empty when no hook was running. The
    # close grace stretches to cover the CLI's own 30 s asyncRewake wait
    # when this — or a live hook process — is seen.
    close_hooks: list[PendingHook] = field(default_factory=list)
    # How many hooks the close cut short: the live hook processes at close,
    # capped by the candidates (the candidate count when the process table
    # couldn't be read). Never more than were running.
    close_hook_count: int = 0
    # Raw live hook-process count at close (None: not scanned / unreadable).
    close_hook_procs: int | None = None
    # #775: the steer window. Set (under ``lock``) when /cancel or /new
    # interrupts an active turn — the process is about to be killed, so a
    # steer must fall back to the queue path instead of being written into a
    # pipe nobody will answer. Closing stdin (``closing``) shuts it too.
    steer_closed: bool = False
    steer_closed_reason: str | None = None
    listeners: list[Callable[[str, dict[str, Any]], Any]] = field(default_factory=list)

    @property
    def idle(self) -> bool:
        return self.state.completed_turns > 0 and not self.state.turn_open

    @property
    def accepting_input(self) -> bool:
        return not self.closing

    @property
    def accepting_steer(self) -> bool:
        return not self.closing and not self.steer_closed


_LIVE_SESSIONS: dict[str, LiveSession] = {}


def idle_live_session_count() -> int:
    """Live sessions idle between turns and not yet closing (#838).

    They still sit inside ``manage_subprocess`` (holding their MCP children),
    so they count toward the pre-spawn concurrency ceiling; the guard names
    them so a block while nothing visibly runs explains itself.
    """
    return sum(1 for s in _LIVE_SESSIONS.values() if s.idle and not s.closing)


def get_live_session(session_id: str) -> LiveSession | None:
    return _LIVE_SESSIONS.get(session_id)


def is_session_accepting(session_id: str) -> bool:
    """True when ``session_id`` has a live Claude process whose stdin is open
    and not closing — i.e. a follow-up can be written into it (#776)."""
    live = _LIVE_SESSIONS.get(session_id)
    return live is not None and live.accepting_input


def add_live_session_listener(
    session_id: str, callback: Callable[[str, dict[str, Any]], Any]
) -> bool:
    """Subscribe to lifecycle notices (``"closing"``) for a live session.

    Callbacks may be sync or async; exceptions are logged and swallowed."""
    live = _LIVE_SESSIONS.get(session_id)
    if live is None:
        return False
    live.listeners.append(callback)
    return True


def live_task_descriptions(state: ClaudeStreamState) -> list[str]:
    return [
        task.description or task.task_type or "task"
        for task in _live_native_tasks(state)
    ]


async def _notify_live_listeners(
    live: LiveSession, kind: str, payload: dict[str, Any]
) -> None:
    for callback in list(live.listeners):
        try:
            result = callback(kind, payload)
            if hasattr(result, "__await__"):
                await result
        except Exception:  # noqa: BLE001 — a listener must never break teardown
            logger.warning(
                "claude.live_session.listener_failed",
                session_id=live.session_id,
                kind=kind,
                exc_info=True,
            )


async def close_live_session(
    session_id: str,
    reason: str,
    *,
    notice: bool = False,
    only_if_idle: bool = False,
) -> bool:
    """Gracefully close a live session's stdin (#776).

    The CLI then stops any live background Bash (recording the stop in the
    transcript, F3) and exits rc=0 — no SIGTERM, no quarantine. Background
    *agents* ignore EOF and run to completion (#829 P0 G6), so a close over
    one runs into the grace and the SIGINT path (``_await_live_exit_or_force``).
    Idempotent; returns False when there is nothing (left) to close. With
    ``notice`` listeners get a ``"closing"`` event naming the tasks being
    stopped; the lifecycle later sends one ``"closed"`` event with the
    outcome (#829).
    """
    live = _LIVE_SESSIONS.get(session_id)
    if live is None:
        return False
    shells: list[int] | None = None
    if not live.closing and _hooks_outstanding(live.state):
        # #812: which hooks are still running is known only as a count of
        # live hook processes. None left while idle → every unpaired hook has
        # finished (a plain ``async`` hook's response is withheld until the
        # next turn): release them so this close doesn't report them cut
        # short.
        shells = await _hook_processes(live.state, live.pid)
        if live.idle and shells is not None and not shells:
            release_settled_async_hooks(live.state)
    async with live.lock:
        if live.closing:
            return False
        if only_if_idle and (not live.idle or live.state.awaiting_injected):
            # A follow-up was written (or a turn opened) since the caller
            # decided to close — re-checked under the injection lock
            # (review finding, #776).
            return False
        live.closing = True
        live.close_reason = reason
        live.state.live_close_reason = reason
        live.closed_idle_clean = _is_clean_idle(live)
        live.closed_turn_idle = live.idle and not live.state.awaiting_injected
        candidates = _hooks_at_close(live)
        # Never claim more hooks than live hook processes (and never more than
        # the candidates); the candidate count only when unreadable.
        count = len(candidates) if shells is None else min(len(shells), len(candidates))
        live.close_hooks = candidates if count else []
        live.close_hook_count = count
        live.close_hook_procs = None if shells is None else len(shells)
    tasks = live_task_descriptions(live.state)
    live.close_tasks = list(tasks)
    logger.info(
        "claude.live_session.stdin_closed",
        session_id=session_id,
        reason=reason,
        live_tasks=len(tasks),
        turn=live.state.turn,
        age_s=round(time.monotonic() - live.spawned_at, 1),
        # #829: how long the background work had been quiet at the close.
        last_progress_age_s=_last_progress_age_s(live.state),
    )
    hooks = _hook_event_labels(live.close_hooks)
    if hooks:
        _log_async_hook_killed(
            live,
            hook_count=live.close_hook_count,
            live_hook_processes=live.close_hook_procs,
            hook_events=hooks,
            hook_names=[h.name or h.label for h in live.close_hooks],
            hook_ids=[h.hook_id for h in live.close_hooks],
            source="stream",
        )
    # #812: an automatic close over a background hook tells the user its
    # feedback was lost (a user-initiated close already has its own notice).
    hook_notice = bool(hooks) and reason not in _USER_CLOSE_REASONS
    if notice or hook_notice:
        payload: dict[str, Any] = {"reason": reason, "tasks": tasks}
        if reason == "max_hold":
            # #829: the notice says how long nothing progressed (or, with the
            # re-arm switched off, that the hold limit was reached).
            payload["max_hold_s"] = live.state.bg_max_hold_s
            payload["rearm_on_progress"] = live.state.bg_hold_rearm_on_progress
        if hook_notice:
            payload["hooks"] = hooks
            payload["hook_count"] = live.close_hook_count
        await _notify_live_listeners(live, "closing", payload)
    lock = _stdin_lock(live.stdin)
    with contextlib.suppress(Exception):
        if lock is None:
            await live.stdin.aclose()
        else:
            async with lock:
                await live.stdin.aclose()
    return True


# #812: closes the user (or an operator restart) asked for — a hook they
# cut short is expected, so it logs at INFO and gets no extra notice.
_USER_CLOSE_REASONS = frozenset({"cancel", "new", "drain", "options_changed"})
# #829 B2: Untether-initiated closes that may stop cleanly on SIGINT (rc 0,
# transcript complete, resumable — P0 G7-G9). ``abs_cap`` closes mid-turn and
# ``error`` follows a failed run: both keep the forced-teardown quarantine.
_STOPPED_CLEAN_REASONS = frozenset(
    {"max_hold", "cancel", "new", "drain", "options_changed"}
)


async def _cli_children(pid: int | None) -> CliScan | None:
    if not isinstance(pid, int):
        return None
    from ..utils.proc_diag import cli_children

    try:
        return await anyio.to_thread.run_sync(cli_children, pid)
    except Exception:  # noqa: BLE001 — a scan failure must not break a close
        logger.debug("claude.hook.proc_scan_failed", exc_info=True)
        return None


def _oldest_unpaired_hook_at(state: ClaudeStreamState) -> float | None:
    """#812: when (``hook_clock()``) the oldest hook still unpaired — pending
    or expired — was seen starting; no hook process of theirs can predate
    it. None when there is none or a start is unknown (no timing
    exemption)."""
    starts = [h.started_clock for h in _hooks_outstanding(state)]
    if not starts or any(t is None for t in starts):
        return None
    return min(t for t in starts if t is not None)


async def _hook_processes(
    state: ClaudeStreamState, pid: int | None
) -> list[int] | None:
    """#812: the CLI children that may be a running command hook — any
    direct child except Bash-tool shells, children older than the oldest
    unpaired hook, and non-detached ones that are in the session baseline
    (MCP servers) or look like a service (see
    ``proc_diag.hook_evidence_children``). A shell that execs a single hook
    command (bash/zsh — macOS) leaves no ``<shell> -c`` behind, so the
    wrapper alone isn't enough. None when unknown (no pid, unreadable
    table): the caller must not assume no hook is running, so the hold
    stays bounded by ``async_hook_max_hold_s``."""
    scan = await _cli_children(pid)
    if scan is None:
        return None
    from ..utils.proc_diag import hook_evidence_children

    return hook_evidence_children(
        scan, state.cli_baseline_children, since=_oldest_unpaired_hook_at(state)
    )


async def capture_cli_baseline(state: ClaudeStreamState, pid: int | None) -> None:
    """#812: record the CLI's non-detached children right after
    ``system/init`` — its long-lived MCP servers (started before init) — so
    they are never taken for a running hook. Hooks are spawned detached, so
    one still running at init (a ``UserPromptSubmit`` hook starts before it)
    is left out; entries are (pid, start time), so a reused PID isn't
    exempt. Once per process; a failed scan leaves None (then nothing is
    exempt: the hold errs long, bounded)."""
    if state.cli_baseline_children is not None:
        return
    scan = await _cli_children(pid)
    if scan is None:
        return
    from ..utils.proc_diag import baseline_ids

    state.cli_baseline_children = baseline_ids(scan)
    logger.debug(
        "claude.hook.cli_baseline",
        pid=pid,
        cli_pid=scan.cli_pid,
        children=len(scan.children),
        baselined=len(state.cli_baseline_children),
    )


def _hooks_at_close(live: LiveSession) -> list[PendingHook]:
    """#812: background hooks a close is about to cut short. Mid-turn, the
    open turn's own (synchronous) hooks don't count — only earlier turns'."""
    state = live.state
    return [
        hook
        for hook in _hooks_outstanding(state)
        if live.idle or hook.turn < state.turn
    ]


def _hook_event_labels(hooks: Iterable[PendingHook]) -> list[str]:
    """#812: the distinct events among ``hooks``, sorted — what a message
    can honestly say about them when it can't tell which one is running."""
    return sorted({h.label for h in hooks})


def _log_async_hook_killed(
    live: LiveSession,
    *,
    hook_count: int,
    hook_names: list[str],
    hook_events: list[str],
    source: str,
    live_hook_processes: int | None = None,
    hook_ids: list[str] | None = None,
) -> None:
    reason = live.close_reason
    log = logger.info if reason in _USER_CLOSE_REASONS else logger.warning
    log(
        "claude.live_session.async_hook_killed",
        session_id=live.session_id,
        close_reason=reason,
        hook_count=hook_count,
        live_hook_processes=live_hook_processes,
        hook_events=hook_events,
        hook_names=hook_names,
        hook_ids=hook_ids or [],
        note=(
            "hook_count were running; hook_names/hook_ids are the unpaired "
            "candidates (frames carry no pid)"
            if source == "stream"
            else "hook_names are hook scripts found under the CLI"
        ),
        source=source,
    )


_HOOK_SCAN_MAX_PIDS = 64


def _scan_hook_children(child_pids: Iterable[int]) -> list[str]:
    """#812: labels of hook scripts running under the CLI at close — the
    fallback signal when the stream can't say (no ``--include-hook-events``).
    Scans raw argv via ``hook_script_label`` (``describe_process`` output
    loses the ``/hooks/`` segment). Best-effort; never raises."""
    from ..utils.proc_diag import find_descendants, hook_script_label

    labels: list[str] = []
    try:
        pids: list[int] = []
        for child in child_pids:
            pids.append(child)
            pids.extend(find_descendants(child))
            if len(pids) >= _HOOK_SCAN_MAX_PIDS:
                break
        for pid in pids[:_HOOK_SCAN_MAX_PIDS]:
            label = hook_script_label(pid)
            if label is not None and label not in labels:
                labels.append(label)
    except Exception:  # noqa: BLE001 — diagnostics must never break teardown
        logger.debug("claude.live_session.hook_scan_failed", exc_info=True)
    return labels


def _is_clean_idle(live: LiveSession) -> bool:
    """#791: the session sits between turns with nothing live or pending —
    its last turn's transcript is complete (no dangling tool_use for a
    SIGTERM to strand, the #632 concern)."""
    state = live.state
    return (
        live.idle
        and not state.awaiting_injected
        and not _live_native_tasks(state)
        and not has_live_background_work(state)
    )


def _may_stop_clean(live: LiveSession) -> bool:
    """#829 B2: an Untether-initiated close (``_STOPPED_CLEAN_REASONS``) of a
    session whose turn was closed when stdin was closed. If the CLI then
    exits rc 0 on SIGINT it stopped cleanly — no quarantine."""
    return live.close_reason in _STOPPED_CLEAN_REASONS and live.closed_turn_idle


def _lifecycle_exit_reason(
    *,
    exit_reason: str,
    cancelled: bool,
    process_gone: bool,
    closing: bool,
    stage: str | None,
) -> str:
    """#820: how a live session ended, for ``lifecycle_exited``.

    The run's reader cancels the task group as soon as the CLI's stdout ends,
    so the lifecycle is usually cancelled at the await right after its own
    close — the await's return value is lost. Classify from state that is
    settled by the time ``finally`` runs instead: whether the process is gone,
    whether the session was closing, and the escalation stage written before
    each signal. ``cancelled`` is kept only for a cancellation while the CLI
    was still running (a ``/cancel`` that gave up waiting, a drain)."""
    if cancelled and not process_gone:
        return "cancelled"
    if cancelled or exit_reason == "reader_done":
        if closing:
            return stage or "exited_after_close"
        return "reader_done"  # the CLI ended without a close
    return exit_reason  # the await completed: exited_after_close|sigint|sigterm|sigkill


def _close_grace_diag(pid: int, start: Any) -> dict[str, Any]:
    """#791: a structured process snapshot for a live close that overran its
    grace — what the CLI was doing when Untether had to signal it."""
    from ..utils.proc_diag import (
        collect_proc_diag,
        describe_process,
        format_diag,
        is_cpu_active,
        is_tree_cpu_active,
        read_wchan,
    )

    fields: dict[str, Any] = {}
    try:
        diag = collect_proc_diag(pid)
        if diag is None:
            return {"diag": None}
        fields["diag"] = format_diag(diag)
        fields["process_state"] = diag.state
        fields["wchan"] = read_wchan(pid)
        fields["rss_kb"] = diag.rss_kb
        fields["threads"] = diag.threads
        fields["fd_count"] = diag.fd_count
        fields["tcp_established"] = diag.tcp_established
        fields["tcp_total"] = diag.tcp_total
        # CPU across the whole grace window: busy (flushing / shutting MCP
        # servers down) vs. blocked.
        fields["cpu_active_during_grace"] = is_cpu_active(start, diag)
        fields["tree_cpu_active_during_grace"] = is_tree_cpu_active(start, diag)
        fields["children"] = [
            {
                "pid": child,
                "wchan": read_wchan(child),
                "cmd": describe_process(child),
            }
            for child in diag.child_pids[:12]
        ]
        fields["child_count"] = len(diag.child_pids)
    except Exception:  # noqa: BLE001 — diagnostics must never break teardown
        logger.debug("claude.live_session.close_diag_failed", exc_info=True)
    return fields


_INJECTED_TURN_TIMEOUT_S = 120.0


def _awaiting_injected(state: ClaudeStreamState) -> bool:
    """True while an injected follow-up hasn't started its turn yet. Entries
    expire (logged) so a line the CLI never picked up can't pin the session."""
    if not state.awaiting_injected:
        return False
    now = time.monotonic()
    for command_uuid, written_at in list(state.awaiting_injected.items()):
        if now - written_at > _INJECTED_TURN_TIMEOUT_S:
            state.awaiting_injected.pop(command_uuid, None)
            logger.warning(
                "claude.live_session.injected_turn_timeout",
                command_uuid=command_uuid,
                waited_s=round(now - written_at, 1),
            )
    return bool(state.awaiting_injected)


async def inject_when_idle(
    session_id: str,
    text: str,
    *,
    command_uuid: str,
    poll_s: float = 0.2,
) -> bool:
    """Queue-semantics follow-up (#776 phase 06): wait until the live session
    is idle — its turn ended and no earlier injected line is still waiting to
    start — then write ``text`` as a new turn. Returns False (caller falls
    back to --resume) if the session is gone or starts closing first.

    Writing mid-turn would fold the message into the running turn (probe F5
    — that is #775's *steer*), which is why this waits.
    """
    while True:
        live = _LIVE_SESSIONS.get(session_id)
        if live is None or not live.accepting_input:
            return False
        if live.idle and not _awaiting_injected(live.state):
            async with live.lock:
                if not live.accepting_input:
                    return False
                if live.idle and not live.state.awaiting_injected:
                    if live.state.plan_rearm_failed:
                        # #383: the CLI refused the plan re-arm — resume a
                        # fresh plan-mode process instead.
                        return False
                    await _write_plan_rearm_if_needed(
                        live, reason="followup", command_uuid=command_uuid
                    )
                    ok = await write_user_message(
                        session_id, text, command_uuid=command_uuid
                    )
                    if ok:
                        live.idle_since = time.monotonic()
                    else:
                        live.state.unplanned_commands.pop(command_uuid, None)
                    return ok
        await anyio.sleep(poll_s)


# ``steered``: written while a turn was open (folded into it, or run as the
# next turn if it arrived after the last tool call). ``written_idle``: the
# session sat between turns, so the line simply runs as the next turn — an
# ordinary follow-up, nothing was steered.
SteerOutcome = Literal[
    "steered",
    "written_idle",
    "no_live_session",
    "window_closed",
    "options_changed",
    "write_failed",
]

_UNSET_OPTIONS: Any = object()


async def close_steer_window(session_id: str, reason: str) -> bool:
    """Stop accepting steers into ``session_id`` (#775 race guard).

    Taken under ``LiveSession.lock`` — the same lock :func:`steer_into_session`
    holds while it checks the window and writes — so a steer either lands
    before this returns or sees the window closed and falls back to the queue
    path. Idempotent; False when there is no live session.
    """
    live = _LIVE_SESSIONS.get(session_id)
    if live is None:
        return False
    async with live.lock:
        if live.steer_closed:
            return True
        live.steer_closed = True
        live.steer_closed_reason = reason
    logger.info(
        "claude.live_session.steer_window_closed", session_id=session_id, reason=reason
    )
    return True


async def steer_into_session(
    session_id: str,
    text: str,
    *,
    command_uuid: str,
    run_options: Any = _UNSET_OPTIONS,
) -> SteerOutcome:
    """Steer mode (#775): write ``text`` into the live session *now*.

    Unlike :func:`inject_when_idle` this does not wait for the turn to end:
    written mid-turn the CLI folds it into the running turn at the next tool
    boundary (F5; the fold is confirmed by ``command_lifecycle{started}``
    arriving while the turn is open — see :func:`_absorb_injected`); written
    after the turn's last tool call or between turns it becomes the next turn
    in the same process (F6), delivered as a follow-up turn.

    The window check and the write happen under ``LiveSession.lock``, which
    :func:`close_live_session` and :func:`close_steer_window` also take, so a
    steer can never be written into a pipe that is closing (no orphan turns).
    ``run_options``: when given and the session is idle between turns, a
    mismatch with the options the process was spawned with returns
    ``options_changed`` — the queue path then restarts it with the new ones.
    """
    live = _LIVE_SESSIONS.get(session_id)
    if live is None:
        return "no_live_session"
    async with live.lock:
        if not live.accepting_steer:
            return "window_closed"
        if (
            run_options is not _UNSET_OPTIONS
            and live.idle
            and run_options != live.state.spawn_run_options
        ):
            return "options_changed"
        state = live.state
        if live.idle and state.plan_rearm_failed:
            # #383: the CLI refused the plan re-arm — never run this turn
            # unplanned; the queue path closes the session and resumes fresh.
            return "options_changed"
        if live.idle:
            # #383: an idle steer runs as the next turn — re-arm first. A
            # mid-turn fold is not a boundary.
            await _write_plan_rearm_if_needed(
                live, reason="steer", command_uuid=command_uuid
            )
        # Record before writing: command_lifecycle can race the send.
        state.steered_commands[command_uuid] = text
        ok = await write_user_message(session_id, text, command_uuid=command_uuid)
        if not ok:
            state.steered_commands.pop(command_uuid, None)
            state.unplanned_commands.pop(command_uuid, None)
            return "write_failed"
        mid_turn = not live.idle
        if not mid_turn:
            live.idle_since = time.monotonic()
    logger.info(
        "claude.live_session.steered",
        session_id=session_id,
        command_uuid=command_uuid,
        mid_turn=mid_turn,
        turn=live.state.turn,
    )
    return "steered" if mid_turn else "written_idle"


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


def session_live_bg_count(session_id: str) -> int:
    """Return the number of live background handles held by the still-running
    subprocess that owns ``session_id``, or 0 when there is no live owner or
    no live background work.

    #647: consumed by the bridge's handoff branch so a follow-up message that
    arrives while the prior owner is legitimately finishing background
    subagents can wait longer (and tell the user why) instead of silently
    diverting to a fresh contextless session after the base 30 s timeout.
    """
    state = _SESSION_BG_STATE.get(session_id)
    if state is None:
        return 0
    watchers, bg_tasks = _live_background_counts(state)
    return watchers + bg_tasks


def session_linger_info(session_id: str) -> tuple[bool, int] | None:
    """Return ``(post_result, live_bg_count)`` for the still-running
    subprocess that owns ``session_id``, or ``None`` when there is no live
    owner.

    #654: consumed by the Telegram loop's queued-progress path. A follow-up
    that arrives while the prior owner lingers post-result (finishing
    background work) queues silently behind it — this tells the queued
    message WHY it is waiting. ``post_result`` distinguishes that linger
    window from a normal mid-run queue, where the active progress message
    already explains itself.
    """
    if not is_session_alive(session_id):
        return None
    state = _SESSION_BG_STATE.get(session_id)
    post_result = state is not None and state.result_received_at is not None
    return post_result, session_live_bg_count(session_id)


SESSION_HANDOFF_POLL_S: float = 0.25


async def wait_for_session_handoff(
    session_id: str,
    timeout_s: float,
    *,
    poll_s: float = SESSION_HANDOFF_POLL_S,
) -> str:
    """Wait for any live subprocess owning ``session_id`` to exit.

    Returns ``"free"`` immediately if no subprocess owns the session,
    ``"exited"`` if one did and it exited within ``timeout_s``, or
    ``"timed_out"`` if it was still alive when the budget elapsed.

    #633 (W4) — one-owner-per-session serialisation. A follow-up message can
    otherwise spawn ``--resume <sid>`` while the previous subprocess for that
    same session is still alive in post-result limbo (the reproductions show a
    resume 6 s after the prior process was SIGTERM'd). Two owners of one
    session id is exactly what corrupts the upstream turn/queue state and
    produces the 0-turn empty resume that rc7 could only recover from after
    the fact.

    This is condition-based, not a fixed sleep: it resolves the instant the
    prior owner deregisters, so the common case (already exited) costs one
    dict lookup and the contended case costs only as long as the handoff
    actually takes. The wait is always bounded — on ``"timed_out"`` the caller
    quarantines the session and starts fresh rather than racing the resume.

    Liveness is read from ``_SESSION_STDIN`` via :func:`is_session_alive`,
    which ``run_impl``'s finally block clears. Note the runner's own
    ``session_locks`` cannot serve this purpose: it is a
    ``WeakValueDictionary``, so entries disappear as soon as nothing holds a
    reference to the semaphore.
    """
    if not is_session_alive(session_id):
        return "free"
    # #647 observability: the wait can absorb minutes of user-perceived
    # latency (base timeout + background-aware extension), and previously
    # left no trace in the journal — log entry and exit with elapsed.
    started_at = time.monotonic()
    logger.info(
        "session.handoff_wait",
        session_id=session_id,
        timeout_s=timeout_s,
        live_bg_count=session_live_bg_count(session_id),
    )
    deadline = started_at + max(0.0, timeout_s)
    outcome = "timed_out"
    while time.monotonic() < deadline:
        await anyio.sleep(poll_s)
        if not is_session_alive(session_id):
            outcome = "exited"
            break
    # Final probe: the loop can overshoot the deadline by up to ``poll_s``, and
    # an owner that exited during that last sleep should be reported as
    # "exited" rather than being penalised for our polling granularity.
    if outcome == "timed_out" and not is_session_alive(session_id):
        outcome = "exited"
    logger.info(
        "session.handoff_wait_done",
        session_id=session_id,
        outcome=outcome,
        elapsed_s=round(time.monotonic() - started_at, 1),
    )
    return outcome


def pending_control_requests_for_session(session_id: str | None) -> int:
    """Return how many control requests for ``session_id`` are still awaiting
    a response (approval buttons or AskUserQuestion).

    This is the **authoritative** "is this session blocked on the user?"
    signal: :data:`_REQUEST_TO_SESSION` is populated the moment a
    ``control_request`` is intercepted and popped again in
    :func:`write_control_response` (and in the Telegram callback handlers)
    as soon as the request is answered — so an entry means the request is
    genuinely outstanding right now, not merely that one happened earlier.

    #495/#499/#500: the stall detector previously inferred this from
    presentation state (``_has_pending_approval`` — the most recent action's
    ``inline_keyboard`` detail) or from the JSONL ring buffer
    (``_approval_pending`` — then the newest ring entry). Both are
    "most recent thing" heuristics and both go stale during a long approval
    wait: an ExitPlanMode permission request carries no ``inline_keyboard``
    detail, and after hours of waiting the newest ring entry is a stale
    ``user``/``result`` frame. The registry does not go stale. #697: the
    watchdog now consults this registry first, keeping a non-positional
    backward scan of the ring buffer only as a True-only fallback.

    The post-result idle watchdog and the pre-result silence cap already
    consult these same registries to decide whether to defer; this helper
    centralises that query so the three consumers cannot drift apart.
    """
    if not session_id:
        return 0
    pending = sum(1 for v in _REQUEST_TO_SESSION.values() if v == session_id)
    pending += sum(
        1 for k in _PENDING_ASK_REQUESTS if _REQUEST_TO_SESSION.get(k) == session_id
    )
    return pending


@dataclass(frozen=True, slots=True)
class ControlRequestSnapshot:
    """One control request still registered for a user answer (#684).

    Read by the bridge's ``control_request.unanswerable`` detector.
    ``kind``: ``tool`` | ``ask`` | ``outline_hold`` | ``synthetic`` (``da:``).
    ``answerable_by_text``: a text reply in the chat routes to it (a pending
    AskUserQuestion). ``writer_ok``: a live stdin writer exists for the
    session.
    """

    request_id: str
    session_id: str
    age_s: float
    tool_name: str
    kind: str
    answerable_by_text: bool
    writer_ok: bool


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

# #698: flows that were answered and torn down: request_id -> (channel_id, ts).
# #550 strips the inline keyboard so late taps can't fire
# ``ask_question.flow_missing``, but that strip is an async outbox edit issued
# *after* the flow is popped — and a tap already in flight cannot be recalled,
# so the losing side of that race is unavoidable (1s on nsd; wider in a busy
# group chat where the edit queues behind other traffic). Remembering the
# answered flow briefly lets a late tap resolve to "already answered" instead
# of looking like an unexplained missing flow.
# #684: values are (channel_id, ts, outcome) — ``outcome`` is ``answered``
# or ``cancelled`` (the CLI withdrew the question), so a late tap can say
# "No longer needed" rather than "Already answered".
_ANSWERED_ASK_FLOWS: dict[str, tuple[int, float, str]] = {}
ANSWERED_ASK_FLOW_TTL_S: float = 300.0
_ANSWERED_ASK_FLOWS_MAX = 32
# Untether policy, event-driven: swept only when another interactive
# control_request arrives (#684) — a lone request never expires. The CLI has
# no deadline on a permission prompt (findings 2026-09-30 Q1 §5); a request
# that can't be answered is reported by ``control_request.unanswerable``.
CONTROL_REQUEST_TIMEOUT_SECONDS: float = 300.0  # 5 minutes

# #374 (rc7): bounded keep for background-agent handles (Agent/Task
# tool_use that runs in the background — #646: that is the *default*
# upstream, i.e. every Agent/Task except an explicit run_in_background=False;
# see `_agent_runs_in_background`). An interim tool_result no longer
# clears the handle immediately (see `_is_terminal_tool_result`), but every
# "keep the handle" branch MUST have a bounded age-out — a handle that
# never clears wedges the post-result idle watchdog into a permanent hang.
# 15 minutes mirrors the existing MCP-tool / subagent stall thresholds
# (`runner_bridge.py` `_STALL_THRESHOLD_MCP_TOOL` / `_STALL_THRESHOLD_SUBAGENT`,
# both 900s): long enough for a genuine background subagent to keep
# suppressing stall warnings, short enough that a truly abandoned handle
# self-heals within one watchdog cycle instead of leaking forever.
BG_AGENT_MAX_KEEP_S: float = 15 * 60.0  # 900s

# #573 (rc8 slice): age-out backstops for the two primitives that previously
# had none. Generous on purpose — these are a last-resort ceiling, not a
# prediction of how long the work takes. A real terminal signal (KillShell
# tool_result, completion line) still clears the handle much earlier; this only
# stops a missing signal pinning `has_live_background_work` for the whole run,
# which suppresses the post-result watchdog and leaves the process in the limbo
# state that gets SIGTERM'd and poisons the session (#631/#632).
BG_BASH_MAX_KEEP_S: float = 60 * 60.0  # 1h — long builds/deploys are normal
REMOTE_TRIGGER_MAX_KEEP_S: float = 60 * 60.0  # 1h

_DISCUSS_ESCALATION_MESSAGE = (
    "REJECTED — your ExitPlanMode call was automatically blocked because you have not "
    "written enough visible text yet.\n\n"
    "The user is waiting to read your plan outline on their phone. Write it NOW as your "
    "next assistant message — at least 15 lines of visible text covering files, changes, "
    "order, and key decisions.\n\n"
    "Do NOT call ExitPlanMode again until you have written the outline. "
    "Any further calls without visible outline text will also be rejected."
)


# #776: task statuses that keep a native background task live. Anything else
# reported by ``task_updated.patch.status`` / ``task_notification.status``
# (completed, killed, stopped, failed, ...) is terminal — ending on an unknown
# status is the safe direction, because the ``background_tasks_changed``
# snapshot would otherwise be the only backstop against a pinned session.
_TASK_LIVE_STATUSES = frozenset({"running", "pending"})

# #801: a snapshot is a provisional signal next to the authoritative
# task_started / task_updated. Within this window of an end (or a revival) a
# snapshot that disagrees is taken to straddle those events, not to revive
# (or re-end) the task: it can neither pin a just-finished task open nor end
# a just-resumed one. task_started / task_updated always apply at once.
_TASK_REVIVE_GRACE_S = 5.0


@dataclass(slots=True)
class ClaudeTask:
    """One entry of the native task map, keyed by the CLI's ``task_id`` (#776).

    Built from ``system/task_*`` events (verified on CLI 2.1.283). The CLI's
    own lifecycle replaces the tool_use/tool_result guesses for Monitor,
    background Bash and background Agent work.
    """

    task_id: str
    task_type: str | None = None
    tool_use_id: str | None = None
    description: str | None = None
    subagent_type: str | None = None
    is_backgrounded: bool = False
    owned_by_subagent: bool = False
    status: str = "running"
    started_at: float = field(default_factory=time.monotonic)
    ended_at: float | None = None
    last_usage: dict[str, Any] | None = None
    last_tool_name: str | None = None
    # #801: times this task_id came back after ending — Claude resuming a
    # finished agent (SendMessage) reuses its id. ``started_at`` is reset to
    # the latest revival, so ``started_at``/``ended_at`` describe the current
    # run only.
    revived_count: int = 0
    # #777: the agent's current step from ``task_progress.description``
    # ("Running <step>") — kept apart from ``description`` (the task's label).
    last_step: str | None = None
    # #795: the live-session turn the task was launched in (1 = the run's
    # own prompt), so its wake turn can reply to the message that asked.
    origin_turn: int | None = None
    # #777: for a subagent's own task, the ``tool_use_id`` of the Agent call
    # that spawned the subagent (from the subagent's tool_use
    # ``parent_tool_use_id``) — the status panel only lists such a task on
    # its own once that agent is gone. None = unknown.
    owner_tool_use_id: str | None = None
    # #829: when this task last showed activity (monotonic) and what showed
    # it — its start / revival, a ``task_progress`` frame (one per subagent
    # tool call, P0 G1), or a subagent-owned foreground tool starting or
    # ending (``agent_tool``). The live session's background hold counts
    # from the newest of these (``latest_background_progress``).
    last_progress_at: float = field(default_factory=time.monotonic)
    last_progress_source: str = "task_started"

    @property
    def is_live_background(self) -> bool:
        """A live background task the parent session launched itself — the
        ones the parent's ``background_tasks_changed`` snapshot lists and
        wake turns are attributed to (#785)."""
        return (
            self.is_backgrounded
            and not self.owned_by_subagent
            and self.status in _TASK_LIVE_STATUSES
        )

    @property
    def holds_session(self) -> bool:
        """#801: live background work the live session must stay open for,
        whoever launched it. A subagent's backgrounded task (``Bash
        run_in_background`` inside an agent) outlives the agent that started
        it; closing stdin under it kills it. A subagent's foreground tools
        (``is_backgrounded=False``) die with the subagent and don't count."""
        return self.is_backgrounded and self.status in _TASK_LIVE_STATUSES


# #814: safeguard-stop outcomes, as logged and carried on ``usage``.
SAFEGUARD_OUTCOME_RETRIED = "retried"
SAFEGUARD_OUTCOME_SWITCHED = "switched"
SAFEGUARD_OUTCOME_NOT_RETRIED = "not_retried"


@dataclass(slots=True)
class SafeguardTurn:
    """#814: safeguard stops seen in one turn (reset when a turn opens).

    A stop can surface twice — as the refused assistant frame
    (``stop_reason == "refusal"``) and as the CLI's reaction to it (the
    ``informational`` "continuing once" notice or a ``model_refusal_*``
    frame) — so the turn's count is ``max(refusals, notices)``, never the
    sum.
    """

    refusals: int = 0
    notices: int = 0
    # Stops already written to the ``claude.safeguard_stop`` log.
    logged: int = 0
    outcome: str | None = None
    category: str | None = None
    model: str | None = None
    fallback_model: str | None = None
    # The first signal seen this turn (stop_reason / informational /
    # model_refusal_fallback / model_refusal_no_fallback).
    source: str | None = None
    # A main-thread assistant frame arrived after the refusal — the CLI
    # re-ran the request (used when no notice says so before the result).
    output_after_refusal: bool = False
    refused_message_ids: set[str] = field(default_factory=set)

    @property
    def stops(self) -> int:
        return max(self.refusals, self.notices)


@dataclass(slots=True)
class PendingHook:
    """#812: a hook whose ``hook_started`` hasn't been paired with its
    ``hook_response`` yet (``--include-hook-events``). After the turn's
    result that is the only runtime sign of a hook still running in the
    background — the frames carry no async marker (findings §A2)."""

    hook_id: str
    name: str | None
    event: str | None
    started_at: float
    # The turn the hook belongs to: the open turn, or — when it started
    # while idle (the next turn's UserPromptSubmit) — the upcoming one.
    turn: int
    # ``proc_diag.hook_clock()`` when the frame was read (counts through
    # system sleep; compared with CLI children's start times). None: unknown.
    started_clock: float | None = None

    @property
    def label(self) -> str:
        return self.event or self.name or "hook"


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
    # #684: request_id -> time.monotonic() when it was registered for a user
    # answer (normal, hold-open and ``da:``), for the unanswerable detector.
    control_registered_at: dict[str, float] = field(default_factory=dict)
    # Auto-approve ExitPlanMode when permission_mode is `plan-auto` (#741;
    # spelled `auto` before 0.35.5rc8)
    auto_approve_exit_plan_mode: bool = False
    # #749 the run's permission mode promises the user a prompt, so every
    # stage-6 `can_use_tool` request routes to Telegram instead of being
    # blanket-approved.  Armed in `new_state()` from
    # `is_claude_prompting_mode`.  Default False keeps the legacy `-p` path
    # (no control channel, no requests) and every autonomous mode unchanged.
    prompting_mode: bool = False
    # #751 the mode this process asked the CLI for, normalised to what
    # `system/init.permissionMode` reports when honoured (`manual`→`default`,
    # `plan-auto`→`plan`, `bypassPermissions` under
    # `--dangerously-skip-permissions`). None on the legacy `-p` path.
    requested_permission_mode: str | None = None
    # #751 the first init has been compared with the request (once per
    # process: compaction re-emits `init`, live turns re-emit it too).
    permission_mode_checked: bool = False
    # #835 the trigger source (`cron:<id>` / `webhook:<id>`) when nobody is
    # present to answer a Telegram prompt; armed in `new_state()` from
    # `EngineRunOptions.unattended_trigger` (control channel only). Every
    # stage-6 request that would wait for a tap is denied instead.
    unattended_trigger: str | None = None
    # #835 the Untether permission mode the unattended run asked for
    # (`plan-auto` kept distinct from `plan`; `bypassPermissions` under
    # --dangerously-skip-permissions) — drives which approvals are denied.
    unattended_mode: str | None = None
    # #835 tools denied in the current turn (reset per turn, one entry per
    # denial); finalised into `usage["unattended"]` at the result.
    unattended_denials: list[str] = field(default_factory=list)
    # #383: the run's configured mode maps to CLI `plan` (`plan` or
    # `plan-auto`). Armed in `new_state()`; drives the approval caption.
    configured_plan_mode: bool = False
    # #383 the CLI's effective permission mode, tracked from the native
    # signals: every `system/init.permissionMode`, `system/status` frames
    # carrying a `permissionMode` (emitted on every mode change) and the ack
    # of our own `set_permission_mode`. None until the first init.
    effective_permission_mode: str | None = None
    # The CLI reported `plan` at least once in this process (guards the
    # re-arm against fighting the CLI's own precedence, e.g.
    # --dangerously-skip-permissions overriding plan).
    plan_mode_observed: bool = False
    # When (monotonic) / in which turn a plan chat left plan mode; cleared
    # when plan is observed again. Also stamped by the approval paths, for
    # CLIs that emit no status frames.
    plan_exited_at: float | None = None
    plan_exit_turn: int | None = None
    # #383 plan re-arm (`[watchdog] rearm_plan_mode`, read per spawn).
    rearm_plan_mode: bool = True
    plan_rearm_seq: int = 0
    # Set at a live turn close when the session must go back to plan mode;
    # drained before the turn-closing event is yielded.
    plan_rearm_pending: bool = False
    # What set ``plan_rearm_pending`` (the ``rearm_sent`` reason): ``idle``
    # (a turn closed) or ``agents_done`` (#383 C4: an approved-plan agent
    # ended while the session idled).
    plan_rearm_pending_reason: str = "idle"
    # #383 C4: the re-arm waits while background agents launched in the
    # turn that left plan mode still run — they inherit the parent's live
    # mode (probe P-3), so re-arming would switch the approved plan's workers
    # back into planning. Agents counted at the last deferred claim (0 = no
    # deferral), when it began (monotonic), and whether this boundary has
    # logged ``rearm_deferred`` yet.
    plan_rearm_deferred: int = 0
    plan_rearm_deferred_since: float | None = None
    plan_rearm_deferred_logged: bool = False
    # Follow-ups / idle steers written while the re-arm was deferred
    # (command_uuid -> agents): their turn is flagged ``plan_deferred``.
    unplanned_commands: dict[str, int] = field(default_factory=dict)
    # Request id of the one re-arm on the wire (single flight).
    plan_rearm_inflight: str | None = None
    # The CLI refused a re-arm: the session is closed once idle and the next
    # message resumes a fresh `--permission-mode plan` process.
    plan_rearm_failed: bool = False
    # Whether this run is a resume (for error diagnostics)
    resumed: bool = False
    # Track max text block length seen (for cooldown bypass — survives overwrites)
    max_text_len_since_cooldown: int = 0
    # Store outline text for embedding in synthetic approve/deny action
    outline_text: str | None = None
    # #508 ExitPlanMode plan body the user APPROVED in this turn, re-emitted
    # as "📋 Plan (approved)" in the final answer when the post-approval
    # result is brief or empty (research/audit tasks where Claude has nothing
    # left to say after the user approves).  Plan messages on Telegram are
    # deleted on approve, so this is the only path to retain the body.
    # #793: set only when a request is approved — never from a denied one.
    last_exitplanmode_plan: str | None = None
    # #793: ExitPlanMode ``input.plan`` per control request awaiting a
    # decision (request_id -> input, "" when absent). Resolved to the plan
    # body at decision time — see ``_resolve_exitplanmode_plan``.
    exitplanmode_plans: dict[str, str] = field(default_factory=dict)
    # #793: plan bodies the user explicitly rejected (❌ Deny) in this
    # process — only consulted when the body had to come from the
    # (possibly stale) ``input.plan`` fallback.
    rejected_exitplanmode_plans: set[str] = field(default_factory=set)
    # #793: the plan file (``…/.claude/plans/*.md``) this session writes, and
    # its content when the last write was a full ``Write``. The plan file is
    # the source of truth: on plan-file CLIs ExitPlanMode's ``input.plan``
    # lags it by one write when both are issued in the same message.
    plan_file_path: str | None = None
    plan_file_content: str | None = None
    # Cumulative seconds the session spent in Anthropic-side rate-limit waits (#349).
    # #790: only *throttling* events accrue (a `rejected` snapshot, or the
    # legacy retry_after_ms/reset-ts shape) — `allowed` heartbeats never do.
    # Repeats against one deadline accrue only the extension.
    rate_limit_total_s: float = 0.0
    # Count of throttling rate_limit_events in this session (#349 v2); `allowed`
    # / `allowed_warning` snapshots and bare events are not counted (#790).
    rate_limit_count: int = 0
    # #790: latest quota snapshot from any rate_limit_event — `status` plus the
    # per-window {utilization, resets_at} from `unifiedWindows`, keyed by
    # window name (five_hour / seven_day / seven_day_overage_included).
    # Stashed for the subscription footer / #692 work; nothing reads it for
    # throttle decisions.
    rate_limit_status: str | None = None
    rate_limit_windows: dict[str, dict[str, float | None]] = field(default_factory=dict)
    # #790: (rate_limit_type, resets_at) pairs already announced by an
    # `allowed_warning` note — one heads-up per window, not one per snapshot.
    rate_limit_warned: set[tuple[str | None, float | None]] = field(default_factory=set)
    # #790: the throttle note for the current deadline, so repeated
    # `rejected` snapshots for one window update it in place.
    rate_limit_action_id: str | None = None
    rate_limit_action_deadline: float = 0.0

    # #347 per-session background-task tracking. Claude Code v2.1.72+ has
    # primitives that arm long-running work and return the subprocess to
    # "ready" state while the primitive continues in the background:
    # `Monitor`, `Bash run_in_background=true`, `Agent`/`Task` (background by
    # default upstream — #646), `ScheduleWakeup`, `RemoteTrigger`. Untether
    # tracks them so (a) #346's
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
    live_remote_triggers: set[str] = field(default_factory=set)

    # #776: native task map from ``system/task_*`` events, keyed by task_id.
    # ``native_tasks_seen`` flips on the first such event; from then on the
    # map is authoritative for Monitor / Bash-bg / Agent-bg liveness and the
    # tool_use handles above only matter for ScheduleWakeup / RemoteTrigger,
    # which emit no task events (F9). Before that (older CLI) the legacy
    # handles decide, unchanged.
    tasks: dict[str, ClaudeTask] = field(default_factory=dict)
    native_tasks_seen: bool = False
    # #777: subagent tool_use id -> the parent Agent's tool_use id (from
    # ``parent_tool_use_id``), linking a subagent-owned task to its agent.
    tool_parents: dict[str, str] = field(default_factory=dict)

    # #776 live-session turn segmentation. ``live_mode`` is armed by
    # ``ClaudeRunner.run_impl`` in control-channel mode (kill switch
    # ``[watchdog] live_sessions``). Turn 1 is the run itself; after its
    # result every later turn is bracketed by TurnEvent(started/completed).
    live_mode: bool = False
    completed_turns: int = 0
    turn: int = 1
    turn_open: bool = True
    turn_reason: str = "unknown"
    turn_command_uuid: str | None = None
    # Hints collected while idle, consumed when the next turn opens.
    pending_command_uuid: str | None = None
    # #815: when the CLI announced the next turn (``command_lifecycle
    # {started}``). The turn only opens on its first frame — for a tool-free
    # turn that is the final assistant message, seconds later — so the
    # TurnEvent carries the lead time for the bridge's elapsed header.
    pending_turn_since: float | None = None
    turn_notifications: list[str] = field(default_factory=list)
    # #785: task ids behind ``turn_notifications`` (a notification for a task
    # the map doesn't know contributes a label but no id).
    turn_notification_ids: list[str] = field(default_factory=list)
    # #785: the open turn's TurnEvent detail, re-sent on its completion.
    turn_detail: dict[str, Any] = field(default_factory=dict)
    # #785: top-level background tasks that ended while an ``unknown`` turn
    # was open — the CLI starts the wake turn on an agent's result before any
    # task event names it, so the turn is attributed at its completion.
    # #825: also while a ``task_finished`` turn is open — the CLI folds a
    # second finish into that turn, so it is added to its header.
    turn_ended_tasks: list[tuple[str, str]] = field(default_factory=list)
    # #825 R17-821: model requests sent in the open turn (see
    # ``turn_request_sent``), and per task in ``turn_ended_tasks`` the count when it
    # ended. A task that ended after the turn's last request began was never
    # seen by the model in this turn — the CLI wakes Claude for it next.
    turn_model_requests: int = 0
    turn_last_message_id: str | None = None
    # #825 re-test: a request is counted when the CLI *sends* it — after a
    # top-level tool_result — not when its first block streams back (without
    # partial messages that's the end of a long thinking block, so a task
    # ending mid-generation looked seen). True until that request's message
    # id arrives, so the id doesn't count it twice.
    turn_request_sent: bool = False
    turn_ended_at_request: dict[str, int] = field(default_factory=dict)
    # #825: late-ended tasks the model couldn't have seen in the turn they
    # ended in — ``(task_id, label, at)``; the next non-empty ``unknown``
    # turn (the CLI's wake for them) is attributed to them at completion.
    pending_late_tasks: list[tuple[str, str, float]] = field(default_factory=list)
    # #785: tasks whose finish a wake turn already delivered; a later turn
    # opened only by their notification is the same finish, not news.
    announced_task_ids: set[str] = field(default_factory=set)
    # #813: task id -> the ``unknown`` wake turn its end was paired with, so
    # the bridge files that turn's folded ack under the task (not merely the
    # latest unattributed ack).
    task_announced_turn: dict[str, int] = field(default_factory=dict)
    # #785: when the last wake turn completed still ``unknown``; a task
    # ending within ``_WAKE_PAIR_WINDOW_S`` after it is paired with it.
    unattributed_turn_completed_at: float | None = None
    # uuid -> monotonic write time for user lines Untether injected (#776).
    injected_commands: dict[str, float] = field(default_factory=dict)
    # Injected lines whose turn hasn't opened yet: the lifecycle must not
    # close stdin under them, and the next queued follow-up waits for them.
    awaiting_injected: dict[str, float] = field(default_factory=dict)
    # #775: uuid -> text of lines written in steer mode (a subset of
    # ``injected_commands``), used to label the "steer received" row.
    steered_commands: dict[str, str] = field(default_factory=dict)
    # #775: injected lines the CLI folded into an already-open turn.
    absorbed_commands: set[str] = field(default_factory=set)
    # #776 resume guard (F11): the stopped-task replay + 0-turn result that
    # precede the real answer on --resume of a session whose previous
    # process ended with live background work.
    saw_assistant_output: bool = False
    stopped_notification_pre_output: bool = False
    absorbed_results: int = 0
    absorbed_cost_baseline: float | None = None
    live_session_max_s: float = 14400.0
    # #776: the per-chat run options this process was spawned with (CLI
    # flags + runtime toggles). A follow-up is only written into the live
    # process when the chat's current options still match.
    spawn_run_options: Any = None
    # #776: a ScheduleWakeup the CLI will fire itself while stdin stays open
    # (F9). Monotonic deadline = announced fire time + 60 s grace; the
    # tool_use handle is cleared by its own confirmation tool_result, so it
    # can't be what holds the session.
    pending_wakeup_until: float | None = None
    # #872: the delay that wake-up announced ("in 94s"), for the
    # ``hold_extended`` log — set and cleared with ``pending_wakeup_until``.
    pending_wakeup_delay_s: float | None = None
    # #776: why the live session's stdin was closed (idle_no_tasks /
    # max_hold / abs_cap / cancel / new / drain); None while still open.
    live_close_reason: str | None = None

    # #374 (rc7): deadline map paralleling `live_bg_agents`. Kept as a
    # separate dict (rather than converting `live_bg_agents` to
    # `dict[str, float]`) to avoid touching every existing
    # `live_bg_agents` call site — see the design note in
    # `_register_background_handle`. Set at register time to
    # `time.monotonic() + BG_AGENT_MAX_KEEP_S`; consulted by
    # `_is_terminal_tool_result` and `has_live_background_work` to bound
    # how long an Agent/Task-bg handle can be kept across interim
    # tool_results before it is treated as terminal regardless of whether
    # an explicit terminal signal (`is_error`) ever arrives. Popped in
    # `_clear_background_handle` alongside the `live_bg_agents` discard.
    bg_agent_deadlines: dict[str, float] = field(default_factory=dict)

    # #573 (rc8 slice): the same parallel-deadline treatment for the two
    # remaining unbounded primitives. Before this, `live_bg_bashes` and
    # `live_remote_triggers` had no deadline at all, so any entry made
    # `has_live_background_work` return True for the rest of the run — which
    # suppresses the post-result watchdog and leaves the process lingering in
    # limbo, the state that gets SIGTERM'd and poisons the session
    # (#631/#632). Populated at register time, popped in
    # `_clear_background_handle`, aged out by `_live_bounded_handle_count`.
    bg_bash_deadlines: dict[str, float] = field(default_factory=dict)
    remote_trigger_deadlines: dict[str, float] = field(default_factory=dict)

    # #631 (W5-diag): sticky flag — True once any background-task primitive
    # (Monitor / Bash-bg / Agent-bg / ScheduleWakeup / RemoteTrigger) has
    # been observed in this session, regardless of whether it has since
    # completed. Set in `_register_background_handle`; never reset. Mirrored
    # onto the engine-agnostic `JsonlStreamState.background_observed` (see
    # `runner.py::_handle_jsonl_line`) so the `runner.empty_result`
    # diagnostic can read it without importing Claude-specific state.
    background_observed: bool = False

    # #544 ScheduleWakeup arm-time `delaySeconds` high-water-mark for the
    # current turn. The rc11 #507 fix stored this per-tool_id in a sibling
    # ``live_wakeups_arm_delay`` dict, but that dict was popped by
    # ``_clear_background_handle`` on the ScheduleWakeup tool_result —
    # which is the *schedule confirmation*, not a terminal signal — so by
    # the time ``_post_result_idle_watchdog`` ticked (after the ``result``
    # event, which lands AFTER tool_result) the dict was empty and the
    # dead-wakeup shortcut never engaged. The scalar survives
    # ``_clear_background_handle`` for the rest of the turn, then resets
    # on the next user prompt (StreamUserMessage with non-tool_result
    # content) or in ``new_state`` for fresh runs. ``max`` semantics so
    # multiple ScheduleWakeup calls in one turn use the longest arm.
    last_schedule_wakeup_arm_delay: float | None = None

    # #333: per-turn high-water-mark monotonic timestamp of the most recent
    # ``Bash(run_in_background=True)`` tool_use observed in this turn.
    # Mirrors ``last_schedule_wakeup_arm_delay`` (#544): survives
    # ``_clear_background_handle`` so the post-result idle watchdog tick log
    # can see that a bg-bash was launched even after the tool_result pops
    # the entry from ``live_bg_bashes``.
    #
    # CAVEAT: this is a LAUNCH tracker, not a LIFETIME tracker. A
    # ``run_in_background=True`` Bash can outlive multiple user turns
    # (long ``npm install``, ``tail -f``), so this scalar resets on every
    # fresh user prompt. For true liveness, the bridge already uses
    # ``_has_fresh_bash_output`` / ``_has_recent_bash_action``
    # (runner_bridge.py:1738, 1753) — DO NOT replace those with this
    # scalar. Observability-only today; suppression semantics for bg-bash
    # are out of scope until the #374 lifecycle refactor.
    last_bg_bash_launched_at: float | None = None

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
    # #590: descendant PIDs captured while the subprocess was alive (at the
    # result event, at reader-done, and at limbo detection — see
    # _capture_orphan_descendants). Read by manage_subprocess's post-exit
    # orphan sweep so pgroup ESCAPEES — MCP chains that setpgid/setsid into
    # their own group/session and survive a plain killpg — are still
    # terminated after the leader exits. The result-event capture is the one
    # that fires on fast clean rc=0 runs (no limbo, leader may exit before
    # reader-done), which is where the dembrandt-mcp leak was observed.
    orphan_pid_snapshot: list[int] = field(default_factory=list)
    # #590 hardening: {pid: /proc starttime} recorded alongside each captured
    # orphan PID so the post-exit sweep can reject a recycled PID before
    # signalling it (guards against PID reuse during the capture→teardown
    # window). Populated by _capture_orphan_descendants.
    orphan_pid_starttimes: dict[int, int] = field(default_factory=dict)
    # #592: one-shot latch — the pre-result silence cap fired and killed
    # the subprocess; prevents re-firing on subsequent watchdog ticks.
    pre_result_silence_killed: bool = False
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
    # #821: monotonic time of the previous result — unlike
    # ``result_received_at`` never cleared at a turn open, so each result can
    # tell which background agents were active since the one before it.
    prev_result_at: float | None = None

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

    # #495/#499/#500: monotonic deadline while Claude is throttled by a
    # ``rate_limit_event``. Armed alongside ``rate_limit_total_s`` in the
    # translate branch; the stall detector treats "still inside the retry
    # window" as an expected wait, not a stall. ``0.0`` = not throttled.
    rate_limit_wait_until: float = 0.0

    # #792: ``system/api_retry`` back-off tracking. ``api_retry_wait_until``
    # is a monotonic deadline (retry delay, plus the retry's first-byte
    # window when ``no_response`` is present) read by awaiting_api_retry().
    # Kept apart from the rate-limit fields: a 529/5xx back-off is not a
    # quota throttle, and conflating them would skew rate_limit_total_s.
    api_retry_wait_until: float = 0.0
    api_retry_count: int = 0
    api_retry_total_s: float = 0.0
    # One updating note per retry sequence: the action id is reused until
    # the attempt counter goes backwards (a new sequence).
    api_retry_action_id: str | None = None
    api_retry_last_attempt: int = 0

    # #814: safeguard stops in the current turn (reset per turn) and in the
    # whole session; ``session_model`` is the last model the stream named
    # (system/init or a main-thread assistant frame), used when a
    # safeguard signal carries none.
    safeguard: SafeguardTurn = field(default_factory=SafeguardTurn)
    safeguard_session_count: int = 0
    session_model: str | None = None

    # #819 context-window use. ``ctx_used`` is the input side of the latest
    # main-thread assistant ``usage`` (None until one arrives, and again
    # after a compaction — D5) and ``ctx_model`` that frame's model;
    # ``ctx_init_model`` is the last ``system/init`` model (the ``[1m]``
    # rule — ``session_model`` is overwritten by message models).
    # ``ctx_emitted`` is the last ``(pct, window)`` sent, for dedupe.
    ctx_used: int | None = None
    ctx_model: str | None = None
    ctx_init_model: str | None = None
    ctx_emitted: tuple[int | None, int | None] | None = None

    # #819 compaction (``system/status`` + ``system/compact_boundary``).
    # ``compaction_action_id`` is the open 🗜️ row (None when none is
    # running); ``compaction_refine_id`` the row a ``compact_result:
    # success`` closed, which the following boundary re-titles with the
    # token counts. ``compaction_wait_until`` is the bounded liveness latch
    # read by :meth:`awaiting_compaction` (refreshed by every ``compacting``
    # heartbeat). ``last_compact_result`` is the ``compact_result`` the
    # boundary consumes; ``turn_compactions`` the per-segment record behind
    # ``usage["compaction"]`` (reset at every result and turn open).
    # ``compaction_summary_pending``: the next non-tool_result ``user``
    # frame is the compaction summary, not a fresh prompt.
    compaction_action_id: str | None = None
    compaction_refine_id: str | None = None
    compaction_wait_until: float = 0.0
    last_compact_result: str | None = None
    turn_compactions: list[dict[str, Any]] = field(default_factory=list)
    compaction_summary_pending: bool = False

    # #812: hooks seen via ``--include-hook-events``. ``pending_hooks`` is
    # hook_id -> PendingHook (insertion-ordered, capped at
    # ``_PENDING_HOOKS_MAX``); SessionStart / Setup are never recorded (the
    # CLI settles those in-band). Entries past ``async_hook_max_hold_s``
    # move to ``expired_hooks`` so a later close can still name them.
    pending_hooks: dict[str, PendingHook] = field(default_factory=dict)
    expired_hooks: dict[str, PendingHook] = field(default_factory=dict)
    # Plain ``async: true`` hooks whose process has already exited: the CLI
    # withholds their ``hook_response`` until the next turn or teardown
    # (docs: "If the session is idle, the response waits until the next
    # user interaction"), so they must not hold the session. Only
    # ``asyncRewake`` hooks report back while idle — and a finished process
    # can no longer rewake. See ``release_settled_async_hooks``.
    deferred_hooks: dict[str, PendingHook] = field(default_factory=dict)
    # Monotonic time the live-session lifecycle first found no hook process
    # under the CLI while hooks were pending (None once one is seen again).
    hook_procs_gone_since: float | None = None
    # Live hook processes at the lifecycle's last scan (None: unreadable /
    # not scanned). Labels a hold expiry — never more hooks than this.
    live_hook_processes: int | None = None
    # (pid, start) of the CLI's non-detached children right after
    # ``system/init`` (MCP servers): never hook evidence (None: not captured
    # — nothing exempt).
    cli_baseline_children: frozenset[tuple[int, int | None]] | None = None
    # (hook name, hook event, monotonic ts) of an async hook that outlived
    # the turn it started in and exited 2 (the asyncRewake wake signal, #828)
    # while idle; the next turn opening within ``_HOOK_REWAKE_HINT_TTL_S`` is
    # its rewake. Cleared on every turn open (carried into ``turn_hook_hint``
    # only within ``_HOOK_REWAKE_CARRY_TTL_S``).
    hook_rewake_hint: tuple[str | None, str | None, float] | None = None
    # The hint the open turn saw (stale at open, or an earlier turn's async
    # hook exiting 2 mid-turn) — the result's ``origin`` confirms it.
    turn_hook_hint: tuple[str | None, str | None, float] | None = None
    hooks_started: int = 0
    # Mirrored from ``[watchdog] hold_for_async_hooks`` /
    # ``async_hook_max_hold`` by ``run_impl`` (read per spawn, so a config
    # edit applies to the next run).
    hold_for_async_hooks: bool = True
    async_hook_max_hold_s: float = 630.0
    # ``claude.hook.pending_hold`` fires once per hold; reset when it ends.
    hook_hold_logged: bool = False
    # #829: mirrored from ``[watchdog] bg_hold_rearm_on_progress`` /
    # ``post_result_bg_max_hold`` by ``run_impl`` (per spawn). With the switch
    # on, a live session's background hold measures the time since the last
    # background activity (``latest_background_progress``), not since the
    # last turn.
    bg_hold_rearm_on_progress: bool = True
    bg_max_hold_s: float = 1800.0
    # #872: mirrored from ``[watchdog] bg_hold_declared_waits`` (per spawn).
    # On, the hold never closes before a declared wait ends
    # (``declared_wait_until``).
    bg_hold_declared_waits: bool = True
    # #829: background Bash output files, tool_use_id -> path, from the Bash
    # tool_result ("Output is being written to: …/tasks/<id>.output"). The
    # file grows while the command prints (P0 G4) — the only activity signal
    # a ``local_bash`` task has.
    bg_output_files: dict[str, str] = field(default_factory=dict)
    # #872: the budget Claude declared for a background Bash — its ``timeout``
    # with ``run_in_background`` (tool_use_id -> seconds). The CLI stops the
    # command at that limit (30 min default, 2 h max), so the live session's
    # hold waits for it. Never cleared by ``_clear_background_handle`` (the
    # tool_result is only the launch confirmation), like ``bg_output_files``.
    bg_bash_timeouts: dict[str, float] = field(default_factory=dict)

    # #572: set when the run's StreamResultMessage was a Stream-idle-timeout
    # failure — "type_a" (mid-generation stall, retryable) or "type_b"
    # (cold-start zero-byte stall, never retried). runner_bridge reads this
    # via engine_state duck-typing to gate the bounded auto-retry.
    stream_idle_class: str | None = None

    def awaiting_user_approval(self) -> bool:
        """True while this session has an unanswered control request.

        #495/#499/#500: the engine-agnostic stall detector reaches this via
        ``getattr(stream, "engine_state", None)`` duck-typing (the same
        pattern used for ``has_live_background_work``), so non-Claude engines
        degrade to False rather than needing to know anything about Claude's
        control channel. Delegates to
        :func:`pending_control_requests_for_session`, which is backed by the
        self-cleaning ``_REQUEST_TO_SESSION`` registry — see that docstring
        for why the previous presentation-state and ring-buffer heuristics
        both went stale during long approval waits.
        """
        sid = self.factory.resume.value if self.factory.resume is not None else None
        return pending_control_requests_for_session(sid) > 0

    def control_request_snapshot(
        self, now: float | None = None
    ) -> list[ControlRequestSnapshot]:
        """#684: this session's still-registered control requests, with age.

        Pure read of the registries (plus pruning of stale
        ``control_registered_at`` keys). A request with no registration time
        (unknown age) is skipped rather than guessed.
        """
        sid = self.factory.resume.value if self.factory.resume is not None else None
        if not sid:
            return []
        now = time.monotonic() if now is None else now
        registered = self.control_registered_at
        for rid in [r for r in registered if r not in _REQUEST_TO_SESSION]:
            del registered[rid]
        writer_ok = sid in _SESSION_STDIN and sid in _ACTIVE_RUNNERS
        snaps: list[ControlRequestSnapshot] = []
        for rid, owner in list(_REQUEST_TO_SESSION.items()):
            if owner != sid:
                continue
            at = registered.get(rid)
            if at is None:
                continue
            synthetic = rid.startswith("da:")
            if synthetic:
                kind = "synthetic"
            elif self.request_to_action.get(rid, "").startswith(
                "claude.discuss_approve."
            ):
                kind = "outline_hold"
            elif rid in _PENDING_ASK_REQUESTS:
                kind = "ask"
            else:
                kind = "tool"
            snaps.append(
                ControlRequestSnapshot(
                    request_id=rid,
                    session_id=sid,
                    age_s=max(0.0, now - at),
                    tool_name="DiscussApproval"
                    if synthetic
                    else _REQUEST_TO_TOOL_NAME.get(rid, ""),
                    kind=kind,
                    answerable_by_text=rid in _PENDING_ASK_REQUESTS,
                    writer_ok=writer_ok,
                )
            )
        return snaps

    def awaiting_rate_limit_retry(self) -> bool:
        """True while Claude is inside an upstream rate-limit retry window."""
        return self.rate_limit_wait_until > time.monotonic()

    def awaiting_api_retry(self) -> bool:
        """#792: True while the CLI is backing off before retrying a failed
        API call (``system/api_retry``). Probed by the bridge's stall
        monitor via engine_state duck-typing, like
        :meth:`awaiting_rate_limit_retry` — silence here is expected."""
        return self.api_retry_wait_until > time.monotonic()

    def awaiting_compaction(self) -> bool:
        """#819: True while the CLI is compacting the context (a
        ``system/status: compacting`` heartbeat within the last
        ``_COMPACTION_LATCH_S``). Probed by the bridge's stall monitor like
        :meth:`awaiting_api_retry`; bounded, so a wedged compaction still
        warns once the heartbeats stop."""
        return self.compaction_wait_until > time.monotonic()


# #657 → #790: conservative wait window latched when a *confirmed* rejection
# (`status: "rejected"`) arrives with no parseable timing at all — no
# `resetsAt`, no legacy `retry_after_ms` / reset timestamps, no harvested
# #692 reset. Without *some* deadline `awaiting_rate_limit_retry()` reports
# False while the session genuinely is waiting on upstream. 60s is well under
# every stall threshold (600s+), so a wrong guess can only delay a stall
# verdict, never mask one.
#
# #657 applied this to *bare* events (no status, no timing). That premise was
# a schema-mismatch artefact: the real status snapshot decoded to all-None,
# so every healthy `allowed` heartbeat faked a 60s throttle. Bare events no
# longer latch anything (#790).
DEFAULT_REJECTED_RATE_LIMIT_WAIT_S = 60.0

# #790: a `rejected` window can reset days away (seven_day*). The stall latch
# is clamped so one snapshot can't park the stall detector for a week; the
# on-screen wait still shows the true time.
MAX_RATE_LIMIT_LATCH_S = 24 * 3600.0

# #790: upstream suppresses `allowed_warning` below ~70% utilization; mirror
# that so the one-shot heads-up note only fires when it means something.
RATE_LIMIT_WARNING_UTILIZATION = 0.7

# #790: unknown `rate_limit_info.status` values already warned about, so a new
# upstream enum member is surfaced once per process rather than per event.
_UNKNOWN_RATE_LIMIT_STATUSES_LOGGED: set[str] = set()

_RATE_LIMIT_WINDOW_LABELS: dict[str, str] = {
    "five_hour": "5h",
    "seven_day": "7-day",
    "seven_day_opus": "7-day Opus",
    "seven_day_sonnet": "7-day Sonnet",
    "seven_day_overage_included": "7-day (incl. extra usage)",
    "overage": "extra-usage",
}

# #692: subscription-cap reset deadlines harvested from result-error text
# ("… resets 5:30pm (Australia/Melbourne)"), keyed by auth namespace
# (CLAUDE_CONFIG_DIR proxy — caps are account-wide, so any run under the same
# namespace shares the reset). Value: (monotonic deadline, display string).
_RATE_LIMIT_RESET_LATCH: dict[str, tuple[float, str]] = {}

# Case-insensitive; timezone REQUIRED — without an explicit zone the clock
# time is unresolvable (containers commonly run UTC while the account does
# not), so we fail closed to the 60s default rather than guess.
_RESET_CLAUSE_RE = re.compile(
    r"resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)\s*\(([^)]+)\)",
    re.IGNORECASE,
)


# #701: the OTHER cap class — "You've reached your Fable 5 limit. Run
# /usage-credits to continue or switch models with /model." carries no time at
# all, so #692's result_error tier has nothing to harvest and every subsequent
# rejection without a reset fell through to the 60s default. Showing "~60s" for a cap
# whose remedy is an action (not a wait) is not merely inaccurate — it is the
# wrong *kind* of answer, and the user waits instead of acting.
#
# Keyed by auth namespace like _RATE_LIMIT_RESET_LATCH. Value:
# (monotonic expiry, model display or "" when the message names no model).
_RATE_LIMIT_ACTION_LATCH: dict[str, tuple[float, str]] = {}

# Bounded TTL rather than a real deadline: this cap class has no reset time, and
# nsd's 2026-07-27 window showed one clearing on its own ~15 min later. The latch
# is only ever *read* while genuinely throttled, so a generous window costs
# nothing while a short one would drop the remedy mid-throttle.
ACTION_REQUIRED_LATCH_TTL_S = 30 * 60.0

# Both halves required: the "reached your <X> limit" phrasing alone also appears
# on time-based caps, and it's the /usage-credits | /model remedy that marks this
# as the action-required class. Fail closed to the 60s default when either is absent.
_ACTION_CAP_RE = re.compile(
    r"reached\s+your\s+(?P<model>[\w.\- ]{1,40}?)\s+limit",
    re.IGNORECASE,
)
_ACTION_REMEDY_RE = re.compile(
    r"/usage-credits|switch\s+models\s+with\s+/model",
    re.IGNORECASE,
)


def _rate_limit_latch_key() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or "default"


def _parse_rate_limit_reset_clause(text: str | None) -> tuple[float, str] | None:
    """#692: parse "resets 5:30pm (Australia/Melbourne)" into
    (seconds_until_reset, display). Fail-closed: any parse miss, unknown
    timezone, nonexistent (DST spring-forward) time, or a wait outside
    (90s-expired, 24h] returns None and the caller keeps the 60s default.
    """
    if not text:
        return None
    m = _RESET_CLAUSE_RE.search(text)
    if m is None:
        return None
    hour12 = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = m.group(3).lower()
    tz_name = m.group(4).strip()
    if not 1 <= hour12 <= 12 or minute > 59:
        return None
    hour = (hour12 % 12) + (12 if ampm == "pm" else 0)
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(tz_name)
    except (KeyError, ValueError, OSError):
        return None
    now = datetime.now(tz)
    # fold=1 picks the LATER occurrence of a DST-ambiguous time — retrying
    # early is the failure mode we're fixing.
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0, fold=1)
    # A nonexistent spring-forward time doesn't survive a UTC round-trip.
    round_trip = candidate.astimezone(UTC).astimezone(tz)
    if (round_trip.hour, round_trip.minute) != (hour, minute):
        return None
    delta = (candidate - now).total_seconds()
    if delta <= 0:
        if delta > -90:
            # "resets 5:30pm" parsed at 5:30:20pm — just expired; don't
            # roll the deadline ~24h forward.
            return None
        candidate = candidate + timedelta(days=1)
        delta = (candidate - now).total_seconds()
    if delta > 24 * 3600:
        return None
    display = f"{hour12}:{minute:02d}{ampm} ({tz_name})"
    return delta, display


def _maybe_latch_rate_limit_reset(
    result_text: str | None, *, state: ClaudeStreamState
) -> None:
    """#692: harvest the reset clause from a result error and latch it for
    subsequent `rejected` rate_limit_events that carry no `resetsAt` of
    their own (#790; this run and the next ones in the
    same process/auth namespace)."""
    parsed = _parse_rate_limit_reset_clause(result_text)
    if parsed is None:
        return
    wait_s, display = parsed
    deadline = time.monotonic() + wait_s
    _RATE_LIMIT_RESET_LATCH[_rate_limit_latch_key()] = (deadline, display)
    # Keep the stall detector's "throttled upstream, not hung" context alive
    # for the whole window, not just 60s past the last rate_limit_event.
    state.rate_limit_wait_until = max(state.rate_limit_wait_until, deadline)
    logger.info(
        "claude.rate_limit_reset_latched",
        wait_s=round(wait_s, 1),
        resets_display=display,
        source="result_error",
    )


def _latched_rate_limit_reset() -> tuple[float, str] | None:
    """Remaining (seconds, display) from the harvested reset, or None when
    absent/expired (expired entries are pruned)."""
    key = _rate_limit_latch_key()
    entry = _RATE_LIMIT_RESET_LATCH.get(key)
    if entry is None:
        return None
    deadline, display = entry
    remaining = deadline - time.monotonic()
    if remaining <= 1.0:
        _RATE_LIMIT_RESET_LATCH.pop(key, None)
        return None
    return remaining, display


def _parse_action_required_cap(text: str | None) -> str | None:
    """#701: recognise the no-reset cap shape and return the model it names
    ("Fable 5"), or "" when the remedy is present but no model is named.

    ``None`` means "not this cap class" — the caller falls through to the
    60s default rather than claiming an action is required.
    """
    if not text or _ACTION_REMEDY_RE.search(text) is None:
        return None
    m = _ACTION_CAP_RE.search(text)
    if m is None:
        return ""
    return m.group("model").strip()


def _maybe_latch_action_required(result_text: str | None) -> None:
    """#701: arm the action-required latch from a result error so subsequent
    `rejected` rate_limit_events without a `resetsAt` render the remedy
    instead of a countdown (#790)."""
    model = _parse_action_required_cap(result_text)
    if model is None:
        return
    _RATE_LIMIT_ACTION_LATCH[_rate_limit_latch_key()] = (
        time.monotonic() + ACTION_REQUIRED_LATCH_TTL_S,
        model,
    )
    logger.info(
        "claude.rate_limit_action_required_latched",
        model=model or None,
        ttl_s=ACTION_REQUIRED_LATCH_TTL_S,
        source="result_error",
    )


def _latched_action_required() -> str | None:
    """Model display from the armed action-required latch, or None when
    absent/expired (expired entries are pruned)."""
    key = _rate_limit_latch_key()
    entry = _RATE_LIMIT_ACTION_LATCH.get(key)
    if entry is None:
        return None
    expiry, model = entry
    if expiry - time.monotonic() <= 0:
        _RATE_LIMIT_ACTION_LATCH.pop(key, None)
        return None
    return model


def _format_action_required_title(model: str) -> str:
    """#701: name the remedy, and hedge the timer claim — nsd saw one of these
    caps clear on its own ~15 min later, so a flat "this will never clear"
    would be its own inaccuracy."""
    subject = f"{model} limit" if model else "Model limit"
    return (
        f"⛔ {subject} reached — may not clear on a timer; "
        f"run /usage-credits or switch with /model"
    )


def _format_wait_approx(seconds: float) -> str:
    """Round UP (rounding down implies an earlier retry): minutes under 2h,
    then "Xh Ym"."""
    minutes = max(1, (int(seconds) + 59) // 60)
    if minutes < 120:
        return f"~{minutes} min"
    hours, rem = divmod(minutes, 60)
    return f"~{hours}h {rem}m" if rem else f"~{hours}h"


def _format_reset_clock(epoch_s: float) -> str:
    """#790: render a `resetsAt` epoch as host-local wall-clock time —
    "17:30 AEST" today, "Wed 17:30 AEST" on another day. The zone
    abbreviation stays so a UTC-configured host can't mislead."""
    from datetime import datetime

    reset = datetime.fromtimestamp(epoch_s, UTC).astimezone()
    now = datetime.now(UTC).astimezone()
    clock = reset.strftime("%H:%M")
    zone = reset.strftime("%Z")
    if reset.date() != now.date():
        clock = f"{reset.strftime('%a')} {clock}"
    return f"{clock} {zone}".strip()


def _rate_limit_window_label(rate_limit_type: str | None) -> str:
    if not rate_limit_type:
        return "Usage"
    return _RATE_LIMIT_WINDOW_LABELS.get(rate_limit_type, rate_limit_type)


def _rate_limit_utilization(info: claude_schema.RateLimitInfo) -> float | None:
    """Utilization for the snapshot's own window: the top-level field when
    present, else the matching `unifiedWindows` entry."""
    if info.utilization is not None:
        return info.utilization
    windows = info.unified_windows
    if windows is None or not info.rate_limit_type:
        return None
    window = getattr(windows, info.rate_limit_type, None)
    if isinstance(window, claude_schema.RateLimitWindow):
        return window.utilization
    return None


def _stash_rate_limit_snapshot(
    info: claude_schema.RateLimitInfo, state: ClaudeStreamState
) -> None:
    """#790: keep the latest quota snapshot on the state (footer / #692)."""
    state.rate_limit_status = info.status
    windows = info.unified_windows
    if windows is None:
        return
    for name in ("five_hour", "seven_day", "seven_day_overage_included"):
        window = getattr(windows, name, None)
        if window is not None:
            state.rate_limit_windows[name] = {
                "utilization": window.utilization,
                "resets_at": window.resets_at,
            }


def _rejection_needs_action(info: claude_schema.RateLimitInfo) -> bool:
    """#790 + #701: a rejection with no reset time whose remedy is an action
    (buy credits / switch model) rather than a wait."""
    return (
        info.error_code == "credits_required"
        or info.rate_limit_type == "overage"
        or info.overage_disabled_reason == "out_of_credits"
    )


def _has_legacy_rate_limit_timing(info: claude_schema.RateLimitInfo | None) -> bool:
    return info is not None and (
        info.retry_after_ms is not None
        or bool(info.requests_reset)
        or bool(info.tokens_reset)
    )


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


def _legacy_retry_after(
    info: claude_schema.RateLimitInfo | None,
) -> tuple[float, str] | None:
    """#349/#518 timing from the legacy shape: explicit ``retry_after_ms``
    first, then the earlier of the ISO reset timestamps."""
    if info is None:
        return None
    if info.retry_after_ms is not None:
        return info.retry_after_ms / 1000.0, "retry_after_ms"
    derived = _derive_retry_after_s(info)
    if derived is not None:
        return derived, "reset_ts"
    return None


def _rate_limit_note(
    factory: EventFactory,
    *,
    action_id: str,
    title: str,
    detail: dict[str, Any],
) -> list[UntetherEvent]:
    return [
        factory.action_started(
            action_id=action_id, kind="note", title=title, detail=detail
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


def _rate_limit_log_fields(info: claude_schema.RateLimitInfo | None) -> dict[str, Any]:
    """Structured fields for ``claude.rate_limit_*`` logs (#518: log what
    upstream actually sent, not a back-inferred summary)."""
    if info is None:
        return {"status": None}
    legacy: dict[str, Any] = {}
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
            legacy[field_name] = value
    return {
        "status": info.status,
        "rate_limit_type": info.rate_limit_type,
        "resets_at": info.resets_at,
        "utilization": _rate_limit_utilization(info),
        "overage_status": info.overage_status,
        "is_using_overage": info.is_using_overage,
        "error_code": info.error_code,
        "info": legacy or None,
    }


def _rate_limit_warning_note(
    info: claude_schema.RateLimitInfo,
    *,
    state: ClaudeStreamState,
    factory: EventFactory,
) -> list[UntetherEvent]:
    """#790: `allowed_warning` — a heads-up, never a latch. One note per
    (window, reset); silent below the upstream ~70% threshold or while paid
    extra usage covers the overflow (mirrors the CLI's own banner logic,
    which also warns when utilization is absent)."""
    utilization = _rate_limit_utilization(info)
    key = (info.rate_limit_type, info.resets_at)
    if (
        info.is_using_overage
        or (utilization is not None and utilization < RATE_LIMIT_WARNING_UTILIZATION)
        or key in state.rate_limit_warned
    ):
        logger.debug("claude.rate_limit_snapshot", **_rate_limit_log_fields(info))
        return []
    state.rate_limit_warned.add(key)
    label = _rate_limit_window_label(info.rate_limit_type)
    if utilization is not None:
        title = f"⚠️ {label} limit {round(utilization * 100)}% used"
    else:
        title = f"⚠️ Approaching {label} limit"
    if info.resets_at is not None and info.resets_at > time.time():
        title += f" — resets {_format_reset_clock(info.resets_at)}"
    logger.info("claude.rate_limit_warning", **_rate_limit_log_fields(info))
    state.note_seq += 1
    detail: dict[str, Any] = {"status": info.status}
    if info.rate_limit_type:
        detail["rate_limit_type"] = info.rate_limit_type
    if utilization is not None:
        detail["utilization"] = utilization
    return _rate_limit_note(
        factory,
        action_id=f"rate_limit_warning_{state.note_seq}",
        title=title,
        detail=detail,
    )


def _translate_rate_limit_event(
    info: claude_schema.RateLimitInfo | None,
    *,
    state: ClaudeStreamState,
    factory: EventFactory,
) -> list[UntetherEvent]:
    """#349/#518/#692/#701/#790: `rate_limit_event` → throttle note + latch,
    only when the snapshot says we are actually throttled.

    The real event (CLI 2.1.283) is a quota-status snapshot sent on every
    API response that moves a rounded utilization or reset. Decision table:

    * ``allowed`` — snapshot only: stash windows, DEBUG log, nothing else.
    * ``allowed_warning`` — optional one-shot heads-up note, no latch.
    * ``rejected`` (not covered by overage, reset still ahead) — throttle:
      latch until ``resetsAt`` (clamped 24h), extension-only accounting.
      Without ``resetsAt``: legacy timing → action-required remedy
      (credits_required / overage) → #692 harvested reset → #701 latch →
      conservative 60s default.
    * unknown status — WARN once per value, no latch.
    * no status: legacy ``retry_after_ms`` / reset timestamps keep the #518
      path; a truly bare event latches nothing (#657's 60s guess retired).
    """
    retry_s: float | None = None
    source = ""
    reset_display: str | None = None
    action_display: str | None = None
    extension_only = False

    if info is None or info.status is None:
        legacy = _legacy_retry_after(info)
        if legacy is None:
            logger.info(
                "claude.rate_limit_event",
                retry_after_s=None,
                retry_after_source="bare",
                count=state.rate_limit_count,
                cumulative_s=state.rate_limit_total_s,
                **_rate_limit_log_fields(info),
            )
            return []
        retry_s, source = legacy
    else:
        status = info.status
        _stash_rate_limit_snapshot(info, state)
        if status == "allowed":
            logger.debug("claude.rate_limit_snapshot", **_rate_limit_log_fields(info))
            return []
        if status == "allowed_warning":
            return _rate_limit_warning_note(info, state=state, factory=factory)
        if status != "rejected":
            if status not in _UNKNOWN_RATE_LIMIT_STATUSES_LOGGED:
                _UNKNOWN_RATE_LIMIT_STATUSES_LOGGED.add(status)
                logger.warning(
                    "claude.rate_limit_event.unknown_status",
                    known=list(claude_schema.CLAUDE_RATE_LIMIT_STATUSES),
                    **_rate_limit_log_fields(info),
                )
            return []
        if info.is_using_overage:
            # Paid extra usage covers the overflow — nothing is cut off.
            logger.info(
                "claude.rate_limit_event",
                retry_after_s=None,
                retry_after_source="covered_by_overage",
                count=state.rate_limit_count,
                cumulative_s=state.rate_limit_total_s,
                **_rate_limit_log_fields(info),
            )
            return []
        if info.resets_at is not None:
            remaining = info.resets_at - time.time()
            if remaining <= 0:
                # Upstream treats a rejection whose reset has passed as stale.
                logger.info(
                    "claude.rate_limit_event",
                    retry_after_s=None,
                    retry_after_source="stale",
                    count=state.rate_limit_count,
                    cumulative_s=state.rate_limit_total_s,
                    **_rate_limit_log_fields(info),
                )
                return []
            # The stream's own reset time is authoritative — it beats the
            # #692 result-text parse.
            retry_s = remaining
            source = "resets_at"
            reset_display = _format_reset_clock(info.resets_at)
            extension_only = True
        elif (legacy := _legacy_retry_after(info)) is not None:
            retry_s, source = legacy
        elif _rejection_needs_action(info):
            # #701 class, flagged by the stream itself: no countdown, but the
            # stall detector still gets a deadline.
            retry_s = DEFAULT_REJECTED_RATE_LIMIT_WAIT_S
            source = "action_required"
            action_display = _latched_action_required() or ""
        elif (latched := _latched_rate_limit_reset()) is not None:
            # #692: a reset deadline harvested from an earlier result error
            # ("resets 5:30pm (…)") beats guessing.
            retry_s, reset_display = latched
            source = "result_error"
            extension_only = True
        elif (action_model := _latched_action_required()) is not None:
            # #701: an action-required cap carries no reset time.
            retry_s = DEFAULT_REJECTED_RATE_LIMIT_WAIT_S
            source = "action_required"
            action_display = action_model
        else:
            retry_s = DEFAULT_REJECTED_RATE_LIMIT_WAIT_S
            source = "default"

    now_mono = time.monotonic()
    latch_s = min(retry_s, MAX_RATE_LIMIT_LATCH_S)
    new_deadline = now_mono + latch_s
    action_id: str | None = None
    if extension_only:
        # #692/#790: repeated events sharing ONE deadline accrue only the
        # extension beyond the existing wait, and update the same note.
        prev_deadline = max(state.rate_limit_wait_until, now_mono)
        state.rate_limit_total_s += max(0.0, new_deadline - prev_deadline)
        if (
            state.rate_limit_action_id is not None
            and abs(new_deadline - state.rate_limit_action_deadline) < 5.0
        ):
            action_id = state.rate_limit_action_id
    else:
        state.rate_limit_total_s += retry_s
    # #495/#499/#500: latch a deadline so the stall detector can tell
    # "throttled upstream, will resume by itself" apart from "hung".
    state.rate_limit_wait_until = new_deadline
    state.rate_limit_count += 1
    if action_id is None:
        state.note_seq += 1
        action_id = f"rate_limit_{state.note_seq}"
    state.rate_limit_action_id = action_id
    state.rate_limit_action_deadline = new_deadline

    display_s = int(retry_s) if retry_s >= 1 else f"{retry_s:.1f}"
    if source in ("resets_at", "result_error"):
        title = (
            f"⏳ Rate limited until {reset_display} ({_format_wait_approx(retry_s)})"
        )
    elif source == "action_required":
        # #701: no countdown at all — this cap wants an action.
        title = _format_action_required_title(action_display or "")
    elif source == "default":
        # A guessed window is shown as an estimate, not as fact.
        title = f"⏳ Rate limited — waiting to retry (~{display_s}s)"
    else:
        title = f"⏳ Rate limited — retrying in {display_s}s"

    detail: dict[str, Any] = {}
    if info is not None:
        if info.status is not None:
            detail["status"] = info.status
        if info.rate_limit_type is not None:
            detail["rate_limit_type"] = info.rate_limit_type
        if info.resets_at is not None:
            detail["resets_at"] = info.resets_at
        if info.tokens_remaining is not None:
            detail["tokens_remaining"] = info.tokens_remaining
        if info.requests_remaining is not None:
            detail["requests_remaining"] = info.requests_remaining
        if info.retry_after_ms is not None:
            detail["retry_after_ms"] = info.retry_after_ms
    logger.info(
        "claude.rate_limit_event",
        retry_after_s=retry_s,
        retry_after_source=source,
        count=state.rate_limit_count,
        cumulative_s=state.rate_limit_total_s,
        # #701: greppable — `retry_after_source=action_required` says the wait
        # was never going to help.
        action_model=action_display or None,
        **_rate_limit_log_fields(info),
    )
    return _rate_limit_note(factory, action_id=action_id, title=title, detail=detail)


def _format_retry_seconds(seconds: float) -> str:
    if 0 < seconds < 1:
        return f"{seconds:.1f}s"
    if seconds < 120:
        return f"{round(seconds)}s"
    return _format_wait_approx(seconds).lstrip("~")


def _translate_api_retry(
    event: claude_schema.StreamSystemMessage,
    *,
    state: ClaudeStreamState,
    factory: EventFactory,
) -> list[UntetherEvent]:
    """#792: ``system/api_retry`` → one updating progress note per retry
    sequence plus a short expected-wait latch for the stall monitor.

    The CLI emits this when an API call fails with a retryable error
    (429/529/5xx/connection) and it is about to back off; before #792 the
    frame was dropped, so a back-off looked like a silent hang.
    """
    attempt = event.attempt or 0
    max_retries = event.max_retries or 0
    delay_s = max(0.0, (event.retry_delay_ms or 0) / 1000.0)
    no_response = event.no_response
    header_wait_s = 0.0
    if no_response is not None and no_response.retry_wait_ms:
        header_wait_s = max(0.0, no_response.retry_wait_ms / 1000.0)

    now_mono = time.monotonic()
    # The retry itself may legitimately sit silent for the first-byte
    # window, so the expected wait covers delay + that window.
    state.api_retry_wait_until = max(
        state.api_retry_wait_until, now_mono + delay_s + header_wait_s
    )
    state.api_retry_count += 1
    state.api_retry_total_s += delay_s

    if state.api_retry_action_id is None or attempt <= state.api_retry_last_attempt:
        state.note_seq += 1
        state.api_retry_action_id = f"api_retry_{state.note_seq}"
    state.api_retry_last_attempt = attempt
    action_id = state.api_retry_action_id

    category = event.error if isinstance(event.error, str) else None
    if event.error_status is not None:
        head = f"API error {event.error_status}"
        if category and category != "unknown":
            head += f" ({category.replace('_', ' ')})"
    elif no_response is not None and no_response.waited_ms:
        head = (
            "No response from API after "
            f"{_format_retry_seconds(no_response.waited_ms / 1000.0)}"
        )
    else:
        head = "API unreachable"
    counter = (
        f"attempt {attempt}/{max_retries}" if max_retries else f"attempt {attempt}"
    )
    title = f"🔁 {head} — retrying in {_format_retry_seconds(delay_s)} ({counter})"

    final_attempt = max_retries > 0 and attempt >= max_retries
    log = logger.warning if final_attempt else logger.info
    log(
        "claude.api_retry",
        attempt=attempt,
        max_retries=max_retries,
        retry_delay_ms=event.retry_delay_ms,
        error_status=event.error_status,
        error=category,
        no_response_waited_ms=no_response.waited_ms if no_response else None,
        count=state.api_retry_count,
        cumulative_s=round(state.api_retry_total_s, 1),
        session_id=event.session_id,
    )
    detail: dict[str, Any] = {
        "attempt": attempt,
        "max_retries": max_retries,
        "retry_delay_ms": event.retry_delay_ms,
        "error_status": event.error_status,
    }
    if category:
        detail["error"] = category
    return [
        factory.action_started(
            action_id=action_id, kind="note", title=title, detail=detail
        ),
        factory.action_completed(
            action_id=action_id,
            kind="note",
            title=title,
            ok=True,
            level="warning" if final_attempt else "info",
            detail=detail,
        ),
    ]


def _str_or_none(value: Any) -> str | None:
    """Normalise an ``Any``-typed schema field (#814): a non-empty string,
    else None — so a shape drift degrades to "field missing"."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


# #814: the CLI's same-model refusal-retry notice (CLI 2.1.285, level notice):
#   "<Model>'s safeguards stopped the response above · continuing once with
#    that noted"
_SAFEGUARD_NOTICE_RE = re.compile(r"safeguards stopped", re.IGNORECASE)
_SAFEGUARD_NOTICE_MODEL_RE = re.compile(
    r"^(.+?)['’]s safeguards stopped", re.IGNORECASE
)
_INFORMATIONAL_MAX_CHARS = 200
_INFORMATIONAL_ROW_LEVELS = frozenset({"warning", "notice"})


def _safeguard_model_label(model: str | None) -> str | None:
    """Display form of a model named by a safeguard signal: an ID
    (``claude-opus-5-5``) is shortened like the footer does; a display name
    (``Opus 5.5``, from the notice text) is kept as-is."""
    if not model:
        return None
    if model.lower().startswith("claude-"):
        return _short_model_name(model)
    return model


def _safeguard_outcome_label(outcome: str | None, fallback_model: str | None) -> str:
    """Human wording for a #814 outcome (progress row and footer)."""
    if outcome == SAFEGUARD_OUTCOME_RETRIED:
        return "retried once"
    if outcome == SAFEGUARD_OUTCOME_SWITCHED:
        target = _safeguard_model_label(fallback_model)
        return f"switched to {target}" if target else "switched model"
    if outcome == SAFEGUARD_OUTCOME_NOT_RETRIED:
        return "not retried"
    return "outcome pending"


def _log_safeguard_stop(
    state: ClaudeStreamState, factory: EventFactory, *, outcome: str
) -> None:
    """Write ``claude.safeguard_stop`` once per newly-resolved stop."""
    sg = state.safeguard
    if sg.stops <= sg.logged:
        return
    sg.logged = sg.stops
    logger.info(
        "claude.safeguard_stop",
        session_id=factory.resume.value if factory.resume else None,
        model=sg.model,
        source=sg.source,
        category=sg.category,
        outcome=outcome,
        fallback_model=sg.fallback_model,
        turn=state.turn,
        turn_count=sg.stops,
        session_count=state.safeguard_session_count,
    )


def _note_safeguard(
    state: ClaudeStreamState,
    factory: EventFactory,
    *,
    source: str,
    outcome: str | None,
    category: str | None = None,
    model: str | None = None,
    fallback_model: str | None = None,
) -> list[UntetherEvent]:
    """#814: a safeguard stop → one updating ``🛡️`` note per turn, the
    ``claude.safeguard_stop`` log, and the per-turn tally the result's
    ``usage["safeguard"]`` carries to the footer.

    ``outcome`` is None for the refused assistant frame itself — the CLI's
    reaction (notice / fallback frame) resolves it; the result infers it
    when that reaction never came first (ordering is unverified). Never an
    error: the row is ``ok=True`` and the run's outcome is untouched.
    """
    sg = state.safeguard
    before = sg.stops
    if source == "stop_reason":
        sg.refusals += 1
    else:
        sg.notices += 1
    state.safeguard_session_count += sg.stops - before
    if sg.source is None:
        sg.source = source
    if outcome is not None:
        sg.outcome = outcome
    if fallback_model:
        sg.fallback_model = fallback_model
    if category:
        sg.category = category
    if sg.model is None:
        # The first model named this turn is the one that was stopped.
        sg.model = model or state.session_model
    if sg.outcome is not None:
        _log_safeguard_stop(state, factory, outcome=sg.outcome)
    if not state.turn_open:
        # Arrived after the turn's result (live session): counted and
        # logged above, but that turn's progress/final is already gone.
        return []
    return _safeguard_row(state, factory)


def _safeguard_row(
    state: ClaudeStreamState, factory: EventFactory
) -> list[UntetherEvent]:
    sg = state.safeguard
    label = _safeguard_model_label(sg.model)
    who = f"{label} safeguards" if label else "Safeguards"
    title = f"\N{SHIELD}\N{VARIATION SELECTOR-16} {who} stopped a response"
    if sg.outcome is not None:
        title += f" · {_safeguard_outcome_label(sg.outcome, sg.fallback_model)}"
    if sg.stops > 1:
        title += f" (×{sg.stops})"
    action_id = f"claude.safeguard.{state.turn}"
    detail: dict[str, Any] = {"safeguard": True, "stops": sg.stops}
    if sg.outcome is not None:
        detail["outcome"] = sg.outcome
    if sg.category:
        detail["category"] = sg.category
    level = "warning" if sg.outcome == SAFEGUARD_OUTCOME_NOT_RETRIED else "info"
    return [
        factory.action_started(
            action_id=action_id, kind="note", title=title, detail=detail
        ),
        factory.action_completed(
            action_id=action_id,
            kind="note",
            title=title,
            ok=True,
            level=level,
            detail=detail,
        ),
    ]


def _note_assistant_refusal(
    event: claude_schema.StreamAssistantMessage,
    *,
    state: ClaudeStreamState,
    factory: EventFactory,
) -> list[UntetherEvent]:
    """#814: main-thread assistant frames — track the model, mark output
    after a refusal, and note a ``stop_reason == "refusal"`` frame."""
    if event.parent_tool_use_id is not None:
        return []
    message = event.message
    if message.model and message.model != "<synthetic>":
        state.session_model = message.model
    sg = state.safeguard
    if message.stop_reason != "refusal":
        if sg.refusals:
            sg.output_after_refusal = True
        return []
    frame_id = message.id or event.uuid
    if frame_id is not None:
        if frame_id in sg.refused_message_ids:
            return []
        sg.refused_message_ids.add(frame_id)
    details = message.stop_details if isinstance(message.stop_details, dict) else {}
    return _note_safeguard(
        state,
        factory,
        source="stop_reason",
        outcome=None,
        category=_str_or_none(details.get("category")),
        model=message.model if message.model != "<synthetic>" else None,
    )


def _finalize_safeguard_turn(
    state: ClaudeStreamState, factory: EventFactory
) -> dict[str, Any] | None:
    """At the turn's result: resolve a still-pending outcome and return the
    ``usage["safeguard"]`` payload (None when the turn had no stop)."""
    sg = state.safeguard
    if sg.stops == 0:
        return None
    if sg.outcome is None:
        # A refused frame with no CLI reaction yet: later output means the
        # request was re-run; none means it wasn't.
        sg.outcome = (
            SAFEGUARD_OUTCOME_RETRIED
            if sg.output_after_refusal
            else SAFEGUARD_OUTCOME_NOT_RETRIED
        )
    _log_safeguard_stop(state, factory, outcome=sg.outcome)
    payload: dict[str, Any] = {
        "stops": sg.stops,
        "outcome": sg.outcome,
        "outcome_label": _safeguard_outcome_label(sg.outcome, sg.fallback_model),
        "category": sg.category,
        "model": _safeguard_model_label(sg.model),
    }
    if sg.fallback_model:
        payload["fallback_model"] = _safeguard_model_label(sg.fallback_model)
    return payload


def _translate_informational(
    event: claude_schema.StreamSystemMessage,
    *,
    state: ClaudeStreamState,
    factory: EventFactory,
) -> list[UntetherEvent]:
    """#814 ``system/informational``: the safeguard notice → the 🛡️ row;
    other warning/notice banners → a generic row; info/suggestion → log
    only (D-15: widen after the 24 h audit shows the level mix)."""
    content = _str_or_none(event.content)
    level = _str_or_none(event.level)
    if content and _SAFEGUARD_NOTICE_RE.search(content):
        m = _SAFEGUARD_NOTICE_MODEL_RE.match(content)
        return _note_safeguard(
            state,
            factory,
            source="informational",
            outcome=SAFEGUARD_OUTCOME_RETRIED,
            model=m.group(1).strip() if m else None,
        )
    first_line = (content or "").splitlines()[0].strip() if content else ""
    if len(first_line) > _INFORMATIONAL_MAX_CHARS:
        first_line = first_line[: _INFORMATIONAL_MAX_CHARS - 1] + "…"
    rendered = level in _INFORMATIONAL_ROW_LEVELS and bool(first_line)
    (logger.info if rendered else logger.debug)(
        "claude.informational",
        session_id=event.session_id,
        level=level,
        prevent_continuation=event.prevent_continuation is True,
        content_len=len(content or ""),
        rendered=rendered,
    )
    if not rendered or not state.turn_open:
        return []
    tool_use_id = _str_or_none(event.tool_use_id)
    if tool_use_id is not None:
        # "Dedupes progress messages for the same tool use" — one row.
        action_id = f"claude.informational.{tool_use_id}"
    else:
        state.note_seq += 1
        action_id = f"claude.informational.{state.note_seq}"
    icon = (
        "\N{WARNING SIGN}\N{VARIATION SELECTOR-16}"
        if level == "warning"
        else "\N{INFORMATION SOURCE}\N{VARIATION SELECTOR-16}"
    )
    title = f"{icon} {first_line}"
    detail: dict[str, Any] = {"level": level}
    return [
        factory.action_started(
            action_id=action_id, kind="note", title=title, detail=detail
        ),
        factory.action_completed(
            action_id=action_id,
            kind="note",
            title=title,
            ok=True,
            level="warning" if level == "warning" else "info",
            detail=detail,
        ),
    ]


def _translate_model_refusal_fallback(
    event: claude_schema.StreamSystemMessage,
    *,
    state: ClaudeStreamState,
    factory: EventFactory,
) -> list[UntetherEvent]:
    """#814 ``system/model_refusal_fallback``: the refused request is re-run
    on the fallback model."""
    fallback = _str_or_none(event.fallback_model)
    if fallback and _str_or_none(event.scope) != "local":
        # ``scope:"local"`` = a subagent / side question fell back; the
        # session model is unchanged.
        state.session_model = fallback
    return _note_safeguard(
        state,
        factory,
        source="model_refusal_fallback",
        outcome=SAFEGUARD_OUTCOME_SWITCHED,
        category=_str_or_none(event.api_refusal_category),
        model=_str_or_none(event.original_model),
        fallback_model=fallback,
    )


def _translate_model_refusal_no_fallback(
    event: claude_schema.StreamSystemMessage,
    *,
    state: ClaudeStreamState,
    factory: EventFactory,
) -> list[UntetherEvent]:
    """#814 ``system/model_refusal_no_fallback``: refused and not re-run."""
    return _note_safeguard(
        state,
        factory,
        source="model_refusal_no_fallback",
        outcome=SAFEGUARD_OUTCOME_NOT_RETRIED,
        category=_str_or_none(event.api_refusal_category),
        model=_str_or_none(event.original_model),
    )


def _translate_model_fallback(
    event: claude_schema.StreamSystemMessage,
    *,
    state: ClaudeStreamState,
    factory: EventFactory,
) -> list[UntetherEvent]:
    """#814 ``system/model_fallback``: the turn switched to the configured
    fallback model (overloaded / model_not_found / …) — not a safeguard
    stop, but the user should know which model answered."""
    original = _str_or_none(event.original_model)
    fallback = _str_or_none(event.fallback_model)
    trigger = _str_or_none(event.trigger)
    logger.info(
        "claude.model_fallback",
        session_id=event.session_id,
        original_model=original,
        fallback_model=fallback,
        trigger=trigger,
    )
    if fallback:
        state.session_model = fallback
    if not state.turn_open:
        return []
    src = _safeguard_model_label(original) or "?"
    dst = _safeguard_model_label(fallback) or "?"
    title = (
        f"\N{RIGHTWARDS ARROW WITH HOOK}\N{VARIATION SELECTOR-16} "
        f"Switched model {src} \N{RIGHTWARDS ARROW} {dst}"
    )
    if trigger:
        title += f" ({trigger.replace('_', ' ')})"
    state.note_seq += 1
    action_id = f"claude.model_fallback.{state.note_seq}"
    detail: dict[str, Any] = {"original_model": original, "fallback_model": fallback}
    return [
        factory.action_started(
            action_id=action_id, kind="note", title=title, detail=detail
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


# #835: tools that change files or run commands. In an unattended `plan` run
# they are denied at stage 6 — plan mode sends them to the host on current
# CLIs (`decision_reason_type: "mode"`, the Probe-G regression), and stage 6
# would otherwise approve them with nobody having seen a plan.
_UNATTENDED_PLAN_DENIED_TOOLS = frozenset(
    {"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"}
)
# #835: modes where the CLI resolves permissions itself, so anything that
# still reaches stage 6 is an ask-class request — an `ask` rule, a hook's
# `ask`, a tool that requires user interaction, a critical-path rm, or (in
# `auto`) the classifier falling back to prompting after repeated blocks
# (probe 2026-10-02, CLI 2.1.287: `decision_reason_type: "classifier"`).
# `dontAsk` auto-denies anything that would prompt, so a request that still
# reaches stage 6 under it is ask-class too — never approved unattended.
_UNATTENDED_DENY_ALL_MODES = frozenset({"bypassPermissions", "auto", "dontAsk"})


def _unattended_autonomous_deny_reason(
    state: ClaudeStreamState, tool_name: str, *, diff_gate: bool
) -> str | None:
    """Why stage 6's autonomous auto-approve must deny in an unattended run.

    ``None`` means "approve as an attended run would" (e.g. Glob in `plan`,
    anything in `plan-auto`). Never called for attended runs' decisions.
    """
    if state.unattended_trigger is None:
        return None
    mode = state.unattended_mode
    if mode in _UNATTENDED_DENY_ALL_MODES:
        return "ask_class"
    if mode == "plan" and tool_name in _UNATTENDED_PLAN_DENIED_TOOLS:
        return "plan_mode"
    if diff_gate:
        return "diff_preview"
    return None


def _unattended_deny_message(trigger: str, tool: str, reason: str) -> str:
    kind = "webhook" if trigger.startswith("webhook:") else "scheduled"
    head = f"Untether: this is an unattended {kind} run ({trigger}) — "
    if tool == "ExitPlanMode":
        return (
            head + "nobody can approve a plan. Do not call ExitPlanMode again. "
            "Finish by giving the complete plan as your final answer."
        )
    if tool == "AskUserQuestion":
        return (
            head + "nobody can answer questions. Proceed with reasonable "
            "defaults and state your assumptions."
        )
    if reason == "diff_preview":
        what = f"nobody can review this change, so {tool} was denied"
    elif reason == "plan_mode":
        what = (
            f"it runs in plan mode and nobody can approve a plan, so {tool} was denied"
        )
    else:
        what = f"nobody is available to approve {tool}, so it was denied"
    return (
        head + what + ". Continue only with actions that don't need approval; "
        "if the task can't be finished without it, stop and report what you "
        "would have done and why it needs approval."
    )


def _unattended_deny(
    state: ClaudeStreamState,
    factory: EventFactory,
    request_id: str,
    request: Any,
    *,
    reason: str,
) -> list[UntetherEvent]:
    """#835: deny a request an unattended run would otherwise wait on.

    Queued on ``auto_deny_queue`` (written by ``_drain_auto_deny``), never
    registered for a tap. One ``🔒`` note row per turn; every denial logs
    ``permission.unattended_deny`` and is counted for the final's footer.
    """
    trigger = state.unattended_trigger or "?"
    if isinstance(request, claude_schema.ControlCanUseToolRequest):
        tool = getattr(request, "tool_name", "") or "unknown"
    else:
        tool = type(request).__name__.replace("Control", "").replace("Request", "")
    message = _unattended_deny_message(trigger, tool, reason)
    if tool == "ExitPlanMode":
        _drop_exitplanmode_plan(
            state,
            request_id,
            rejected=False,
            reason="unattended",
            session_id=factory.resume.value if factory.resume else None,
        )
    _REQUEST_TO_INPUT.pop(request_id, None)
    _REQUEST_TO_TOOL_NAME.pop(request_id, None)
    state.auto_deny_queue.append((request_id, message))
    state.unattended_denials.append(tool)
    logger.warning(
        "permission.unattended_deny",
        tool_name=tool,
        trigger_source=trigger,
        session_id=factory.resume.value if factory.resume else None,
        request_id=request_id,
        permission_mode=state.unattended_mode,
        effective_permission_mode=state.effective_permission_mode,
        reason=reason,
        turn_denials=len(state.unattended_denials),
    )
    if len(state.unattended_denials) > 1:
        return []
    title = f"\N{LOCK} Unattended run — denied {tool}: nobody to approve it"
    state.note_seq += 1
    action_id = f"claude.unattended_deny.{state.note_seq}"
    detail: dict[str, Any] = {"trigger": trigger, "tool_name": tool, "reason": reason}
    return [
        factory.action_started(
            action_id=action_id, kind="note", title=title, detail=detail
        ),
        factory.action_completed(
            action_id=action_id,
            kind="note",
            title=title,
            ok=True,
            level="warning",
            detail=detail,
        ),
    ]


def _unattended_usage_payload(state: ClaudeStreamState) -> dict[str, Any] | None:
    """#835 ``usage["unattended"]`` for the turn (None without denials)."""
    if state.unattended_trigger is None or not state.unattended_denials:
        return None
    denied: dict[str, int] = {}
    for tool in state.unattended_denials:
        denied[tool] = denied.get(tool, 0) + 1
    return {
        "trigger": state.unattended_trigger,
        "mode": state.unattended_mode,
        "denied": denied,
    }


def _permission_mode_mismatch_rows(
    event: claude_schema.StreamSystemMessage,
    *,
    state: ClaudeStreamState,
    factory: EventFactory,
) -> list[UntetherEvent]:
    """#751: compare the first ``system/init.permissionMode`` with the mode
    this process asked for, once per process.

    The CLI does not warn when it can't honour ``--permission-mode`` (findings
    Q3: ``auto`` on Haiku silently runs as ``default``). A mismatch logs
    ``claude.permission_mode.mismatch`` and returns a warning ``note`` row.

    Enforcement: when the CLI actually runs a prompting mode but the run was
    classed autonomous at ``new_state`` (so ``--allowedTools`` went out and
    stage 6 would blanket-approve), ``prompting_mode`` is re-armed so every
    later ``can_use_tool`` routes to Telegram. ``system/init`` precedes every
    control request of the run. A gate is never disarmed on a CLI report.
    """
    if state.permission_mode_checked:
        return []
    requested = state.requested_permission_mode
    reported = event.permissionMode
    if requested is None or not isinstance(reported, str) or not reported:
        return []
    state.permission_mode_checked = True
    effective = normalise_claude_cli_mode(reported) or reported
    if effective == requested:
        return []
    rearmed = False
    if is_claude_prompting_mode(effective) and not state.prompting_mode:
        state.prompting_mode = True
        rearmed = True
    logger.warning(
        "claude.permission_mode.mismatch",
        session_id=event.session_id,
        requested=requested,
        effective=reported,
        model=_str_or_none(event.model),
        resumed=state.resumed,
        prompting_rearmed=rearmed,
    )
    title = (
        f"\N{WARNING SIGN}\N{VARIATION SELECTOR-16} Asked for {requested} mode"
        f" \N{EM DASH} Claude Code is running {reported}"
    )
    if requested == "auto":
        title += " (auto mode isn't available for this model)"
    if rearmed:
        title += "; approvals will be requested"
    state.note_seq += 1
    action_id = f"claude.permission_mode_mismatch.{state.note_seq}"
    detail: dict[str, Any] = {
        "requested": requested,
        "effective": reported,
        "prompting_rearmed": rearmed,
    }
    return [
        factory.action_started(
            action_id=action_id, kind="note", title=title, detail=detail
        ),
        factory.action_completed(
            action_id=action_id,
            kind="note",
            title=title,
            ok=True,
            level="warning",
            detail=detail,
        ),
    ]


# #812 hook lifecycle (``--include-hook-events``).
_PENDING_HOOKS_MAX = 256
# SessionStart / Setup hook events are always emitted and settled in-band by
# the CLI itself (findings §A1 P2) — they never hold a session.
_HOOK_NEVER_HOLD_EVENTS = frozenset({"SessionStart", "Setup"})
# A rewake turn opens right after its hook's ``hook_response`` (P5-A: same
# millisecond); a hint older than this is not taken as the turn's cause.
_HOOK_REWAKE_HINT_TTL_S = 10.0
# #828: a hint the next turn didn't open on (a slow turn start) is carried
# into that turn for its result's ``origin`` check only this long; an older
# hint is dropped (P5-A: hint → ``init`` is ~20 ms, so this is generous).
_HOOK_REWAKE_CARRY_TTL_S = 60.0
# The asyncRewake wake signal: exit code 2 (``outcome: "error"``).
_HOOK_REWAKE_EXIT_CODE = 2


def _hook_event_name(event: claude_schema.StreamSystemMessage) -> str | None:
    hook_event = _str_or_none(event.hook_event)
    if hook_event:
        return hook_event
    # ``hook_name`` is ``Stop`` or ``SessionStart:startup`` style.
    name = _str_or_none(event.hook_name)
    return name.split(":", 1)[0] if name else None


def _hook_never_holds(event_name: str | None) -> bool:
    return event_name in _HOOK_NEVER_HOLD_EVENTS


def _apply_hook_event(
    state: ClaudeStreamState, event: claude_schema.StreamSystemMessage
) -> None:
    """#812: track ``hook_started`` / ``hook_response`` pairs by ``hook_id``.

    #828: only a hook that **outlived the turn it started in** can be an
    asyncRewake signal — a background subagent's sync ``PreToolUse`` denial
    (or the next turn's ``UserPromptSubmit`` blocker) starts and ends while
    the parent idles, and the open turn's own sync hooks end inside it. Such
    exit-2 responses log ``claude.hook.blocking_exit`` instead.

    Produces no UntetherEvents — hook traffic (every configured hook on every
    tool call) must never reach progress rows or the bridge's stall timers.
    """
    subtype = event.subtype
    hook_id = _str_or_none(event.hook_id)
    name = _str_or_none(event.hook_name)
    hook_event = _hook_event_name(event)
    session_id = event.session_id
    if subtype == "hook_started":
        state.hooks_started += 1
        if hook_id is None or _hook_never_holds(hook_event):
            return
        if hook_id not in state.pending_hooks and (
            len(state.pending_hooks) >= _PENDING_HOOKS_MAX
        ):
            oldest_id = next(iter(state.pending_hooks))
            oldest = state.pending_hooks.pop(oldest_id)
            logger.debug(
                "claude.hook.pending_evicted",
                session_id=session_id,
                hook_id=oldest_id,
                hook_event=oldest.event,
                cap=_PENDING_HOOKS_MAX,
            )
        state.pending_hooks[hook_id] = PendingHook(
            hook_id=hook_id,
            name=name,
            event=hook_event,
            started_at=time.monotonic(),
            # Started while idle → the upcoming turn's hook (e.g. its
            # UserPromptSubmit, emitted before ``system/init``).
            turn=state.turn if state.turn_open else state.turn + 1,
            started_clock=hook_clock(),
        )
        return
    if subtype != "hook_response":
        return  # hook_progress: output polling, nothing to track.
    pending = state.pending_hooks.pop(hook_id, None) if hook_id else None
    expired = state.expired_hooks.pop(hook_id, None) if hook_id else None
    deferred = state.deferred_hooks.pop(hook_id, None) if hook_id else None
    known = pending or expired or deferred
    outcome = _str_or_none(event.outcome)
    exit_code = event.exit_code
    exit_2 = (
        outcome == "error"
        and isinstance(exit_code, int)
        and not isinstance(exit_code, bool)
        and exit_code == _HOOK_REWAKE_EXIT_CODE
        and not _hook_never_holds(hook_event)
    )
    # #828: it outlived its turn — started in a turn that has since closed
    # (idle: any turn up to the last one; mid-turn: an earlier turn). A hook
    # started while idle is tagged ``state.turn + 1`` and never qualifies;
    # an unknown hook id (never seen / evicted) is too weak to count.
    outlived = known is not None and (
        known.turn < state.turn if state.turn_open else known.turn <= state.turn
    )
    is_rewake_signal = exit_2 and outlived
    held_s = round(time.monotonic() - known.started_at, 1) if known else None
    if is_rewake_signal:
        hint = (name, hook_event, time.monotonic())
        if not state.turn_open:
            state.hook_rewake_hint = hint
        else:
            # An earlier turn's async hook exited 2 after this turn opened
            # (the turn's own cause, confirmed by ``origin`` at its result).
            state.turn_hook_hint = hint
        logger.info(
            "claude.hook.rewake_signal",
            session_id=session_id,
            hook_name=name,
            hook_event=hook_event,
            turn_open=state.turn_open,
            started_turn=known.turn if known else None,
            held_s=held_s,
        )
    elif exit_2:
        # #828: a blocking (sync) hook said no — a denial, not a rewake.
        logger.info(
            "claude.hook.blocking_exit",
            session_id=session_id,
            hook_name=name,
            hook_event=hook_event,
            turn_open=state.turn_open,
            started_turn=known.turn if known else None,
            held_s=held_s,
            known=known is not None,
        )
    elif outcome == "cancelled":
        logger.info(
            "claude.hook.cancelled",
            session_id=session_id,
            hook_name=name,
            hook_event=hook_event,
            exit_code=exit_code,
        )
    else:
        logger.debug(
            "claude.hook.response",
            session_id=session_id,
            hook_name=name,
            hook_event=hook_event,
            outcome=outcome,
            exit_code=exit_code,
        )


def _translate_hook_event(
    event: claude_schema.StreamSystemMessage,
    *,
    state: ClaudeStreamState,
    factory: EventFactory,
) -> list[UntetherEvent]:
    _apply_hook_event(state, event)
    return []


def has_pending_async_hooks(state: ClaudeStreamState) -> bool:
    """#812: True while a hook may still be running in the background — a
    ``hook_started`` with no ``hook_response`` yet, inside the hold bound.
    The live-session lifecycle holds stdin open on it (closing stdin makes
    the CLI drop an asyncRewake hook's findings, §A1 P5-B).

    Deliberately separate from :func:`has_live_background_work` (D-1): hooks
    aren't background tasks, so they must not show in footers or the #777
    panel, nor change the #592 cap, the #346 gate or ``_is_clean_idle``.

    All-or-nothing: the frames carry no pid and hooks started in the same
    millisecond can't be told apart, so no unpaired hook is judged finished
    on its own (``release_settled_async_hooks`` releases them together once
    no hook process is left). The hold therefore lasts until the *newest*
    unpaired hook is ``async_hook_max_hold_s`` old — whichever one is
    running gets its full bound — then every unpaired hook moves to
    ``expired_hooks`` with one ``claude.hook.hold_expired`` WARN, even if a
    hook process is still up (the bound is the bound).
    """
    if not state.hold_for_async_hooks or not state.pending_hooks:
        return False
    hooks = [h for h in state.pending_hooks.values() if not _hook_never_holds(h.event)]
    if not hooks:
        return False
    max_hold = state.async_hook_max_hold_s
    if max_hold <= 0:
        return False
    now = time.monotonic()
    if now - max(h.started_at for h in hooks) < max_hold:
        return True
    for hook in hooks:
        state.pending_hooks.pop(hook.hook_id, None)
        if len(state.expired_hooks) >= _PENDING_HOOKS_MAX:
            state.expired_hooks.pop(next(iter(state.expired_hooks)))
        state.expired_hooks[hook.hook_id] = hook
    logger.warning(
        "claude.hook.hold_expired",
        session_id=(
            state.factory.resume.value if state.factory.resume is not None else None
        ),
        # The hooks actually still running (None: process table unreadable);
        # ``pending_hooks`` counts the unpaired candidates, most of which may
        # be plain ``async`` hooks that finished long ago.
        live_hook_processes=state.live_hook_processes,
        pending_hooks=len(hooks),
        hook_events=_hook_event_labels(hooks),
        hook_ids=[h.hook_id for h in hooks],
        held_s=round(now - min(h.started_at for h in hooks), 1),
        max_hold_s=max_hold,
    )
    return False


# A hook younger than this is never judged finished on a process scan (its
# ``/bin/sh -c`` child may not be visible yet), and the lifecycle releases
# only after finding no hook process for this long (a hook that just exited
# may not have reported back yet — exit → ``hook_response`` isn't atomic).
_HOOK_PROC_SETTLE_S = 1.0


def release_settled_async_hooks(
    state: ClaudeStreamState, *, now: float | None = None
) -> list[PendingHook]:
    """#812: the CLI has no hook process left, so every unpaired hook (older
    than ``_HOOK_PROC_SETTLE_S``) has finished. The ones still unpaired are
    plain ``async: true`` hooks — the CLI withholds their ``hook_response``
    until the next turn or teardown, so waiting for it would hold the
    session to ``async_hook_max_hold_s`` for nothing. An ``asyncRewake``
    hook reports back as soon as it exits (and a finished process can't
    rewake), so none is lost. Moves them — and any expired ones, equally
    finished — to ``deferred_hooks`` (still paired when the response lands;
    never "killed" at a close). Returns the pending hooks released.

    Only ever called on "no hook process at all": while any hook process
    is alive, nothing is released, because a shell can't be tied to a hook."""
    now = time.monotonic() if now is None else now
    released: list[PendingHook] = []
    for hook_id, hook in list(state.pending_hooks.items()):
        if _hook_never_holds(hook.event) or now - hook.started_at < _HOOK_PROC_SETTLE_S:
            continue
        state.pending_hooks.pop(hook_id, None)
        released.append(hook)
        _defer_hook(state, hook)
    for hook_id, hook in list(state.expired_hooks.items()):
        state.expired_hooks.pop(hook_id, None)
        _defer_hook(state, hook)
    return released


def _defer_hook(state: ClaudeStreamState, hook: PendingHook) -> None:
    if len(state.deferred_hooks) >= _PENDING_HOOKS_MAX:
        state.deferred_hooks.pop(next(iter(state.deferred_hooks)))
    state.deferred_hooks[hook.hook_id] = hook


def _hooks_outstanding(state: ClaudeStreamState) -> list[PendingHook]:
    """#812: hooks still unpaired at a close — pending or past their hold."""
    return [
        hook
        for hook in (*state.pending_hooks.values(), *state.expired_hooks.values())
        if not _hook_never_holds(hook.event)
    ]


# ── #383: plan approvals are turn-scoped, and the approval says so ─────────

_PLAN_APPROVE_BUTTON = "✅ Approve Plan"
_PLAN_CAPTION_CARRY_OUT = (
    "Approving lets Claude carry out this plan without further prompts."
)
# #383 C4: worded to stay true under the agent deferral — plan mode comes
# back after the background agents the approved reply starts have finished.
_PLAN_CAPTION_RESUMES = (
    " Plan mode resumes when this reply ends,"
    " or after the background agents it starts have finished."
)
_PLAN_CAPTION_PROMPTING = (
    "Approving ends planning; Claude still asks before each action."
)
_PLAN_APPROVED_FEEDBACK = "✅ Plan approved — Claude will carry it out now"
_PLAN_APPROVED_FEEDBACK_RESUMES = " · plan mode resumes when it's done"


def _plan_mode_resumes(state: ClaudeStreamState) -> bool:
    """True when the chat is a plan chat AND plan mode really comes back once
    the approved reply ends: live sessions off (every message respawns with
    ``--permission-mode plan``), or the live-session re-arm is on. The
    approval UI only claims it when true."""
    if not state.configured_plan_mode:
        return False
    return not state.live_mode or state.rearm_plan_mode


def _plan_approve_caption(state: ClaudeStreamState) -> str | None:
    """#383: the line under an ExitPlanMode approval saying what approving
    does. Plan / plan-auto chats get the carry-out sentence (plus the
    "resumes" clause only when :func:`_plan_mode_resumes`); a prompting-mode
    chat (Claude entered plan mode itself) gets its own wording; other
    autonomous modes get none."""
    if state.configured_plan_mode:
        caption = _PLAN_CAPTION_CARRY_OUT
        if _plan_mode_resumes(state):
            caption += _PLAN_CAPTION_RESUMES
        return caption
    if state.prompting_mode:
        return _PLAN_CAPTION_PROMPTING
    return None


def plan_approved_feedback(session_id: str | None) -> str:
    """#383: the feedback edit shown after a plan is approved in Telegram."""
    text = _PLAN_APPROVED_FEEDBACK
    state = _SESSION_BG_STATE.get(session_id) if session_id else None
    if state is not None and _plan_mode_resumes(state):
        text += _PLAN_APPROVED_FEEDBACK_RESUMES
    return text


def _scope_plan_approvals_to_turn(
    session_id: str | None, *, turn: int, reason: str
) -> None:
    """#383: a live-session turn boundary ends the reply a plan approval was
    given in. ``_PLAN_EXIT_APPROVED`` is cleared outright; an unconsumed
    post-outline approval (``_DISCUSS_APPROVED``) survives exactly one
    boundary (``_DISCUSS_CARRY``) so "outline → Approve Plan → go ahead"
    needs one tap, and is cleared at the second."""
    if session_id is None:
        return
    cleared: list[str] = []
    if session_id in _PLAN_EXIT_APPROVED:
        _PLAN_EXIT_APPROVED.discard(session_id)
        cleared.append("plan_exit_approved")
    if session_id in _DISCUSS_CARRY:
        _DISCUSS_CARRY.discard(session_id)
        if session_id in _DISCUSS_APPROVED:
            _DISCUSS_APPROVED.discard(session_id)
            cleared.append("discuss_approved")
    elif session_id in _DISCUSS_APPROVED:
        _DISCUSS_CARRY.add(session_id)
        logger.info(
            "claude.plan_approval.carried",
            session_id=session_id,
            turn=turn,
            turn_reason=reason,
        )
    if cleared:
        logger.info(
            "claude.plan_approval.cleared",
            session_id=session_id,
            turn=turn,
            turn_reason=reason,
            reason="turn_boundary",
            cleared=cleared,
        )


_PLAN_REARM_ID_PREFIX = "ut_plan_rearm_"


def _stamp_plan_exit(state: ClaudeStreamState) -> None:
    """#383 fallback for CLIs without status frames: an approval path just
    let a plan chat leave plan mode."""
    if state.configured_plan_mode and state.plan_exited_at is None:
        state.plan_exited_at = time.monotonic()
        state.plan_exit_turn = state.turn


def _note_permission_mode(
    state: ClaudeStreamState, mode: str, *, source: str, session_id: str | None
) -> None:
    """#383: record the CLI's effective permission mode from a native signal
    (``source`` ∈ ``init`` | ``status`` | ``ack``)."""
    previous = state.effective_permission_mode
    if mode != previous:
        state.effective_permission_mode = mode
        logger.info(
            "claude.permission_mode.changed",
            session_id=session_id,
            source=source,
            turn=state.turn,
            **{"from": previous, "to": mode},
        )
    if mode == "plan":
        state.plan_mode_observed = True
        state.plan_exited_at = None
        state.plan_exit_turn = None
        state.plan_rearm_failed = False
        state.plan_rearm_deferred = 0
        state.plan_rearm_deferred_since = None
        # Back in plan mode: nothing the old approval covered is running
        # unplanned any more. `_DISCUSS_APPROVED` is a pre-exit approval
        # and follows only the one-boundary carry.
        if session_id is not None and session_id in _PLAN_EXIT_APPROVED:
            _PLAN_EXIT_APPROVED.discard(session_id)
            logger.info(
                "claude.plan_approval.cleared",
                session_id=session_id,
                turn=state.turn,
                reason="plan_rearmed",
                cleared=["plan_exit_approved"],
            )
    elif state.configured_plan_mode:
        _stamp_plan_exit(state)


def _translate_status(
    event: claude_schema.StreamSystemMessage,
    *,
    state: ClaudeStreamState,
    factory: EventFactory,
) -> list[UntetherEvent]:
    """``system/status`` frames — one handler, two shapes.

    #383: a string ``permissionMode`` is the CLI's mode-change edge
    (``status`` is null); it produces no events. #819: ``status:
    "compacting"`` and the ``status: null`` + ``compact_result`` that ends
    it drive the 🗜️ compaction row (:func:`_translate_compaction_status`).
    A ``status: null`` with neither a result nor an open row is the #383
    frame only."""
    mode = event.permissionMode
    if isinstance(mode, str):
        _note_permission_mode(
            state,
            mode,
            source="status",
            session_id=event.session_id
            or (factory.resume.value if factory.resume else None),
        )
    return _translate_compaction_status(event, state=state, factory=factory)


def _handle_plan_rearm_ack(
    state: ClaudeStreamState,
    response: claude_schema.ControlResponse,
    *,
    session_id: str | None,
) -> None:
    """#383: the CLI's answer to our ``set_permission_mode`` request. Only
    the one in flight counts; a stale id is logged and ignored."""
    request_id = response.request_id
    if request_id != state.plan_rearm_inflight:
        logger.debug(
            "claude.permission_mode.rearm_ack_stale",
            session_id=session_id,
            request_id=request_id,
            inflight=state.plan_rearm_inflight,
        )
        return
    state.plan_rearm_inflight = None
    match response:
        case claude_schema.ControlSuccessResponse():
            body = response.response or {}
            mode = body.get("mode") if isinstance(body, dict) else None
            logger.info(
                "claude.permission_mode.rearm_ack",
                session_id=session_id,
                request_id=request_id,
                mode=mode,
            )
            if isinstance(mode, str):
                _note_permission_mode(state, mode, source="ack", session_id=session_id)
        case claude_schema.ControlErrorResponse():
            state.plan_rearm_failed = True
            logger.warning(
                "claude.permission_mode.rearm_failed",
                session_id=session_id,
                request_id=request_id,
                error=response.error,
                error_code=response.error_code,
            )


# #383 Decision 6: plan-auto re-arms before follow-ups and idle steers only.
# Its rubber stamp approves every ExitPlanMode, so planning a wake turn adds
# no gate — only a plan-model call and an ExitPlanMode round trip per tick.
_PLAN_AUTO_REARM_AT_IDLE = False


# Re-arm reasons that write a user line right after the re-arm; every other
# reason is idle-side: ``idle`` (a turn closed) and, #383 C4, ``agents_done``
# / ``agents_idle`` / ``ceiling`` (the approved plan's agents finished, went
# quiet, or outlived the cap while the session idled).
_REARM_USER_LINE_REASONS = frozenset({"followup", "steer"})


def _plan_rearm_needed(state: ClaudeStreamState, *, reason: str) -> bool:
    """#383: should this live session be put back into CLI plan mode?

    ``reason``: ``followup`` or ``steer`` (a user line is about to be
    written), else idle-side (see ``_REARM_USER_LINE_REASONS``). Never for
    non-plan chats, never outside a live session, never when the CLI never
    reported plan (its own precedence won, e.g.
    --dangerously-skip-permissions), never twice. The agent deferral (C4) is
    applied by :func:`_claim_plan_rearm`, not here."""
    if not (state.rearm_plan_mode and state.live_mode):
        return False
    if not state.configured_plan_mode:
        return False
    if (
        reason not in _REARM_USER_LINE_REASONS
        and state.auto_approve_exit_plan_mode
        and not _PLAN_AUTO_REARM_AT_IDLE
    ):
        return False
    if not state.plan_mode_observed or state.plan_rearm_failed:
        return False
    if state.plan_rearm_inflight is not None:
        return False
    return state.effective_permission_mode != "plan" or state.plan_exited_at is not None


def _exit_turn_agents(state: ClaudeStreamState) -> list[ClaudeTask]:
    """#383 C4: background agents launched in the turn that left plan mode
    (the approved plan's workers) that still hold the session. Keyed on
    ``origin_turn``, not start time: agents a later (unplanned) turn
    launches carry a later turn and never extend the deferral, and a #801
    revival re-stamps ``origin_turn`` to the reviving turn."""
    exit_turn = state.plan_exit_turn
    if exit_turn is None:
        return []
    return [
        task
        for task in state.tasks.values()
        if task.task_type == "local_agent"
        and task.holds_session
        and task.origin_turn == exit_turn
    ]


def _rearm_deferral(
    state: ClaudeStreamState, *, now: float | None = None
) -> tuple[int, str]:
    """#383 C4 (plan 21 D7): ``(agents, why)`` — agents > 0 while the re-arm
    must wait (``why="live_agents"``). It ends when the exit-turn agents are
    gone (``agents_done``), when none of them has shown activity
    (``latest_background_progress``: a ``task_progress`` frame, a subagent
    tool starting or ending) for ``post_result_bg_max_hold``
    (``agents_idle``; 0 = no inactivity bound), or ``live_session_max_s``
    after the plan exit (``ceiling``)."""
    agents = _exit_turn_agents(state)
    if not agents:
        return 0, "agents_done"
    now = time.monotonic() if now is None else now
    exited = state.plan_exited_at
    if (
        exited is not None
        and state.live_session_max_s > 0
        and now - exited >= state.live_session_max_s
    ):
        return 0, "ceiling"
    if state.bg_max_hold_s > 0:
        last = latest_background_progress(state, [task.task_id for task in agents])
        if last is None or now - last >= state.bg_max_hold_s:
            return 0, "agents_idle"
    return len(agents), "live_agents"


def _claim_plan_rearm(
    state: ClaudeStreamState,
    session_id: str,
    *,
    reason: str,
    command_uuid: str | None = None,
) -> bytes | None:
    """Claim the (single-flight) re-arm synchronously — before any await, so
    two writers can't both send — and return the request line, or None.

    #383 C4: None while the approved plan's agents still run
    (:func:`_rearm_deferral`); a follow-up / idle steer written meanwhile is
    remembered by ``command_uuid`` so its turn says it wasn't re-planned."""
    if not _plan_rearm_needed(state, reason=reason):
        return None
    agents, why = _rearm_deferral(state)
    if agents:
        state.plan_rearm_pending = False
        state.plan_rearm_deferred = agents
        if state.plan_rearm_deferred_since is None:
            state.plan_rearm_deferred_since = time.monotonic()
        if command_uuid is not None:
            state.unplanned_commands[command_uuid] = agents
        if not state.plan_rearm_deferred_logged:
            state.plan_rearm_deferred_logged = True
            logger.info(
                "claude.permission_mode.rearm_deferred",
                session_id=session_id,
                reason=why,
                trigger=reason,
                agents=agents,
                plan_exit_turn=state.plan_exit_turn,
                turn=state.turn,
            )
        return None
    if state.plan_rearm_deferred:
        since = state.plan_rearm_deferred_since
        logger.info(
            "claude.permission_mode.rearm_deferral_ended",
            session_id=session_id,
            reason=why,
            trigger=reason,
            plan_exit_turn=state.plan_exit_turn,
            deferred_s=(
                round(time.monotonic() - since, 1) if since is not None else None
            ),
        )
        state.plan_rearm_deferred = 0
        state.plan_rearm_deferred_since = None
    state.plan_rearm_seq += 1
    request_id = f"{_PLAN_REARM_ID_PREFIX}{session_id}_{state.plan_rearm_seq}"
    state.plan_rearm_inflight = request_id
    state.plan_rearm_pending = False
    request = {
        "type": "control_request",
        "request_id": request_id,
        "request": {"subtype": "set_permission_mode", "mode": "plan"},
    }
    return (json.dumps(request) + "\n").encode()


async def _send_plan_rearm(
    state: ClaudeStreamState,
    stdin: Any,
    payload: bytes,
    *,
    session_id: str,
    reason: str,
) -> bool:
    request_id = state.plan_rearm_inflight
    try:
        await _locked_send(stdin, payload)
    except (OSError, anyio.ClosedResourceError, anyio.BrokenResourceError) as exc:
        state.plan_rearm_inflight = None
        logger.warning(
            "claude.permission_mode.rearm_write_failed",
            session_id=session_id,
            request_id=request_id,
            reason=reason,
            error_type=exc.__class__.__name__,
        )
        return False
    logger.info(
        "claude.permission_mode.rearm_sent",
        session_id=session_id,
        request_id=request_id,
        reason=reason,
        effective=state.effective_permission_mode,
        turn=state.turn,
    )
    return True


async def _write_plan_rearm_if_needed(
    live: LiveSession, *, reason: str, command_uuid: str | None = None
) -> bool:
    """#383 backstop before a follow-up / idle steer is written: FIFO on
    stdin makes the CLI apply plan before it starts that line's turn (a
    ``plan`` request can't be refused on 2.1.285, so no ack wait). Call it
    under ``live.lock``, immediately before ``write_user_message``. Also the
    lifecycle's write when a C4 deferral ends while the session idles."""
    payload = _claim_plan_rearm(
        live.state, live.session_id, reason=reason, command_uuid=command_uuid
    )
    if payload is None:
        return False
    return await _send_plan_rearm(
        live.state, live.stdin, payload, session_id=live.session_id, reason=reason
    )


class _SystemSubtypeHandler(Protocol):
    def __call__(
        self,
        event: claude_schema.StreamSystemMessage,
        *,
        state: ClaudeStreamState,
        factory: EventFactory,
    ) -> list[UntetherEvent]: ...


# ── #819: context-window use (``N% ctx`` in the status line) ─────────────

# Model id -> ``contextWindow`` learned from ``result.modelUsage``. Shared
# across runs and chats: the window is a property of the model, not a run.
_CONTEXT_WINDOWS: dict[str, int] = {}
_CONTEXT_WINDOWS_MAX = 64
_CONTEXT_ONE_M_SUFFIX = "[1m]"
_CONTEXT_ONE_M_WINDOW = 1_000_000
# (model, init model) pairs already logged as a window miss (DEBUG, once).
_CONTEXT_WINDOW_MISSES: set[tuple[str | None, str | None]] = set()
# (session, model) pairs already warned for a value over the window.
_CONTEXT_OVER_WINDOW_WARNED: set[tuple[str | None, str | None]] = set()
_CONTEXT_WARN_MAX = 256
_CONTEXT_ACTION_ID = "claude.context"


def _usage_context_tokens(usage: Any) -> int | None:
    """Input side of an API ``usage``: ``input_tokens`` +
    ``cache_creation_input_tokens`` + ``cache_read_input_tokens`` — the usage
    part of the CLI's ``/context`` total (D8). Non-negative ints only (bools
    excluded); None when ``usage`` is not a dict or holds none of them."""
    if not isinstance(usage, dict):
        return None
    total = 0
    seen = False
    for key in (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            total += value
            seen = True
    return total if seen else None


def _context_window_for(model: str | None, session_model: str | None) -> int | None:
    """The context window for *model* (the frame's) in a session started on
    *session_model* (``system/init``), or None when not yet learned (D3).

    1. Either id ends with ``[1m]`` and the other is absent, equal or its
       stripped base → 1 000 000. This wins over a cache hit on the stripped
       id, which may hold the base window learned from a non-``[1m]``
       session.
    2. Exact cache hit on *model*, then on *session_model*.

    Dated ids are never fuzzy-matched to a base id: a miss is logged instead
    (``claude.context.window_miss``) so a capture shows the real keys.
    """
    for one, other in ((session_model, model), (model, session_model)):
        if one and one.lower().endswith(_CONTEXT_ONE_M_SUFFIX):
            base = one[: -len(_CONTEXT_ONE_M_SUFFIX)]
            if other is None or other in (one, base):
                return _CONTEXT_ONE_M_WINDOW
    for key in (model, session_model):
        if key and (window := _CONTEXT_WINDOWS.get(key)) is not None:
            return window
    miss = (model, session_model)
    if miss not in _CONTEXT_WINDOW_MISSES and len(_CONTEXT_WINDOW_MISSES) < (
        _CONTEXT_WARN_MAX
    ):
        _CONTEXT_WINDOW_MISSES.add(miss)
        logger.debug(
            "claude.context.window_miss",
            model=model,
            session_model=session_model,
            known=sorted(_CONTEXT_WINDOWS),
        )
    return None


def _context_pct(used: int, window: int) -> int:
    """``Math.round(used / window * 100)`` with JS half-up semantics in
    integer maths (Python's ``round`` is banker's), floored at 0. Not
    clamped — the caller decides what a value over 100 means."""
    if window <= 0:
        return 0
    return max(0, (used * 200 + window) // (2 * window))


def _learn_context_windows(model_usage: Any) -> None:
    """Record every ``modelUsage.<model>.contextWindow`` (positive int)."""
    if not isinstance(model_usage, dict):
        return
    for model, entry in model_usage.items():
        if not isinstance(model, str) or not model or not isinstance(entry, dict):
            continue
        window = entry.get("contextWindow")
        if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
            continue
        if _CONTEXT_WINDOWS.get(model) == window:
            continue
        if model not in _CONTEXT_WINDOWS and len(_CONTEXT_WINDOWS) >= (
            _CONTEXT_WINDOWS_MAX
        ):
            continue
        _CONTEXT_WINDOWS[model] = window
        logger.info("claude.context.window_learned", model=model, context_window=window)


def _context_value(
    state: ClaudeStreamState, factory: EventFactory
) -> tuple[int | None, int | None]:
    """``(pct, window)`` for the run's current context value; ``pct`` is
    None while the numerator or the window is unknown. A raw value over 100
    is displayed as 100 after one ``claude.context.over_window`` WARN per
    (session, model)."""
    used = state.ctx_used
    if used is None:
        return None, None
    window = _context_window_for(state.ctx_model, state.ctx_init_model)
    if window is None:
        return None, None
    pct = _context_pct(used, window)
    if pct > 100:
        session_id = factory.resume.value if factory.resume else None
        key = (session_id, state.ctx_model)
        if key not in _CONTEXT_OVER_WINDOW_WARNED and len(
            _CONTEXT_OVER_WINDOW_WARNED
        ) < (_CONTEXT_WARN_MAX):
            _CONTEXT_OVER_WINDOW_WARNED.add(key)
            logger.warning(
                "claude.context.over_window",
                session_id=session_id,
                model=state.ctx_model,
                used=used,
                context_window=window,
                pct=pct,
            )
        pct = 100
    return pct, window


def _emit_context(
    state: ClaudeStreamState, factory: EventFactory, *, force: bool = False
) -> list[UntetherEvent]:
    """The ``telemetry`` ActionEvent carrying the header's ``% ctx`` — only
    when the value changed (or, with *force*, whenever one is known: a new
    live turn's tracker starts empty).

    Never emitted between live turns (after a result, before the next turn
    opens): the value is kept and the next turn open re-emits it."""
    pct, window = _context_value(state, factory)
    if force:
        if pct is None:
            return []
    else:
        key = (pct, window)
        if key == state.ctx_emitted or (state.ctx_emitted is None and pct is None):
            return []
        if state.live_mode and state.completed_turns > 0 and not state.turn_open:
            return []
    state.ctx_emitted = (pct, window)
    return [
        factory.action_updated(
            action_id=_CONTEXT_ACTION_ID,
            kind="telemetry",
            title="context",
            detail={
                "context_pct": pct,
                "context_used": state.ctx_used,
                "context_window": window,
                "model": state.ctx_model,
            },
        )
    ]


def _context_usage_payload(
    state: ClaudeStreamState, factory: EventFactory
) -> dict[str, Any] | None:
    """``usage["context"]`` at a result (log field, not rendered)."""
    pct, window = _context_value(state, factory)
    if pct is None:
        return None
    return {
        "pct": pct,
        "used": state.ctx_used,
        "window": window,
        "model": state.ctx_model,
    }


def _note_assistant_context(
    event: claude_schema.StreamAssistantMessage,
    *,
    state: ClaudeStreamState,
    factory: EventFactory,
) -> list[UntetherEvent]:
    """#819: a main-thread response's usage is the new context value.
    Subagent frames (``parent_tool_use_id``) and ``<synthetic>`` frames
    don't count — the CLI's ``/context`` skips them too."""
    if event.parent_tool_use_id is not None:
        return []
    message = event.message
    if not message.model or message.model == "<synthetic>":
        return []
    used = _usage_context_tokens(message.usage)
    if used is None:
        return []
    state.ctx_used = used
    state.ctx_model = message.model
    return _emit_context(state, factory)


# #819: how long one ``status: "compacting"`` frame keeps the compaction an
# expected wait. The CLI re-sends the frame every 30 s while compacting, so
# 120 s is four missed heartbeats — after that a wedged compaction is a
# stall again.
_COMPACTION_LATCH_S = 120.0
_COMPACTION_ERROR_CHARS = 80
_COMPACTION_ROW = "\N{COMPRESSION}\N{VARIATION SELECTOR-16}"
_COMPACTING_TITLE = f"{_COMPACTION_ROW} Compacting context…"
_COMPACTED_TITLE = f"{_COMPACTION_ROW} Context compacted"


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _new_compaction_id(state: ClaudeStreamState) -> str:
    state.note_seq += 1
    return f"claude.compaction.{state.note_seq}"


def _compacted_title(
    trigger: str | None, pre_tokens: int | None, post_tokens: int | None
) -> str:
    """``🗜️ Context compacted · 182k → 41k tokens (auto)`` — the arrow and
    the after-size only when ``post_tokens`` is known, the trigger only when
    it is a string."""
    title = _COMPACTED_TITLE
    if pre_tokens is not None and post_tokens is not None:
        title += f" · {format_tokens(pre_tokens)} → {format_tokens(post_tokens)} tokens"
    elif pre_tokens is not None:
        title += f" · {format_tokens(pre_tokens)} tokens before"
    if trigger:
        title += f" ({trigger})"
    return title


def _close_compaction_row(state: ClaudeStreamState) -> None:
    state.compaction_action_id = None
    state.compaction_wait_until = 0.0


def _reset_compaction_segment(state: ClaudeStreamState) -> None:
    """Per-segment compaction state — at every result and turn open. The
    summary flag goes too, so a CLI that omits the summary frame can never
    make the *next* real prompt skip its #544/#333 reset."""
    state.turn_compactions = []
    state.last_compact_result = None
    state.compaction_refine_id = None
    state.compaction_summary_pending = False
    _close_compaction_row(state)


def _compaction_usage(
    state: ClaudeStreamState, event: claude_schema.StreamResultMessage
) -> dict[str, Any] | None:
    """``usage["compaction"]`` for a result whose segment compacted (None
    otherwise). ``manual_success`` is the narrow #819 §4.5 exemption from
    the #596/#631 empty-result recovery: every compaction recorded in the
    segment was a ``manual`` one whose ``compact_result`` was ``success``
    (so it reached a boundary), and the result itself is not an error. An
    auto compaction — a #596 poisoned session can auto-compact on resume and
    *then* return the upstream 0-turn result — or a failed one never
    qualifies."""
    records = state.turn_compactions
    if not records:
        return None
    last = records[-1]
    manual_success = not event.is_error and all(
        rec.get("trigger") == "manual" and rec.get("result") == "success"
        for rec in records
    )
    return {
        "count": len(records),
        "trigger": last.get("trigger"),
        "pre_tokens": last.get("pre_tokens"),
        "post_tokens": last.get("post_tokens"),
        "result": last.get("result"),
        "manual_success": manual_success,
    }


def _translate_compaction_status(
    event: claude_schema.StreamSystemMessage,
    *,
    state: ClaudeStreamState,
    factory: EventFactory,
) -> list[UntetherEvent]:
    """#819: the 🗜️ row from ``system/status`` (Claude-Q4 / probe Z10).

    ``compacting`` opens the row (or, as the 30 s heartbeat, updates it and
    refreshes the liveness latch); ``status: null`` closes it — ``success``
    (the boundary re-titles it with token counts), ``failed`` (a warning
    row), or no result at all (a PreCompact hook skipped it). Anything
    else — ``requesting``, a mode-change ``status: null`` with no row open —
    produces nothing."""
    status = event.status
    session_id = event.session_id or (factory.resume.value if factory.resume else None)
    if status == "compacting":
        state.compaction_wait_until = time.monotonic() + _COMPACTION_LATCH_S
        if state.compaction_action_id is not None:
            return [
                factory.action_updated(
                    action_id=state.compaction_action_id,
                    kind="note",
                    title=_COMPACTING_TITLE,
                )
            ]
        state.compaction_action_id = _new_compaction_id(state)
        logger.info("claude.compaction.started", session_id=session_id)
        return [
            factory.action_started(
                action_id=state.compaction_action_id,
                kind="note",
                title=_COMPACTING_TITLE,
            )
        ]
    if status is not None:
        logger.debug("claude.status.ignored", status=status, session_id=session_id)
        return []
    result = event.compact_result if isinstance(event.compact_result, str) else None
    action_id = state.compaction_action_id
    if result == "failed":
        error = event.compact_error if isinstance(event.compact_error, str) else None
        title = f"{_COMPACTION_ROW} Compaction failed"
        if error and error.strip():
            snippet = " ".join(error.split())
            if len(snippet) > _COMPACTION_ERROR_CHARS:
                snippet = snippet[: _COMPACTION_ERROR_CHARS - 1] + "…"
            title += f" · {snippet}"
        logger.warning("claude.compaction.failed", session_id=session_id, error=error)
        # A failed compaction never produces a boundary: record it here.
        state.turn_compactions.append(
            {
                "trigger": None,
                "pre_tokens": None,
                "post_tokens": None,
                "result": "failed",
            }
        )
        state.last_compact_result = None
        _close_compaction_row(state)
        return [
            factory.action_completed(
                action_id=action_id or _new_compaction_id(state),
                kind="note",
                title=title,
                ok=False,
                level="warning",
            )
        ]
    if result is not None:
        # ``success`` (or a future value): the boundary that follows
        # consumes it and refines the row.
        state.last_compact_result = result
        _close_compaction_row(state)
        if action_id is None:
            return []
        state.compaction_refine_id = action_id
        return [
            factory.action_completed(
                action_id=action_id,
                kind="note",
                title=_COMPACTED_TITLE,
                ok=True,
            )
        ]
    if action_id is None or isinstance(event.permissionMode, str):
        # The #383 permission-mode frame (or a stray ``status: null``) — a
        # mode change while a row is open doesn't end the compaction.
        return []
    # A PreCompact hook skipped the compaction: plain ``status: null``.
    _close_compaction_row(state)
    logger.info("claude.compaction.skipped", session_id=session_id)
    return [
        factory.action_completed(
            action_id=action_id,
            kind="note",
            title=f"{_COMPACTION_ROW} Compaction skipped",
            ok=True,
        )
    ]


def _translate_compact_boundary(
    event: claude_schema.StreamSystemMessage,
    *,
    state: ClaudeStreamState,
    factory: EventFactory,
) -> list[UntetherEvent]:
    """``system/compact_boundary``: the context was compacted.

    Re-titles the row the ``success`` status closed (a fresh row when there
    was none) with ``pre → post`` tokens and the trigger — the same id, so
    the row stays one step (D10) — records the compaction for
    ``usage["compaction"]``, logs ``claude.compaction`` and flags the
    summary ``user`` frame that follows. #819 D5 — drops ``% ctx`` until
    the next main-thread response (``post_tokens`` excludes the system
    prompt and tools, so it would under-report)."""
    meta = event.compact_metadata if isinstance(event.compact_metadata, dict) else {}
    trigger = meta.get("trigger") if isinstance(meta.get("trigger"), str) else None
    pre_tokens = _int_or_none(meta.get("pre_tokens"))
    post_tokens = _int_or_none(meta.get("post_tokens"))
    result = state.last_compact_result
    state.turn_compactions.append(
        {
            "trigger": trigger,
            "pre_tokens": pre_tokens,
            "post_tokens": post_tokens,
            "result": result,
        }
    )
    state.last_compact_result = None
    action_id = (
        state.compaction_refine_id
        or state.compaction_action_id
        or _new_compaction_id(state)
    )
    state.compaction_refine_id = None
    _close_compaction_row(state)
    state.compaction_summary_pending = True
    logger.info(
        "claude.compaction",
        trigger=trigger,
        pre_tokens=pre_tokens,
        post_tokens=post_tokens,
        cumulative_dropped_tokens=_int_or_none(meta.get("cumulative_dropped_tokens")),
        duration_ms=_int_or_none(meta.get("duration_ms")),
        result=result,
        session_id=event.session_id
        or (factory.resume.value if factory.resume else None),
    )
    state.ctx_used = None
    return [
        factory.action_completed(
            action_id=action_id,
            kind="note",
            title=_compacted_title(trigger, pre_tokens, post_tokens),
            ok=True,
            detail={
                "trigger": trigger,
                "pre_tokens": pre_tokens,
                "post_tokens": post_tokens,
            },
        ),
        *_emit_context(state, factory),
    ]


# System subtypes with their own translation (#792, #814). Add a subtype by
# writing a handler with this signature and registering it here; anything
# not listed falls through to the ``claude.system_event.non_init`` debug
# log in ``_translate_claude_event_base``.
_SYSTEM_SUBTYPE_HANDLERS: dict[str, _SystemSubtypeHandler] = {
    "api_retry": _translate_api_retry,
    "informational": _translate_informational,
    "model_refusal_fallback": _translate_model_refusal_fallback,
    "model_refusal_no_fallback": _translate_model_refusal_no_fallback,
    "model_fallback": _translate_model_fallback,
    # #383: permission-mode edges; #819: the 🗜️ compaction row.
    "status": _translate_status,
    # #819: compaction boundary (refines the row, clears ``% ctx``).
    "compact_boundary": _translate_compact_boundary,
    # #812: hook lifecycle frames — tracked, never surfaced.
    "hook_started": _translate_hook_event,
    "hook_progress": _translate_hook_event,
    "hook_response": _translate_hook_event,
}


def _translate_system_subtype(
    event: claude_schema.StreamSystemMessage,
    *,
    state: ClaudeStreamState,
    factory: EventFactory,
) -> list[UntetherEvent] | None:
    """Dispatch a system frame to its registered handler; None when the
    subtype has none."""
    handler = _SYSTEM_SUBTYPE_HANDLERS.get(event.subtype)
    if handler is None and event.subtype.startswith("hook_"):
        # #812: any future hook lifecycle subtype stays silent too.
        handler = _translate_hook_event
    if handler is None:
        return None
    return handler(event, state=state, factory=factory)


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


# #749 permission modes already warned about an explicit `allowed_tools`
# override.  Process-scoped so a long-lived bot logs the interaction once per
# mode instead of once per run.
_PROMPTING_MODE_ALLOWLIST_LOGGED: set[str] = set()


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


def _agent_runs_in_background(raw_input: dict) -> bool:
    """Decide whether an Agent/Task tool_use launches a *background* subagent (#646).

    Claude Code runs subagents in the background **by default**: the tool
    contract reads "Subagents run in the background by default; ... Pass
    ``run_in_background: false`` for a synchronous run." The flag is therefore
    normally ABSENT from `input` — the observed shape is just
    ``{"description": ..., "subagent_type": ...}``.

    The pre-#646 predicate was ``bool(raw_input.get("run_in_background"))``,
    which read an omitted key as *foreground*. That inverted the upstream
    default and missed every real background subagent (measured: 11/11 Agent
    calls across the 5 sessions quarantined on nsd 2026-07-18 omit the key).
    An unregistered handle leaves `has_live_background_work()` False, so the
    post-result idle watchdog applies the 60s limbo grace instead of the full
    timeout, SIGTERMs a subprocess whose subagents are still working, and the
    #632 forced-teardown path then quarantines a perfectly healthy session —
    so the user's next message diverts to a fresh, contextless one.

    Hence: background unless the caller *explicitly* opted out with literal
    ``False``. Anything else (absent, None, true, or a malformed value) counts
    as background — over-registering is the safe direction, because a stale
    handle only forfeits the 60s shortcut while the 600s post-result ceiling
    and the 900s ``BG_AGENT_MAX_KEEP_S`` age-out both still bound teardown.
    Under-registering force-kills live work and poisons the session.

    Deliberately NOT applied to ``Bash(run_in_background=...)``, whose flag is
    genuinely opt-in upstream — see `_register_background_handle`.
    """
    return raw_input.get("run_in_background") is not False


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
        state.background_observed = True
        timeout_ms = raw_input.get("timeout_ms")
        if isinstance(timeout_ms, (int, float)) and timeout_ms > 0:
            state.live_monitors[tool_id] = time.monotonic() + (timeout_ms / 1000.0)
        else:
            # Unknown deadline → store 0.0 so membership tests still work
            state.live_monitors[tool_id] = 0.0
    elif tool_name == "Bash" and bool(raw_input.get("run_in_background")):
        state.background_observed = True
        state.live_bg_bashes.add(tool_id)
        # #573 (rc8 slice): bounded keep, same rationale as bg-agents in rc7.
        # A backgrounded Bash whose KillShell/completion signal never arrives
        # must not pin has_live_background_work() for the rest of the run.
        state.bg_bash_deadlines[tool_id] = time.monotonic() + BG_BASH_MAX_KEEP_S
        # #333: scalar high-water-mark that survives _clear_background_handle
        # (see ClaudeStreamState.last_bg_bash_launched_at docstring). Used by
        # the post-result idle watchdog tick log for observability only.
        state.last_bg_bash_launched_at = time.monotonic()
        # #872: the declared background budget (only with run_in_background —
        # a foreground ``timeout`` is a different limit, 2-10 min, and a
        # foreground command the CLI moves to the background gets 30 min from
        # the move, so it keeps the quiet-time rule).
        timeout_ms = raw_input.get("timeout")
        if (
            isinstance(timeout_ms, (int, float))
            and not isinstance(timeout_ms, bool)
            and timeout_ms > 0
        ):
            state.bg_bash_timeouts[tool_id] = timeout_ms / 1000.0
    elif tool_name in ("Agent", "Task") and _agent_runs_in_background(raw_input):
        state.background_observed = True
        state.live_bg_agents.add(tool_id)
        # #374 (rc7): bounded keep — see BG_AGENT_MAX_KEEP_S and
        # ClaudeStreamState.bg_agent_deadlines docstrings.
        state.bg_agent_deadlines[tool_id] = time.monotonic() + BG_AGENT_MAX_KEEP_S
    elif tool_name == "ScheduleWakeup":
        state.background_observed = True
        # #481: the actual Claude Code ScheduleWakeup tool schema (per
        # #289 / claude-agent-sdk-python) emits ``delaySeconds`` as the
        # canonical field. Earlier versions of this code read
        # ``delay_ms``/``timeout_ms`` only, which always missed in
        # production (live_wakeups[tool_id] fell to 0.0 → countdown
        # rendering broken, though membership-only suppression still
        # worked). Read delaySeconds first; keep the legacy fallbacks so
        # existing test fixtures parameterised on delay_ms still work.
        #
        # #544: also feed ``state.last_schedule_wakeup_arm_delay`` (a
        # per-turn scalar high-water-mark) so the post-result idle
        # watchdog's dead-wakeup shortcut survives the tool_result that
        # immediately pops ``live_wakeups`` via ``_clear_background_handle``.
        delay_seconds_raw = raw_input.get("delaySeconds")
        arm_delay_s: float | None = None
        if isinstance(delay_seconds_raw, (int, float)) and delay_seconds_raw > 0:
            arm_delay_s = float(delay_seconds_raw)
            state.live_wakeups[tool_id] = time.monotonic() + arm_delay_s
        else:
            delay_ms = raw_input.get("delay_ms") or raw_input.get("timeout_ms")
            if isinstance(delay_ms, (int, float)) and delay_ms > 0:
                arm_delay_s = delay_ms / 1000.0
                state.live_wakeups[tool_id] = time.monotonic() + arm_delay_s
            else:
                arm_delay_s = 0.0
                state.live_wakeups[tool_id] = 0.0
        # max(prev or 0, this) so multi-wakeup turns keep the longest arm
        prev = state.last_schedule_wakeup_arm_delay or 0.0
        state.last_schedule_wakeup_arm_delay = max(prev, arm_delay_s)
    elif tool_name == "RemoteTrigger":
        state.background_observed = True
        state.live_remote_triggers.add(tool_id)
        # #573 (rc8 slice): membership-only before, so a RemoteTrigger could
        # pin the session open forever. Age-out backstop only — a real
        # terminal signal still clears it earlier.
        state.remote_trigger_deadlines[tool_id] = (
            time.monotonic() + REMOTE_TRIGGER_MAX_KEEP_S
        )


def _clear_background_handle(
    state: ClaudeStreamState, tool_use_id: str, *, is_terminal: bool = True
) -> None:
    """Remove a background-task entry when its tool_result is *terminal* (#347, #374).

    #374: a tool_result is not always a terminal signal. A long-running Monitor
    streams *multiple* interim tool_results while it runs; clearing the
    ``live_monitors`` entry on the first one dropped the
    ``stall_monitor_active_suppressed`` branch (runner_bridge), so spurious stall
    warnings rose again while the Monitor was legitimately still working. The same
    premature-drain bug applies to Agent/Task-bg (rc7): clearing on the first
    interim result poisoned the "no live background work" signal the empty-resume
    detector relies on (#596). When ``is_terminal`` is False the handle is left in
    place — ``_is_terminal_tool_result`` makes the call, using a bounded deadline
    for both primitives (Monitor's own ``timeout_ms``; Agent/Task-bg's
    ``BG_AGENT_MAX_KEEP_S``) so a handle can never be kept forever. ``is_terminal``
    defaults True so direct callers and the remaining primitives (Bash-bg,
    ScheduleWakeup, RemoteTrigger) keep their pre-#374 clear-on-result behaviour
    (see ``_is_terminal_tool_result`` for why their interim-handling is still
    deferred to the v0.35.5 lifecycle refactor, #573).

    Note: ``state.last_schedule_wakeup_arm_delay`` and
    ``state.last_bg_bash_launched_at`` are deliberately NOT cleared here.
    A tool_result for ScheduleWakeup or ``Bash(run_in_background=True)``
    is the arm/launch confirmation, not a terminal signal — the wakeup
    fires later (or never, outside ``/loop dynamic mode``) and a
    backgrounded bash continues running until it finishes. The
    post-result idle watchdog (#507/#333) needs these scalars to
    survive this clear so its diagnostic tick log can see them after
    the matching ``result`` event lands. Both scalars reset on the
    next user prompt or on ``new_state`` (#544, #333).
    """
    if not is_terminal:
        return
    state.live_monitors.pop(tool_use_id, None)
    state.live_bg_bashes.discard(tool_use_id)
    state.bg_bash_deadlines.pop(tool_use_id, None)
    state.live_bg_agents.discard(tool_use_id)
    state.bg_agent_deadlines.pop(tool_use_id, None)
    state.live_wakeups.pop(tool_use_id, None)
    state.live_remote_triggers.discard(tool_use_id)
    state.remote_trigger_deadlines.pop(tool_use_id, None)


def _is_terminal_tool_result(
    content: claude_schema.StreamToolResultBlock
    | claude_schema.StreamAdvisorToolResultBlock,
    state: ClaudeStreamState,
    tool_use_id: str,
) -> bool:
    """Decide whether a tool_result terminates its background handle (#374).

    Two primitives can receive *interim* (non-terminal) results today, each with
    its own bounded deadline so neither risks keeping a handle forever:

    - **Monitor** feeds back the watched command's **raw stdout lines as they
      appear** (see ``docs/plans/2026-05-06-289-loop-and-cron-interception.md``),
      so clearing ``live_monitors`` on the first line dropped the
      ``stall_monitor_active_suppressed`` branch while the Monitor was still
      running. Its bound is the primitive's own ``timeout_ms`` deadline
      (``has_live_background_work`` already uses the same deadline to age the
      handle out — so no leak).

    - **Agent/Task running in the background** (rc7, #374) — i.e. every Agent/Task
      except an explicit ``run_in_background=False``, since background is the
      upstream default (#646, ``_agent_runs_in_background``). These likewise emit
      an interim tool_result (observed: ``"Async agent launched successfully."``
      ~25ms after the tool_use) before the subagent actually finishes. Clearing
      ``live_bg_agents`` on that first result reintroduced the same premature-drain
      bug the Monitor fix addressed: it poisoned the "no live background work"
      signal the empty-resume detector relies on (#596). There is no reliable
      upstream completion signal for Agent/Task yet (true-terminal detection via
      KillShell, subprocess-exit reconciliation, and child-PID cleanup is the
      v0.35.5 lifecycle refactor, #573), so its bound is the fixed
      ``BG_AGENT_MAX_KEEP_S`` deadline set at register time in
      ``_register_background_handle`` — the safe trade-off between "keep
      suppressing stall warnings while genuinely running" and "never leave a
      handle uncleared forever".

    Because tool_result text is arbitrary in both cases (Monitor: raw command
    stdout; Agent/Task: subagent-authored prose), we deliberately do NOT scan it
    for "completed"/"cancelled" markers for either primitive — that is unreliable
    in both directions (a build printing "Done" mid-stream would false-clear and
    reintroduce the bug; a real completion that doesn't print a magic word would be
    missed). The reliable terminal signals are ``is_error`` and the primitive's own
    bounded deadline.

    Every other tool_result — including Bash-bg, ScheduleWakeup, and
    RemoteTrigger, whose true-terminal detection remains deferred to the v0.35.5
    refactor (#573) — is treated as terminal, preserving the pre-#374
    clear-on-first-result behaviour for foreground tools and those primitives.
    """
    monitor_deadline = state.live_monitors.get(tool_use_id)
    if monitor_deadline is not None:
        if content.is_error is True:
            return True
        # Unknown (0.0) or already-expired deadline → clear now (no leak risk;
        # matches has_live_background_work's expiry semantics). A live future
        # deadline means an interim stdout line → keep the handle so
        # stall-suppression keeps firing.
        return monitor_deadline == 0.0 or monitor_deadline <= time.monotonic()

    bg_agent_deadline = state.bg_agent_deadlines.get(tool_use_id)
    if bg_agent_deadline is not None:
        if content.is_error is True:
            return True
        # Past-deadline → aged out, terminal regardless of whether an explicit
        # completion signal ever arrives (no permanent-hang guarantee — see
        # BG_AGENT_MAX_KEEP_S).
        return bg_agent_deadline <= time.monotonic()

    # Not a tracked Monitor or bg-agent → terminal (unchanged behaviour).
    return True


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
    from ..utils.paths import get_run_channel_id, get_run_thread_id

    chat_id = get_run_channel_id()
    if chat_id is None:
        return  # not in a chat-scoped run (probes, ad-hoc spawns)
    # #826: record the run's topic so /new and /cancel in another forum topic
    # leave this loop alone, and fires land back in the originating topic.
    thread_id = get_run_thread_id()
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
                thread_id=thread_id,
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
                thread_id=thread_id,
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


def _live_bg_agent_count(state: ClaudeStreamState) -> int:
    """Count Agent/Task-bg handles whose bounded deadline hasn't passed (#374).

    ``live_bg_agents`` and ``bg_agent_deadlines`` are parallel structures (see
    ``ClaudeStreamState.bg_agent_deadlines`` docstring) — every registered
    handle should have a matching deadline, but a handle with no deadline entry
    is defensively treated as already expired (not live) rather than live
    forever, matching the bounded-keep bias of ``_is_terminal_tool_result``.

    Both consumers below (the #346 wedge-detector gate and the progress-footer
    summary) need to age out expired bg-agent handles identically, so the
    expiry expression lives here once instead of being duplicated at each call
    site.
    """
    now = time.monotonic()
    return sum(
        1
        for tool_id in state.live_bg_agents
        if state.bg_agent_deadlines.get(tool_id, 0.0) > now
    )


def _live_bounded_handle_count(handles: set[str], deadlines: dict[str, float]) -> int:
    """Count handles in ``handles`` whose parallel deadline hasn't passed.

    #573 — generalises the rc7 ``_live_bg_agent_count`` pattern to any
    background primitive tracked as a set plus a parallel deadline map. A
    handle with no deadline entry is defensively treated as expired rather
    than live forever: an over-eager expiry costs at most one premature
    watchdog tick, whereas a handle that never expires pins the session open
    indefinitely.
    """
    now = time.monotonic()
    return sum(1 for tool_id in handles if deadlines.get(tool_id, 0.0) > now)


def _live_native_tasks(state: ClaudeStreamState) -> list[ClaudeTask]:
    """Live background tasks that hold the session open — top-level ones and
    a subagent's backgrounded tasks alike (#801)."""
    return [task for task in state.tasks.values() if task.holds_session]


def _is_native_monitor(state: ClaudeStreamState, task: ClaudeTask) -> bool:
    # Monitor registers as ``task_type=local_bash`` (F10); the tool_use handle
    # registered in ``live_monitors`` (keyed by tool_use_id) tells it apart
    # from a ``Bash(run_in_background=true)``.
    return task.tool_use_id is not None and task.tool_use_id in state.live_monitors


def _live_deadline_count(deadlines: dict[str, float]) -> int:
    now = time.monotonic()
    return sum(
        1 for deadline in deadlines.values() if deadline == 0.0 or deadline > now
    )


def _live_background_counts(state: ClaudeStreamState) -> tuple[int, int]:
    """Return ``(watchers, bg_tasks)`` currently live — the single source for
    every liveness consumer (#776, D-4).

    ScheduleWakeup and RemoteTrigger always come from their tool_use handles
    (no native events exist for them). Monitor / Bash-bg / Agent-bg come from
    the native task map once the CLI has emitted any task event in this
    process; otherwise from the legacy handles (older CLIs).
    """
    watchers = _live_deadline_count(state.live_wakeups)
    bg_tasks = _live_bounded_handle_count(
        state.live_remote_triggers, state.remote_trigger_deadlines
    )
    if state.native_tasks_seen:
        for task in _live_native_tasks(state):
            if _is_native_monitor(state, task):
                watchers += 1
            else:
                bg_tasks += 1
    else:
        watchers += _live_deadline_count(state.live_monitors)
        bg_tasks += _live_bg_agent_count(state) + _live_bounded_handle_count(
            state.live_bg_bashes, state.bg_bash_deadlines
        )
    return watchers, bg_tasks


def has_live_background_work(state: ClaudeStreamState) -> bool:
    """Return True when the session has any live background work (#346 gate).

    #776: native ``system/task_*`` events decide for Monitor / Bash-bg /
    Agent-bg as soon as the CLI emits them; the bounded tool_use handles
    (#374/#573) remain for ScheduleWakeup / RemoteTrigger and as the fallback
    for CLIs that never emit task events. See ``_live_background_counts``.
    """
    watchers, bg_tasks = _live_background_counts(state)
    return watchers + bg_tasks > 0


# ── #829: background activity (what re-arms the live session's hold) ──────


@dataclass(frozen=True, slots=True)
class BackgroundActivity:
    """The newest background activity a live session has seen (#829).

    ``at`` is ``time.monotonic()``; ``source`` is ``task_started`` (a start or
    revival), ``task_progress`` (an agent's per-tool-call frame),
    ``agent_tool`` (a subagent-owned foreground tool started, ended, or is
    still running) or ``bash_output`` (a background Bash's output file was
    written)."""

    at: float
    source: str
    task_id: str


def _stamp_progress(task: ClaudeTask, source: str) -> None:
    task.last_progress_at = time.monotonic()
    task.last_progress_source = source


def _task_by_tool_use_id(
    state: ClaudeStreamState, tool_use_id: str
) -> ClaudeTask | None:
    for task in state.tasks.values():
        if task.tool_use_id == tool_use_id:
            return task
    return None


def _is_owned_foreground(task: ClaudeTask) -> bool:
    """A subagent's own foreground tool (``owned_by_subagent``, not
    backgrounded): the CLI registers one that runs longer than ~3 s and ends
    it with a ``task_notification`` (P0 G3). It never holds the session."""
    return task.owned_by_subagent and not task.is_backgrounded


def _stamp_owner(state: ClaudeStreamState, task: ClaudeTask) -> None:
    """#829 A.2: a subagent's foreground tool starting or ending means its
    owning agent is working — the agent emits no ``task_progress`` while one
    long tool runs (P0 G2)."""
    if not _is_owned_foreground(task) or not task.owner_tool_use_id:
        return
    owner = _task_by_tool_use_id(state, task.owner_tool_use_id)
    if owner is not None and owner.ended_at is None:
        _stamp_progress(owner, "agent_tool")


def latest_background_activity(
    state: ClaudeStreamState, task_ids: Iterable[str] | None = None
) -> BackgroundActivity | None:
    """The newest activity among the tasks holding the live session open
    (``ClaudeTask.holds_session``), optionally restricted to ``task_ids``.

    A holding agent with a live subagent-owned foreground tool counts as
    active *now* (``agent_tool``): it is working through one long call.
    Only native task-map evidence — a background Bash's output file is
    checked separately (``_bash_output_activity``), off-thread, and only when
    the hold would expire. None when no (matching) task holds the session.
    """
    wanted = None if task_ids is None else set(task_ids)
    busy_owners = {
        task.owner_tool_use_id
        for task in state.tasks.values()
        if _is_owned_foreground(task)
        and task.owner_tool_use_id
        and task.ended_at is None
        and task.status in _TASK_LIVE_STATUSES
    }
    now = time.monotonic()
    best: BackgroundActivity | None = None
    for task in _live_native_tasks(state):
        if wanted is not None and task.task_id not in wanted:
            continue
        if task.tool_use_id is not None and task.tool_use_id in busy_owners:
            candidate = BackgroundActivity(now, "agent_tool", task.task_id)
        else:
            candidate = BackgroundActivity(
                task.last_progress_at, task.last_progress_source, task.task_id
            )
        if best is None or candidate.at > best.at:
            best = candidate
    return best


def latest_background_progress(
    state: ClaudeStreamState, task_ids: Iterable[str] | None = None
) -> float | None:
    """#829: monotonic time of the newest background activity among the
    tasks holding the live session (see ``latest_background_activity``);
    None when none holds it.

    Stable API: #383's plan-mode re-arm deferral bounds itself on "the
    exit-turn agents idle for ``post_result_bg_max_hold``" by passing those
    agents' ``task_ids`` — ``time.monotonic() - latest_background_progress(
    state, task_ids) >= max_hold`` (None: none of them holds the session any
    more), with ``live_session_max_s`` as the ceiling.
    """
    activity = latest_background_activity(state, task_ids)
    return None if activity is None else activity.at


def _last_progress_age_s(state: ClaudeStreamState) -> float | None:
    at = latest_background_progress(state)
    return None if at is None else round(time.monotonic() - at, 1)


_BG_OUTPUT_FILE_RE = re.compile(r"Output is being written to: (\S+?\.output)\b")


def _note_bg_output_file(
    state: ClaudeStreamState, tool_use_id: str, content: Any
) -> None:
    """#829: remember a background Bash's output file from its tool_result."""
    if isinstance(content, list):
        content = " ".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    if not isinstance(content, str) or "Output is being written to:" not in content:
        return
    match = _BG_OUTPUT_FILE_RE.search(content)
    if match is not None:
        state.bg_output_files[tool_use_id] = match.group(1)


def _output_file_mtimes(paths: list[str]) -> dict[str, float]:
    """Wall-clock mtimes of the files that exist (runs in a worker thread)."""
    out: dict[str, float] = {}
    for path in paths:
        with contextlib.suppress(OSError):
            out[path] = os.stat(path).st_mtime
    return out


async def _bash_output_activity(state: ClaudeStreamState) -> BackgroundActivity | None:
    """#829 fallback: ``local_bash`` emits no ``task_progress`` (P0 G4), but
    its output file grows while the command prints. The newest write among
    the holding, non-Monitor background Bash tasks, as monotonic time; None
    when there is none (a silent command never re-arms the hold)."""
    candidates: list[tuple[ClaudeTask, str]] = []
    for task in _live_native_tasks(state):
        if task.task_type != "local_bash" or _is_native_monitor(state, task):
            continue
        path = state.bg_output_files.get(task.tool_use_id or "")
        if path:
            candidates.append((task, path))
    if not candidates:
        return None
    try:
        mtimes = await anyio.to_thread.run_sync(
            _output_file_mtimes, [path for _, path in candidates]
        )
    except Exception:  # noqa: BLE001 — a stat failure must not break the hold
        logger.debug("claude.live_session.output_stat_failed", exc_info=True)
        return None
    wall_now = time.time()
    mono_now = time.monotonic()
    best: BackgroundActivity | None = None
    for task, path in candidates:
        mtime = mtimes.get(path)
        if mtime is None:
            continue
        at = mono_now - max(0.0, wall_now - mtime)
        if best is None or at > best.at:
            best = BackgroundActivity(at, "bash_output", task.task_id)
    return best


@dataclass(frozen=True, slots=True)
class DeclaredWait:
    """#872: a wait Claude declared — a background Bash's ``timeout``
    (``bash_timeout``) or a pending ScheduleWakeup (``scheduled_wakeup``).

    ``until`` is ``time.monotonic()`` (grace included); ``declared_s`` is the
    budget Claude declared (the Bash ``timeout``, or the wake-up's delay from
    the announced fire time); ``remaining_s`` is ``until - now``."""

    until: float
    source: str
    task_id: str | None
    declared_s: float
    remaining_s: float


def declared_wait_until(
    state: ClaudeStreamState, *, grace_s: float, now: float | None = None
) -> DeclaredWait | None:
    """#872: the latest declared wait still running, or None.

    - A live, non-Monitor ``local_bash`` task whose tool_use carried a
      background ``timeout`` (``bg_bash_timeouts``) waits until its start +
      that timeout + ``grace_s`` — the CLI stops the command at the timeout
      (counted from when it entered the background) and wakes Claude.
      Subagent-launched background Bash counts too (#801).
    - A pending ScheduleWakeup waits until its announced fire time
      (``pending_wakeup_until``, already graced by ``_WAKEUP_FIRE_GRACE_S``).

    Pure (no I/O)."""
    if now is None:
        now = time.monotonic()
    best: DeclaredWait | None = None
    for task in _live_native_tasks(state):
        if task.task_type != "local_bash" or _is_native_monitor(state, task):
            continue
        timeout_s = state.bg_bash_timeouts.get(task.tool_use_id or "")
        if not timeout_s:
            continue
        until = task.started_at + timeout_s + grace_s
        if until > now and (best is None or until > best.until):
            best = DeclaredWait(
                until=until,
                source="bash_timeout",
                task_id=task.task_id,
                declared_s=timeout_s,
                remaining_s=until - now,
            )
    if _has_pending_wakeup(state) and state.pending_wakeup_until is not None:
        until = state.pending_wakeup_until
        if until > now and (best is None or until > best.until):
            best = DeclaredWait(
                until=until,
                source="scheduled_wakeup",
                task_id=None,
                declared_s=(
                    state.pending_wakeup_delay_s
                    if state.pending_wakeup_delay_s is not None
                    else max(0.0, until - _WAKEUP_FIRE_GRACE_S - now)
                ),
                remaining_s=until - now,
            )
    return best


def background_task_summary(state: ClaudeStreamState) -> str | None:
    """Return a compact "⏳ 2 watchers · 1 bg task" summary or None if empty.

    Used by progress footer rendering (#347 v2) and the `/background`
    command. Counts come from ``_live_background_counts`` so the footer and
    the liveness gate can never disagree (#776).
    """
    watchers, bg_tasks = _live_background_counts(state)
    if watchers == 0 and bg_tasks == 0:
        return None
    parts: list[str] = []
    if watchers:
        parts.append(f"{watchers} watcher{'s' if watchers != 1 else ''}")
    if bg_tasks:
        parts.append(f"{bg_tasks} bg task{'s' if bg_tasks != 1 else ''}")
    return "⏳ " + " · ".join(parts)


# #785: a task ending this soon after an ``unknown`` wake turn completed is
# taken to be what that turn answered (nsd evidence: ~8 s between the turn's
# result and the task's ``background_tasks_changed`` end).
_WAKE_PAIR_WINDOW_S = 30.0
# #825: how long a late task the model didn't see stays pending for the
# wake turn the CLI starts for it (R17-821: 9 s, after an empty turn).
_LATE_TASK_CARRY_S = 120.0


def _is_top_level_background(task: ClaudeTask) -> bool:
    """A background task the parent session launched itself — not a
    subagent's own (nested / foreground) tool."""
    return task.is_backgrounded and not task.owned_by_subagent


def _task_label(task: ClaudeTask) -> str:
    return task.description or task.task_type or "background task"


def _note_task_end(state: ClaudeStreamState, task: ClaudeTask) -> None:
    """#785: attribute a top-level background task's end to the wake turn it
    belongs to — the open ``unknown`` turn (retro-attributed at completion)
    or an ``unknown`` turn that completed moments ago (paired: the task's own
    notification turn that follows is then flagged as already announced).
    #825: a task ending while a ``task_finished`` turn is open (one it didn't
    open) is collected too and added to that turn's header at completion."""
    if not state.live_mode or state.completed_turns == 0:
        return
    if not _is_top_level_background(task) or task.task_id in state.announced_task_ids:
        return
    if state.turn_open:
        if (
            state.turn_reason in ("unknown", "task_finished")
            and task.task_id not in state.turn_detail.get("task_ids", [])
            and all(task.task_id != tid for tid, _ in state.turn_ended_tasks)
        ):
            state.turn_ended_tasks.append((task.task_id, _task_label(task)))
            state.turn_ended_at_request[task.task_id] = state.turn_model_requests
        return
    at = state.unattributed_turn_completed_at
    if at is None:
        return
    gap = time.monotonic() - at
    if gap > _WAKE_PAIR_WINDOW_S:
        return
    state.unattributed_turn_completed_at = None
    state.announced_task_ids.add(task.task_id)
    state.task_announced_turn[task.task_id] = state.turn
    logger.info(
        "claude.turn.task_end_paired",
        turn=state.turn,
        task_id=task.task_id,
        description=_task_label(task)[:80],
        gap_s=round(gap, 1),
    )


def _seen_late_tasks(
    state: ClaudeStreamState,
    event: claude_schema.StreamResultMessage,
    detail: dict[str, Any],
) -> list[tuple[str, str]]:
    """#825: the tasks that ended during this ``task_finished`` turn (and
    don't already label it) *and* that the model could have seen in it.

    The CLI hands a finished task's notification to the model at its next
    request; a task that ended after the turn's last request began (R17-821:
    124 ms before the result, the body already saying "B is still running")
    gets a wake turn of its own instead. Those are kept in
    ``pending_late_tasks`` for that turn rather than named here."""
    late = [
        (tid, label)
        for tid, label in state.turn_ended_tasks
        if tid not in detail.get("task_ids", [])
    ]
    seen: list[tuple[str, str]] = []
    unseen: list[tuple[str, str]] = []
    for tid, label in late:
        ended_at = state.turn_ended_at_request.get(tid)
        if ended_at is None or state.turn_model_requests > ended_at:
            seen.append((tid, label))
        else:
            unseen.append((tid, label))
    if unseen:
        now = time.monotonic()
        state.pending_late_tasks.extend((tid, label, now) for tid, label in unseen)
        logger.info(
            "claude.turn.late_tasks_deferred",
            session_id=event.session_id,
            turn=state.turn,
            task_ids=[tid for tid, _ in unseen],
            model_requests=state.turn_model_requests,
        )
    return seen


def _take_pending_late_tasks(state: ClaudeStreamState) -> list[tuple[str, str]]:
    """#825: the deferred late tasks still waiting for their wake turn
    (not announced since, not older than ``_LATE_TASK_CARRY_S``); clears
    the list."""
    now = time.monotonic()
    carried = [
        (tid, label)
        for tid, label, at in state.pending_late_tasks
        if tid not in state.announced_task_ids and now - at <= _LATE_TASK_CARRY_S
    ]
    state.pending_late_tasks = []
    return carried


def _notification_labels_turn(
    event: claude_schema.StreamSystemMessage, task: ClaudeTask | None
) -> bool:
    """#785: only a top-level background task's notification may attribute a
    wake turn. A subagent's own task (``owned_by_subagent`` / foreground)
    finishing is not the parent's news and named the wrong task on nsd."""
    if task is not None:
        return _is_top_level_background(task)
    return event.owned_by_subagent is not True and event.is_backgrounded is not False


def _end_task(
    state: ClaudeStreamState, task: ClaudeTask, status: str, reason: str
) -> None:
    if task.ended_at is not None:
        # The CLI sends the empty background_tasks_changed snapshot a moment
        # before task_updated (F1/F3): let the authoritative status replace
        # the snapshot's provisional "ended".
        if task.status == "ended" and status != "ended":
            task.status = status
            logger.debug(
                "claude.task.status_refined",
                task_id=task.task_id,
                status=status,
                reason=reason,
            )
        return
    task.status = status
    task.ended_at = time.monotonic()
    # #829 A.2: a subagent's long foreground tool finishing = its agent works.
    _stamp_owner(state, task)
    log = logger.info if task.is_backgrounded else logger.debug
    log(
        "claude.task.ended",
        task_id=task.task_id,
        task_type=task.task_type,
        status=status,
        reason=reason,
        duration_s=round(task.ended_at - task.started_at, 1),
    )
    _note_task_end(state, task)
    _note_plan_deferral_task_end(state, task)


def _note_plan_deferral_task_end(state: ClaudeStreamState, task: ClaudeTask) -> None:
    """#383 C4: an agent the approved (plan-exit) turn launched ended while
    the session idles — queue a re-check of the deferred re-arm. The
    post-line drain writes it right after this line, ahead of the wake turn
    the CLI starts for the finish (same residual window as probe P-6)."""
    if (
        state.plan_rearm_deferred
        and state.live_mode
        and not state.turn_open
        and state.plan_rearm_inflight is None
        and task.task_type == "local_agent"
        and task.origin_turn == state.plan_exit_turn
    ):
        state.plan_rearm_pending = True
        state.plan_rearm_pending_reason = "agents_done"


def _revive_task(
    state: ClaudeStreamState, task: ClaudeTask, source: str, status: str = "running"
) -> None:
    """#801: bring an ended task back to life. Claude resuming a finished
    agent reuses its ``task_id``; left terminal, the task reads as idle and
    the live session closes under the resumed agent."""
    prior_status = task.status
    ended_at = task.ended_at
    task.status = status
    task.ended_at = None
    task.started_at = time.monotonic()
    _stamp_progress(task, "task_started")  # #829: a resumed agent is working
    task.revived_count += 1
    # #795: the turn that resumed it is the one its next wake turn answers.
    task.origin_turn = state.turn
    # Its next end is a new finish (#785): a wake turn may announce it again.
    state.announced_task_ids.discard(task.task_id)
    if task.holds_session:
        state.background_observed = True
    log = logger.info if task.is_backgrounded else logger.debug
    log(
        "claude.task.revived",
        task_id=task.task_id,
        task_type=task.task_type,
        prior_status=prior_status,
        ended_ago_s=(
            round(task.started_at - ended_at, 1) if ended_at is not None else None
        ),
        revived_count=task.revived_count,
        description=(task.description or "")[:80],
        source=source,
    )


def _mark_backgrounded(state: ClaudeStreamState, task: ClaudeTask, source: str) -> None:
    """#825/#876: the CLI moved a running foreground task to the background
    (a foreground command past its ``timeout``, a message arriving while it
    ran, Ctrl+B, an agent's ``autoBackgroundMs``). It now holds the live
    session (#776) and — when the parent launched it — labels its wake turn.
    ``source``: ``task_updated`` (``patch.is_backgrounded``), ``snapshot``
    (listed in ``background_tasks_changed``) or ``idle_notification`` (its
    notification reached an idle parent, which a foreground tool can't)."""
    if task.is_backgrounded:
        return
    task.is_backgrounded = True
    if task.holds_session:
        state.background_observed = True
        _stamp_progress(task, "task_started")  # #829: it is running now
    logger.info(
        "claude.task.backgrounded",
        task_id=task.task_id,
        task_type=task.task_type,
        owned_by_subagent=task.owned_by_subagent,
        description=(task.description or "")[:80],
        source=source,
    )


def _mark_announced(state: ClaudeStreamState, task_ids: Iterable[str]) -> None:
    """#785: record finishes a wake turn delivered — except a task that has
    been revived since (#801): its next end is news, not the same finish."""
    for task_id in task_ids:
        task = state.tasks.get(task_id)
        if task is not None and task.ended_at is None:
            continue
        state.announced_task_ids.add(task_id)


def _register_task(
    state: ClaudeStreamState, event: claude_schema.StreamSystemMessage, source: str
) -> ClaudeTask:
    task_id = event.task_id or ""
    task = state.tasks.get(task_id)
    created = task is None
    if task is None:
        task = ClaudeTask(task_id=task_id)
        state.tasks[task_id] = task
    elif task.ended_at is not None and source == "task_started":
        _revive_task(state, task, source)
    if event.task_type is not None:
        task.task_type = event.task_type
    if event.tool_use_id is not None:
        task.tool_use_id = event.tool_use_id
    if event.description is not None:
        task.description = event.description
    if event.subagent_type is not None:
        task.subagent_type = event.subagent_type
    if event.is_backgrounded is not None:
        task.is_backgrounded = event.is_backgrounded
    if event.owned_by_subagent is not None:
        task.owned_by_subagent = event.owned_by_subagent
    if task.holds_session:
        state.background_observed = True
    if created or source == "task_started":
        log = logger.info if task.is_backgrounded else logger.debug
        log(
            "claude.task.registered",
            task_id=task_id,
            task_type=task.task_type,
            is_backgrounded=task.is_backgrounded,
            owned_by_subagent=task.owned_by_subagent,
            subagent_type=task.subagent_type,
            description=(task.description or "")[:80],
            source=source,
        )
    return task


def _stamp_task_origin(state: ClaudeStreamState, task: ClaudeTask) -> None:
    """#795: remember the turn a task was launched in (first sighting wins)."""
    if task.origin_turn is None:
        task.origin_turn = state.turn


def _task_attribution(state: ClaudeStreamState, task_ids: list[str]) -> dict[str, Any]:
    """#795/#785: TurnEvent detail naming the tasks a wake turn answers and
    the turn that launched them (the bridge replies to that turn's message)."""
    detail: dict[str, Any] = {"task_ids": list(task_ids)}
    for tid in task_ids:
        task = state.tasks.get(tid)
        if task is not None and task.origin_turn is not None:
            detail["origin_turn"] = task.origin_turn
            break
    return detail


def _apply_task_event(
    state: ClaudeStreamState, event: claude_schema.StreamSystemMessage
) -> None:
    """Fold one ``system/task_*`` / ``background_tasks_changed`` event into the
    native task map (#776). Never emits Untether events."""
    subtype = event.subtype
    state.native_tasks_seen = True
    if subtype == "background_tasks_changed":
        snapshot = event.tasks or []
        present: set[str] = set()
        now = time.monotonic()
        for entry in snapshot:
            task_id = entry.get("task_id") if isinstance(entry, dict) else None
            if not isinstance(task_id, str) or not task_id:
                continue
            present.add(task_id)
            known = state.tasks.get(task_id)
            if known is not None and known.ended_at is not None:
                # #801: a finished task listed again — Claude resumed it (the
                # snapshot precedes its task_started). A listing moments
                # after its end straddles the end events instead.
                if now - known.ended_at >= _TASK_REVIVE_GRACE_S:
                    _revive_task(state, known, "snapshot")
                else:
                    logger.debug(
                        "claude.task.snapshot_revive_skipped",
                        task_id=task_id,
                        status=known.status,
                        ended_ago_s=round(now - known.ended_at, 1),
                    )
            elif (
                known is not None
                and not known.is_backgrounded
                and not known.owned_by_subagent
                and known.status in _TASK_LIVE_STATUSES
            ):
                # #825/#876: the parent's own background list now names a
                # task it started in the foreground — the CLI moved it.
                _mark_backgrounded(state, known, "snapshot")
            elif known is None:
                # The snapshot lands a moment before task_started; register a
                # background placeholder so the gap can't read as "idle".
                task = ClaudeTask(
                    task_id=task_id,
                    task_type=entry.get("task_type"),
                    description=entry.get("description"),
                    is_backgrounded=True,
                )
                state.tasks[task_id] = task
                state.background_observed = True
                _stamp_task_origin(state, task)
                logger.info(
                    "claude.task.registered",
                    task_id=task_id,
                    task_type=task.task_type,
                    is_backgrounded=True,
                    owned_by_subagent=False,
                    subagent_type=None,
                    description=(task.description or "")[:80],
                    source="snapshot",
                )
        for task in list(state.tasks.values()):
            if task.is_live_background and task.task_id not in present:
                if task.revived_count and now - task.started_at < _TASK_REVIVE_GRACE_S:
                    # #801: a snapshot from before the revival; the resumed
                    # run's own task_updated (or a later snapshot) ends it.
                    logger.debug(
                        "claude.task.snapshot_end_deferred",
                        task_id=task.task_id,
                        revived_ago_s=round(now - task.started_at, 1),
                    )
                    continue
                _end_task(state, task, "ended", "snapshot")
        return
    task_id = event.task_id
    if not task_id:
        return
    if subtype == "task_started":
        task = _register_task(state, event, "task_started")
        _stamp_task_origin(state, task)
        if task.owned_by_subagent and task.tool_use_id:
            task.owner_tool_use_id = state.tool_parents.get(task.tool_use_id)
        _stamp_progress(task, "task_started")
        # #829 A.2: the owning agent entered a long foreground tool.
        _stamp_owner(state, task)
        return
    task = state.tasks.get(task_id)
    if task is None:
        # e.g. the "stopped" notification the CLI replays on --resume for a
        # previous process's task (F11) — nothing live to track.
        logger.debug(
            "claude.task.unknown", task_id=task_id, subtype=subtype, status=event.status
        )
        return
    if subtype == "task_progress":
        # #801: progress carries no status, so it never revives an ended task
        # — a straggler must not pin the session; a real resume sends
        # task_started (and a snapshot listing the id).
        if event.usage is not None:
            task.last_usage = dict(event.usage)
        if task.ended_at is None:
            # #829 D1: frames are per tool call with usage rising (P0 G1), so
            # any frame is activity. Never a straggler for an ended id.
            _stamp_progress(task, "task_progress")
        if event.last_tool_name is not None:
            task.last_tool_name = event.last_tool_name
        if event.description:
            # #777: on task_progress the description is the agent's current
            # step ("Running tests"), not the task's label.
            task.last_step = event.description
        return
    if subtype == "task_updated":
        backgrounded = (event.patch or {}).get("is_backgrounded")
        if backgrounded is True:
            # #825/#876: the CLI's own foreground→background transition.
            _mark_backgrounded(state, task, "task_updated")
        elif backgrounded is False and task.is_backgrounded:
            # The CLI's truth; not seen in practice.
            task.is_backgrounded = False
            logger.debug(
                "claude.task.foregrounded", task_id=task.task_id, source="task_updated"
            )
        status = (event.patch or {}).get("status")
        if isinstance(status, str) and status not in _TASK_LIVE_STATUSES:
            _end_task(state, task, status, "task_updated")
        elif isinstance(status, str) and task.ended_at is not None:
            # #801: the CLI's own status patch says it runs again.
            _revive_task(state, task, "task_updated", status)
        return
    if subtype == "task_notification":
        if event.usage is not None:
            task.last_usage = dict(event.usage)
        status = event.status or "completed"
        if status not in _TASK_LIVE_STATUSES:
            _end_task(state, task, status, "task_notification")


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

    def _fenced(lines: list[str], info: str = "diff") -> str:
        # The approval text is rendered as Markdown: bare ``+ x`` lines
        # became ``- x`` list items (an added line shown as removed) and the
        # lines ran together. A fenced block keeps them verbatim; the fence
        # outruns any backtick run in the content so it can't close early.
        body = "\n".join(lines)
        fence = backtick_fence(body, minimum=3)
        return f"{fence}{info}\n{body}\n{fence}"

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
        return _fenced(lines)

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
        return _fenced(lines)

    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if not command:
            return ""
        shown = _truncate(command.strip("\n"), 200)
        # #871: the command the user approves must survive Markdown: a
        # single line becomes a backtick-safe code span; a multi-line
        # command (heredoc, script) keeps its lines in a fenced block.
        if len(shown.splitlines()) > 1:
            return f"$\n{_fenced(shown.splitlines(), info='sh')}"
        return f"$ {inline_code(shown)}"

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


def _stream_idle_timeout_class(
    event: claude_schema.StreamResultMessage,
) -> str | None:
    """#438/#572: return ``"type_a"`` / ``"type_b"`` for a Stream idle
    timeout failure, or None when the result is not a stall. Shared by the
    visible annotation below and the bridge's bounded auto-retry gate."""
    result = event.result if isinstance(event.result, str) else ""
    if _STREAM_IDLE_TIMEOUT_PATTERN not in result:
        return None
    if event.num_turns <= 1 and (
        event.duration_api_ms is None or event.duration_api_ms == 0
    ):
        return "type_b"
    return "type_a"


def _classify_stream_idle_timeout(
    event: claude_schema.StreamResultMessage,
) -> str | None:
    """Return a short Type-A / Type-B annotation, or None if not a stall."""
    stall_class = _stream_idle_timeout_class(event)
    if stall_class == "type_b":
        # Type B — cold-start zero-byte stall. No bytes from API.
        return (
            "🌐 Cold-start API stall (Type B): Anthropic API returned no "
            "bytes within the watchdog window. Likely upstream API "
            "queueing/availability — raising CLAUDE_STREAM_IDLE_TIMEOUT_MS "
            "will NOT help. Retry shortly."
        )
    if stall_class == "type_a":
        # Type A — mid-generation stall. Model emitted output then went silent.
        return (
            "⏳ Mid-generation API stall (Type A): SSE stream went silent after "
            "partial output. Often legitimate long reasoning that exceeded the "
            "watchdog — consider raising [watchdog] claude_stream_idle_timeout_ms "
            "in untether.toml."
        )
    return None


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


def _exitplanmode_plan_input(raw_input: Any) -> str | None:
    plan = raw_input.get("plan") if isinstance(raw_input, dict) else None
    return plan if isinstance(plan, str) and plan.strip() else None


_PLAN_FILE_MAX_BYTES = 256 * 1024
_PLAN_FILE_TOOLS = frozenset({"Write", "Edit", "MultiEdit"})


def _is_plan_file_path(path: Path) -> bool:
    """A Claude Code plan file: ``<config dir>/plans/<name>.md`` where the
    config dir is ``~/.claude`` (any home) or ``$CLAUDE_CONFIG_DIR``."""
    if not path.is_absolute() or path.suffix != ".md":
        return False
    plans = path.parent
    if plans.name != "plans":
        return False
    if plans.parent.name == ".claude":
        return True
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    return bool(config_dir) and plans.parent == Path(config_dir).expanduser()


def _observe_plan_file_write(
    state: ClaudeStreamState, tool_name: str, raw_input: Any
) -> None:
    """#793: remember the session's plan file, and its full content when the
    CLI writes it whole — that content is the plan the next ExitPlanMode is
    about, whatever its lagging ``input.plan`` says."""
    if tool_name not in _PLAN_FILE_TOOLS or not isinstance(raw_input, dict):
        return
    file_path = raw_input.get("file_path")
    if not isinstance(file_path, str) or not _is_plan_file_path(Path(file_path)):
        return
    state.plan_file_path = file_path
    content = raw_input.get("content") if tool_name == "Write" else None
    # An Edit/MultiEdit changes part of the file: read it from disk later.
    state.plan_file_content = content if isinstance(content, str) else None


def _read_plan_file(path_str: str) -> str | None:
    """Read the plan file, bounded and only if it still resolves to a plan
    file (no symlink escape). ``None`` on any problem."""
    try:
        path = Path(path_str).resolve(strict=True)
        if not _is_plan_file_path(path) or not path.is_file():
            return None
        if path.stat().st_size > _PLAN_FILE_MAX_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def _resolve_exitplanmode_plan(
    state: ClaudeStreamState,
    request_id: str,
    input_body: str,
    *,
    session_id: str | None,
    decision: str,
) -> tuple[str | None, str]:
    """#793: the plan body a decision on ``request_id`` is about, and where
    it came from (``write`` / ``file`` / ``input``).

    The plan file wins over ExitPlanMode's ``input.plan``: when the CLI
    issues the plan-file Write and ExitPlanMode in one message, the input is
    read before the Write lands and carries the PREVIOUS plan (CLI 2.1.284,
    dev-bot transcript). A disagreement is logged as the stale-input quirk.
    """
    file_body: str | None = None
    source = "input"
    if state.plan_file_content is not None and state.plan_file_content.strip():
        file_body, source = state.plan_file_content, "write"
    elif state.plan_file_path is not None:
        disk = _read_plan_file(state.plan_file_path)
        if disk is not None and disk.strip():
            file_body, source = disk, "file"
    if file_body is None:
        return (input_body or None), "input"
    if input_body and input_body.strip() != file_body.strip():
        logger.info(
            "claude.plan.stale_input",
            request_id=request_id,
            session_id=session_id,
            decision=decision,
            plan_source=source,
            input_chars=len(input_body),
            file_chars=len(file_body),
        )
    return file_body, source


def _approve_exitplanmode_plan(
    state: ClaudeStreamState,
    request_id: str,
    *,
    session_id: str | None,
    source: str,
) -> None:
    """#793: the user approved ExitPlanMode request ``request_id`` (Telegram
    Approve, the ``plan-auto`` stamp, or the post-outline auto-approve) —
    its plan body becomes the one the final answer may re-show."""
    input_body = state.exitplanmode_plans.pop(request_id, None)
    if input_body is None:
        return
    body, plan_source = _resolve_exitplanmode_plan(
        state, request_id, input_body, session_id=session_id, decision="approved"
    )
    if body is None:
        return
    if plan_source == "input" and body in state.rejected_exitplanmode_plans:
        # No plan file to check against, and the input repeats a plan the
        # user denied — the stale-input quirk. Labelling it "approved" would
        # be wrong, and an earlier approved body isn't what the agent now
        # executes, so show none.
        state.last_exitplanmode_plan = None
        logger.info(
            "claude.plan.stale_input",
            request_id=request_id,
            session_id=session_id,
            decision="approved",
            plan_source="input",
            reason="matches_rejected",
            input_chars=len(body),
        )
        return
    state.last_exitplanmode_plan = body
    logger.debug(
        "claude.plan.approved",
        request_id=request_id,
        session_id=session_id,
        source=source,
        plan_source=plan_source,
        plan_chars=len(body),
    )


def _drop_exitplanmode_plan(
    state: ClaudeStreamState,
    request_id: str,
    *,
    rejected: bool,
    reason: str,
    session_id: str | None = None,
) -> None:
    """#793: ExitPlanMode request ``request_id`` was not approved — its body
    is never re-shown. ``rejected`` is the user's explicit ❌ Deny; procedural
    denials (Pause & Outline, Let's discuss, outline guard, timeout) are not
    a verdict on the plan, so a later identical approval still counts."""
    input_body = state.exitplanmode_plans.pop(request_id, None)
    if input_body is None:
        return
    plan_source = None
    if rejected:
        body, plan_source = _resolve_exitplanmode_plan(
            state, request_id, input_body, session_id=session_id, decision="denied"
        )
        if body is not None:
            state.rejected_exitplanmode_plans.add(body)
    logger.debug(
        "claude.plan.not_approved",
        request_id=request_id,
        rejected=rejected,
        reason=reason,
        plan_source=plan_source,
    )


def _complete_control_action(
    state: ClaudeStreamState, request_id: str, *, title: str
) -> ActionEvent | None:
    """Complete the action that carries *request_id*'s keyboard (#684).

    The presenter's newest-incomplete rule then drops the keyboard on the
    next render — the only way buttons go (runner ``translate`` is sync and
    never calls Telegram).
    """
    action_id = state.request_to_action.pop(request_id, None)
    if not action_id:
        return None
    state.control_action_for_tool = {
        k: v for k, v in state.control_action_for_tool.items() if v != action_id
    }
    return state.factory.action_completed(
        action_id=action_id, kind="warning", title=title, ok=True
    )


def _retire_superseded_discuss_approval(
    state: ClaudeStreamState, *, session_id: str
) -> list[UntetherEvent]:
    """#684 D5: retire the outline-guard ``da:<session>`` escalation button
    once the same session's real ExitPlanMode is held open with its own
    buttons. Left alone it stays in ``_REQUEST_TO_SESSION`` for the process's
    lifetime, pausing the live-session timers until the 4 h cap. A late tap
    on it reads "This request has expired — replaced by the outlined plan".
    """
    stale = f"da:{session_id}"
    if _REQUEST_TO_SESSION.get(stale) != session_id:
        return []
    del _REQUEST_TO_SESSION[stale]
    state.control_registered_at.pop(stale, None)
    mark_request_handled(stale, action="superseded", outcome="expired")
    logger.info("control_request.da_superseded", session_id=session_id)
    completed = _complete_control_action(
        state, stale, title="Superseded by the outlined plan"
    )
    return [completed] if completed is not None else []


def _handle_control_cancel(
    request_id: str | None,
    *,
    state: ClaudeStreamState,
    factory: EventFactory,
) -> list[UntetherEvent]:
    """#684: the CLI withdrew a pending request (``control_cancel_request``).

    The CLI stops waiting at once and ignores any answer that still arrives,
    and there is no reply to the cancel itself — so nothing is written. The
    request is retired locally: registries cleared, keyboard stripped (its
    action completed), a later tap answers "No longer needed", and a pending
    question stops capturing the chat's next message. The frame carries no
    ``session_id``: only this run's own state scopes it (#510).
    """
    if not request_id:
        logger.debug("control_request.cancel_without_id")
        return []
    rid = request_id
    sid = factory.resume.value if factory.resume else None
    # A queued auto-answer would be ignored by the CLI; skipping it avoids a
    # misleading ``control_response.sent``.
    state.auto_approve_queue[:] = [r for r in state.auto_approve_queue if r != rid]
    state.auto_deny_queue[:] = [e for e in state.auto_deny_queue if e[0] != rid]

    known = (
        rid in state.pending_control_requests
        or rid in state.request_to_action
        or (sid is not None and _REQUEST_TO_SESSION.get(rid) == sid)
    )
    inflight = rid in _INFLIGHT_CONTROL_RESPONSES
    if not known:
        logger.debug(
            "control_request.cancel_after_answer"
            if rid in _HANDLED_REQUESTS
            else "control_request.cancel_unknown",
            request_id=rid,
            session_id=sid,
        )
        return []

    registered_at = state.control_registered_at.pop(rid, None)
    age_s = (
        round(time.monotonic() - registered_at, 1)
        if registered_at is not None
        else None
    )
    tool_name = _REQUEST_TO_TOOL_NAME.get(rid)
    action_for_rid = state.request_to_action.get(rid, "")
    is_ask = rid in _PENDING_ASK_REQUESTS or rid in _ASK_QUESTION_FLOWS
    kind = (
        "ask"
        if is_ask
        else "outline_hold"
        if action_for_rid.startswith("claude.discuss_approve.")
        else "tool"
    )

    # State-side retirement (always ours, whoever owns the registries).
    state.pending_control_requests.pop(rid, None)
    _PENDING_ASK_REQUESTS.pop(rid, None)
    flow = _ASK_QUESTION_FLOWS.pop(rid, None)
    _drop_exitplanmode_plan(
        state, rid, rejected=False, reason="cancelled", session_id=sid
    )

    still_registered = sid is not None and _REQUEST_TO_SESSION.get(rid) == sid
    if rid in _HANDLED_REQUESTS and not inflight and not still_registered:
        # Our answer was written before the abort reached us (an answer pops
        # the registration): the record stays what it is — a terminal record
        # is never downgraded, and an answer isn't rewritten as cancelled.
        # Just finish the keyboard.
        completed = _complete_control_action(state, rid, title="Permission resolved")
        logger.debug(
            "control_request.cancel_after_answer",
            request_id=rid,
            session_id=sid,
        )
        return [completed] if completed is not None else []

    if flow is not None:
        _record_answered_ask_flow(rid, flow.channel_id, outcome="cancelled")

    if inflight:
        # A tap is writing its answer right now: ``write_control_response``
        # still reads ``_REQUEST_TO_*``, so leave them to the writer and let
        # it record the ``cancelled`` outcome (#685 respond path).
        _CANCELLED_DURING_WRITE.add(rid)
    else:
        if still_registered:
            del _REQUEST_TO_SESSION[rid]
        _REQUEST_TO_INPUT.pop(rid, None)
        _REQUEST_TO_TOOL_NAME.pop(rid, None)
        mark_request_handled(
            rid,
            action="cancelled",
            outcome="cancelled",
            channel_id=get_run_channel_id(),
        )

    completed = _complete_control_action(
        state,
        rid,
        title="⏹️ Permission request withdrawn — Claude Code no longer needs an answer",
    )
    logger.info(
        "control_request.cancelled_by_cli",
        request_id=rid,
        session_id=sid,
        tool_name=tool_name,
        kind=kind,
        age_s=age_s,
        had_action=completed is not None,
        inflight=inflight,
    )
    return [completed] if completed is not None else []


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


# #595: process-lifetime dedup for needs-auth/failed catalog warnings.
# Keyed (server, status) — a connector that can never connect as configured
# warns once per service lifetime instead of once per subprocess spawn.
_CATALOG_STALENESS_WARNED: set[tuple[str, str]] = set()


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

    #595 severity split: ``pending`` at init is a startup race (the server
    is still connecting when Claude snapshots the catalog), not staleness —
    it logs at INFO (``catalog_staleness.pending``). Persistent
    ``needs-auth``/``failed`` keep the WARNING but dedup across runs via
    the process-lifetime ``_CATALOG_STALENESS_WARNED`` registry: the
    per-state set resets on every subprocess spawn, which multiplied a few
    broken connectors into ~2,930 WARNINGs/48h fleet-wide.
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
        if status == "pending":
            logger.info(
                "catalog_staleness.pending",
                session_id=session_id,
                pid=state.pid,
                server=name,
                status=status,
                source="system.init",
            )
            continue
        process_key = (name, status)
        if process_key in _CATALOG_STALENESS_WARNED:
            logger.debug(
                "catalog_staleness.suppressed",
                session_id=session_id,
                server=name,
                status=status,
            )
            continue
        _CATALOG_STALENESS_WARNED.add(process_key)
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
        "subtype",
    ):
        value = getattr(event, key, None)
        if value is not None:
            usage[key] = value
    if event.usage is not None:
        usage["usage"] = event.usage
    return usage


# #821: at most this many task ids ride on ``usage["background"]``.
_BACKGROUND_USAGE_MAX_IDS = 10


def _background_usage(
    state: ClaudeStreamState, now: float | None = None
) -> dict[str, Any] | None:
    """#821: the background agents active since the previous result — their
    spend is in this result's cost delta (``total_cost_usd`` counts subagent
    requests, with no per-agent breakdown). Active = a backgrounded
    ``local_agent`` (top-level or nested) still live, or that ended or showed
    activity after the previous result (every agent, for the run's first
    result). None when there are none."""
    if now is None:
        now = time.monotonic()
    prev = state.prev_result_at
    active = [
        task
        for task in state.tasks.values()
        if task.task_type == "local_agent"
        and task.is_backgrounded
        and (
            task.ended_at is None
            or prev is None
            or task.ended_at > prev
            or task.last_progress_at > prev
        )
    ]
    if not active:
        return None
    live = sum(1 for task in active if task.ended_at is None)
    return {
        "agents": len(active),
        "agents_live": live,
        "agents_ended": len(active) - live,
        "task_ids": [task.task_id for task in active][:_BACKGROUND_USAGE_MAX_IDS],
        "since_s": None if prev is None else round(now - prev, 1),
    }


def _capture_orphan_descendants(
    state: ClaudeStreamState, *, source: str, pid: int | None = None
) -> None:
    """#590: snapshot descendants of the live Claude process for the
    post-exit orphan sweep in ``manage_subprocess``.

    Some MCP servers ``setpgid`` into their own process group (observed:
    ``dembrandt-mcp`` on nsd — distinct PGID, still in Claude's session).
    They survive ``killpg(claude_pgid)`` and are only reachable by their
    recorded PID via ``reap_orphaned_group``'s ``extra_pids`` path. The
    reader-done capture is gated on ``proc.returncode is None`` and the
    limbo capture only fires after the limbo threshold, so a FAST CLEAN
    (rc=0, no-limbo) run captured nothing and leaked one child every run.

    ``result`` is the reliable capture point: the CLI has just emitted the
    result event and lingers alive for MCP teardown, and every MCP child has
    already spawned. Walk recursively (``find_descendants``, depth 4) so
    ``claude → npx → node`` wrapper grandchildren are caught. Best-effort:
    no-ops on missing PID / non-Linux / /proc read errors.

    NOTE: recorded PIDs are signalled by ``reap_orphaned_group`` at teardown
    after an ``_pid_alive`` check only (no birth-identity guard). The
    capture→teardown window is normally seconds, but a limbo run can widen it;
    hardening the reaper with a /proc starttime identity token is tracked
    separately.
    """
    target = pid if pid is not None else state.pid
    if not target or target <= 0:
        return
    try:
        from ..utils.proc_diag import find_descendants, pid_starttime

        new = [
            p for p in find_descendants(target) if p not in state.orphan_pid_snapshot
        ]
    except OSError:
        return
    if new:
        state.orphan_pid_snapshot.extend(new)
        # Record each PID's birth-identity so the sweep can reject a recycled
        # PID before signalling it (#590 hardening).
        for p in new:
            st = pid_starttime(p)
            if st is not None:
                state.orphan_pid_starttimes[p] = st
        logger.debug(
            "subprocess.orphan_snapshot",
            source=source,
            pid=target,
            added=new,
            total=len(state.orphan_pid_snapshot),
        )


def _should_absorb_resume_result(
    event: claude_schema.StreamResultMessage, state: ClaudeStreamState
) -> bool:
    """#776 resume guard (F11): on ``--resume`` of a session whose previous
    process ended with background work still live, the CLI first replays a
    ``task_notification{status: stopped}`` and answers it with a synthetic
    0-turn result (~0.3 s, ``duration_api_ms == 0``) — only then does it run
    the real turn. That first result is not the answer. At most once per run:
    a second 0-turn result is a genuine empty result."""
    return (
        state.resumed
        and state.completed_turns == 0
        and state.absorbed_results == 0
        and state.stopped_notification_pre_output
        and not state.saw_assistant_output
        and event.num_turns == 0
        and event.duration_api_ms == 0
        and not event.is_error
        # #819: a compaction's 0-turn result is never the stopped-task replay.
        and not state.turn_compactions
    )


_WAKEUP_IN_RE = re.compile(r"\(in (\d+)\s*s\)")
_WAKEUP_FIRE_GRACE_S = 60.0


def _note_pending_wakeup(
    state: ClaudeStreamState, tool_use_id: str, raw_result: Any
) -> None:
    """Record when a ScheduleWakeup will fire (#776, F9).

    The confirmation reads e.g. "Next wakeup scheduled for 19:15:00 (in
    94s)" — the CLI rounds to its own boundary, so the announced delay beats
    the requested ``delaySeconds``. Falls back to the handle's deadline."""
    now = time.monotonic()
    match = _WAKEUP_IN_RE.search(_normalize_tool_result(raw_result) or "")
    if match:
        fire_at = now + float(match.group(1))
    else:
        deadline = state.live_wakeups.get(tool_use_id, 0.0)
        fire_at = deadline if deadline > now else now + 60.0
    until = fire_at + _WAKEUP_FIRE_GRACE_S
    if state.pending_wakeup_until is None or until > state.pending_wakeup_until:
        state.pending_wakeup_until = until
        state.pending_wakeup_delay_s = max(0.0, fire_at - now)


def _has_pending_wakeup(state: ClaudeStreamState) -> bool:
    return (
        state.pending_wakeup_until is not None
        and state.pending_wakeup_until > time.monotonic()
    )


def _completed_keeps_session_live(evt: CompletedEvent) -> bool:
    """A live session only survives a successful, non-empty first result —
    or a successful manual ``/compact`` (#819: its result is 0-turn and
    empty by design)."""
    if not evt.ok:
        return False
    usage = evt.usage or {}
    compaction = usage.get("compaction")
    if isinstance(compaction, dict) and compaction.get("manual_success") is True:
        return True
    return not (
        not (evt.answer or "").strip()
        and (usage.get("num_turns", 1) or 0) == 0
        and (usage.get("duration_api_ms", 1) or 0) == 0
    )


def _result_origin_kind(event: claude_schema.StreamResultMessage) -> str | None:
    """The result's ``origin.kind`` (``task-notification`` for a CLI-started
    turn, #812 §A3), read defensively — ``origin`` is typed Any."""
    origin = event.origin
    if isinstance(origin, dict):
        return _str_or_none(origin.get("kind"))
    return None


def _turn_plan_deferred(
    state: ClaudeStreamState, reason: str, command_uuid: str | None
) -> int:
    """#383 C4: agents holding back the re-arm for the turn about to open —
    a follow-up / idle steer written while the re-arm was deferred, or (plan
    chats, whose wake turns are re-armed) a wake turn opened during it."""
    if reason == "followup":
        if command_uuid is None:
            return 0
        return state.unplanned_commands.pop(command_uuid, 0)
    if not state.plan_rearm_deferred or not _plan_rearm_needed(state, reason="idle"):
        return 0
    agents, _ = _rearm_deferral(state)
    return agents


def _open_followup_turn(
    state: ClaudeStreamState, factory: EventFactory
) -> UntetherEvent:
    """Open turn N+1 after a result, attributing it from the idle-time hints."""
    reason = "unknown"
    command_uuid = state.pending_command_uuid
    detail: dict[str, Any] = {}
    if command_uuid is not None and command_uuid in state.injected_commands:
        reason = "followup"
    elif state.turn_notifications:
        reason = "task_finished"
        detail["tasks"] = list(state.turn_notifications)
        ids = list(state.turn_notification_ids)
        detail.update(_task_attribution(state, ids))
        if ids and all(tid in state.announced_task_ids for tid in ids):
            # #785: the second wake turn for one finish (the first opened as
            # ``unknown`` and was attributed to it) — the bridge won't push.
            detail["already_announced"] = True
        # #813: the wake turn(s) these finishes were paired with.
        if announced_turns := sorted(
            {
                state.task_announced_turn[t]
                for t in ids
                if t in state.task_announced_turn
            }
        ):
            detail["announced_turns"] = announced_turns
        _mark_announced(state, ids)
    elif (hint := state.hook_rewake_hint) is not None and time.monotonic() - hint[
        2
    ] <= _HOOK_REWAKE_HINT_TTL_S:
        # #812: an asyncRewake hook exited 2 while idle; the CLI enqueued
        # its findings as a task-notification turn (§A3) — no
        # command_lifecycle, so this sits before scheduled_wakeup.
        reason = "hook_rewake"
        detail["hook"] = hint[0]
        detail["hook_event"] = hint[1]
        logger.info(
            "claude.turn.hook_rewake",
            session_id=factory.resume.value if factory.resume else None,
            turn=state.turn + 1,
            hook_name=hint[0],
            hook_event=hint[1],
            attributed="open",
        )
    elif command_uuid is not None:
        reason = "scheduled_wakeup"
    elif monitors := [
        task
        for task in _live_native_tasks(state)
        if task.is_live_background and _is_native_monitor(state, task)
    ]:
        reason = "monitor_event"
        detail["tasks"] = [t.description or "Monitor" for t in monitors]
        detail.update(_task_attribution(state, [t.task_id for t in monitors]))
    if reason == "scheduled_wakeup":
        state.pending_wakeup_until = None
        state.pending_wakeup_delay_s = None
    if reason == "followup" and command_uuid is not None:
        state.awaiting_injected.pop(command_uuid, None)
    # #383 C4: this turn runs unplanned because the approved plan's agents
    # are still working — say so (the bridge adds a line to its header).
    if agents := _turn_plan_deferred(state, reason, command_uuid):
        detail["plan_deferred"] = {"agents": agents}
    state.plan_rearm_deferred_logged = False
    state.turn += 1
    state.turn_open = True
    state.turn_reason = reason
    state.turn_command_uuid = command_uuid if reason == "followup" else None
    state.pending_command_uuid = None
    started_ago_s = (
        max(0.0, time.monotonic() - state.pending_turn_since)
        if state.pending_turn_since is not None
        else None
    )
    state.pending_turn_since = None
    state.turn_notifications = []
    state.turn_notification_ids = []
    state.turn_ended_tasks = []
    state.turn_model_requests = 0
    state.turn_last_message_id = None
    state.turn_request_sent = False
    state.turn_ended_at_request = {}
    state.turn_detail = detail
    state.unattributed_turn_completed_at = None
    # #812: a stale hint (a slow turn start) is kept for the result's
    # ``origin`` check — #828: only within ``_HOOK_REWAKE_CARRY_TTL_S``;
    # either way the idle-time hint is spent.
    carried = None if reason == "hook_rewake" else state.hook_rewake_hint
    if carried is not None:
        age_s = time.monotonic() - carried[2]
        if age_s > _HOOK_REWAKE_CARRY_TTL_S:
            logger.debug(
                "claude.hook.rewake_hint_expired",
                session_id=factory.resume.value if factory.resume else None,
                hook_name=carried[0],
                hook_event=carried[1],
                age_s=round(age_s, 1),
            )
            carried = None
    state.turn_hook_hint = carried
    state.hook_rewake_hint = None
    # Per-turn scalars (see their field docs) start fresh for the new turn.
    state.safeguard = SafeguardTurn()
    state.unattended_denials = []  # #835
    state.last_assistant_text = None
    state.last_exitplanmode_plan = None
    state.last_schedule_wakeup_arm_delay = None
    state.last_bg_bash_launched_at = None
    # #819: compaction rows / record / summary flag are per turn.
    _reset_compaction_segment(state)
    # Not idle any more: the stall / post-result logic keys off this.
    state.result_received_at = None
    # #383: the reply a plan approval was given in has ended.
    _scope_plan_approvals_to_turn(
        factory.resume.value if factory.resume else None,
        turn=state.turn,
        reason=reason,
    )
    logger.info(
        "claude.turn.started",
        session_id=factory.resume.value if factory.resume else None,
        turn=state.turn,
        reason=reason,
        command_uuid=state.turn_command_uuid,
    )
    return factory.turn_started(
        turn=state.turn,
        reason=reason,  # type: ignore[arg-type]
        command_uuid=state.turn_command_uuid,
        detail=detail,
        started_ago_s=started_ago_s,
    )


def _open_turn_events(
    state: ClaudeStreamState, factory: EventFactory
) -> list[UntetherEvent]:
    """Open turn N+1 and hand the new turn's tracker the session's current
    ``% ctx`` (#819) — the bridge's turn tracker starts empty."""
    return [
        _open_followup_turn(state, factory),
        *_emit_context(state, factory, force=True),
    ]


_STEER_SNIPPET_CHARS = 80


def _absorb_injected(
    state: ClaudeStreamState, factory: EventFactory, command_uuid: str
) -> list[UntetherEvent]:
    """#775: an injected line the CLI picked up while a turn was already open
    — it was folded into that turn (F5) instead of starting its own.

    Clears the awaiting marker (so the idle close and the next queued
    follow-up aren't held for a turn that will never open) and surfaces a
    "steer received" row in the running turn's progress. The bridge pops the
    line's reply anchor on seeing ``detail["absorbed_command_uuid"]`` — its
    answer is this turn's answer, so no separate reply is owed.
    """
    state.absorbed_commands.add(command_uuid)
    state.awaiting_injected.pop(command_uuid, None)
    state.unplanned_commands.pop(command_uuid, None)  # #383 C4: no turn of its own
    steer_text = state.steered_commands.get(command_uuid)
    label = "steer" if steer_text is not None else "follow-up"
    title = f"\N{RIGHTWARDS ARROW WITH HOOK}\N{VARIATION SELECTOR-16} {label} received"
    if steer_text:
        snippet = " ".join(steer_text.split())
        if len(snippet) > _STEER_SNIPPET_CHARS:
            snippet = snippet[: _STEER_SNIPPET_CHARS - 1] + "…"
        title = f"{title}: {snippet}"
    logger.info(
        "claude.live_session.injected_absorbed",
        session_id=factory.resume.value if factory.resume else None,
        command_uuid=command_uuid,
        steer=steer_text is not None,
        turn=state.turn,
    )
    state.note_seq += 1
    action_id = f"claude.steer.{state.note_seq}"
    detail = {"absorbed_command_uuid": command_uuid, "steer": steer_text is not None}
    return [
        factory.action_started(
            action_id=action_id, kind="note", title=title, detail=detail
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


def translate_claude_event(
    event: claude_schema.StreamJsonMessage,
    *,
    title: str,
    state: ClaudeStreamState,
    factory: EventFactory,
) -> list[UntetherEvent]:
    """Translate one CLI line, adding #776 turn segmentation around
    :func:`_translate_claude_event_base`."""
    if (
        isinstance(event, claude_schema.StreamCommandLifecycleMessage)
        and event.state == "started"
        and state.turn_open
        and event.command_uuid is not None
        and event.command_uuid in state.injected_commands
        and event.command_uuid not in state.absorbed_commands
    ):
        # #775: started while a turn is open → folded into it (probed on CLI
        # 2.1.284: queued at write, started after the running tool's result).
        return _absorb_injected(state, factory, event.command_uuid)
    if (
        isinstance(event, claude_schema.StreamSystemMessage)
        and event.subtype == "task_notification"
        and event.status == "stopped"
        and state.completed_turns == 0
        and not state.saw_assistant_output
    ):
        state.stopped_notification_pre_output = True
    if isinstance(
        event, claude_schema.StreamResultMessage
    ) and _should_absorb_resume_result(event, state):
        state.absorbed_results += 1
        state.absorbed_cost_baseline = event.total_cost_usd
        logger.info(
            "claude.resume_guard.absorbed",
            session_id=event.session_id,
            total_cost_usd=event.total_cost_usd,
        )
        return []

    if not state.live_mode or state.completed_turns == 0:
        if isinstance(event, claude_schema.StreamAssistantMessage):
            state.saw_assistant_output = True
        events = _translate_claude_event_base(
            event, title=title, state=state, factory=factory
        )
        if any(isinstance(evt, CompletedEvent) for evt in events):
            state.completed_turns = 1
            state.turn_open = False
            if state.live_mode:
                # #383: whatever the outcome — a failed turn can have left
                # plan mode too.
                state.plan_rearm_pending = _plan_rearm_needed(state, reason="idle")
                state.plan_rearm_pending_reason = "idle"
            if state.absorbed_cost_baseline is not None:
                # #778: the absorbed result carried the previous process's
                # session total — hand it to the cost ledger as a baseline.
                events = [
                    dataclasses.replace(
                        evt,
                        usage={
                            **(evt.usage or {}),
                            "session_cost_baseline": state.absorbed_cost_baseline,
                        },
                    )
                    if isinstance(evt, CompletedEvent)
                    else evt
                    for evt in events
                ]
        return events

    # ── live session, after the run's own result (#776) ──────────────────
    match event:
        case claude_schema.StreamCommandLifecycleMessage(state=cmd_state):
            if cmd_state == "started" and not state.turn_open:
                state.pending_command_uuid = event.command_uuid
                if state.pending_turn_since is None:
                    state.pending_turn_since = time.monotonic()
            return []
        case claude_schema.StreamSystemMessage(subtype=subtype):
            if subtype == "task_notification" and not state.turn_open:
                task = state.tasks.get(event.task_id or "")
                if (
                    task is not None
                    and not task.is_backgrounded
                    and not task.owned_by_subagent
                ):
                    # #825/#876 fallback: the parent's own foreground tool
                    # can't outlive its turn unless the CLI backgrounded it.
                    # (A subagent's foreground task stays ignored — the #785
                    # nsd wrong-name guard.)
                    _mark_backgrounded(state, task, "idle_notification")
                if _notification_labels_turn(event, task):
                    label = event.summary or event.description
                    if task is not None and task.description:
                        # Prefer the registered top-level description (#785).
                        label = task.description
                    state.turn_notifications.append(label or "background task")
                    if task is not None:
                        state.turn_notification_ids.append(task.task_id)
                else:
                    logger.info(
                        "claude.turn.notification_ignored",
                        task_id=event.task_id,
                        owned_by_subagent=(
                            task.owned_by_subagent
                            if task is not None
                            else event.owned_by_subagent
                        ),
                        is_backgrounded=(
                            task.is_backgrounded
                            if task is not None
                            else event.is_backgrounded
                        ),
                    )
            out: list[UntetherEvent] = []
            if not state.turn_open and (
                subtype == "init"
                # #819: a ``/compact`` follow-up's first frame is its
                # compacting status — its rows belong to its own turn, not
                # to the run's finalised progress message. The mid-command
                # init that follows then sees the turn already open.
                or (subtype == "status" and event.status == "compacting")
            ):
                out.extend(_open_turn_events(state, factory))
            # Keep the base side effects (task map, MCP catalog capture) but
            # never re-emit a StartedEvent inside a live session.
            out.extend(
                evt
                for evt in _translate_claude_event_base(
                    event, title=title, state=state, factory=factory
                )
                if not isinstance(evt, (StartedEvent, CompletedEvent))
            )
            return out
        case claude_schema.StreamResultMessage():
            out = []
            if not state.turn_open:
                out.extend(_open_turn_events(state, factory))
            base = _translate_claude_event_base(
                event, title=title, state=state, factory=factory
            )
            completed = next(
                (evt for evt in base if isinstance(evt, CompletedEvent)), None
            )
            # #819: result-time ActionEvents (the ``% ctx`` telemetry) belong
            # to this turn — forward them before it closes.
            out.extend(evt for evt in base if isinstance(evt, ActionEvent))
            state.turn_open = False
            state.completed_turns += 1
            # #383: re-arm plan mode at every live turn close.
            state.plan_rearm_pending = _plan_rearm_needed(state, reason="idle")
            state.plan_rearm_pending_reason = "idle"
            detail = dict(state.turn_detail)
            if state.turn_reason == "unknown" and state.turn_ended_tasks:
                # #785: the turn opened before any task event named what it
                # answered; the task(s) ended during it — attribute it now so
                # its final gets the real header.
                detail["tasks"] = [label for _, label in state.turn_ended_tasks]
                detail["retro_attributed"] = True
                detail.update(
                    _task_attribution(state, [tid for tid, _ in state.turn_ended_tasks])
                )
                _mark_announced(state, (tid for tid, _ in state.turn_ended_tasks))
                state.turn_reason = "task_finished"
                logger.info(
                    "claude.turn.retro_attributed",
                    session_id=event.session_id,
                    turn=state.turn,
                    task_ids=[tid for tid, _ in state.turn_ended_tasks],
                )
            elif state.turn_reason == "task_finished" and (
                late := _seen_late_tasks(state, event, detail)
            ):
                # #825: another task finished while this wake turn ran — the
                # CLI folded its notification into the turn, so name it too.
                detail["tasks"] = [
                    *detail.get("tasks", []),
                    *(label for _, label in late),
                ]
                detail["task_ids"] = [
                    *detail.get("task_ids", []),
                    *(tid for tid, _ in late),
                ]
                detail["late_tasks"] = [label for _, label in late]
                detail.pop("already_announced", None)  # a new finish is news
                _mark_announced(state, (tid for tid, _ in late))
                logger.info(
                    "claude.turn.late_tasks_attributed",
                    session_id=event.session_id,
                    turn=state.turn,
                    task_ids=[tid for tid, _ in late],
                )
            elif (
                state.turn_reason == "unknown"
                and (hook_hint := state.turn_hook_hint) is not None
                and _result_origin_kind(event) == "task-notification"
            ):
                # #812: the turn opened without a fresh hint (slow start, or
                # the hook's exit-2 response landed after the turn opened);
                # the CLI's task-notification origin confirms it was the
                # rewake.
                state.turn_reason = "hook_rewake"
                detail["hook"] = hook_hint[0]
                detail["hook_event"] = hook_hint[1]
                detail["retro_attributed"] = True
                logger.info(
                    "claude.turn.hook_rewake",
                    session_id=event.session_id,
                    turn=state.turn,
                    hook_name=hook_hint[0],
                    hook_event=hook_hint[1],
                    attributed="result",
                )
            elif (
                state.turn_reason == "unknown"
                and event.num_turns > 0
                and (completed is not None and completed.answer.strip())
                and (carried := _take_pending_late_tasks(state))
            ):
                # #825 R17-821: the CLI's wake turn for a task that ended too
                # late for the previous turn to see — it opened before any
                # task event named it (the end was already consumed).
                ids = [tid for tid, _ in carried]
                detail["tasks"] = [label for _, label in carried]
                detail["retro_attributed"] = True
                detail.update(_task_attribution(state, ids))
                _mark_announced(state, ids)
                state.turn_reason = "task_finished"
                logger.info(
                    "claude.turn.late_tasks_carried",
                    session_id=event.session_id,
                    turn=state.turn,
                    task_ids=ids,
                )
            state.turn_hook_hint = None
            state.turn_ended_tasks = []
            state.unattributed_turn_completed_at = (
                time.monotonic() if state.turn_reason == "unknown" else None
            )
            if completed is not None:
                logger.info(
                    "claude.turn.completed",
                    session_id=event.session_id,
                    turn=state.turn,
                    reason=state.turn_reason,
                    ok=completed.ok,
                    num_turns=event.num_turns,
                )
                out.append(
                    factory.turn_completed(
                        turn=state.turn,
                        ok=completed.ok,
                        answer=completed.answer,
                        reason=state.turn_reason,  # type: ignore[arg-type]
                        error=completed.error,
                        usage=completed.usage,
                        command_uuid=state.turn_command_uuid,
                        detail=detail,
                    )
                )
            return out
        case claude_schema.StreamAssistantMessage() | claude_schema.StreamUserMessage():
            out = []
            if not state.turn_open and event.parent_tool_use_id is not None:
                # A background subagent streams its own assistant/tool events
                # on stdout while the parent is idle (F2). They are not the
                # parent starting a turn — keep their side effects (pending
                # actions, task bookkeeping) but surface nothing; the wake
                # turn comes after its task_notification. #777 renders
                # background progress.
                _translate_claude_event_base(
                    event, title=title, state=state, factory=factory
                )
                return []
            if not state.turn_open and not _is_tool_result_only(event):
                out.extend(_open_turn_events(state, factory))
            if (
                state.turn_open
                and isinstance(event, claude_schema.StreamAssistantMessage)
                and event.parent_tool_use_id is None
                and (message_id := event.message.id)
                and message_id != state.turn_last_message_id
            ):
                # #825: one more model request in this turn — unless it
                # was already counted when the CLI sent it (below).
                state.turn_last_message_id = message_id
                if state.turn_request_sent:
                    state.turn_request_sent = False
                else:
                    state.turn_model_requests += 1
            elif (
                state.turn_open
                and isinstance(event, claude_schema.StreamUserMessage)
                and event.parent_tool_use_id is None
                and not state.turn_request_sent
                and _is_tool_result_only(event)
            ):
                # #825: the CLI sends the next request with this tool result,
                # and with it any task notification queued so far.
                state.turn_request_sent = True
                state.turn_model_requests += 1
            out.extend(
                evt
                for evt in _translate_claude_event_base(
                    event, title=title, state=state, factory=factory
                )
                if not isinstance(evt, (StartedEvent, CompletedEvent))
            )
            return out
        case _:
            return [
                evt
                for evt in _translate_claude_event_base(
                    event, title=title, state=state, factory=factory
                )
                if not isinstance(evt, (StartedEvent, CompletedEvent))
            ]


def _is_tool_result_only(event: claude_schema.StreamJsonMessage) -> bool:
    if not isinstance(event, claude_schema.StreamUserMessage):
        return False
    content = event.message.content
    return isinstance(content, list) and all(
        isinstance(
            block,
            (
                claude_schema.StreamToolResultBlock,
                claude_schema.StreamAdvisorToolResultBlock,
            ),
        )
        for block in content
    )


def _translate_claude_event_base(
    event: claude_schema.StreamJsonMessage,
    *,
    title: str,
    state: ClaudeStreamState,
    factory: EventFactory,
) -> list[UntetherEvent]:
    match event:
        case claude_schema.StreamSystemMessage(subtype=subtype):
            if subtype.startswith("task_") or subtype == "background_tasks_changed":
                _apply_task_event(state, event)
                return []
            handled = _translate_system_subtype(event, state=state, factory=factory)
            if handled is not None:
                return handled
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
            # #383: every init (live turns included) reports the mode the
            # turn starts in.
            if isinstance(event.permissionMode, str):
                _note_permission_mode(
                    state, event.permissionMode, source="init", session_id=session_id
                )
            # #751: the CLI can silently run a different mode than requested
            # (`auto` on Haiku → `default`); surface it and re-arm the gate.
            mismatch_rows = _permission_mode_mismatch_rows(
                event, state=state, factory=factory
            )
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
            if isinstance(model, str) and model:
                state.session_model = model
                state.ctx_init_model = model  # #819
            token = ResumeToken(engine=ENGINE, value=session_id)
            event_title = str(model) if isinstance(model, str) and model else title
            return [
                factory.started(token, title=event_title, meta=meta or None),
                *mismatch_rows,
            ]
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
                        if parent_tool_use_id and content.id:
                            state.tool_parents[content.id] = parent_tool_use_id
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
                        # #508/#793: the ExitPlanMode plan body is recorded
                        # from its control_request (keyed by request_id) and
                        # kept only if that request is approved — not here,
                        # where the user hasn't decided yet. The parent's
                        # plan-file writes are tracked as its source of truth.
                        if parent_tool_use_id is None:
                            _observe_plan_file_write(
                                state, str(content.name or ""), content.input
                            )
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
            # #814: a refused main-thread response → the 🛡️ row.
            out.extend(_note_assistant_refusal(event, state=state, factory=factory))
            # #819: the response's usage is the header's context value.
            out.extend(_note_assistant_context(event, state=state, factory=factory))
            return out
        case claude_schema.StreamUserMessage(message=message):
            # #819: the compaction summary the CLI writes after a
            # ``compact_boundary`` (and ``/compact``'s replayed stdout) is
            # not a fresh prompt — it must not reset the #544/#333 per-turn
            # scalars mid-turn.
            compaction_frame = bool(event.isCompactSummary) or bool(event.isReplay)
            if state.compaction_summary_pending and not _is_tool_result_only(event):
                state.compaction_summary_pending = False
                compaction_frame = True
            if not isinstance(message.content, list):
                return []
            out: list[UntetherEvent] = []
            saw_tool_result = False
            saw_non_tool_result = False
            for content in message.content:
                # #489 advisor_tool_result shares the tool_result translation.
                if not isinstance(
                    content,
                    (
                        claude_schema.StreamToolResultBlock,
                        claude_schema.StreamAdvisorToolResultBlock,
                    ),
                ):
                    # #544: any non-tool_result block signals a real user
                    # prompt arrived (text, image, etc.) — reset the
                    # ScheduleWakeup arm-delay high-water-mark so a new
                    # turn that doesn't call ScheduleWakeup falls back
                    # to the default post-result idle timeout.
                    saw_non_tool_result = True
                    continue
                saw_tool_result = True
                tool_use_id = content.tool_use_id
                # #829: a background Bash's output file (hold fallback).
                _note_bg_output_file(state, tool_use_id, content.content)
                if tool_use_id in state.live_wakeups:
                    _note_pending_wakeup(state, tool_use_id, content.content)
                # #347/#374 clear a background-task entry only on a *terminal*
                # tool_result — interim Monitor results keep the handle so the
                # stall-suppression branch keeps firing while it runs.
                _clear_background_handle(
                    state,
                    tool_use_id,
                    is_terminal=_is_terminal_tool_result(content, state, tool_use_id),
                )
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
            # #544: reset the ScheduleWakeup arm-delay high-water-mark when
            # a fresh user prompt arrives (any non-tool_result content) and
            # NO tool_result is present in the same batch. Mixed batches
            # (rare in practice) keep the scalar — the tool turn is still
            # in flight. The reset must happen here (not in StreamResultMessage)
            # because the watchdog reads the scalar AFTER result_received_at
            # is set, so resetting on result would defeat the shortcut.
            if saw_non_tool_result and not saw_tool_result and not compaction_frame:
                state.last_schedule_wakeup_arm_delay = None
                # #333: same reset semantics as the #544 ScheduleWakeup
                # scalar — a fresh user prompt clears the per-turn
                # launch tracker. See last_bg_bash_launched_at docstring
                # for why this is a launch tracker, not a lifetime
                # tracker.
                state.last_bg_bash_launched_at = None
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
            if not ok:
                # #692: the subscription-cap reset time lives only in the
                # raw result-error text — harvest it for this run's stall
                # context and for subsequent runs' reset-less rejections.
                _maybe_latch_rate_limit_reset(event.result, state=state)
                # #701: the other cap class — no time to harvest, but a
                # remedy to name.
                _maybe_latch_action_required(event.result)
            usage = _usage_payload(event)
            # #814: rides on usage (not StartedEvent meta) so it reaches both
            # CompletedEvent and a live turn's TurnEvent — D-12.
            if (safeguard := _finalize_safeguard_turn(state, factory)) is not None:
                usage["safeguard"] = safeguard
            # #835: the turn's unattended denials, for the final's footer.
            if (unattended := _unattended_usage_payload(state)) is not None:
                usage["unattended"] = unattended
            if event.terminal_reason in claude_schema.CLAUDE_ABORTED_TERMINAL_REASONS:
                # #806: an interrupted turn — the bridge renders it as
                # cancelled rather than as an answer / error.
                usage["terminal_reason"] = event.terminal_reason
            # #819: learn the context window(s) this result reports, so the
            # first turn on an unseen model still gets its ``% ctx`` in the
            # final; ``usage["context"]`` is for the ``runner.completed`` log.
            _learn_context_windows(event.modelUsage)
            context_events = _emit_context(state, factory)
            if (context_usage := _context_usage_payload(state, factory)) is not None:
                usage["context"] = context_usage
            # #819: the segment's compaction record rides on usage (like
            # ``usage["safeguard"]``, so live turns get it too), then ends
            # with its result.
            if (compaction := _compaction_usage(state, event)) is not None:
                usage["compaction"] = compaction
            _reset_compaction_segment(state)
            # #821: which background agents' spend this cost delta includes.
            result_at = time.monotonic()
            if (background := _background_usage(state, result_at)) is not None:
                usage["background"] = background
            state.prev_result_at = result_at

            # #572: record the stream-idle classification so the bridge's
            # bounded auto-retry gate can read it via engine_state duck-typing.
            state.stream_idle_class = None if ok else _stream_idle_timeout_class(event)

            # #333: arm the post-result idle watchdog. Reset on every
            # result (multi-turn re-arms the timer per turn boundary).
            state.result_received_at = time.monotonic()

            # #590: capture descendant PIDs NOW — the CLI is still alive
            # (lingering for MCP teardown) and every MCP child has spawned.
            # This is the only snapshot that fires on a fast clean rc=0 run,
            # closing the leak for pgroup-escapee MCP children. Runs before
            # the CompletedEvent is yielded (yielding hands control to the
            # consumer, which may cancel/tear down the generator).
            _capture_orphan_descendants(state, source="result")

            events_out: list[UntetherEvent] = [*context_events]
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
                        meta={"complete": TURN_COMPLETE_MARKER},
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

            # #822: one INFO line per tool request reaching Untether, before
            # any branch decides it, so auto-approved / auto-denied requests
            # are attributable too (join on request_id with the outcome
            # lines). Name only — never the tool input.
            if isinstance(request, claude_schema.ControlCanUseToolRequest):
                logger.info(
                    "control_request.received",
                    request_id=request_id,
                    tool_name=getattr(request, "tool_name", None),
                    session_id=factory.resume.value if factory.resume else None,
                    permission_mode=state.effective_permission_mode,
                    unattended=state.unattended_trigger,  # #835
                )

            # #793: record every ExitPlanMode plan body against its request;
            # the approval paths below (and write_control_response) promote
            # it, every denial path drops it.
            if (
                isinstance(request, claude_schema.ControlCanUseToolRequest)
                and getattr(request, "tool_name", "") == "ExitPlanMode"
            ):
                # Recorded even without an input body: the plan file may
                # still supply one at decision time.
                state.exitplanmode_plans[request_id] = (
                    _exitplanmode_plan_input(getattr(request, "input", {})) or ""
                )

            # Auto-approve tool requests that don't need user interaction.
            # _DIFF_PREVIEW_TOOLS is module-scoped — see top of file.
            #
            # #749: in a prompting mode (`default`/`manual`/`acceptEdits`) the
            # user was promised an approval prompt, so NOTHING is auto-approved
            # here — a stage-6 request is unresolved permission work by
            # definition (decisions.md D-1).  Autonomous modes retain the
            # historical two-tool set: `DEFAULT_ALLOWED_TOOLS` only pre-approves
            # Bash/Read/Edit/Write, so Glob/Grep/WebFetch/Task already arrive
            # here in plan mode, and gating them would raise a button per tool
            # in the fleet's most-used mode for no safety gain (probes G/H/I).
            #
            # Known gap, carried to v0.35.6: an explicit `ask` rule reaches
            # stage 6 even under `bypassPermissions`, and this branch still
            # approves it.  Closing that needs a stage-5 change (the allowlist),
            # not a wider handler gate.
            _TOOLS_REQUIRING_APPROVAL = {"ExitPlanMode", "AskUserQuestion"}
            if isinstance(request, claude_schema.ControlCanUseToolRequest):
                tool_name = getattr(request, "tool_name", "unknown")
                if (
                    not state.prompting_mode
                    and tool_name not in _TOOLS_REQUIRING_APPROVAL
                ):
                    # When diff_preview is enabled, route previewable tools
                    # through interactive approval so users see the diff.
                    # Bypass after ExitPlanMode approval — the user already
                    # reviewed the plan, per-tool approval is redundant (#283).
                    run_opts = get_run_options()
                    session_id = factory.resume.value if factory.resume else None
                    plan_approved = (
                        session_id is not None and session_id in _PLAN_EXIT_APPROVED
                    )
                    diff_gate = bool(
                        run_opts
                        and run_opts.diff_preview is True
                        and tool_name in _DIFF_PREVIEW_TOOLS
                        and not plan_approved
                    )
                    # #835: an unattended run never waits for a tap and never
                    # approves what an attended run would have asked about.
                    unattended_reason = _unattended_autonomous_deny_reason(
                        state, tool_name, diff_gate=diff_gate
                    )
                    if unattended_reason is not None:
                        return _unattended_deny(
                            state,
                            factory,
                            request_id,
                            request,
                            reason=unattended_reason,
                        )
                    if diff_gate:
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
                    _stamp_plan_exit(state)  # #383
                    _approve_exitplanmode_plan(
                        state, request_id, session_id=auto_session, source="plan_auto"
                    )
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
                        _DISCUSS_CARRY.discard(session_id)
                        _OUTLINE_PENDING.discard(session_id)
                        # #283: bypass diff_preview gate for subsequent tools
                        # in this reply (#309).
                        _PLAN_EXIT_APPROVED.add(session_id)
                        _stamp_plan_exit(state)  # #383
                        logger.info(
                            "control_request.discuss_approved",
                            request_id=request_id,
                            session_id=session_id,
                        )
                        _approve_exitplanmode_plan(
                            state,
                            request_id,
                            session_id=session_id,
                            source="discuss_approved",
                        )
                        _REQUEST_TO_INPUT[request_id] = getattr(request, "input", {})
                        state.auto_approve_queue.append(request_id)
                        return []

            # #835: everything still unresolved here would wait for a Telegram
            # tap (plan approval, question, prompting-mode tool, …). In an
            # unattended run nobody can give it — deny now instead of holding
            # the process (and a concurrency slot) until someone notices.
            if state.unattended_trigger is not None:
                return _unattended_deny(
                    state, factory, request_id, request, reason="would_wait"
                )

            # Gate ExitPlanMode while an outline is pending (Pause & Outline).
            # Both paths (outline written / not written) bypass the normal
            # 3-button flow: without this, an outline-pending retry would show
            # the same "Pause & Outline Plan" button again — a confusing loop.
            # #570: the additional time-based cooldown arm that lived here was
            # a v2.1.72-74 workaround; removed after verifying the upstream
            # immediate-retry loop is fixed on CLI 2.1.215.
            if isinstance(request, claude_schema.ControlCanUseToolRequest):
                tool_name = getattr(request, "tool_name", "")
                if tool_name == "ExitPlanMode" and factory.resume:
                    session_id = factory.resume.value
                    text_len = state.max_text_len_since_cooldown

                    # #659: on plan-file CLIs (≥ ~2.1.2xx) the plan body
                    # arrives in ExitPlanMode's `plan` input and NO chat text
                    # is ever written — observed live: 4 consecutive
                    # outline_guard denies until Claude gave up. The plan
                    # input IS the outline, so let it satisfy the gate and
                    # render it as the standalone outline message.
                    _epm_gate_input = getattr(request, "input", {})
                    _epm_plan_body = (
                        _epm_gate_input.get("plan")
                        if isinstance(_epm_gate_input, dict)
                        else None
                    )
                    if isinstance(_epm_plan_body, str):
                        text_len = max(text_len, len(_epm_plan_body))
                        if (
                            state.outline_text is None
                            and len(_epm_plan_body) >= _OUTLINE_MIN_CHARS
                        ):
                            state.outline_text = _epm_plan_body

                    # Guard: outline pending but Claude hasn't written enough
                    # visible text — auto-deny with the outline instruction.
                    outline_guard = (
                        session_id in _OUTLINE_PENDING and text_len < _OUTLINE_MIN_CHARS
                    )
                    # Outline written — hold the request open and show the
                    # synthetic Approve/Deny buttons.
                    outline_ready = (
                        session_id in _OUTLINE_PENDING
                        and text_len >= _OUTLINE_MIN_CHARS
                    )

                    if outline_guard or outline_ready:
                        pre_events: list[UntetherEvent] = []
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
                            _bind_request_channel(request_id)  # #388
                            state.control_registered_at[request_id] = time.monotonic()
                            _REQUEST_TO_INPUT[request_id] = getattr(
                                request, "input", {}
                            )
                            _REQUEST_TO_TOOL_NAME[request_id] = getattr(
                                request, "tool_name", ""
                            )
                            # #684 D5: the real request now carries the
                            # buttons; an earlier outline-guard ``da:`` entry
                            # would otherwise pause the live session until
                            # the 4 h cap.
                            pre_events.extend(
                                _retire_superseded_discuss_approval(
                                    state, session_id=session_id
                                )
                            )
                        else:
                            # Retry without outline — auto-deny with the
                            # write-the-outline-first instruction.
                            logger.info(
                                "control_request.outline_guard_deny",
                                request_id=request_id,
                                session_id=session_id,
                            )
                            _drop_exitplanmode_plan(
                                state,
                                request_id,
                                rejected=False,
                                reason="outline_guard",
                            )
                            _REQUEST_TO_INPUT.pop(request_id, None)
                            _REQUEST_TO_TOOL_NAME.pop(request_id, None)
                            state.auto_deny_queue.append(
                                (request_id, _DISCUSS_ESCALATION_MESSAGE)
                            )

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
                            _bind_request_channel(button_request_id)  # #388
                            state.control_registered_at[button_request_id] = (
                                time.monotonic()
                            )
                            # #685: the id is shared by every outline round in
                            # the session. Drop round 1's handled record, or
                            # the reconcile loop completes round 2's synthetic
                            # action (strips its keyboard) at the next
                            # control_request while it is still pending.
                            _HANDLED_REQUESTS.pop(button_request_id, None)
                        # #683: map the button's request_id to this synthetic
                        # action so the reconcile loop can COMPLETE it once the
                        # user taps Approve/Deny. The early return below skips
                        # the normal mapping at ~line 2465, so without this the
                        # reconciler resolves no action_id, the action stays
                        # uncompleted for the rest of the run, and its keyboard
                        # shadowed every later approval/option keyboard.
                        state.request_to_action[button_request_id] = synth_action_id

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
                        # #383: say what approving does.
                        if caption := _plan_approve_caption(state):
                            synth_title = f"{synth_title}\n{caption}"

                        return [
                            *pre_events,
                            state.factory.action_started(
                                action_id=synth_action_id,
                                kind="warning",
                                title=synth_title,
                                detail={
                                    **outline_detail,
                                    "request_id": button_request_id,
                                    "request_type": "DiscussApproval",
                                    "tool_name": "ExitPlanMode",  # #822
                                    "inline_keyboard": {
                                        "buttons": [
                                            [
                                                {
                                                    "text": _PLAN_APPROVE_BUTTON,
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
                            # #871/#418: the title is markdown — a raw value
                            # lost its backticks (`x` → x) and a heredoc's
                            # newline split the title. A one-line code span
                            # whose fence outruns any inner backtick.
                            value = inline_code(str(tool_input[key]), width=50)
                            if value:
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
            is_exit_plan_mode = (
                isinstance(request, claude_schema.ControlCanUseToolRequest)
                and getattr(request, "tool_name", "") == "ExitPlanMode"
            )
            # #383: an ExitPlanMode approval says what approving does.
            if is_exit_plan_mode and (caption := _plan_approve_caption(state)):
                warning_text += f"\n{caption}"

            # Store in pending requests with timestamp
            state.pending_control_requests[request_id] = (event, time.time())

            # Phase 2: Register request_id -> session_id mapping for callback routing
            if factory.resume:
                session_id = factory.resume.value
                _REQUEST_TO_SESSION[request_id] = session_id
                _bind_request_channel(request_id)  # #388
                state.control_registered_at[request_id] = time.monotonic()  # #684
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
            # #683: also sweep request_to_action, not just
            # pending_control_requests. The Pause & Outline hold-open path
            # returns its synthetic action early — before the request is
            # recorded in pending_control_requests — so its action_id is only
            # reachable here. dict.fromkeys de-dupes while preserving order.
            callback_handled = [
                rid
                for rid in dict.fromkeys(
                    (*state.pending_control_requests, *state.request_to_action)
                )
                if rid in _HANDLED_REQUESTS
            ]
            for rid in callback_handled:
                state.pending_control_requests.pop(rid, None)
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
                # #685: a user's answer being written right now wins; the
                # next sweep catches the id if it is somehow still pending.
                and rid not in _INFLIGHT_CONTROL_RESPONSES
            ]
            for rid in expired:
                del state.pending_control_requests[rid]
                _drop_exitplanmode_plan(state, rid, rejected=False, reason="timeout")
                _REQUEST_TO_INPUT.pop(rid, None)
                _REQUEST_TO_TOOL_NAME.pop(rid, None)
                # #685: the request is no longer answerable — drop the
                # mapping (a later tap reads "expired", not "pending", and the
                # entry stops pausing the live session) and record why.
                _REQUEST_TO_SESSION.pop(rid, None)
                mark_request_handled(rid, action="timeout", outcome="expired")
                expired_action_id = state.request_to_action.pop(rid, None)
                if expired_action_id:
                    # #685: strip the swept request's Approve/Deny keyboard.
                    state.control_action_for_tool = {
                        k: v
                        for k, v in state.control_action_for_tool.items()
                        if v != expired_action_id
                    }
                    reconciled_events.append(
                        factory.action_completed(
                            action_id=expired_action_id,
                            kind="warning",
                            title="⏱️ Timed out: auto-denied after 5 min",
                            ok=True,
                        )
                    )
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
                        # #383: the plan button names what it approves.
                        "text": _PLAN_APPROVE_BUTTON
                        if is_exit_plan_mode
                        else "✅ Approve",
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
            # #709: True only when an AskQuestionState flow was created, i.e.
            # when the `aq` callback handler (not Approve/Deny) drives this
            # action. That's the discriminator the bridge binds the tracked
            # action on — `ask_question` alone is absent when extraction
            # produced an empty question string.
            ask_flow_created = False
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
                            ask_flow_created = True
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
                # #822: names the tool on the keyboard logs (never the input).
                "tool_name": (
                    getattr(request, "tool_name", None)
                    if isinstance(request, claude_schema.ControlCanUseToolRequest)
                    else None
                ),
                "inline_keyboard": {
                    "buttons": button_rows,
                },
            }
            if ask_question:
                detail["ask_question"] = ask_question
            if ask_flow_created:
                detail["ask_flow"] = True

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
            return _translate_rate_limit_event(info, state=state, factory=factory)
        case claude_schema.StreamControlResponse(response=resp) if (
            resp.request_id.startswith(_PLAN_REARM_ID_PREFIX)
        ):
            # #383: the answer to our own set_permission_mode re-arm.
            _handle_plan_rearm_ack(
                state,
                resp,
                session_id=factory.resume.value if factory.resume else None,
            )
            return []
        case claude_schema.StreamControlCancelRequest(request_id=cancel_rid):
            # #684: the CLI withdrew a request it no longer needs.
            return _handle_control_cancel(cancel_rid, state=state, factory=factory)
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
    # #749 True when `allowed_tools` came from an explicit
    # `[engines.claude] allowed_tools` key rather than DEFAULT_ALLOWED_TOOLS.
    # `build_runner` collapses both into `allowed_tools`, so without this flag
    # a prompting-mode run cannot tell a deliberate user choice (which must be
    # honoured) from inherited plumbing (which must be dropped).
    allowed_tools_explicit: bool = False
    extra_args: list[str] = field(default_factory=list)
    dangerously_skip_permissions: bool = False
    use_api_billing: bool = False
    session_title: str = "claude"
    logger = logger

    # Phase 2: Control channel support
    supports_control_channel: bool = True
    _pty_master_fd: int | None = None  # legacy PTY approach (non-permission mode)
    _proc_stdin: Any | None = None  # PIPE stdin for control channel (permission mode)
    # #333 Tier 1 / Tier 3: subcountdown tuning constants. Class-level so
    # tests can override via monkeypatch without touching production code.
    _subcountdown_poll_interval_s: float = 5.0
    _subcountdown_limbo_detect_threshold_s: float = 30.0
    _subcountdown_sigterm_grace_s: float = 5.0
    _subcountdown_sigterm_grace_poll_s: float = 0.5
    # #591: when the reader is done and NOTHING references the session (no
    # pending control/ask requests, no live background work), waiting the
    # full post_result_idle_timeout before SIGTERM only lets MCP children
    # hold the process (and its RSS/TCP) open. Cap the wait at this grace
    # instead. 0 disables the shortcut (full timeout always applies).
    _post_result_limbo_grace_s: float = 60.0
    # #647/#646: liveness-aware extension of the post-result ceiling. When
    # the subcountdown deadline expires while the session still has live
    # background work and the process tree is not demonstrably idle, the
    # SIGTERM is deferred (re-checked each poll) instead of killing live
    # subagent work mid-flight — the kill quarantines the session and
    # produces the fresh-session amnesia (#646/#647). Absolute bound on the
    # deferral, measured from reader-EOF; background handles independently
    # age out at BG_AGENT_MAX_KEEP_S. 0 disables the extension.
    _post_result_bg_max_hold_s: float = 1800.0
    # #650: cadence of the ``subcountdown_tick`` observability line. Class
    # attr so tests can shrink it; production logs every ~30 s.
    _subcountdown_tick_log_interval_s: float = 30.0
    # Floor for the post-result watchdog poll cadence (class attr so tests
    # can shrink it; production keeps the 5s floor).
    _watchdog_min_poll_s: float = 5.0

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
        self,
        request_id: str,
        approved: bool,
        *,
        deny_message: str | None = None,
        rejects_plan: bool = True,
    ) -> bool:
        """Write a control response to the Claude Code process via PIPE or PTY.

        Uses _SESSION_STDIN to find the correct stdin for the session,
        supporting concurrent sessions on the same runner instance.

        ``rejects_plan`` (#793): whether a denial of an ExitPlanMode request
        is the user rejecting the plan (❌ Deny) rather than a procedural
        denial (Pause & Outline, Let's discuss).
        """
        # #793: settle the request's recorded plan body on the run's own
        # state (per-session, never the shared runner attributes).
        plan_session = _REQUEST_TO_SESSION.get(request_id)
        plan_state = _SESSION_BG_STATE.get(plan_session) if plan_session else None
        if plan_state is not None:
            if approved:
                _approve_exitplanmode_plan(
                    plan_state, request_id, session_id=plan_session, source="telegram"
                )
            else:
                _drop_exitplanmode_plan(
                    plan_state,
                    request_id,
                    rejected=rejects_plan,
                    reason="telegram_deny" if rejects_plan else "telegram_procedural",
                    session_id=plan_session,
                )
        # #822: read once for both branches (both always popped it) so every
        # write log names the tool and the CLI's effective mode — never the
        # tool input.
        tool_name = _REQUEST_TO_TOOL_NAME.pop(request_id, None)
        write_log: dict[str, Any] = {
            "tool_name": tool_name,
            "permission_mode": (
                plan_state.effective_permission_mode if plan_state else None
            ),
        }
        if approved:
            inner: dict[str, Any] = {"behavior": "allow"}
            # Claude Code CLI requires updatedInput for can_use_tool responses
            if request_id in _REQUEST_TO_INPUT:
                inner["updatedInput"] = _REQUEST_TO_INPUT.pop(request_id)
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
            if tool_name == "ExitPlanMode" and plan_state is not None:
                _stamp_plan_exit(plan_state)  # #383
        else:
            inner = {"behavior": "deny", "message": deny_message or "User denied"}
            # Clean up stored input on denial too
            _REQUEST_TO_INPUT.pop(request_id, None)
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
                await _locked_send(stdin_to_use, jsonl_line.encode())
                logger.info(
                    "control_response.sent",
                    request_id=request_id,
                    approved=approved,
                    session_id=session_id,
                    channel="pipe",
                    **write_log,
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
                    **write_log,
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
                    **write_log,
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
                    **write_log,
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
                    **write_log,
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
                    **write_log,
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
        # #812: hook lifecycle frames let the live session hold stdin open
        # for a background (async / asyncRewake) hook. Control-channel mode
        # only (live sessions need it), behind the kill switch, only when the
        # CLI lists the flag, and never twice (a config may already pass it).
        if (
            effective_mode is not None
            and _HOOK_EVENTS_FLAG not in self.extra_args
            and _load_hook_hold_settings()[0]
            and cli_supports_hook_events(self.claude_cmd)
        ):
            args.append(_HOOK_EVENTS_FLAG)

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
        # #749 stage 5 sits BEFORE the stage-6 prompt, so an allowlist covering
        # Bash/Read/Edit/Write pre-approves exactly the tools a prompting mode
        # exists to ask about — phase 01's gate would never see them.  Drop it
        # for those modes unless the user asked for it by name.
        allowed_tools = _coerce_comma_list(self.allowed_tools)
        if allowed_tools is not None and is_claude_prompting_mode(effective_mode):
            if self.allowed_tools_explicit:
                # An explicit choice is honoured, but the interaction is
                # surprising enough to deserve one line in the log.
                if effective_mode not in _PROMPTING_MODE_ALLOWLIST_LOGGED:
                    _PROMPTING_MODE_ALLOWLIST_LOGGED.add(effective_mode)
                    logger.info(
                        "claude.allowed_tools.prompting_mode_override",
                        permission_mode=effective_mode,
                        allowed_tools=allowed_tools,
                        detail=(
                            "explicit [engines.claude] allowed_tools pre-approves "
                            "these tools at stage 5, so they will not raise a "
                            "Telegram approval in this mode (#749)"
                        ),
                    )
            else:
                allowed_tools = None
        if allowed_tools is not None:
            args.extend(["--allowedTools", allowed_tools])
        if self.dangerously_skip_permissions is True:
            args.append("--dangerously-skip-permissions")

        if effective_mode is not None:
            # #741 every genuine CLI mode passes through verbatim — only
            # Untether's own `plan-auto` sugar is translated (to `plan`).
            # Until 0.35.5rc8 this line remapped `auto` to `plan`, which made
            # the CLI's own classifier-gated `auto` mode unreachable.
            cli_mode = claude_cli_permission_mode(effective_mode)
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
        state.auto_approve_exit_plan_mode = is_claude_plan_auto(
            self._effective_permission_mode()
        )
        # #749 arm the stage-6 gate from the same resolved mode.  This must
        # read `_effective_permission_mode()` (per-chat override → engine
        # config), not `self.permission_mode`: `translate_claude_event` is a
        # module-level function with no access to the runner, so the decision
        # has to be made here and carried on the state.
        state.prompting_mode = is_claude_prompting_mode(
            self._effective_permission_mode()
        )
        # #751: what the first `system/init` should report if the CLI honours
        # the request; compared once in the init branch.
        requested_mode = self._effective_permission_mode()
        if requested_mode is not None:
            state.requested_permission_mode = (
                "bypassPermissions"
                if self.dangerously_skip_permissions is True
                else normalise_claude_cli_mode(requested_mode)
            )
        # #835: an unattended (cron / webhook) run denies anything that would
        # wait for a tap. Control channel only — the legacy `-p` path has no
        # stage 6 to gate.
        run_opts = get_run_options()
        if (
            requested_mode is not None
            and run_opts is not None
            and run_opts.unattended_trigger is not None
        ):
            state.unattended_trigger = run_opts.unattended_trigger
            state.unattended_mode = (
                "bypassPermissions"
                if self.dangerously_skip_permissions is True
                else requested_mode
            )
        # #383: a plan chat — the approval caption and the plan re-arm key
        # off the configured mode, never the CLI's current one.
        state.configured_plan_mode = (
            claude_cli_permission_mode(self._effective_permission_mode()) == "plan"
        )
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
                    # #647: expose this run's background-handle state to the
                    # bridge's handoff wait. Cleared with the other
                    # registries in _cleanup_session_registries.
                    _SESSION_BG_STATE[registered_session_id] = state
                    if state.live_mode and session_stdin is not None:
                        _LIVE_SESSIONS[registered_session_id] = LiveSession(
                            session_id=registered_session_id,
                            state=state,
                            stdin=session_stdin,
                            pid=pid,
                        )
                        # #812: MCP servers are up by system/init. Always
                        # captured: the close-grace check uses it even with
                        # the hold switched off.
                        await capture_cli_baseline(state, pid)
                    logger.info(
                        "session_stdin.registered",
                        session_id=registered_session_id,
                        pid=pid,
                    )
                if (
                    isinstance(evt, CompletedEvent)
                    and state.live_mode
                    and not _completed_keeps_session_live(evt)
                ):
                    # Review finding (#776): an errored or empty first result
                    # must not leave a live session behind — the bridge only
                    # delivers ok results early, so the error (and #596/#572
                    # recovery) would wait for the idle close, and follow-ups
                    # could be injected into a broken session. Close it now:
                    # the CLI exits, the reader hits EOF, the run ends and the
                    # error is delivered straight away.
                    sid = evt.resume.value if evt.resume else registered_session_id
                    if sid is not None:
                        await close_live_session(sid, "error")
                    logger.info(
                        "claude.live_session.closed_after_result",
                        session_id=sid,
                        ok=evt.ok,
                    )
                if state.plan_rearm_pending and (
                    isinstance(evt, CompletedEvent)
                    or (isinstance(evt, TurnEvent) and evt.phase == "completed")
                ):
                    # #383: put the session back into plan mode BEFORE the
                    # turn close reaches the bridge — its shielded
                    # on_completed / turn router can hold this yield for up
                    # to 60 s, and a wake turn the CLI starts from an
                    # already-queued notification needs no stdin line.
                    await self._drain_plan_rearm_pre_yield(state, stdin=session_stdin)
                yield evt
            # Drain auto-approve and auto-deny queues after EVERY line, even if no events
            # were yielded.  This prevents deadlock when auto-handled requests produce no events.
            await self._drain_auto_approve(state, stdin=session_stdin)
            await self._drain_auto_deny(state, stdin=session_stdin)
            # #383 backstop: a re-arm the pre-yield drain couldn't write.
            await self._drain_plan_rearm(state, stdin=session_stdin)
            # #365 fire-and-forget mcp_status control_requests queued by
            # translate_claude_event on tool_result. Drain last so the
            # response (if any) arrives after Claude has processed the
            # tool_result itself.
            await self._drain_catalog_refresh(state, stdin=session_stdin)
            # After CompletedEvent, stop reading stdout immediately.
            # Claude Code's MCP server child processes may inherit the stdout pipe FD,
            # keeping it open even after Claude Code exits. Without this break,
            # we'd block forever waiting for EOF that never comes.
            # #776: a live session keeps reading; `_subprocess_watchdog`
            # carries the #505 protection instead (post-exit drain + close).
            if stream.did_emit_completed and not stream.followup_turns:
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
                    await _locked_send(pipe, payload)
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
                    await _locked_send(pipe, payload)
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

    async def _drain_plan_rearm_pre_yield(
        self, state: ClaudeStreamState, *, stdin: Any = None
    ) -> None:
        """#383: the idle-boundary re-arm, written before the turn-closing
        event is yielded (a separate hook so a test can prove the placement
        matters)."""
        await self._drain_plan_rearm(state, stdin=stdin)

    async def _drain_plan_rearm(
        self, state: ClaudeStreamState, *, stdin: Any = None
    ) -> None:
        """#383: write the pending idle-boundary ``set_permission_mode plan``
        re-arm. No-op unless one is pending; skips a closing session or one
        without stdin. ``plan_rearm_pending_reason``: ``idle`` (a turn
        closed) or ``agents_done`` (C4: an approved-plan agent ended while
        the session idled — written right after that line, ahead of the
        wake turn the CLI starts for it)."""
        if not state.plan_rearm_pending:
            return
        state.plan_rearm_pending = False
        reason = state.plan_rearm_pending_reason
        state.plan_rearm_pending_reason = "idle"
        session_id = state.factory.resume.value if state.factory.resume else None
        live = _LIVE_SESSIONS.get(session_id) if session_id else None
        if session_id is None or live is None or live.closing or stdin is None:
            return
        payload = _claim_plan_rearm(state, session_id, reason=reason)
        if payload is None:
            return
        await _send_plan_rearm(
            state, stdin, payload, session_id=session_id, reason=reason
        )

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
                    await _locked_send(pipe, payload)
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

    async def _maybe_cancel_pre_result_silence(
        self,
        *,
        state: ClaudeStreamState,
        stream: Any,
        proc: Any,
        run_logger: Any,
        silence_timeout_s: float,
        started_at: float,
    ) -> bool:
        """#592: kill a run whose stream went silent forever pre-first-result.

        The post-result watchdog only arms after a ``result`` event and the
        liveness machinery never escalates an alive-but-idle process — a run
        that goes silent before its first result idled FOREVER (8-day zombie
        Claude subprocess + leaked MCP children + held session lock on mac).

        Fires only when: zero stream output for ``silence_timeout_s``
        (measured from the later of watchdog start and the last stdout
        line), NO pending permission/ask requests (plan-approval waits are
        by design, #526/#527) and NO live background work. The kill is
        descendant-aware; a reason line is appended to ``stderr_capture`` so
        the run's error message tells the user what happened. Returns True
        when the cap fired.
        """
        if silence_timeout_s <= 0 or proc is None or proc.returncode is not None:
            return False
        if state.pre_result_silence_killed:
            return False
        last_out = float(getattr(stream, "last_stdout_at", 0.0) or 0.0)
        silence_s = time.monotonic() - max(last_out, started_at)
        if silence_s < silence_timeout_s:
            return False
        sid = state.factory.resume.value if state.factory.resume is not None else None
        pending_requests = (
            [k for k, v in _REQUEST_TO_SESSION.items() if v == sid] if sid else []
        )
        pending_asks = (
            [k for k in _PENDING_ASK_REQUESTS if _REQUEST_TO_SESSION.get(k) == sid]
            if sid
            else []
        )
        live_bg = has_live_background_work(state)
        if pending_requests or pending_asks or live_bg:
            run_logger.info(
                "claude.pre_result_silence.suppressed",
                session_id=sid,
                pid=proc.pid,
                silence_s=round(silence_s, 1),
                pending_requests=len(pending_requests),
                pending_asks=len(pending_asks),
                live_background_work=live_bg,
            )
            return False
        state.pre_result_silence_killed = True
        run_logger.warning(
            "claude.pre_result_silence.cancel",
            session_id=sid,
            pid=proc.pid,
            silence_s=round(silence_s, 1),
            timeout_s=silence_timeout_s,
        )
        # Thread the reason into the run's error message via the stderr
        # excerpt (#575 machinery) — the resulting rc=143 error would
        # otherwise be indistinguishable from any other kill.
        if hasattr(stream, "stderr_capture"):
            stream.stderr_capture.append(
                f"untether: no stream output for {int(silence_s // 60)}m before "
                f"any result — pre-result silence cap "
                f"({int(silence_timeout_s // 60)}m) hit; run auto-cancelled (#592)"
            )
        signal_pid_group(proc.pid, signal.SIGTERM)
        grace_deadline = time.monotonic() + self._subcountdown_sigterm_grace_s
        while time.monotonic() < grace_deadline:
            await anyio.sleep(self._subcountdown_sigterm_grace_poll_s)
            if proc.returncode is not None:
                return True
        if proc.returncode is None:
            run_logger.warning(
                "claude.pre_result_silence.sigkill_after_grace",
                session_id=sid,
                pid=proc.pid,
            )
            signal_pid_group(proc.pid, signal.SIGKILL)
        return True

    # #776 live-session lifecycle knobs (class attrs so tests can shrink them).
    _live_poll_s: float = 1.0
    _live_close_grace_s: float = 15.0
    # #791: after the close grace, SIGINT (the CLI's Ctrl-C path) gets this
    # long before the SIGTERM escalation.
    _live_close_sigint_grace_s: float = 5.0
    # #812: the close grace when a background hook is evident at close — the
    # CLI's 30 s asyncRewake exit wait (``Rxo``, §A1) + 5 s.
    _live_close_grace_hooks_s: float = 35.0
    # #829: ``claude.live_session.hold_rearmed`` logs the first re-arm of an
    # idle period, then at most once per this many seconds.
    _hold_rearm_log_every_s: float = 300.0
    # #872: after a declared wait's deadline (a background Bash's ``timeout``,
    # a wake-up's fire time) the hold waits this much longer, so the CLI's own
    # stop notice / wake turn lands before Untether would close.
    _declared_wait_grace_s: float = 60.0

    async def _live_session_lifecycle(
        self,
        *,
        state: ClaudeStreamState,
        reader_done: anyio.Event,
        run_logger: Any,
        proc: Any,
        stream: Any,
        idle_grace_s: float,
        max_hold_s: float,
        abs_cap_s: float,
    ) -> None:
        """Decide when a live session's stdin closes (#776, phase 03).

        TURN_ACTIVE → IDLE on each result. While IDLE:
        - pending approvals / asks pause every timer;
        - nothing live (no native bg task, no pending ScheduleWakeup) for
          ``idle_grace_s`` → graceful close (``idle_no_tasks``);
        - work still live ``max_hold_s`` after the last turn ended → notice +
          graceful close (``max_hold``; re-armed by every turn and — #829,
          ``state.bg_hold_rearm_on_progress`` — by background activity:
          ``latest_background_progress`` while idle, and a background Bash's
          output file when the hold would expire);
        - #872, ``state.bg_hold_declared_waits``: never before a declared wait
          ends (``declared_wait_until``: a background Bash's ``timeout``, a
          pending ScheduleWakeup's fire time, + ``_declared_wait_grace_s``);
        - ``abs_cap_s`` from spawn → notice + close (``abs_cap``).
        Closing stdin makes the CLI stop its tasks and exit rc=0 (F3/F4). Only
        if it doesn't exit within ``_live_close_grace_s`` does
        ``_await_live_exit_or_force`` log ``close_grace_expired`` and escalate
        (SIGINT, then SIGTERM/SIGKILL); a clean idle close is not quarantined
        (#791). ``claude.live_session.lifecycle_exited`` reports how the
        session actually ended (``_lifecycle_exit_reason``, #820) with the
        close reason beside it.
        """
        exit_reason = "reader_done"
        cancelled = False
        # #829: the session this lifecycle watches — kept so the ``"closed"``
        # notice can be sent from ``finally`` on every exit path, including a
        # CLI that exits within one poll of the close.
        tracked: LiveSession | None = None
        try:
            while not reader_done.is_set():
                await anyio.sleep(self._live_poll_s)
                if reader_done.is_set():
                    return
                sid = (
                    state.factory.resume.value
                    if state.factory.resume is not None
                    else None
                )
                live = _LIVE_SESSIONS.get(sid) if sid else None
                if live is None:
                    continue
                tracked = live
                if live.closing:
                    exit_reason = await self._await_live_exit_or_force(
                        live=live,
                        proc=proc,
                        stream=stream,
                        run_logger=run_logger,
                        reader_done=reader_done,
                    )
                    return
                now = time.monotonic()
                # #812: a hook still running in the background holds stdin
                # open too (sibling predicate, D-1) — closing it would make
                # the CLI drop an asyncRewake hook's findings. All or
                # nothing: while ANY hook process lives every unpaired hook
                # holds (a shell can't be tied to a hook); once none has
                # been alive for the settle, they have all finished (plain
                # ``async``: response withheld by the CLI until the next
                # turn) and are released together.
                if (
                    live.idle
                    and state.hold_for_async_hooks
                    and state.async_hook_max_hold_s > 0
                    and state.pending_hooks
                ):
                    shells = await _hook_processes(
                        state,
                        getattr(proc, "pid", None)
                        if getattr(proc, "returncode", None) is None
                        else None,
                    )
                    state.live_hook_processes = None if shells is None else len(shells)
                    if shells is None or shells:
                        # Unreadable → the bounded hold; alive → hold.
                        state.hook_procs_gone_since = None
                    elif state.hook_procs_gone_since is None:
                        # First scan with no hook process: give a hook that
                        # just exited a moment to report back first.
                        state.hook_procs_gone_since = now
                    elif now - state.hook_procs_gone_since >= _HOOK_PROC_SETTLE_S:
                        released = release_settled_async_hooks(state, now=now)
                        if released:
                            run_logger.info(
                                "claude.hook.hold_released",
                                session_id=sid,
                                hook_names=[h.label for h in released],
                                hook_ids=[h.hook_id for h in released],
                                reason="no_hook_process",
                                held_s=round(
                                    now - min(h.started_at for h in released), 1
                                ),
                            )
                else:
                    state.hook_procs_gone_since = None
                hooks_pending = live.idle and has_pending_async_hooks(state)
                live_work = (
                    has_live_background_work(state)
                    or _has_pending_wakeup(state)
                    or hooks_pending
                )
                if hooks_pending and not state.hook_hold_logged:
                    state.hook_hold_logged = True
                    run_logger.info(
                        "claude.hook.pending_hold",
                        session_id=sid,
                        hook_names=[
                            h.label
                            for h in state.pending_hooks.values()
                            if not _hook_never_holds(h.event)
                        ],
                        max_hold_s=state.async_hook_max_hold_s,
                    )
                elif not hooks_pending:
                    state.hook_hold_logged = False
                if abs_cap_s > 0 and now - live.spawned_at >= abs_cap_s:
                    await close_live_session(sid, "abs_cap", notice=live_work)
                    continue
                if not live.idle:
                    live.idle_since = None
                    live.hold_started = None
                    live.idle_period_started = None
                    live.declared_wait_holding = False
                    continue
                if live.idle_period_started is None:
                    live.idle_period_started = now
                    live.rearm_logged_at = None
                    live.hold_extended_logged.clear()
                if state.plan_rearm_failed:
                    # #383: the CLI refused the plan re-arm; don't let wake
                    # turns keep running unplanned. The next message resumes
                    # a fresh `--permission-mode plan` process.
                    await close_live_session(
                        sid, "plan_rearm_failed", only_if_idle=True
                    )
                    continue
                if state.plan_rearm_deferred and state.plan_rearm_inflight is None:
                    await self._lift_idle_plan_deferral(live, now=now)
                if _awaiting_injected(state):
                    # A follow-up was written; its turn hasn't opened yet.
                    live.idle_since = now
                    live.hold_started = now
                    continue
                if any(v == sid for v in _REQUEST_TO_SESSION.values()):
                    # A pending approval / ask pauses every timer.
                    live.idle_since = now
                    live.hold_started = now
                    continue
                if live.idle_since is None:
                    live.idle_since = now
                    live.hold_started = now
                elif live.had_live_work and not live_work:
                    # Work just ended without a wake turn: fresh idle grace.
                    live.idle_since = now
                live.had_live_work = live_work
                if live_work:
                    if (
                        live.declared_wait_holding
                        and declared_wait_until(
                            state, grace_s=self._declared_wait_grace_s, now=now
                        )
                        is None
                    ):
                        # #872: the declared wait just ended. The quiet-time
                        # clock still dates from before it, so the expired
                        # rule would close at once — over the wake turn's
                        # prompt and its UserPromptSubmit hook (R17-01a).
                        # Restart it; ``abs_cap`` above still bounds it.
                        live.declared_wait_holding = False
                        live.hold_started = now
                        run_logger.info(
                            "claude.live_session.hold_rearmed",
                            session_id=sid,
                            source="declared_wait_ended",
                            task_id=None,
                            since_turn_s=(
                                round(now - live.idle_period_started, 1)
                                if live.idle_period_started is not None
                                else None
                            ),
                        )
                    rearm = state.bg_hold_rearm_on_progress
                    if rearm and live.hold_started is not None:
                        # #829: the hold measures quiet time, not time since
                        # the turn — any newer background activity moves it.
                        activity = latest_background_activity(state)
                        if activity is not None and activity.at > live.hold_started:
                            live.hold_started = activity.at
                            self._log_hold_rearm(live, activity, run_logger, now)
                    if (
                        max_hold_s > 0
                        and live.hold_started is not None
                        and now - live.hold_started >= max_hold_s
                    ):
                        if state.bg_hold_declared_waits:
                            # #872: a wait Claude declared (a background
                            # Bash's ``timeout``, a pending wake-up) is not
                            # cut short for being quiet; ``abs_cap`` above
                            # still bounds it. ``hold_started`` is untouched,
                            # so once it lapses the quiet-time rule applies.
                            wait = declared_wait_until(
                                state, grace_s=self._declared_wait_grace_s, now=now
                            )
                            if wait is not None:
                                live.declared_wait_holding = True
                                self._log_hold_extended(live, wait, run_logger, now)
                                continue
                        if rearm:
                            # #829 fallback, only at would-expire: a
                            # background Bash that is still printing.
                            output = await _bash_output_activity(state)
                            if output is not None and output.at > live.hold_started:
                                live.hold_started = output.at
                                self._log_hold_rearm(live, output, run_logger, now)
                                continue
                        await close_live_session(
                            sid, "max_hold", notice=True, only_if_idle=True
                        )
                    continue
                if now - live.idle_since >= idle_grace_s:
                    await close_live_session(sid, "idle_no_tasks", only_if_idle=True)
        except (anyio.get_cancelled_exc_class(), KeyboardInterrupt):
            cancelled = True
            raise
        finally:
            reason = _lifecycle_exit_reason(
                exit_reason=exit_reason,
                cancelled=cancelled,
                process_gone=(
                    reader_done.is_set()
                    or getattr(proc, "returncode", None) is not None
                ),
                closing=tracked is not None and tracked.closing,
                stage=tracked.exit_stage if tracked is not None else None,
            )
            run_logger.info(
                "claude.live_session.lifecycle_exited",
                session_id=(
                    state.factory.resume.value
                    if state.factory.resume is not None
                    else None
                ),
                reason=reason,
                close_reason=tracked.close_reason if tracked is not None else None,
            )
            # The run's task group is cancelled as soon as the CLI's stdout
            # ends, so a cancellation is also the normal exit after a close
            # (#820 classifies it from the settled state above). Report the
            # close once the process is gone — never while it may still be
            # running (``reason == "cancelled"``: e.g. a /cancel that gave up
            # waiting).
            if (
                tracked is not None
                and tracked.closing
                and not tracked.closed_notified
                and reason != "cancelled"
            ):
                await self._notify_live_closed(tracked, run_logger)

    async def _lift_idle_plan_deferral(self, live: LiveSession, *, now: float) -> None:
        """#383 C4: while the session idles, the approved plan's agents can
        go quiet past the hold or reach the ceiling with no turn close to
        re-check the deferral — re-arm then, so the next wake turn is
        planned. (An agent *finishing* is handled on its task frame.)"""
        state = live.state
        agents, why = _rearm_deferral(state, now=now)
        if agents:
            return
        if not _plan_rearm_needed(state, reason=why):
            # plan-auto: wake turns aren't re-armed; its follow-ups still are.
            state.plan_rearm_deferred = 0
            state.plan_rearm_deferred_since = None
            return
        async with live.lock:
            if live.idle and live.accepting_input:
                await _write_plan_rearm_if_needed(live, reason=why)

    def _log_hold_rearm(
        self,
        live: LiveSession,
        activity: BackgroundActivity,
        run_logger: Any,
        now: float,
    ) -> None:
        """#829: ``claude.live_session.hold_rearmed`` — the first re-arm of
        an idle period, then at most every ``_hold_rearm_log_every_s``
        (counted from the previous line)."""
        last = live.rearm_logged_at
        if last is not None and now - last < self._hold_rearm_log_every_s:
            return
        live.rearm_logged_at = now
        run_logger.info(
            "claude.live_session.hold_rearmed",
            session_id=live.session_id,
            source=activity.source,
            task_id=activity.task_id,
            since_turn_s=(
                round(now - live.idle_period_started, 1)
                if live.idle_period_started is not None
                else None
            ),
            activity_age_s=round(max(0.0, now - activity.at), 1),
        )

    def _log_hold_extended(
        self,
        live: LiveSession,
        wait: DeclaredWait,
        run_logger: Any,
        now: float,
    ) -> None:
        """#872: ``claude.live_session.hold_extended`` — once per idle period
        per ``(source, task_id)``."""
        key = f"{wait.source}:{wait.task_id}"
        if key in live.hold_extended_logged:
            return
        live.hold_extended_logged.add(key)
        run_logger.info(
            "claude.live_session.hold_extended",
            session_id=live.session_id,
            source=wait.source,
            task_id=wait.task_id,
            declared_s=round(wait.declared_s, 1),
            remaining_s=round(wait.remaining_s, 1),
            since_turn_s=(
                round(now - live.idle_period_started, 1)
                if live.idle_period_started is not None
                else None
            ),
        )

    async def _notify_live_closed(self, live: LiveSession, run_logger: Any) -> None:
        """#829: tell listeners how a close ended — once, after the process
        is gone. ``quarantined`` is read from the store, so it is right on
        every path (clean exit, SIGINT, SIGTERM, error)."""
        live.closed_notified = True
        quarantined = False
        with contextlib.suppress(Exception):
            quarantined = get_quarantine_store().is_quarantined(
                self.engine, live.session_id
            )
        payload = {
            "reason": live.close_reason,
            "quarantined": quarantined,
            "tasks": list(live.close_tasks),
        }
        run_logger.info(
            "claude.live_session.closed",
            session_id=live.session_id,
            close_reason=live.close_reason,
            quarantined=quarantined,
            tasks=len(live.close_tasks),
        )
        with anyio.CancelScope(shield=True), anyio.move_on_after(5):
            await _notify_live_listeners(live, "closed", payload)

    async def _await_live_exit_or_force(
        self,
        *,
        live: LiveSession,
        proc: Any,
        stream: Any,
        run_logger: Any,
        reader_done: anyio.Event,
    ) -> str:
        from ..utils.proc_diag import collect_proc_diag

        grace_start_diag = None
        if proc is not None and isinstance(getattr(proc, "pid", None), int):
            with contextlib.suppress(Exception):
                grace_start_diag = collect_proc_diag(proc.pid)
        # #812 fallback (flag off, CLI too old, or hold bound hit): with a
        # background hook still evident, give the CLI its own 30 s
        # asyncRewake exit wait (+5 s) before signalling it. This only lets
        # plain hooks finish their teardown — a rewake that fires after
        # stdin closed is dropped by the CLI regardless (§A1 P5-B).
        hooks_evident = bool(live.close_hooks)
        if not hooks_evident and grace_start_diag is not None:
            proc_hooks = _scan_hook_children(grace_start_diag.child_pids)
            if proc_hooks:
                hooks_evident = True
                _log_async_hook_killed(
                    live,
                    hook_count=len(proc_hooks),
                    hook_names=proc_hooks,
                    hook_events=[],
                    source="proc",
                )
                if live.close_reason not in _USER_CLOSE_REASONS:
                    await _notify_live_listeners(
                        live,
                        "closing",
                        {
                            "reason": live.close_reason,
                            "tasks": [],
                            "hooks": proc_hooks,
                            "hook_count": len(proc_hooks),
                        },
                    )
        if not hooks_evident and proc is not None and proc.returncode is None:
            # Process evidence: a live hook process (whatever its command —
            # the ``/hooks/`` path heuristic above misses inline commands)
            # still gets the CLI's asyncRewake exit wait.
            shells = await _hook_processes(live.state, getattr(proc, "pid", None))
            if shells:
                hooks_evident = True
                run_logger.info(
                    "claude.live_session.hook_process_at_close",
                    session_id=live.session_id,
                    close_reason=live.close_reason,
                    live_hook_processes=len(shells),
                )
        grace_s = (
            self._live_close_grace_hooks_s
            if hooks_evident
            else self._live_close_grace_s
        )
        with anyio.move_on_after(grace_s):
            await reader_done.wait()
        if reader_done.is_set() or proc is None or proc.returncode is not None:
            return "exited_after_close"
        sid = live.session_id
        live_tasks = len(live_task_descriptions(live.state))
        # #791: a clean idle close (turn closed, nothing live, set when stdin
        # was closed and still true now) left a complete transcript — the CLI
        # is merely slow to exit (MCP shutdown, transcript flush, exit hook).
        # Quarantining it would cost the user their context on the next
        # message; #631 empty-resume recovery stays the backstop.
        idle_clean = live.closed_idle_clean and _is_clean_idle(live)
        # #829 B2: an Untether-initiated close of a session whose turn was
        # closed (so ``idle_clean`` failed only because tasks were live — a
        # background agent ignores EOF, P0 G6) may still stop cleanly on
        # SIGINT: rc 0, a complete transcript, resumable (P0 G7-G9). Its
        # quarantine is decided after the SIGINT wait instead of before it.
        # ``abs_cap`` (mid-turn) and ``error`` never qualify.
        may_stop_clean = not idle_clean and _may_stop_clean(live)
        # #791 (a): record what the CLI was doing before any signal changes it.
        run_logger.warning(
            "claude.live_session.close_grace_expired",
            session_id=sid,
            pid=proc.pid,
            close_reason=live.close_reason,
            grace_s=grace_s,
            live_tasks=live_tasks,
            idle_clean=idle_clean,
            last_progress_age_s=_last_progress_age_s(live.state),
            **_close_grace_diag(proc.pid, grace_start_diag),
        )

        def quarantine() -> bool:
            if (
                idle_clean
                or stream is None
                or not stream.did_emit_completed
                or not _load_quarantine_on_forced_teardown()
            ):
                return False
            try:
                get_quarantine_store().quarantine(
                    self.engine, sid, reason="forced_teardown_after_result"
                )
            except Exception:  # noqa: BLE001 — never break teardown
                run_logger.debug("session.quarantine_record_failed", exc_info=True)
                return False
            return True

        quarantined = False if may_stop_clean else quarantine()
        # #791 (c): SIGINT first — the CLI's own Ctrl-C shutdown path (the
        # whole process group, as a terminal Ctrl-C would) — then SIGTERM.
        #
        # #829: shielded (bounded by the SIGINT grace + 1 s) — the CLI's exit
        # ends the reader, which cancels this task group; a deferred B2
        # quarantine decision must not be lost to that cancellation.
        stopped_clean = False
        live.exit_stage = "sigint"  # #820: before the signal, not after
        with anyio.CancelScope(shield=True):
            signal_pid_group(proc.pid, signal.SIGINT)
            with anyio.move_on_after(self._live_close_sigint_grace_s):
                await reader_done.wait()
            exited = reader_done.is_set() or proc.returncode is not None
            if may_stop_clean:
                if exited:
                    stopped_clean = await self._await_returncode(proc) == 0
                if not stopped_clean:
                    # rc != 0, or deaf to SIGINT too (the SIGTERM path below
                    # keeps today's quarantine).
                    quarantined = quarantine()
        if exited:
            run_logger.info(
                "claude.live_session.exited_after_sigint",
                session_id=sid,
                pid=proc.pid,
                close_reason=live.close_reason,
                quarantined=quarantined,
                stopped_clean=stopped_clean,
            )
            return "sigint"
        run_logger.warning(
            "claude.live_session.forced_teardown",
            session_id=sid,
            pid=proc.pid,
            close_reason=live.close_reason,
            live_tasks=live_tasks,
            idle_clean=idle_clean,
            quarantined=quarantined,
        )
        if stream is not None:
            stream.sigterm_sent = True
        live.exit_stage = "sigterm"  # #820
        signal_pid_group(proc.pid, signal.SIGTERM)
        deadline = time.monotonic() + self._subcountdown_sigterm_grace_s
        while time.monotonic() < deadline:
            await anyio.sleep(self._subcountdown_sigterm_grace_poll_s)
            if proc.returncode is not None:
                return "sigterm"
        if proc.returncode is None:
            live.exit_stage = "sigkill"  # #820
            signal_pid_group(proc.pid, signal.SIGKILL)
        return "sigkill"

    async def _await_returncode(self, proc: Any, timeout_s: float = 1.0) -> int | None:
        """#829: the exit code once the process has been reaped (stdout EOF
        can precede the reap by a moment); None if still unknown."""
        if proc.returncode is None and hasattr(proc, "wait"):
            with anyio.move_on_after(timeout_s), contextlib.suppress(Exception):
                await proc.wait()
        return proc.returncode

    async def _post_result_idle_watchdog(
        self,
        state: ClaudeStreamState,
        this_proc_stdin: Any,
        reader_done: anyio.Event,
        run_logger: Any,
        timeout_s: float,
        proc: Any = None,
        stream: Any = None,
        limbo_grace_s: float | None = None,
        pre_result_silence_timeout_s: float = 3600.0,
        bg_max_hold_s: float | None = None,
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
        # (_watchdog_min_poll_s is a class attr so tests can shrink it.)
        poll_interval = max(self._watchdog_min_poll_s, min(timeout_s / 20.0, 30.0))
        watchdog_started_at = time.monotonic()

        # #333 instrumentation. channelo rc15→rc16 hit a 43+ min post-result
        # hang where this watchdog silently failed to fire (no
        # ``closing_stdin`` / ``deferred`` log lines despite elapsed ≫
        # ``timeout_s``). The four candidate causes from the original
        # memory note are (1) ``result_received_at`` never set, (2)
        # ``post_result_idle_enabled`` evaluated False, (3)
        # ``reader_done`` set early, (4) task crashed silently or never
        # started. Without entry/exit/tick logs we can't discriminate
        # them. These logs are intentionally verbose for rc17 — at 30 s
        # poll x hours of session = O(120) lines, trivial; rate-limiting
        # now would create ambiguity in the next reproduction. #799: that
        # held per session but not per fleet — unarmed ticks are now DEBUG,
        # with one INFO ``armed`` edge; armed ticks stay INFO.
        #
        # Exception strategy mirrors ``_subprocess_watchdog``
        # (src/untether/runner.py:1010-1079) and
        # ``_drain_catalog_refresh`` (above): per-tick ``try/except``
        # log-and-continue so a transient error (e.g. a flaky structlog
        # ProcessorChain) never cancels the sibling ``_iter_jsonl_events``
        # task in the task group and aborts the user's in-flight turn.
        # The outer ``try/finally`` lets us tag the ``task_exited`` log
        # with the reason for diagnostics.
        sid_at_start = (
            state.factory.resume.value if state.factory.resume is not None else None
        )
        run_logger.info(
            "claude.post_result_idle.task_started",
            session_id=sid_at_start,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval,
        )
        exit_reason = "loop_exited"
        # #799: edge-triggered INFO for the idle timer arming, so the
        # per-tick log can drop to DEBUG while nothing is armed.
        was_armed = False
        try:
            # #333 Tier 1 entry check: if ``reader_done`` is already set
            # before the first poll (e.g. the JSONL reader finished
            # extremely quickly), still run the subcountdown if the
            # subprocess is alive. Mirrors the mid-loop check below.
            if reader_done.is_set():
                sid_now = (
                    state.factory.resume.value
                    if state.factory.resume is not None
                    else None
                )
                if proc is not None and proc.returncode is None:
                    exit_reason = await self._post_result_subcountdown(
                        state=state,
                        proc=proc,
                        run_logger=run_logger,
                        timeout_s=timeout_s,
                        stream=stream,
                        session_id=sid_now,
                        limbo_grace_s=limbo_grace_s,
                        bg_max_hold_s=bg_max_hold_s,
                    )
                    return
                exit_reason = "reader_done"
                return
            while not reader_done.is_set():
                try:
                    await anyio.sleep(poll_interval)
                    if reader_done.is_set():
                        # #333 Tier 1: the JSONL reader exhausted — either
                        # because the subprocess emitted CompletedEvent and
                        # is exiting (the happy path), or because Claude
                        # Code v2.1.143 closed stdout while keeping the
                        # subprocess alive (the limbo path). Before rc18
                        # the watchdog returned here, bypassing the 600 s
                        # countdown and leaving the subprocess + MCP
                        # children to idle for 30+ min until the user
                        # cancelled. Now we check if the subprocess is
                        # still alive and, if so, enter a stdout-closed
                        # subcountdown.
                        sid_now = (
                            state.factory.resume.value
                            if state.factory.resume is not None
                            else None
                        )
                        if proc is not None and proc.returncode is None:
                            exit_reason = await self._post_result_subcountdown(
                                state=state,
                                proc=proc,
                                run_logger=run_logger,
                                timeout_s=timeout_s,
                                stream=stream,
                                session_id=sid_now,
                                limbo_grace_s=limbo_grace_s,
                                bg_max_hold_s=bg_max_hold_s,
                            )
                            return
                        exit_reason = "reader_done"
                        return
                    armed_at = state.result_received_at
                    # A live session's post-result idle belongs to
                    # `_live_session_lifecycle` (#776), so it never arms here.
                    armed_now = armed_at is not None and not state.live_mode
                    if armed_now and not was_armed:
                        run_logger.info(
                            "claude.post_result_idle.armed",
                            session_id=(
                                state.factory.resume.value
                                if state.factory.resume is not None
                                else None
                            ),
                            timeout_s=timeout_s,
                        )
                    was_armed = armed_now
                    if armed_at is None:
                        # Pre-result: tick log still useful so we can
                        # confirm the watchdog is alive even before the
                        # first ``result`` event lands.
                        #
                        # #696: these two counts used to be hardcoded ``0``
                        # here while the post-result branch below computed
                        # them for real — same event name, same field names,
                        # one branch measured and one stubbed. A session
                        # sitting on a visibly-pending approval keyboard
                        # therefore logged ``pending_asks=0`` every 30 s,
                        # which reads as a presenter/runner desync and cost
                        # a source read to clear. Measuring them here also
                        # yields the signal that was missing entirely: a
                        # pre-result tick with ``pending_asks=1`` is a
                        # positive, greppable "this run is waiting on the
                        # user" marker, so an operator can answer "is
                        # anything waiting on me?" from logs alone.
                        pre_sid = (
                            state.factory.resume.value
                            if state.factory.resume is not None
                            else None
                        )
                        pre_pending_requests = (
                            [k for k, v in _REQUEST_TO_SESSION.items() if v == pre_sid]
                            if pre_sid
                            else []
                        )
                        pre_pending_asks = (
                            [
                                k
                                for k in _PENDING_ASK_REQUESTS
                                if _REQUEST_TO_SESSION.get(k) == pre_sid
                            ]
                            if pre_sid
                            else []
                        )
                        # #799: an unarmed tick is a no-op heartbeat — every
                        # session, every 30 s, for the whole turn (a quarter
                        # of nsd's journal on rc12). DEBUG, unless it carries
                        # the #696 "waiting on the user" signal, which stays
                        # greppable at INFO.
                        tick_log = (
                            run_logger.info
                            if pre_pending_requests or pre_pending_asks
                            else run_logger.debug
                        )
                        tick_log(
                            "claude.post_result_idle.tick",
                            session_id=pre_sid,
                            armed=False,
                            elapsed_s=None,
                            effective_timeout_s=None,
                            dead_wakeup=False,
                            pending_requests=len(pre_pending_requests),
                            pending_asks=len(pre_pending_asks),
                            would_close=False,
                            last_bg_bash_launched_at_age_s=None,
                            last_schedule_wakeup_arm_delay=(
                                state.last_schedule_wakeup_arm_delay
                            ),
                        )
                        # #592: a run that never produces its first result
                        # was previously unbounded — nothing owned the
                        # "alive-but-silent forever" case.
                        killed = await self._maybe_cancel_pre_result_silence(
                            state=state,
                            stream=stream,
                            proc=proc,
                            run_logger=run_logger,
                            silence_timeout_s=pre_result_silence_timeout_s,
                            started_at=watchdog_started_at,
                        )
                        if killed:
                            exit_reason = "pre_result_silence_cancelled"
                            return
                        continue
                    if state.live_mode:
                        # #776: after a result a live session belongs to
                        # `_live_session_lifecycle`; this watchdog keeps only
                        # the pre-result silence cap above.
                        continue
                    elapsed = time.monotonic() - armed_at

                    # #507: dead-ScheduleWakeup shortcut. ScheduleWakeup
                    # outside ``/loop dynamic mode`` is a silent no-op
                    # upstream — the wakeup never fires, the agent's turn
                    # ended, and we'd otherwise wait the full
                    # ``timeout_s`` (default 600 s) before closing stdin.
                    # Detect the case via the scalar
                    # ``state.last_schedule_wakeup_arm_delay`` (a
                    # per-turn high-water-mark that survives
                    # ``_clear_background_handle``, #544) and the /loop
                    # master toggle for this chat; cut the effective
                    # timeout to ``max_armed_delay + 60s grace`` so the
                    # session closes within ~delay+grace instead of 10
                    # minutes.
                    effective_timeout = timeout_s
                    dead_wakeup = False
                    if state.last_schedule_wakeup_arm_delay is not None:
                        from ..utils.paths import get_run_channel_id

                        _chat_id = get_run_channel_id()
                        if _chat_id is not None and not _loop_enabled_for_chat(
                            _chat_id
                        ):
                            _max_delay = state.last_schedule_wakeup_arm_delay
                            effective_timeout = min(timeout_s, _max_delay + 60.0)
                            dead_wakeup = True

                    # Locate the session id for the approval-state guard.
                    # The Claude factory's resume token is set during the
                    # very first StartedEvent, so by the time a result
                    # lands we always have one — but defend against the
                    # rare race where the watchdog ticks before that
                    # first started event.
                    sid = (
                        state.factory.resume.value
                        if state.factory.resume is not None
                        else None
                    )
                    pending_requests = (
                        [k for k, v in _REQUEST_TO_SESSION.items() if v == sid]
                        if sid
                        else []
                    )
                    pending_asks = (
                        [
                            k
                            for k in _PENDING_ASK_REQUESTS
                            if _REQUEST_TO_SESSION.get(k) == sid
                        ]
                        if sid
                        else []
                    )
                    bg_bash_age = (
                        round(time.monotonic() - state.last_bg_bash_launched_at, 1)
                        if state.last_bg_bash_launched_at is not None
                        else None
                    )

                    # #333 tick log. ``would_close`` answers "if we
                    # weren't deferring, would this tick close stdin?" —
                    # useful for spotting cases where the timer is
                    # repeatedly re-armed.
                    would_close = elapsed >= effective_timeout and not (
                        pending_requests or pending_asks
                    )
                    run_logger.info(
                        "claude.post_result_idle.tick",
                        session_id=sid,
                        armed=True,
                        elapsed_s=round(elapsed, 1),
                        effective_timeout_s=round(effective_timeout, 1),
                        dead_wakeup=dead_wakeup,
                        pending_requests=len(pending_requests),
                        pending_asks=len(pending_asks),
                        would_close=would_close,
                        last_bg_bash_launched_at_age_s=bg_bash_age,
                        last_schedule_wakeup_arm_delay=(
                            state.last_schedule_wakeup_arm_delay
                        ),
                    )

                    if elapsed < effective_timeout:
                        continue
                    if pending_requests or pending_asks:
                        run_logger.info(
                            "claude.post_result_idle.deferred",
                            session_id=sid,
                            pending_requests=len(pending_requests),
                            pending_asks=len(pending_asks),
                            elapsed_s=round(elapsed, 1),
                            timeout_s=timeout_s,
                        )
                        # Re-arm: push the deadline forward by one full
                        # interval.
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
                    # #470: stamp closed-at signals BEFORE the actual
                    # stdin close so the bridge's heartbeat tick (which
                    # polls engine_state via duck-typing) can fire the
                    # one-shot closing Telegram message.
                    # ``post_result_closing_sent`` stays False — the
                    # bridge sets it after the message is sent
                    # (idempotency).
                    state.post_result_closed_at = time.monotonic()
                    state.post_result_idle_minutes = elapsed / 60.0
                    with contextlib.suppress(Exception):
                        await this_proc_stdin.aclose()
                    exit_reason = "stdin_closed"
                    return
                except (anyio.get_cancelled_exc_class(), KeyboardInterrupt):
                    # Cancellation must propagate so the task group can
                    # tear down cleanly. The outer ``finally`` still
                    # fires and tags ``exit_reason="cancelled"``.
                    exit_reason = "cancelled"
                    raise
                except Exception as e:  # noqa: BLE001
                    # Tick-local failure: log + back off one interval to
                    # avoid hot-looping on a persistent fault. The loop
                    # continues — we MUST NOT let an unhandled exception
                    # bubble into the task group and cancel the sibling
                    # JSONL reader.
                    run_logger.warning(
                        "claude.post_result_idle.tick_error",
                        session_id=(
                            state.factory.resume.value
                            if state.factory.resume is not None
                            else None
                        ),
                        error=str(e),
                        error_type=e.__class__.__name__,
                        exc_info=True,
                    )
                    await anyio.sleep(poll_interval)
        finally:
            run_logger.info(
                "claude.post_result_idle.task_exited",
                session_id=(
                    state.factory.resume.value
                    if state.factory.resume is not None
                    else None
                ),
                reason=exit_reason,
            )

    async def _post_result_subcountdown(
        self,
        *,
        state: ClaudeStreamState,
        proc: Any,
        run_logger: Any,
        timeout_s: float,
        stream: Any = None,
        session_id: str | None = None,
        limbo_grace_s: float | None = None,
        bg_max_hold_s: float | None = None,
    ) -> str:
        """Watch a stdout-closed-but-process-alive subprocess (#333 Tier 1).

        Entered when ``reader_done`` fires while ``proc.returncode is None`` —
        the JSONL reader exhausted but the subprocess is still alive (Claude
        Code v2.1.143 sometimes closes stdout without exiting). We wait up
        to ``timeout_s`` for the subprocess to exit naturally; if it doesn't,
        SIGTERM the process group, wait 5 s, then SIGKILL. Returns the
        ``task_exited`` reason for the caller to record.

        Tier 3: 30 s into the subcountdown, if the subprocess is still
        alive and no real pending state references the session, emit
        ``runner.limbo_detected`` warning. ``untether-issue-watcher``
        picks this up automatically.

        #647/#646: the deadline is liveness-aware — when it expires while the
        session still has live background work (upstream runs subagents in
        the background by default since Claude Code v2.1.198, and their
        completion is NOT signalled on stream-json) and the process tree is
        not demonstrably idle, the SIGTERM is deferred and re-checked each
        poll, bounded by ``bg_max_hold_s``. Killing live subagent work
        quarantines the session (#632) and produces fresh-session amnesia.

        #650: every ~30 s of the countdown emits a
        ``claude.post_result_idle.subcountdown_tick`` INFO — previously the
        loop logged nothing between the one-shot ``limbo_detected`` and its
        exit, a blackout window in which the bridge's independent stall
        detector raced this loop's returncode poll and mislabelled a normal
        post-result exit as ``process_dead``.
        """
        from ..utils.proc_diag import (
            collect_proc_diag,
            is_cpu_active,
            is_tree_cpu_active,
        )

        reader_done_at = time.monotonic()
        run_logger.info(
            "claude.post_result_idle.reader_done_but_alive",
            session_id=session_id,
            pid=proc.pid,
            elapsed_since_result_s=(
                round(time.monotonic() - state.result_received_at, 1)
                if state.result_received_at is not None
                else None
            ),
            timeout_s=timeout_s,
        )
        if stream is not None:
            self._transition_lifecycle(
                stream, "reader_eof", run_logger, pid=proc.pid, session_id=session_id
            )
            self._transition_lifecycle(
                stream,
                "subcountdown",
                run_logger,
                pid=proc.pid,
                session_id=session_id,
                timeout_s=timeout_s,
            )

        # Poll loop: tick every ``_subcountdown_poll_interval_s`` up to
        # ``timeout_s``, exit early if the subprocess dies naturally or
        # pending state appears (in which case we re-arm and stay alive —
        # the user is still interacting).
        limbo_logged = False
        deadline = reader_done_at + timeout_s
        # #591: when nothing references the session — no pending control/ask
        # requests and no live background work — the full ``timeout_s`` wait
        # only lets MCP children hold the process (and its RSS/TCP) open.
        # Cap the wait at ``limbo_grace_s`` in that case; a grace of 0/None
        # disables the shortcut. The cap re-arms alongside ``deadline``
        # whenever pending state appears so a just-answered approval still
        # gets a fresh grace window.
        grace = (
            limbo_grace_s
            if limbo_grace_s is not None
            else self._post_result_limbo_grace_s
        )
        grace_deadline = reader_done_at + grace if grace > 0 else None
        bg_hold = (
            bg_max_hold_s
            if bg_max_hold_s is not None
            else self._post_result_bg_max_hold_s
        )
        prev_diag = None
        ceiling_extended_logged = False
        last_tick_log_at = reader_done_at
        # #699: the liveness verdict below is recomputed every
        # ``_subcountdown_poll_interval_s`` (5 s) but was only ever surfaced by
        # the ~30 s throttled ``subcountdown_tick`` or the one-shot
        # ``limbo_detected``. A healthy fast reap clears the loop before either
        # fires, so a short subcountdown evaluated ``cpu_active`` several times
        # and discarded every one — six passes of production traffic on nsd
        # produced zero samples, which blocked #689's Linux-collateral
        # verification. These carry the last computed verdict out to the
        # terminal ``subcountdown_exit`` line in the ``finally`` below.
        cpu_active: bool | None = None
        tree_active: bool | None = None
        live_bg = False
        limbo_grace_applied = False
        poll_count = 0
        # Overwritten immediately before each ``return``; the initial value is
        # the one that survives when the enclosing scope is cancelled.
        exit_reason_log = "cancelled"
        try:
            while True:
                await anyio.sleep(self._subcountdown_poll_interval_s)
                poll_count += 1
                if proc.returncode is not None:
                    if stream is not None:
                        self._transition_lifecycle(
                            stream,
                            "exited",
                            run_logger,
                            pid=proc.pid,
                            session_id=session_id,
                            rc=proc.returncode,
                        )
                    exit_reason_log = "subprocess_exited"
                    return "subprocess_exited_during_subcountdown"

                sid = (
                    state.factory.resume.value
                    if state.factory.resume is not None
                    else None
                )
                pending_requests = (
                    [k for k, v in _REQUEST_TO_SESSION.items() if v == sid]
                    if sid
                    else []
                )
                pending_asks = (
                    [
                        k
                        for k in _PENDING_ASK_REQUESTS
                        if _REQUEST_TO_SESSION.get(k) == sid
                    ]
                    if sid
                    else []
                )
                if pending_requests or pending_asks:
                    # User is mid-interaction — re-arm the deadline so the
                    # subcountdown doesn't fire while a control_response is
                    # in flight. Match _post_result_idle_watchdog's deferred
                    # re-arm semantics (line 2843).
                    run_logger.info(
                        "claude.post_result_idle.subcountdown_deferred",
                        session_id=sid,
                        pid=proc.pid,
                        pending_requests=len(pending_requests),
                        pending_asks=len(pending_asks),
                    )
                    deadline = time.monotonic() + timeout_s
                    if grace_deadline is not None:
                        grace_deadline = time.monotonic() + grace
                    continue

                elapsed = time.monotonic() - reader_done_at
                # #650: per-poll liveness snapshot — feeds the limbo warning, the
                # ~30 s ``subcountdown_tick`` observability line, and the
                # #647/#646 liveness-aware ceiling below.
                diag = collect_proc_diag(proc.pid)
                cpu_active = (
                    is_cpu_active(prev_diag, diag) if prev_diag and diag else None
                )
                tree_active = (
                    is_tree_cpu_active(prev_diag, diag) if prev_diag and diag else None
                )
                prev_diag = diag
                live_bg = has_live_background_work(state)

                # Tier 3: limbo detection — a one-shot warning surfacing the
                # condition for triage. ``untether-issue-watcher`` files this
                # automatically on the next sweep.
                if (
                    not limbo_logged
                    and elapsed >= self._subcountdown_limbo_detect_threshold_s
                ):
                    limbo_logged = True
                    # #590: refresh the orphan snapshot — children spawned AFTER
                    # the reader-done snapshot (the sl "late leaker" shape) are
                    # captured here so the post-exit sweep can reach them. Use the
                    # recursive walk (find_descendants) rather than diag.child_pids,
                    # which is DIRECT children only and misses the npx→node
                    # grandchild that actually leaks.
                    _capture_orphan_descendants(state, source="limbo", pid=proc.pid)
                    # #653: the level reflects the assessed state, not the
                    # transition into it. Live background work — or a
                    # demonstrably busy process tree — lingering after the
                    # result is healthy, expected behaviour under the
                    # liveness-aware ceiling (#646/#647): INFO. WARNING is
                    # reserved for limbo with no evidence of work, the
                    # genuinely-stuck case the warning was written for.
                    limbo_log = (
                        run_logger.info
                        if (live_bg or cpu_active is True or tree_active is True)
                        else run_logger.warning
                    )
                    limbo_log(
                        "runner.limbo_detected",
                        engine="claude",
                        pid=proc.pid,
                        session_id=sid,
                        seconds_since_reader_done=round(elapsed, 1),
                        seconds_since_last_result=(
                            round(time.monotonic() - state.result_received_at, 1)
                            if state.result_received_at is not None
                            else None
                        ),
                        live_background_work=live_bg,
                        cpu_active=cpu_active,
                        tree_active=tree_active,
                        mcp_child_pids=list(diag.child_pids) if diag else [],
                        rss_kb=diag.rss_kb if diag else None,
                        tcp_total=diag.tcp_total if diag else None,
                    )
                    if stream is not None:
                        self._transition_lifecycle(
                            stream,
                            "limbo",
                            run_logger,
                            pid=proc.pid,
                            session_id=sid,
                            seconds_since_reader_done=round(elapsed, 1),
                        )

                # #591: cap the deadline at the limbo grace when the session is
                # fully quiescent — no live background work means nothing can
                # legitimately produce output any more (pending requests/asks
                # were handled above via the re-arm branch).
                #
                # #655: `not live_bg` alone is NOT quiescence — it only means no
                # *registered* background handle. A process can be busy with
                # direct work (waiting on a build, a slow MCP call, a poll loop)
                # with live_bg False. Consult the same liveness signals the
                # extension gate below uses, so a demonstrably-busy process falls
                # through to the full ``timeout_s``. Tri-state: both signals are
                # None on the first poll (no prev_diag); `is True` keeps unknown
                # liveness from blocking the grace cap, preserving #591's fast
                # reap of genuinely quiescent husks.
                demonstrably_busy = cpu_active is True or tree_active is True
                effective_deadline = deadline
                limbo_grace_applied = False
                if (
                    grace_deadline is not None
                    and grace_deadline < deadline
                    and not live_bg
                    and not demonstrably_busy
                ):
                    effective_deadline = grace_deadline
                    limbo_grace_applied = True

                # #650 (defect 3): per-tick observability, throttled to ~30 s.
                # Previously nothing logged between the one-shot limbo warning
                # and the loop's exit — a blackout in which the bridge's stall
                # detector raced this loop's returncode poll and won.
                now_mono = time.monotonic()
                if (
                    now_mono - last_tick_log_at
                    >= self._subcountdown_tick_log_interval_s
                ):
                    last_tick_log_at = now_mono
                    run_logger.info(
                        "claude.post_result_idle.subcountdown_tick",
                        session_id=sid,
                        pid=proc.pid,
                        elapsed_s=round(elapsed, 1),
                        in_limbo=limbo_logged,
                        live_background_work=live_bg,
                        cpu_active=cpu_active,
                        tree_active=tree_active,
                        child_count=len(diag.child_pids) if diag else None,
                        rss_kb=diag.rss_kb if diag else None,
                        deadline_remaining_s=round(effective_deadline - now_mono, 1),
                    )

                if time.monotonic() >= effective_deadline:
                    # #647/#646: liveness-aware ceiling. Upstream runs subagents
                    # in the background by default (Claude Code ≥2.1.198) and
                    # never signals their completion on stream-json, so the only
                    # evidence is /proc. If background handles are still live and
                    # the process tree is not demonstrably idle, defer the
                    # SIGTERM — killing live subagent work quarantines the
                    # session (#632) and produces fresh-session amnesia. Bounded
                    # twice over: handles age out at BG_AGENT_MAX_KEEP_S, and the
                    # hold never exceeds ``bg_hold`` seconds past reader-EOF.
                    demonstrably_idle = (
                        diag is not None
                        and diag.alive
                        and cpu_active is False
                        and tree_active is False
                    )
                    if (
                        bg_hold > 0
                        and elapsed < bg_hold
                        and live_bg
                        and not demonstrably_idle
                    ):
                        if not ceiling_extended_logged:
                            ceiling_extended_logged = True
                            run_logger.info(
                                "claude.post_result_idle.ceiling_extended",
                                session_id=sid,
                                pid=proc.pid,
                                elapsed_s=round(elapsed, 1),
                                timeout_s=timeout_s,
                                bg_max_hold_s=bg_hold,
                                cpu_active=cpu_active,
                                tree_active=tree_active,
                                child_count=len(diag.child_pids) if diag else None,
                            )
                        continue
                    # #632 (W2): the process already emitted a valid result but
                    # is being force-killed while lingering (MCP children / hung
                    # background work) — its last upstream turn may be left
                    # dangling on the far side, making the session unsafe to
                    # resume. Record the marker BEFORE sending SIGTERM. A store
                    # failure must never block teardown, hence the narrow
                    # try/except. SIGKILL (below) always follows this same
                    # branch on the same pass, so recording once here is
                    # sufficient — no second record site needed at sigkill.
                    quarantined = False
                    if (
                        stream is not None
                        and stream.did_emit_completed
                        and sid is not None
                        and _load_quarantine_on_forced_teardown()
                    ):
                        try:
                            get_quarantine_store().quarantine(
                                self.engine, sid, reason="forced_teardown_after_result"
                            )
                            quarantined = True
                        except Exception:  # noqa: BLE001 — never let a
                            # quarantine store failure break subprocess teardown.
                            run_logger.debug(
                                "session.quarantine_record_failed", exc_info=True
                            )
                    # Timeout: SIGTERM the process group (start_new_session=True
                    # so PID == pgid). 5 s grace, then SIGKILL.
                    run_logger.warning(
                        "claude.post_result_idle.sigterm_after_timeout",
                        session_id=sid,
                        pid=proc.pid,
                        timeout_s=timeout_s,
                        elapsed_s=round(elapsed, 1),
                        limbo_grace_applied=limbo_grace_applied,
                        limbo_grace_s=grace if limbo_grace_applied else None,
                        quarantined=quarantined,
                        # #647: "background work was correctly tracked and killed
                        # anyway" is identified by live_background_work=True here,
                        # regardless of which resume-divert label lands later.
                        live_background_work=live_bg,
                        bg_hold_extended=ceiling_extended_logged,
                        bg_max_hold_s=bg_hold,
                        cpu_active=cpu_active,
                        tree_active=tree_active,
                    )
                    if stream is not None:
                        self._transition_lifecycle(
                            stream,
                            "sigterm_sent",
                            run_logger,
                            pid=proc.pid,
                            session_id=sid,
                        )
                        # #631 (W5-diag): record on the stream itself so the
                        # runner.empty_result diagnostic (in the bridge, on a
                        # SUBSEQUENT message) can see that a forced teardown
                        # happened during this run.
                        stream.sigterm_sent = True
                    # #590: descendant-aware — bare killpg missed MCP chains
                    # that re-parented into separate sessions/pgroups.
                    signal_pid_group(proc.pid, signal.SIGTERM)
                    # Give MCP children configured grace to clean up.
                    grace_deadline = (
                        time.monotonic() + self._subcountdown_sigterm_grace_s
                    )
                    while time.monotonic() < grace_deadline:
                        await anyio.sleep(self._subcountdown_sigterm_grace_poll_s)
                        if proc.returncode is not None:
                            if stream is not None:
                                self._transition_lifecycle(
                                    stream,
                                    "exited",
                                    run_logger,
                                    pid=proc.pid,
                                    session_id=sid,
                                    rc=proc.returncode,
                                )
                            exit_reason_log = "timeout_sigterm_reaped"
                            return "reader_done_but_alive_timeout"
                    # Still alive — SIGKILL the group.
                    run_logger.warning(
                        "claude.post_result_idle.sigkill_after_grace",
                        session_id=sid,
                        pid=proc.pid,
                    )
                    if stream is not None:
                        self._transition_lifecycle(
                            stream,
                            "sigkill_sent",
                            run_logger,
                            pid=proc.pid,
                            session_id=sid,
                        )
                    signal_pid_group(proc.pid, signal.SIGKILL)
                    exit_reason_log = "timeout_sigkill"
                    return "reader_done_but_alive_timeout"
        finally:
            # #699: exactly ONE liveness line per subcountdown, at exit rather
            # than on the 30 s cadence — so a 15 s reap is observable for the
            # first time. Strictly less volume than the tick on any long
            # countdown, and it does not disturb #650's blackout fix (that tick
            # stays; this is a terminal line). ``session_id`` rather than the
            # loop-local ``sid`` because the fast path can return before ``sid``
            # is ever assigned.
            run_logger.info(
                "claude.post_result_idle.subcountdown_exit",
                session_id=session_id,
                pid=proc.pid,
                elapsed_s=round(time.monotonic() - reader_done_at, 1),
                polls=poll_count,
                cpu_active=cpu_active,
                tree_active=tree_active,
                live_background_work=live_bg,
                in_limbo=limbo_logged,
                limbo_grace_applied=limbo_grace_applied,
                exit_reason=exit_reason_log,
            )

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
            # #816: never deregister a newer process that owns the session.
            _cleanup_session_registries(session_id, owner_state=state)

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
        stderr_lines: list[str] | None = None,
    ) -> list[UntetherEvent]:
        # Phase 2: Cleanup runner registration
        session_id = (
            found_session.value if found_session else (resume.value if resume else None)
        )
        if session_id:
            # #816: never deregister a newer process that owns the session.
            _cleanup_session_registries(session_id, owner_state=state)

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
        # #838: the #350 RAM guard and #589 concurrency ceiling. This override
        # never calls the base run_impl, so the guard must run here — first,
        # before start_run registers the session in _ACTIVE_RUNNERS, the #812
        # probe thread starts or a PTY opens; a block registers nothing.
        # Live follow-ups / steers inject into an existing process and never
        # reach run_impl, so they are never checked.
        block_result = self._check_prespawn_ram_guard(resume)
        if block_result is not None:
            yield block_result
            return

        state = self.new_state(prompt, resume)
        self.start_run(prompt, resume, state=state)

        tag = self.tag()
        run_logger = self.get_logger()
        if self._effective_permission_mode() is not None:
            # #812: warm the cached `--help` probe off the event loop so the
            # (synchronous) build_args below only reads the cache.
            with contextlib.suppress(Exception):
                await anyio.to_thread.run_sync(
                    cli_supports_hook_events, self.claude_cmd
                )
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
                # #590: sweep leaked MCP children after exit; the snapshot
                # list is filled at reader-done / limbo time (escapees).
                reap_orphans=self._reap_orphans,
                orphan_pid_snapshot=state.orphan_pid_snapshot,
                orphan_pid_starttimes=state.orphan_pid_starttimes,
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
                # #593/#510: hand the bridge THIS run's pid + stream through
                # the per-run handle. ``last_pid`` / ``current_stream`` stay
                # as diagnostics-only attributes — the runner instance is
                # shared across chats, so they describe the latest spawn in
                # ANY chat and must never feed per-run monitoring.
                self.last_pid = proc.pid
                self.current_stream = stream
                publish_run_stream(stream, proc.pid)
                reader_done = anyio.Event()

                # #333: load post-result idle settings before the task group
                # so the watchdog gets a snapshot. A load failure leaves the
                # legacy "stay alive forever" behaviour in place.
                post_result_idle_enabled = True
                post_result_idle_timeout_s = 600.0
                post_result_limbo_grace_s = self._post_result_limbo_grace_s
                pre_result_silence_timeout_s = 3600.0
                post_result_bg_max_hold_s = self._post_result_bg_max_hold_s
                live_sessions_enabled = True
                live_session_max_s = 14400.0
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
                        post_result_limbo_grace_s = float(
                            settings_obj.watchdog.post_result_limbo_grace
                        )
                        pre_result_silence_timeout_s = float(
                            settings_obj.watchdog.pre_result_silence_timeout
                        )
                        post_result_bg_max_hold_s = float(
                            settings_obj.watchdog.post_result_bg_max_hold
                        )
                        live_sessions_enabled = bool(
                            settings_obj.watchdog.live_sessions
                        )
                        live_session_max_s = float(
                            settings_obj.watchdog.live_session_max_s
                        )
                        # #383: read per spawn like live_sessions.
                        state.rearm_plan_mode = bool(
                            getattr(settings_obj.watchdog, "rearm_plan_mode", True)
                        )
                        # #812: read per spawn like live_sessions.
                        state.hold_for_async_hooks = bool(
                            getattr(settings_obj.watchdog, "hold_for_async_hooks", True)
                        )
                        state.async_hook_max_hold_s = float(
                            getattr(settings_obj.watchdog, "async_hook_max_hold", 630.0)
                        )
                        # #829: read per spawn like its siblings (D2).
                        state.bg_hold_rearm_on_progress = bool(
                            getattr(
                                settings_obj.watchdog,
                                "bg_hold_rearm_on_progress",
                                True,
                            )
                        )
                        # #872: read per spawn like its siblings.
                        state.bg_hold_declared_waits = bool(
                            getattr(
                                settings_obj.watchdog, "bg_hold_declared_waits", True
                            )
                        )
                except Exception:  # noqa: BLE001 — settings errors must not block a run
                    run_logger.debug(
                        "post_result_idle.settings_load_failed", exc_info=True
                    )

                # #776: live-session model — keep reading after the result so
                # background-wake / follow-up turns reach the bridge.
                if use_control_channel and live_sessions_enabled:
                    state.live_mode = True
                    stream.followup_turns = True
                    state.spawn_run_options = get_run_options()
                state.live_session_max_s = live_session_max_s
                state.bg_max_hold_s = post_result_bg_max_hold_s

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
                            proc,
                            stream,
                            post_result_limbo_grace_s,
                            pre_result_silence_timeout_s,
                            post_result_bg_max_hold_s,
                        )
                    if state.live_mode:
                        tg.start_soon(
                            functools.partial(
                                self._live_session_lifecycle,
                                state=state,
                                reader_done=reader_done,
                                run_logger=run_logger,
                                proc=proc,
                                stream=stream,
                                idle_grace_s=post_result_limbo_grace_s,
                                max_hold_s=post_result_bg_max_hold_s,
                                abs_cap_s=live_session_max_s,
                            )
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
                    # #590: refresh the descendant snapshot while the
                    # subprocess is still alive — after exit, /proc children
                    # links are gone and pgroup escapees become invisible.
                    # The sweep in manage_subprocess reads this list at
                    # teardown. This is a refresh: the result-event capture
                    # already seeded the snapshot for fast clean runs, so the
                    # returncode guard here is no longer fatal.
                    if proc.returncode is None:
                        _capture_orphan_descendants(
                            state, source="reader_done", pid=proc.pid
                        )
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
                    # #776: a live session's reader only ends at process exit
                    # (EOF, or the post-exit drain closed stdout), so there is
                    # no linger left for the post-result watchdog to police —
                    # don't let the run wait out its next poll tick.
                    if state.live_mode:
                        tg.cancel_scope.cancel()

                rc = await proc.wait()
                # #640: mirror the base runner (runner.py:1362). ClaudeRunner
                # overrides run_impl wholesale, and this assignment was missing
                # — so `stream.proc_returncode` stayed None for every Claude
                # run and `_is_signal_death(None)` in the bridge's
                # auto-continue gate always returned False. The death-spiral
                # guard #589 relied on was therefore inert for the ONLY engine
                # auto-continue applies to (nsd fleet logs: 2 auto-continues
                # fired straight after a rc=143 SIGTERM exit).
                stream.proc_returncode = rc
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
            # #667: stream.proc_returncode is assigned only on the happy path
            # (after `rc = await proc.wait()` in the try body). Cancellation
            # (/cancel, /new, drain), an exception in the task group / JSONL
            # reader, or the early pipes RuntimeError all skip that assignment,
            # leaving it None — so _is_signal_death(None) stays False and the
            # bridge's auto-continue death-spiral guard (#640) is inert on
            # exactly those paths. manage_subprocess.__aexit__ has already run
            # its shielded, bounded terminate+reap by the time this finally
            # executes (utils/subprocess.py), so proc.returncode is populated;
            # capture it here with no extra wait. Guarded because BOTH `proc`
            # (unbound if manage_subprocess raised in __aenter__) and `stream`
            # (assigned inside the manage_subprocess block, so unbound if we
            # exit before then) can be absent — the sibling `stream.found_session`
            # access below guards the same way.
            with contextlib.suppress(NameError, AttributeError):
                if (
                    stream.proc_returncode is None
                    and proc is not None
                    and proc.returncode is not None
                ):
                    stream.proc_returncode = proc.returncode
            # Clean up global registries on ANY exit (cancel, error, normal).
            # process_error_events/stream_end_events handle normal paths but
            # cancellation skips both, leaving stale outline_guard/cooldown state.
            # #816: a /continue token is ``ResumeToken(value="",
            # is_continue=True)`` — truthy but carrying no session id. The
            # real id only arrives with the CLI's init, so resolve it from
            # found_session; keying on "" skipped the cleanup entirely and
            # left the session looking alive to every later resume.
            _sid = (
                resume.value
                if resume is not None and not resume.is_continue and resume.value
                else None
            )
            if not _sid:
                try:
                    if stream.found_session is not None:
                        _sid = stream.found_session.value
                except (NameError, AttributeError):
                    pass
            if _sid:
                try:
                    _cleanup_session_registries(_sid, owner_state=state)
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


_DSP_WARNED = False


def _warn_dangerously_skip_permissions(config_path: Path) -> None:
    """#209 D6: the explicit opt-in key silently overrides every mode; say so once."""
    global _DSP_WARNED
    if _DSP_WARNED:
        return
    _DSP_WARNED = True
    logger.warning(
        "claude.config.dangerously_skip_permissions",
        config_path=str(config_path),
        note=(
            "--dangerously-skip-permissions overrides permission_mode and every"
            " /planmode choice; no Telegram approvals will be shown"
        ),
    )


def _validate_permission_mode(value: object, config_path: Path) -> str | None:
    """Validate ``[engines.claude] permission_mode`` at config-load time (#742).

    Until 0.35.5rc8 this key was read raw, so a typo passed parse and then
    killed the run at subprocess spawn with a CLI usage error — the exact
    failure the cron-side validator exists to prevent.  Both paths now share
    ``VALID_PERMISSION_MODES_BY_ENGINE["claude"]``.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            f"Invalid `claude.permission_mode` in {config_path}; expected a"
            " non-empty string."
        )
    mode = value.strip()
    allowed = VALID_PERMISSION_MODES_BY_ENGINE["claude"]
    if mode not in allowed:
        raise ConfigError(
            f"Unknown `claude.permission_mode` {mode!r} in {config_path};"
            f" allowed values: {sorted(allowed)}."
        )
    # #741 `auto` used to mean "plan mode + rubber-stamp the plan gate"; it
    # now passes through to the CLI's own auto mode. TOML is hand-authored,
    # so it is never rewritten. The operator WARN moved to the config audit
    # (#751, `untether.permission_audit`), which covers crons and reloads too.
    return mode


def build_runner(config: EngineConfig, config_path: Path) -> Runner:
    claude_cmd = shutil.which("claude") or "claude"

    model = config.get("model")
    # #749 remember which branch this came from: an explicit user choice
    # survives into prompting modes, the inherited default does not.
    allowed_tools_explicit = "allowed_tools" in config
    if allowed_tools_explicit:
        allowed_tools = config.get("allowed_tools")
    else:
        allowed_tools = DEFAULT_ALLOWED_TOOLS
    dangerously_skip_permissions = config.get("dangerously_skip_permissions") is True
    use_api_billing = config.get("use_api_billing") is True
    permission_mode = _validate_permission_mode(
        config.get("permission_mode"), config_path
    )
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

    blocked = find_blocked_claude_args(extra_args)
    if blocked:
        # Flag names only — never the values (#209).
        logger.warning(
            "claude.config.invalid",
            error="blocked extra_args flag",
            flags=[hit.flag for hit in blocked],
            categories=[hit.category for hit in blocked],
            config_path=str(config_path),
        )
        raise BlockedExtraArgsError(format_blocked("claude", config_path, blocked))

    if dangerously_skip_permissions:
        _warn_dangerously_skip_permissions(config_path)

    return ClaudeRunner(
        claude_cmd=claude_cmd,
        model=model,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
        allowed_tools_explicit=allowed_tools_explicit,
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


class ControlLookup(NamedTuple):
    """Result of classifying a control request id (#685)."""

    status: ControlRequestStatus
    prior: HandledControl | None = None
    reason: str | None = None  # NOT_FOUND detail: unknown | channel_mismatch


@dataclass(frozen=True, slots=True)
class ControlSendResult:
    """Outcome of :func:`respond_to_control_request` (#685).

    ``status`` is ``PENDING`` when the request was ours to answer (``sent``
    says whether the response line was actually written). ``prior`` is set
    for ``IN_FLIGHT`` / ``ALREADY_HANDLED`` / ``CANCELLED``. ``reason``:
    unknown | channel_mismatch | no_active_session | write_failed.
    """

    status: ControlRequestStatus
    sent: bool
    session_id: str | None = None
    prior: HandledControl | None = None
    reason: str | None = None
    # #822: the tool the request was for (logged on claude_control.sent).
    tool_name: str | None = None


def mark_request_handled(
    request_id: str,
    *,
    action: str | None = None,
    outcome: str = "answered",
    channel_id: int | None = None,
) -> None:
    """Record *request_id* as resolved so the reconcile loop can retire it.

    The reconcile loop in ``translate`` emits ``action_completed`` for every
    handled request it can resolve to an action id, which is what clears the
    stale inline keyboard (#229) and, since #683, the synthetic
    ``claude.discuss_approve.N`` action from the Pause & Outline hold-open path.

    #685: the record says *how* (``action`` / ``outcome``) and *where*
    (``channel_id``, defaulting to the run's chat) it was resolved, so a later
    tap on the same button can say what actually happened. A ``cancelled`` or
    ``expired`` record is terminal: a later "answered" never overwrites it.

    #197: LRU-evict oldest entries instead of clear()-ing the whole set.
    """
    existing = _HANDLED_REQUESTS.get(request_id)
    if (
        existing is not None
        and existing.outcome in _TERMINAL_CONTROL_OUTCOMES
        and outcome == "answered"
    ):
        logger.debug(
            "control_response.terminal_kept",
            request_id=request_id,
            kept_outcome=existing.outcome,
            kept_action=existing.action,
            ignored_action=action,
        )
        return
    if channel_id is None:
        channel_id = get_run_channel_id()
    _HANDLED_REQUESTS[request_id] = HandledControl(
        action=action, outcome=outcome, channel_id=channel_id, at=time.monotonic()
    )
    _HANDLED_REQUESTS.move_to_end(request_id)
    while len(_HANDLED_REQUESTS) > _HANDLED_REQUESTS_MAX:
        _HANDLED_REQUESTS.popitem(last=False)


def _bind_request_channel(request_id: str) -> None:
    """Bind a just-registered request to the run's chat (#388).

    Called right after every ``_REQUEST_TO_SESSION[...] =`` registration
    (pinned by a structural test). No run chat (tests, legacy) → unbound,
    which keeps the pre-#388 behaviour for that id.
    """
    channel = get_run_channel_id()
    _REQUEST_TO_CHANNEL.pop(request_id, None)
    if channel is None:
        return
    _REQUEST_TO_CHANNEL[request_id] = channel
    if len(_REQUEST_TO_CHANNEL) > _REQUEST_TO_CHANNEL_MAX:
        # Liveness pruning: drop entries no longer pending or claimed; a
        # live binding is never evicted.
        for rid in [
            rid
            for rid in _REQUEST_TO_CHANNEL
            if rid not in _REQUEST_TO_SESSION and rid not in _INFLIGHT_CONTROL_RESPONSES
        ]:
            del _REQUEST_TO_CHANNEL[rid]


def control_request_origin(request_id: str) -> int | None:
    """The chat a pending / claimed request is bound to (#388), or None."""
    if request_id in _REQUEST_TO_SESSION or request_id in _INFLIGHT_CONTROL_RESPONSES:
        return _REQUEST_TO_CHANNEL.get(request_id)
    return None


def classify_control_request(
    request_id: str, *, channel_id: int | None = None
) -> ControlLookup:
    """Classify *request_id* for a tap from *channel_id* (#685). Pure and sync.

    Order: an in-flight claim → ``IN_FLIGHT``; registered → ``PENDING``; a
    handled record → ``CANCELLED`` / ``ALREADY_HANDLED``; else ``NOT_FOUND``.
    A handled record from another chat reads ``NOT_FOUND`` (reason
    ``channel_mismatch``) — one chat can't read another's resolution (#715).

    #388: checked first, a pending or in-flight request bound to another chat
    also reads ``NOT_FOUND`` (``channel_mismatch``) — a forged callback from
    a different chat can't answer it, and learns nothing (not even "being
    handled"). ``channel_id=None`` (internal callers) skips the check.
    """
    if channel_id is not None:
        origin = control_request_origin(request_id)
        if origin is not None and origin != channel_id:
            return ControlLookup(
                ControlRequestStatus.NOT_FOUND, reason="channel_mismatch"
            )
    claim = _INFLIGHT_CONTROL_RESPONSES.get(request_id)
    if claim is not None:
        return ControlLookup(
            ControlRequestStatus.IN_FLIGHT,
            HandledControl(
                action=claim.action,
                outcome="answered",
                channel_id=claim.channel_id,
                at=claim.at,
            ),
        )
    if request_id in _REQUEST_TO_SESSION:
        if channel_id is not None and request_id not in _REQUEST_TO_CHANNEL:
            # #388: a registration site that forgot _bind_request_channel
            # (or a run without a chat) would silently reopen the gap.
            logger.debug(
                "claude_control.origin_unbound",
                request_id=request_id,
                channel_id=channel_id,
            )
        return ControlLookup(ControlRequestStatus.PENDING)
    if request_id in _HANDLED_REQUESTS:
        record = _HANDLED_REQUESTS[request_id]
        if (
            record is not None
            and record.channel_id is not None
            and channel_id is not None
            and record.channel_id != channel_id
        ):
            return ControlLookup(
                ControlRequestStatus.NOT_FOUND, reason="channel_mismatch"
            )
        if record is not None and record.outcome == "cancelled":
            return ControlLookup(ControlRequestStatus.CANCELLED, record)
        return ControlLookup(ControlRequestStatus.ALREADY_HANDLED, record)
    return ControlLookup(ControlRequestStatus.NOT_FOUND, reason="unknown")


def claim_control_request(
    request_id: str,
    *,
    action: str,
    owner: str | None,
    channel_id: int | None = None,
) -> ControlLookup:
    """Classify *request_id* and, if it is ``PENDING``, reserve it (#685).

    Synchronous — nothing is awaited between the check and the reservation,
    so two concurrent taps serialise on the event loop: the first gets
    ``PENDING`` and holds the claim, the second sees ``IN_FLIGHT``. A caller
    that already holds the claim (same ``owner``) gets ``PENDING`` again.
    ``owner=None`` only classifies.
    """
    existing = _INFLIGHT_CONTROL_RESPONSES.get(request_id)
    if existing is not None and owner is not None and existing.owner == owner:
        if request_id in _REQUEST_TO_SESSION:
            return ControlLookup(ControlRequestStatus.PENDING)
        # The request vanished under our claim (session cleanup) — drop it
        # and report what the registries say now.
        del _INFLIGHT_CONTROL_RESPONSES[request_id]
    lookup = classify_control_request(request_id, channel_id=channel_id)
    if lookup.status is ControlRequestStatus.PENDING and owner is not None:
        _INFLIGHT_CONTROL_RESPONSES[request_id] = InflightClaim(
            action=action, owner=owner, channel_id=channel_id, at=time.monotonic()
        )
    return lookup


def release_control_claims(owner: str | None) -> None:
    """Drop every in-flight claim *owner* still holds (idempotent, #685)."""
    if owner is None:
        return
    for rid in [
        rid for rid, c in _INFLIGHT_CONTROL_RESPONSES.items() if c.owner == owner
    ]:
        del _INFLIGHT_CONTROL_RESPONSES[rid]


def new_control_claim_owner() -> str:
    """A unique claim owner for a caller without a callback query id (#685)."""
    return f"direct:{next(_DIRECT_CLAIM_SEQ)}"


async def respond_to_control_request(
    request_id: str,
    approved: bool,
    *,
    action: str,
    channel_id: int | None = None,
    deny_message: str | None = None,
    rejects_plan: bool = True,
    claim_owner: str | None = None,
) -> ControlSendResult:
    """Answer a control request, reporting what actually happened (#685).

    Writes **nothing** unless the request is ``PENDING`` and this caller holds
    (or can take) its in-flight claim. ``claim_owner`` is the callback query id
    whose early toast already reserved the claim; without it a unique owner
    is used. The claim is released in ``finally``.

    Args:
        request_id: The control request ID
        approved: Whether to approve (True) or deny (False) the request
        action: What the tap did (approve / deny / discuss / chat / auto …),
            recorded so a later tap can say it
        channel_id: The chat the tap came from (scopes the handled record)
        deny_message: Custom denial message (used when approved=False)
        rejects_plan: For an ExitPlanMode denial, whether it rejects the plan
            (❌ Deny) or is procedural (Pause & Outline / Let's discuss) — #793
        claim_owner: The dispatcher's callback query id, if it reserved the
            claim before the early answer
    """
    owner = claim_owner if claim_owner is not None else new_control_claim_owner()
    lookup = claim_control_request(
        request_id, action=action, owner=owner, channel_id=channel_id
    )
    if lookup.status is not ControlRequestStatus.PENDING:
        if lookup.status is ControlRequestStatus.NOT_FOUND:
            logger.warning(
                "control_response.request_not_found",
                request_id=request_id,
                reason=lookup.reason,
            )
        else:
            # Duplicate callback (a double tap, or Telegram long-polling
            # delivering the same update twice) — nothing is written.
            logger.debug(
                "control_response.already_handled",
                request_id=request_id,
                status=str(lookup.status),
            )
        return ControlSendResult(
            status=lookup.status,
            sent=False,
            prior=lookup.prior,
            reason=lookup.reason,
        )
    # #822: read before write_control_response pops it.
    tool_name = _REQUEST_TO_TOOL_NAME.get(request_id)
    try:
        session_id = _REQUEST_TO_SESSION[request_id]
        if session_id not in _ACTIVE_RUNNERS:
            logger.warning(
                "control_response.no_active_session",
                session_id=session_id,
                request_id=request_id,
            )
            # Clean up stale mappings
            _REQUEST_TO_SESSION.pop(request_id, None)
            _REQUEST_TO_INPUT.pop(request_id, None)
            _REQUEST_TO_TOOL_NAME.pop(request_id, None)
            return ControlSendResult(
                status=ControlRequestStatus.PENDING,
                sent=False,
                session_id=session_id,
                reason="no_active_session",
                tool_name=tool_name,
            )

        runner, _ = _ACTIVE_RUNNERS[session_id]
        success = await runner.write_control_response(
            request_id, approved, deny_message=deny_message, rejects_plan=rejects_plan
        )

        # Clean up the mapping after use. ``pop``, not ``del``: the claim
        # makes a concurrent delete impossible, but it is cheap insurance.
        _REQUEST_TO_SESSION.pop(request_id, None)
        if request_id in _CANCELLED_DURING_WRITE:
            # #684: the CLI withdrew the request while we were writing; it
            # ignores our answer, so say so rather than "approved".
            mark_request_handled(
                request_id,
                action="cancelled",
                outcome="cancelled",
                channel_id=channel_id,
            )
            return ControlSendResult(
                status=ControlRequestStatus.CANCELLED,
                sent=success,
                session_id=session_id,
                prior=_HANDLED_REQUESTS.get(request_id),
                tool_name=tool_name,
            )
        # A written *or* attempted write marks it handled (a closed pipe
        # means the session is gone either way).
        mark_request_handled(request_id, action=action, channel_id=channel_id)
        return ControlSendResult(
            status=ControlRequestStatus.PENDING,
            sent=success,
            session_id=session_id,
            reason=None if success else "write_failed",
            tool_name=tool_name,
        )
    finally:
        _CANCELLED_DURING_WRITE.discard(request_id)
        claim = _INFLIGHT_CONTROL_RESPONSES.get(request_id)
        if claim is not None and claim.owner == owner:
            del _INFLIGHT_CONTROL_RESPONSES[request_id]


# Phase 2: Public API for sending control responses
async def send_claude_control_response(
    request_id: str,
    approved: bool,
    *,
    deny_message: str | None = None,
    rejects_plan: bool = True,
    action: str | None = None,
) -> bool:
    """Send a control response to an active Claude Code session.

    Thin bool wrapper over :func:`respond_to_control_request`, kept for the
    AskUserQuestion callers (#685). A caller that needs to tell *sent* from
    *already handled* from *not found* must call that function instead.
    ``action`` is recorded on the handled record (default: approve / deny).

    Returns:
        True if the response was written, or the request was already answered
        (in flight / handled — a benign duplicate). False if it is unknown,
        its session is gone, the write failed, or the CLI withdrew it
        (cancelled: a text reply to a withdrawn question must fall through to
        a normal prompt, not be swallowed).
    """
    result = await respond_to_control_request(
        request_id,
        approved,
        action=action or ("approve" if approved else "deny"),
        deny_message=deny_message,
        rejects_plan=rejects_plan,
    )
    if result.status is ControlRequestStatus.CANCELLED:
        return False  # #684: even when written — the CLI ignored it
    return result.sent or result.status in (
        ControlRequestStatus.IN_FLIGHT,
        ControlRequestStatus.ALREADY_HANDLED,
    )


def mark_outline_pending(session_id: str) -> None:
    """Record that 'Pause & Outline Plan' was clicked for this session.

    Subsequent ExitPlanMode requests are gated on visible outline text
    (``_OUTLINE_MIN_CHARS``): auto-denied with the write-the-outline-first
    instruction until enough text is written, then held open behind the
    synthetic Approve/Deny buttons.

    #570: replaces ``set_discuss_cooldown`` — the time-based progressive
    cooldown it also armed was a v2.1.72-74 upstream-loop workaround,
    removed after verifying the fix on CLI 2.1.215.
    """
    _OUTLINE_PENDING.add(session_id)
    logger.info("outline_pending.set", session_id=session_id)


def _cleanup_session_registries(
    session_id: str, *, owner_state: ClaudeStreamState | None = None
) -> None:
    """Clean up all global registries for a session.

    Called from run_impl finally (covers cancel), process_error_events,
    and stream_end_events. All operations are idempotent.

    ``owner_state`` (#816 hardening): the finishing run's stream state. When
    given and the session is registered to a DIFFERENT run's state — a newer
    process now owns the same session id — the cleanup is skipped, so a late
    finally can't deregister (or strip pending approvals from) a live owner.
    """
    owner = _SESSION_BG_STATE.get(session_id)
    if owner_state is not None and owner is not None and owner is not owner_state:
        logger.info(
            "claude_runner.session_cleanup_skipped",
            session_id=session_id,
            reason="owned_by_other_run",
        )
        return
    cleaned: list[str] = []
    if _ACTIVE_RUNNERS.pop(session_id, None) is not None:
        cleaned.append("active_runners")
    if _SESSION_STDIN.pop(session_id, None) is not None:
        cleaned.append("session_stdin")
    if _SESSION_BG_STATE.pop(session_id, None) is not None:
        cleaned.append("session_bg_state")
    if _LIVE_SESSIONS.pop(session_id, None) is not None:
        cleaned.append("live_session")
    if session_id in _DISCUSS_APPROVED:
        cleaned.append("discuss_approved")
    _DISCUSS_APPROVED.discard(session_id)
    _DISCUSS_CARRY.discard(session_id)
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
        _REQUEST_TO_CHANNEL.pop(k, None)  # #388
        # #685: a claim on a request whose session is gone can never
        # complete — drop it so the id doesn't read "in flight" for ever.
        _INFLIGHT_CONTROL_RESPONSES.pop(k, None)
        _CANCELLED_DURING_WRITE.discard(k)  # #684
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
        request_id, approved=False, deny_message=deny_message, action="answer"
    )


def _record_answered_ask_flow(
    request_id: str, channel_id: int, *, outcome: str = "answered"
) -> None:
    """Remember that *request_id*'s AskUserQuestion flow was answered (#698)
    — or withdrawn by the CLI (``outcome="cancelled"``, #684)."""
    now = time.monotonic()
    for rid, (_ch, ts, _outcome) in list(_ANSWERED_ASK_FLOWS.items()):
        if now - ts > ANSWERED_ASK_FLOW_TTL_S:
            del _ANSWERED_ASK_FLOWS[rid]
    _ANSWERED_ASK_FLOWS[request_id] = (channel_id, now, outcome)
    while len(_ANSWERED_ASK_FLOWS) > _ANSWERED_ASK_FLOWS_MAX:
        _ANSWERED_ASK_FLOWS.pop(next(iter(_ANSWERED_ASK_FLOWS)))


def recently_answered_ask_flow(channel_id: int | None = None) -> str | None:
    """Return the request_id of a just-answered AskUserQuestion flow, or None.

    #698: lets the callback handler tell a late tap on an already-answered
    keyboard ("Already answered", INFO) apart from a genuinely unexplained
    missing flow (WARNING). Scoped by channel so one chat's late tap cannot
    claim another chat's answer.
    """
    resolved = recently_resolved_ask_flow(channel_id)
    return resolved[0] if resolved is not None else None


def recently_resolved_ask_flow(
    channel_id: int | None = None,
) -> tuple[str, str] | None:
    """Like :func:`recently_answered_ask_flow`, with the outcome (#684).

    Returns ``(request_id, outcome)`` — ``answered`` or ``cancelled`` (the
    CLI withdrew the question) — or None.
    """
    now = time.monotonic()
    for rid, (ch, ts, outcome) in list(_ANSWERED_ASK_FLOWS.items()):
        if now - ts > ANSWERED_ASK_FLOW_TTL_S:
            del _ANSWERED_ASK_FLOWS[rid]
            continue
        if channel_id is not None and ch != channel_id:
            continue
        return rid, outcome
    return None


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
    _record_answered_ask_flow(request_id, flow.channel_id)

    # Update the stored input to include answers
    stored_input = _REQUEST_TO_INPUT.get(request_id)
    if stored_input is not None:
        stored_input["answers"] = flow.answers

    return await send_claude_control_response(
        request_id, approved=True, action="answer"
    )


def format_question_message(
    flow: AskQuestionState, *, escape_html: bool = False
) -> str:
    """Format the current question in a flow as a display string.

    The question text is agent-authored free text and routinely contains
    angle brackets — a question about an inline ``<svg>``, a generic like
    ``list<T>``, a shell redirect. It is consumed under two different and
    incompatible rendering contracts:

    * ``escape_html=True`` — the caller builds a ``RenderedMessage`` carrying
      ``parse_mode="HTML"``. Telegram parses the whole body as HTML and
      accepts only a small tag whitelist, so an unescaped ``<svg>`` fails the
      **entire** request with ``400 Bad Request: can't parse entities:
      Unsupported start tag``. Because the ask-flow messages carry the option
      keyboard, losing that edit leaves the run unanswerable from Telegram
      (#713 — same class as the #199 fix in ``commands/auth.py``).
    * the default, ``escape_html=False`` — the caller stores the string as a
      progress action title (``advance_ask_action_model``, #709), which is
      rendered via ``render_markdown`` where markdown-it (``html: False``)
      already neutralises tags. Escaping here as well would double-escape and
      show the user a literal ``&lt;svg&gt;``.

    Only the agent's text is escaped; the bot-authored ``❓ Question N of M:``
    prefix contains no HTML-special characters. ``quote=False`` because
    quotation marks are legal in HTML text content — escaping them would
    surface a literal ``&quot;`` for a merely quoted question.
    """
    q = flow.questions[flow.current_index]
    question_text = q.get("question", "")
    if escape_html:
        question_text = html.escape(question_text, quote=False)
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
