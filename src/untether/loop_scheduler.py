"""Untether-side scheduler for /loop and ScheduleWakeup (#289, #925, #926).

The #289 premise — "Claude Code's session-scoped scheduler dies when the
``claude --print`` subprocess exits" (CLI 2.1.129, Probe 1 in
`docs/plans/2026-05-06-289-loop-and-cron-interception.md`) — no longer
holds: live sessions (#776) keep the process and its headless scheduler
running between turns, and CLI 2.1.289 resurrects session-only CronCreate
jobs on ``--resume`` and fires them at once
(`docs/findings/2026-10-04-claude-session-cron-resume-and-host-controls.md`
F1-F4).

So in Loop-mode chats Untether **owns** the schedule (#925): an SDK
PreToolUse hook callback (:mod:`untether.runners.claude`) declines Claude's
``CronCreate`` and registers it here instead, so the CLI never holds a job
it could fire uncapped or resurrect. Each due iteration spawns
``claude --resume <session_id>`` with the original prompt re-issued as a
fresh user turn — closing a clean-idle live session first — and the
``[loop]`` caps apply to every iteration. ``ScheduleWakeup`` entries (waits
longer than ``inline_threshold_seconds``) fire here only once the session
has gone; a live session fires its own wake-up (``cli_fired_live``).

Residual CLI jobs (``-p`` chats, ``[loop] own_schedule = false``, pre-rc20
state) are tracked as *cron suppression* records (#926): a later
control-channel spawn resuming that session gets
``CLAUDE_CODE_DISABLE_CRON=1`` until the CLI's own 7-day resurrect window
has passed.

State is persisted to ``active_loops.json`` (sibling to the config file)
via :func:`untether.utils.json_state.atomic_write_json` so loops survive
Untether restarts.

Loop mode is off by default — opt-in per chat via ``/config → 🔁 Loop
mode``.
"""

from __future__ import annotations

import datetime
import json
import secrets
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import anyio
import anyio.lowlevel
from anyio.abc import TaskGroup

from .context import RunContext
from .logging import get_logger
from .model import EngineId, ResumeToken
from .transport import ChannelId, RenderedMessage, SendOptions, Transport
from .triggers.cron import cron_matches
from .utils.json_state import atomic_write_json

logger = get_logger(__name__)

__all__ = [
    "CLI_CRON_MAX_AGE_S",
    "STATE_FILENAME",
    "LoopSchedulerError",
    "active_count",
    "bind_upstream_id",
    "cancel_by_token",
    "cancel_by_upstream_id",
    "cancel_pending_for_chat",
    "clear_cron_suppressed",
    "cron_suppressed_until",
    "entry_summary",
    "install",
    "is_do_not_resume",
    "mark_cron_suppressed",
    "mark_do_not_resume",
    "next_fire_for_session",
    "own_schedule_enabled",
    "pending_for_chat",
    "register_pending_cron",
    "register_pending_wakeup",
    "token_for_tool_use",
    "uninstall",
]

STATE_FILENAME = "active_loops.json"

# #926: the CLI resurrects a session-only CronCreate job on ``--resume`` for
# as long as it is younger than ``recurringMaxAgeMs`` (default 604800000 ms
# = 7 days; drift-probed in tests/test_claude_cli_schema_drift.py). Remote
# config can change it — ``0`` means no age limit — which is a documented
# residual: suppression records still lapse after this window.
CLI_CRON_MAX_AGE_S = 7 * 86_400

LoopKind = Literal["cron", "wakeup"]
RunJobFn = Callable[..., Awaitable[None]]
IsChatBusyFn = Callable[[int], bool]


@dataclass(slots=True)
class _LoopEntry:
    token: str
    upstream_cron_id: str | None
    tool_use_id: str
    chat_id: int
    thread_id: int | None
    kind: LoopKind
    cron_expression: str | None
    delay_seconds: float | None
    recurring: bool
    prompt: str
    fallback_first_user_message: str | None
    fire_at_monotonic: float
    fire_at_wallclock: float
    iteration_count: int
    max_iterations: int
    max_total_duration_hours: int
    created_at_wallclock: float
    expires_at_wallclock: float
    context: RunContext | None
    engine_override: EngineId | None
    resume_token: str
    # Concurrency: generation increments on every re-arm so older _arm_timer
    # tasks (still sleeping from a previous round) can detect they are stale
    # and bail out instead of double-firing.  cancel_event is set when the
    # entry is cancelled or re-armed so a pending _arm_timer can interrupt
    # its sleep promptly.
    generation: int = 0
    cancel_event: anyio.Event = field(default_factory=anyio.Event)
    cancelled: bool = False
    fired: bool = False


# Module globals — mirror at_scheduler.py shape so install/uninstall feels
# the same to readers familiar with that module.
_TASK_GROUP: TaskGroup | None = None
_RUN_JOB: RunJobFn | None = None
_TRANSPORT: Transport | None = None
_DEFAULT_CHAT_ID: int | None = None
_STATE_PATH: Path | None = None
_IS_CHAT_BUSY: IsChatBusyFn | None = None

_PENDING_BY_TOKEN: dict[str, _LoopEntry] = {}
_PENDING_BY_CHAT: dict[int, set[str]] = defaultdict(set)
_PENDING_BY_TOOL_USE_ID: dict[str, str] = {}
_PENDING_BY_UPSTREAM_ID: dict[str, str] = {}

# The do-not-resume sentinel (issue #289 design doc §5c), scoped per loop
# since #926: session → wall-clock time of its latest cancel. ``_fire``
# refuses to spawn an entry created at or before that time, so a loop
# registered *after* a cancel in the same session fires normally. Records
# older than ``CLI_CRON_MAX_AGE_S`` are pruned. Persisted alongside the
# entries (``do_not_resume`` stays a list for rc19 readers).
_DO_NOT_RESUME_AT: dict[str, float] = {}

# #926: sessions that may still hold a CLI-side cron job the CLI would
# resurrect on ``--resume`` — session → {upstream job id → until}. While a
# record is unexpired, a control-channel spawn resuming the session gets
# ``CLAUDE_CODE_DISABLE_CRON=1`` (``runners/claude.py``). ``"native"`` and
# ``"legacy"`` are placeholder ids (real ids are 8 hex chars) for jobs whose
# id Untether never saw; those clear only by expiry.
_CRON_SUPPRESSED: dict[str, dict[str, float]] = {}


class LoopSchedulerError(Exception):
    """Raised when scheduling a loop cannot proceed."""


def install(
    task_group: TaskGroup,
    run_job: RunJobFn,
    transport: Transport,
    default_chat_id: int,
    *,
    state_path: Path | None = None,
    is_chat_busy: IsChatBusyFn | None = None,
) -> None:
    """Register the task group, ``run_job`` closure, and persistence path.

    Called from :func:`untether.telegram.loop.run_main_loop` once the task
    group is open and ``run_job`` has been defined.  ``state_path`` should
    be ``config_path.with_name(STATE_FILENAME)`` so loop state lives next
    to ``last_update_id.json`` and ``active_progress.json``.  Passing
    ``None`` disables persistence (used in tests).

    ``is_chat_busy`` is an optional callable used by :func:`_fire` to
    drop iterations when a previous loop fire (or any other run) is still
    running for the same chat.  Mirrors upstream's "no catch-up" semantic.
    """
    global _TASK_GROUP, _RUN_JOB, _TRANSPORT, _DEFAULT_CHAT_ID
    global _STATE_PATH, _IS_CHAT_BUSY
    _TASK_GROUP = task_group
    _RUN_JOB = run_job
    _TRANSPORT = transport
    _DEFAULT_CHAT_ID = int(default_chat_id)
    _STATE_PATH = state_path
    _IS_CHAT_BUSY = is_chat_busy
    if state_path is not None:
        _restore_from_disk(state_path)
    logger.info(
        "loop.installed",
        default_chat_id=default_chat_id,
        state_path=str(state_path) if state_path else None,
        restored=len(_PENDING_BY_TOKEN),
    )


def uninstall() -> None:
    """Clear installed references — tests and graceful shutdown use this."""
    global _TASK_GROUP, _RUN_JOB, _TRANSPORT, _DEFAULT_CHAT_ID
    global _STATE_PATH, _IS_CHAT_BUSY
    _TASK_GROUP = None
    _RUN_JOB = None
    _TRANSPORT = None
    _DEFAULT_CHAT_ID = None
    _STATE_PATH = None
    _IS_CHAT_BUSY = None
    _PENDING_BY_TOKEN.clear()
    _PENDING_BY_CHAT.clear()
    _PENDING_BY_TOOL_USE_ID.clear()
    _PENDING_BY_UPSTREAM_ID.clear()
    _DO_NOT_RESUME_AT.clear()
    _CRON_SUPPRESSED.clear()


# ── Registration ────────────────────────────────────────────────────────


def register_pending_cron(
    *,
    session_id: str,
    tool_use_id: str,
    cron_expression: str,
    prompt: str,
    recurring: bool,
    chat_id: int,
    thread_id: int | None = None,
    fallback_first_user_message: str | None = None,
    context: RunContext | None = None,
    engine_override: EngineId | None = None,
    max_iterations: int = 20,
    max_total_duration_hours: int = 4,
    expiry_days: int = 7,
) -> str:
    """Register a recurring (or one-shot) cron observed in the JSONL stream.

    Returns the Untether-side token (``ut_loop_<8hex>``).  The upstream
    cron ID arrives later in the matching tool_result and is bound via
    :func:`bind_upstream_id`.
    """
    if _TASK_GROUP is None or _RUN_JOB is None:
        raise LoopSchedulerError("loop_scheduler not installed")
    fire_at_monotonic = _next_cron_fire(cron_expression)
    if fire_at_monotonic is None:
        raise LoopSchedulerError(f"invalid cron expression: {cron_expression!r}")
    now_monotonic = time.monotonic()
    now_wallclock = time.time()
    fire_at_wallclock = now_wallclock + (fire_at_monotonic - now_monotonic)
    expires_at = now_wallclock + (expiry_days * 86_400)
    return _register(
        kind="cron",
        session_id=session_id,
        tool_use_id=tool_use_id,
        cron_expression=cron_expression,
        delay_seconds=None,
        prompt=prompt,
        recurring=recurring,
        chat_id=chat_id,
        thread_id=thread_id,
        fallback_first_user_message=fallback_first_user_message,
        context=context,
        engine_override=engine_override,
        fire_at_monotonic=fire_at_monotonic,
        fire_at_wallclock=fire_at_wallclock,
        expires_at_wallclock=expires_at,
        max_iterations=max_iterations,
        max_total_duration_hours=max_total_duration_hours,
    )


def register_pending_wakeup(
    *,
    session_id: str,
    tool_use_id: str,
    delay_seconds: float,
    prompt: str,
    chat_id: int,
    thread_id: int | None = None,
    fallback_first_user_message: str | None = None,
    context: RunContext | None = None,
    engine_override: EngineId | None = None,
    max_iterations: int = 20,
    max_total_duration_hours: int = 4,
    expiry_days: int = 7,
) -> str:
    """Register a one-shot wakeup observed in the JSONL stream.

    ScheduleWakeup is one-shot from the runtime's perspective — Claude
    self-paces by calling it again from each woken turn.  We treat each
    observation as a fresh entry with ``recurring=False``.
    """
    if _TASK_GROUP is None or _RUN_JOB is None:
        raise LoopSchedulerError("loop_scheduler not installed")
    if delay_seconds <= 0:
        raise LoopSchedulerError(f"delay must be positive, got {delay_seconds!r}")
    now_monotonic = time.monotonic()
    now_wallclock = time.time()
    fire_at_monotonic = now_monotonic + float(delay_seconds)
    fire_at_wallclock = now_wallclock + float(delay_seconds)
    expires_at = now_wallclock + (expiry_days * 86_400)
    return _register(
        kind="wakeup",
        session_id=session_id,
        tool_use_id=tool_use_id,
        cron_expression=None,
        delay_seconds=float(delay_seconds),
        prompt=prompt,
        recurring=False,
        chat_id=chat_id,
        thread_id=thread_id,
        fallback_first_user_message=fallback_first_user_message,
        context=context,
        engine_override=engine_override,
        fire_at_monotonic=fire_at_monotonic,
        fire_at_wallclock=fire_at_wallclock,
        expires_at_wallclock=expires_at,
        max_iterations=max_iterations,
        max_total_duration_hours=max_total_duration_hours,
    )


def _register(
    *,
    kind: LoopKind,
    session_id: str,
    tool_use_id: str,
    cron_expression: str | None,
    delay_seconds: float | None,
    prompt: str,
    recurring: bool,
    chat_id: int,
    thread_id: int | None,
    fallback_first_user_message: str | None,
    context: RunContext | None,
    engine_override: EngineId | None,
    fire_at_monotonic: float,
    fire_at_wallclock: float,
    expires_at_wallclock: float,
    max_iterations: int,
    max_total_duration_hours: int,
) -> str:
    """Shared body for ``register_pending_cron`` / ``register_pending_wakeup``.

    #925: idempotent per ``tool_use_id`` — the PreToolUse hook callback and
    the assistant ``tool_use`` observer can both see one CronCreate, in
    either order (G4), and must share one entry.
    """
    assert _TASK_GROUP is not None  # caller guards
    existing = token_for_tool_use(tool_use_id)
    if existing is not None:
        logger.debug(
            "loop.register_duplicate",
            token=existing,
            tool_use_id=tool_use_id,
            kind=kind,
        )
        return existing
    token = f"ut_loop_{secrets.token_hex(4)}"
    trigger_source = f"loop:{token}"
    if context is None:
        context = RunContext(trigger_source=trigger_source)
    else:
        context = replace(context, trigger_source=trigger_source)
    entry = _LoopEntry(
        token=token,
        upstream_cron_id=None,
        tool_use_id=tool_use_id,
        chat_id=chat_id,
        thread_id=thread_id,
        kind=kind,
        cron_expression=cron_expression,
        delay_seconds=delay_seconds,
        recurring=recurring,
        prompt=prompt,
        fallback_first_user_message=fallback_first_user_message,
        fire_at_monotonic=fire_at_monotonic,
        fire_at_wallclock=fire_at_wallclock,
        iteration_count=0,
        max_iterations=max_iterations,
        max_total_duration_hours=max_total_duration_hours,
        created_at_wallclock=time.time(),
        expires_at_wallclock=expires_at_wallclock,
        context=context,
        engine_override=engine_override,
        resume_token=session_id,
    )
    _PENDING_BY_TOKEN[token] = entry
    _PENDING_BY_CHAT[chat_id].add(token)
    _PENDING_BY_TOOL_USE_ID[tool_use_id] = token
    _persist()
    _TASK_GROUP.start_soon(_arm_timer, token, entry.generation)
    logger.info(
        "loop.scheduled",
        token=token,
        kind=kind,
        chat_id=chat_id,
        session=session_id,
        cron_expression=cron_expression,
        delay_seconds=delay_seconds,
        recurring=recurring,
        fire_at_wallclock=fire_at_wallclock,
    )
    return token


def token_for_tool_use(tool_use_id: str) -> str | None:
    """#925: the live (not cancelled) entry registered for ``tool_use_id``."""
    token = _PENDING_BY_TOOL_USE_ID.get(tool_use_id)
    if token is None:
        return None
    entry = _PENDING_BY_TOKEN.get(token)
    if entry is None or entry.cancelled:
        return None
    return token


def entry_summary(token: str) -> dict[str, Any] | None:
    """#925: what the hook's model-facing deny reason says about a loop."""
    entry = _PENDING_BY_TOKEN.get(token)
    if entry is None:
        return None
    return {
        "token": entry.token,
        "kind": entry.kind,
        "cron_expression": entry.cron_expression,
        "recurring": entry.recurring,
        "prompt": entry.prompt,
        "max_iterations": entry.max_iterations,
        "max_total_duration_hours": entry.max_total_duration_hours,
    }


def own_schedule_enabled() -> bool:
    """#925: ``[loop] own_schedule`` (the kill switch), read live so it
    hot-reloads. Falls back to the default (True) when settings can't load —
    a config error must not silently hand schedules back to the CLI."""
    try:
        from .settings import load_settings_if_exists

        result = load_settings_if_exists()
        if result is None:
            return True
        settings, _ = result
        return bool(settings.loop.own_schedule)
    except Exception:  # noqa: BLE001
        return True


def bind_upstream_id(tool_use_id: str, upstream_id: str) -> None:
    """Bind the upstream 8-char cron ID to a previously-registered entry.

    Called from the tool_result decode site after parsing the result text
    via the ``\\bjob ([0-9a-f]{8})\\b`` regex.  No-op if no matching entry
    (e.g. registration was rejected or the master toggle was off).

    #925 D-E: a bound id means the CLI *accepted* the job (a ``-p`` chat, a
    spawn without the hook), so it would resurrect on ``--resume``; the
    session is marked cron-suppressed until the job ages out. Not with
    ``[loop] own_schedule = false`` (§13 amendment 7: rc19 behaviour).
    """
    token = _PENDING_BY_TOOL_USE_ID.get(tool_use_id)
    if token is None:
        return
    entry = _PENDING_BY_TOKEN.get(token)
    if entry is None:
        return
    entry.upstream_cron_id = upstream_id
    _PENDING_BY_UPSTREAM_ID[upstream_id] = token
    if entry.kind == "cron" and own_schedule_enabled():
        _mark_cron_suppressed(
            entry.resume_token,
            upstream_id,
            until=entry.created_at_wallclock + CLI_CRON_MAX_AGE_S,
            source="cli_accepted",
        )
    _persist()


# ── Cancellation ────────────────────────────────────────────────────────


def cancel_by_token(token: str, *, reason: str = "user_cancel") -> bool:
    """Cancel a single loop by its Untether-side token.  Returns ``True``
    if a matching pending entry was cancelled, ``False`` otherwise.

    ``reason``: ``user_cancel`` (``/cancel``, ``/new``) or ``cron_delete``
    (Claude's CronDelete of the loop, #925).

    #926: the do-not-resume sentinel is per loop (cancel time), and an entry
    the CLI had accepted (``upstream_cron_id``) leaves its session
    cron-suppressed, so the cancelled job can't come back on ``--resume``.
    """
    entry = _PENDING_BY_TOKEN.get(token)
    if entry is None or entry.cancelled:
        return False
    entry.cancelled = True
    entry.cancel_event.set()
    _drop_indexes(entry)
    _DO_NOT_RESUME_AT[entry.resume_token] = time.time()
    if entry.upstream_cron_id is not None and own_schedule_enabled():
        _mark_cron_suppressed(
            entry.resume_token,
            entry.upstream_cron_id,
            until=entry.created_at_wallclock + CLI_CRON_MAX_AGE_S,
            source="cancel",
        )
    _persist()
    logger.info(
        "loop.cancelled",
        token=token,
        chat_id=entry.chat_id,
        session=entry.resume_token,
        reason=reason,
        iterations_completed=entry.iteration_count,
    )
    return True


def cancel_by_upstream_id(upstream_id: str) -> bool:
    """Cancel a loop by its upstream 8-char cron ID (CronDelete observed).

    #926: the native CronDelete leaves the transcript marker the CLI's
    resume scan honours, so that id no longer needs suppressing. Cleared
    **after** the cancel, which marks it (§13 amendment 1)."""
    token = _PENDING_BY_UPSTREAM_ID.get(upstream_id)
    if token is None:
        return False
    entry = _PENDING_BY_TOKEN.get(token)
    session_id = entry.resume_token if entry is not None else None
    cancelled = cancel_by_token(token, reason="cron_delete")
    if session_id is not None:
        clear_cron_suppressed(session_id, upstream_id)
    return cancelled


def cancel_pending_for_chat(
    chat_id: int,
    *,
    thread_filter: Callable[[int | None], bool] | None = None,
) -> int:
    """Cancel pending loops for ``chat_id``.  Returns count cancelled.

    #826: ``thread_filter`` (when given) limits the cancel to entries whose
    ``thread_id`` it accepts — a forum topic's ``/new`` / ``/cancel`` leaves
    other topics' loops alone.  ``None`` = the whole chat.
    """
    cancelled = 0
    for token in list(_PENDING_BY_CHAT.get(chat_id, ())):
        if thread_filter is not None:
            entry = _PENDING_BY_TOKEN.get(token)
            if entry is None or not thread_filter(entry.thread_id):
                continue
        if cancel_by_token(token):
            cancelled += 1
    if cancelled:
        logger.info(
            "loop.cancelled_for_chat",
            chat_id=chat_id,
            count=cancelled,
            scoped=thread_filter is not None,
        )
    return cancelled


def _drop_indexes(entry: _LoopEntry) -> None:
    """Remove an entry from all secondary indexes (idempotent)."""
    _PENDING_BY_TOKEN.pop(entry.token, None)
    chat_set = _PENDING_BY_CHAT.get(entry.chat_id)
    if chat_set is not None:
        chat_set.discard(entry.token)
        if not chat_set:
            _PENDING_BY_CHAT.pop(entry.chat_id, None)
    _PENDING_BY_TOOL_USE_ID.pop(entry.tool_use_id, None)
    if entry.upstream_cron_id is not None:
        _PENDING_BY_UPSTREAM_ID.pop(entry.upstream_cron_id, None)


# ── Inspection ──────────────────────────────────────────────────────────


def active_count() -> int:
    """Return the number of pending (non-cancelled, non-fired) loops."""
    return sum(1 for e in _PENDING_BY_TOKEN.values() if not e.cancelled and not e.fired)


def pending_for_chat(chat_id: int) -> list[_LoopEntry]:
    """Return a snapshot of pending loop entries for ``chat_id``."""
    tokens = _PENDING_BY_CHAT.get(chat_id, ())
    return [
        _PENDING_BY_TOKEN[t]
        for t in tokens
        if t in _PENDING_BY_TOKEN and not _PENDING_BY_TOKEN[t].cancelled
    ]


def next_fire_for_session(session_id: str) -> float | None:
    """Return the soonest ``fire_at_monotonic`` for ``session_id``, or
    ``None`` if no pending loop targets that session.

    Used by :mod:`untether.markdown` to render an ``⏰ next iter in Xm Ys``
    footer line after the subprocess has exited.
    """
    candidates = [
        e.fire_at_monotonic
        for e in _PENDING_BY_TOKEN.values()
        if e.resume_token == session_id and not e.cancelled
    ]
    if not candidates:
        return None
    return min(candidates)


def is_do_not_resume(session_id: str) -> bool:
    """Return ``True`` if ``session_id`` has a do-not-resume sentinel set.

    Since #926 the sentinel only blocks loops created at or before the
    cancel (:func:`_blocked_by_cancel`).  ``/continue`` is a separate
    user-initiated action and does NOT consult it (handover default).
    """
    return session_id in _DO_NOT_RESUME_AT


def mark_do_not_resume(session_id: str) -> None:
    """Mark ``session_id`` as do-not-resume (now).  Idempotent.  Persisted."""
    if session_id in _DO_NOT_RESUME_AT:
        return
    _DO_NOT_RESUME_AT[session_id] = time.time()
    _persist()


def _blocked_by_cancel(entry: _LoopEntry) -> bool:
    """#926: the entry predates a cancel in its session."""
    cancelled_at = _DO_NOT_RESUME_AT.get(entry.resume_token)
    return cancelled_at is not None and entry.created_at_wallclock <= cancelled_at


# ── Cron suppression (#926) ─────────────────────────────────────────────


def _mark_cron_suppressed(
    session_id: str, upstream_id: str, *, until: float, source: str
) -> bool:
    """Record without persisting (restore batches its write)."""
    if not session_id or until <= time.time():
        return False
    ids = _CRON_SUPPRESSED.setdefault(session_id, {})
    previous = ids.get(upstream_id)
    if previous is not None and previous >= until:
        return False
    ids[upstream_id] = until
    logger.info(
        "loop.cron_suppressed_marked",
        session=session_id,
        upstream_id=upstream_id,
        until=until,
        source=source,
    )
    return True


def mark_cron_suppressed(
    session_id: str, upstream_id: str, *, until: float, source: str
) -> bool:
    """#926: ``session_id`` may hold CLI job ``upstream_id`` until ``until``
    (wall clock). Later control-channel spawns resuming it run with
    ``CLAUDE_CODE_DISABLE_CRON=1``. Keeps the later ``until`` when marked
    twice. Returns whether anything changed. Persisted."""
    changed = _mark_cron_suppressed(session_id, upstream_id, until=until, source=source)
    if changed:
        _persist()
    return changed


def clear_cron_suppressed(session_id: str, upstream_id: str) -> bool:
    """#926: drop one job id (a native CronDelete of it was observed)."""
    ids = _CRON_SUPPRESSED.get(session_id)
    if not ids or upstream_id not in ids:
        return False
    del ids[upstream_id]
    if not ids:
        _CRON_SUPPRESSED.pop(session_id, None)
    _persist()
    logger.info(
        "loop.cron_suppressed_cleared", session=session_id, upstream_id=upstream_id
    )
    return True


def cron_suppressed_until(session_id: str) -> float | None:
    """#926: the latest unexpired suppression for ``session_id`` (wall
    clock), or None. Expired ids are pruned."""
    ids = _CRON_SUPPRESSED.get(session_id)
    if not ids:
        return None
    now = time.time()
    for upstream_id in [k for k, until in ids.items() if until <= now]:
        del ids[upstream_id]
    if not ids:
        _CRON_SUPPRESSED.pop(session_id, None)
        return None
    return max(ids.values())


def _prune_sentinels(now: float | None = None) -> None:
    """#926: drop cancel sentinels and suppression records past their window."""
    now = time.time() if now is None else now
    for sid in [
        s for s, at in _DO_NOT_RESUME_AT.items() if now - at >= CLI_CRON_MAX_AGE_S
    ]:
        del _DO_NOT_RESUME_AT[sid]
    for sid in list(_CRON_SUPPRESSED):
        ids = _CRON_SUPPRESSED[sid]
        for upstream_id in [k for k, until in ids.items() if until <= now]:
            del ids[upstream_id]
        if not ids:
            del _CRON_SUPPRESSED[sid]


# ── Fire path ───────────────────────────────────────────────────────────


async def _arm_timer(token: str, generation: int) -> None:
    """Sleep until ``entry.fire_at_monotonic`` then call :func:`_fire`.

    ``generation`` lets a stale arm_timer (left over from a previous round
    after a re-arm) detect it is no longer the live timer and bail out
    without double-firing.  The sleep is interrupted promptly via
    ``entry.cancel_event``.
    """
    entry = _PENDING_BY_TOKEN.get(token)
    if entry is None or entry.cancelled or entry.generation != generation:
        return
    delay = max(0.0, entry.fire_at_monotonic - time.monotonic())
    if delay > 0:
        with anyio.move_on_after(delay):
            await entry.cancel_event.wait()
    entry = _PENDING_BY_TOKEN.get(token)
    if entry is None or entry.cancelled or entry.generation != generation:
        return
    await _fire(token)


async def _fire(token: str) -> None:
    """Fire one iteration of the loop identified by ``token``.

    Sequence:
    1. Validate entry still pending (not cancelled, not over caps).
    2. Honour the per-loop do-not-resume sentinel (#926).
    3. Drop-on-busy: if another run is in flight for our chat (a run that
       is not a live-idle session), log and skip.  Mirrors upstream's "no
       catch-up" semantic.
    4. A process still owns the session (#925 D-D,
       :func:`_fire_past_live_session`): a clean-idle live session is
       closed so the iteration can resume it; a wake-up a live session
       will fire itself is expired (``cli_fired_live``); otherwise retry,
       bounded by the next cron fire.
    5. Spawn the iteration via :func:`_spawn_loop_iteration`.
    6. Re-arm next fire (recurring) or expire (one-shot).
    """
    entry = _PENDING_BY_TOKEN.get(token)
    if entry is None or entry.cancelled:
        return
    if _expire_if_over_caps(entry):
        return
    if _blocked_by_cancel(entry):
        _expire(entry, reason="do_not_resume")
        return
    if _IS_CHAT_BUSY is not None and _IS_CHAT_BUSY(entry.chat_id):
        logger.warning(
            "loop.iteration_skipped_previous_running",
            token=token,
            chat_id=entry.chat_id,
            iteration=entry.iteration_count + 1,
        )
        # Still re-arm — we want to try the next interval.
        _rearm_or_expire(entry)
        return
    if _is_session_alive_safe(entry.resume_token):
        await _fire_past_live_session(entry)
        return
    await _spawn_loop_iteration(entry)
    _rearm_or_expire(entry)


def _expire_if_over_caps(entry: _LoopEntry) -> bool:
    """Expire ``entry`` when a ``[loop]`` cap is reached; True if it was."""
    now_wallclock = time.time()
    if now_wallclock >= entry.expires_at_wallclock:
        _expire(entry, reason="expired_7d")
        return True
    if entry.iteration_count >= entry.max_iterations:
        _expire(entry, reason="max_iterations")
        return True
    if (
        now_wallclock - entry.created_at_wallclock
        >= entry.max_total_duration_hours * 3600
    ):
        _expire(entry, reason="max_total_duration")
        return True
    return False


def _busy_retry_deadline(entry: _LoopEntry) -> float | None:
    """#925: a recurring cron stops retrying once its next fire is due (that
    iteration is skipped, upstream's "no catch-up"). One-shots and wake-ups
    keep retrying, bounded by the caps re-checked on every attempt."""
    if entry.kind != "cron" or not entry.recurring or entry.cron_expression is None:
        return None
    return _next_cron_fire(entry.cron_expression)


async def _fire_past_live_session(entry: _LoopEntry) -> None:
    """#925 D-D: fire an iteration while a process still owns the session.

    One loop inside this ``_fire`` call (§13 amendment 4): it bails as soon
    as the entry is cancelled, expired or re-armed (``generation``), so a
    retry can never fire an extra iteration.

    - The session went away → spawn as normal.
    - ``wakeup`` and the session accepts input → the live session fires the
      CLI's own wake-up (it holds for it, #872): expire ``cli_fired_live``.
      A process that isn't accepting (limbo, ``-p``, closing) can't fire it
      (§13 amendment 3), so keep retrying and fire once it has exited.
    - ``cron`` and the live session is clean-idle → close it (``loop_fire``)
      and spawn the iteration; a refused close (a follow-up got in first,
      §13 amendment 2) counts as busy.
    - Busy (background work, approval, closing, limbo): retry every
      ``redundancy_check_interval``; a recurring cron skips this iteration
      once its next fire is due (``loop.iteration_skipped_session_busy``).
    """
    token = entry.token
    generation = entry.generation
    session_id = entry.resume_token
    deadline = _busy_retry_deadline(entry)
    interval = max(0.0, float(_redundancy_check_interval()))
    deferred_logged = False
    while True:
        current = _PENDING_BY_TOKEN.get(token)
        if current is not entry or entry.cancelled or entry.generation != generation:
            return
        if _expire_if_over_caps(entry):
            return
        chat_busy = _IS_CHAT_BUSY is not None and _IS_CHAT_BUSY(entry.chat_id)
        if not _is_session_alive_safe(session_id):
            if not chat_busy:
                await _spawn_loop_iteration(entry)
                _rearm_or_expire(entry)
                return
        elif entry.kind == "wakeup":
            if _is_session_accepting_safe(session_id):
                _expire(entry, reason="cli_fired_live")
                return
        elif (
            not chat_busy
            and _live_session_loop_closeable_safe(session_id)
            and await _close_live_session_for_fire(session_id)
        ):
            logger.info(
                "loop.live_session_closed_for_fire",
                token=token,
                session=session_id,
                iteration=entry.iteration_count + 1,
            )
            await _spawn_loop_iteration(entry)
            _rearm_or_expire(entry)
            return
        if deadline is not None and time.monotonic() + interval >= deadline:
            logger.info(
                "loop.iteration_skipped_session_busy",
                token=token,
                session=session_id,
                iteration=entry.iteration_count + 1,
            )
            _rearm_or_expire(entry)
            return
        if not deferred_logged:
            deferred_logged = True
            logger.debug(
                "loop.fire_deferred_session_busy",
                token=token,
                session=session_id,
                kind=entry.kind,
                retry_s=interval,
            )
        with anyio.move_on_after(interval):
            await entry.cancel_event.wait()
        # Yield even with a zero interval so a cancel can land.
        await anyio.lowlevel.checkpoint()


async def _spawn_loop_iteration(entry: _LoopEntry) -> None:
    """Send the notification and dispatch the run via ``_RUN_JOB``."""
    if entry.cancelled:
        return
    assert _RUN_JOB is not None and _TRANSPORT is not None
    iteration = entry.iteration_count + 1
    label = f"\N{ALARM CLOCK} /loop · iter {iteration}/{entry.max_iterations}"
    try:
        notify_ref = await _TRANSPORT.send(
            channel_id=_as_channel_id(entry.chat_id),
            message=RenderedMessage(text=label),
            # #826: the run below goes to entry.thread_id; post the notice
            # (which the run replies to) in the same topic.
            options=SendOptions(notify=False, thread_id=entry.thread_id),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "loop.notify_failed",
            token=entry.token,
            chat_id=entry.chat_id,
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        return
    if notify_ref is None:
        logger.error("loop.notify_failed", token=entry.token, chat_id=entry.chat_id)
        return
    fire_prompt = entry.prompt
    if fire_prompt == "<<autonomous-loop-dynamic>>":
        fire_prompt = entry.fallback_first_user_message or fire_prompt
    wrapped = (
        f"Loop iteration {iteration}: {fire_prompt}. "
        "Do the task now; do not summarize old results unless necessary."
    )
    logger.info(
        "loop.firing",
        token=entry.token,
        iteration=iteration,
        session=entry.resume_token,
        kind=entry.kind,
    )
    try:
        await _RUN_JOB(
            entry.chat_id,
            notify_ref.message_id,
            wrapped,
            ResumeToken(engine="claude", value=entry.resume_token),
            entry.context,
            entry.thread_id,
            None,  # chat_session_key
            None,  # reply_ref
            None,  # on_thread_known
            entry.engine_override,
            None,  # progress_ref
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "loop.fired_failed",
            token=entry.token,
            iteration=iteration,
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        return
    entry.iteration_count = iteration
    logger.info(
        "loop.fired_ok",
        token=entry.token,
        iteration=iteration,
        session=entry.resume_token,
    )


def _rearm_or_expire(entry: _LoopEntry) -> None:
    """After a fire (or busy-skip), re-arm the timer or expire the loop."""
    if entry.cancelled:
        return
    if not entry.recurring:
        _expire(entry, reason="one_shot_complete")
        return
    if entry.iteration_count >= entry.max_iterations:
        _expire(entry, reason="max_iterations")
        return
    if entry.kind == "cron" and entry.cron_expression is not None:
        next_fire = _next_cron_fire(entry.cron_expression)
        if next_fire is None:
            _expire(entry, reason="cron_unparseable")
            return
        entry.fire_at_monotonic = next_fire
        entry.fire_at_wallclock = time.time() + (next_fire - time.monotonic())
    elif entry.delay_seconds is not None:
        entry.fire_at_monotonic = time.monotonic() + entry.delay_seconds
        entry.fire_at_wallclock = time.time() + entry.delay_seconds
    # Bump generation so any stale _arm_timer task from the previous round
    # bails out instead of double-firing.  Reset cancel_event for the new
    # round so the fresh _arm_timer starts unset.
    entry.generation += 1
    entry.cancel_event = anyio.Event()
    _persist()
    if _TASK_GROUP is not None:
        _TASK_GROUP.start_soon(_arm_timer, entry.token, entry.generation)


def _expire(entry: _LoopEntry, *, reason: str) -> None:
    """Mark an entry as fired/cancelled and drop from indexes.  Logs once."""
    if entry.cancelled and reason != "do_not_resume":
        return
    entry.cancelled = True
    entry.cancel_event.set()
    _drop_indexes(entry)
    _persist()
    logger.info(
        "loop.expired",
        token=entry.token,
        chat_id=entry.chat_id,
        session=entry.resume_token,
        reason=reason,
        iterations_completed=entry.iteration_count,
    )


def _is_session_alive_safe(session_id: str) -> bool:
    """Lazy import of :func:`untether.runners.claude.is_session_alive`.

    Lazy because the runner module imports back into this module at observer
    wiring time (Commit B in the #289 plan).
    """
    try:
        from .runners.claude import is_session_alive
    except ImportError:
        return False
    return is_session_alive(session_id)


def _is_session_accepting_safe(session_id: str) -> bool:
    """#925: a live session can take input (and so fire the CLI's own
    wake-up). Lazy import, like :func:`_is_session_alive_safe`."""
    try:
        from .runners.claude import is_session_accepting
    except ImportError:
        return False
    return is_session_accepting(session_id)


def _live_session_loop_closeable_safe(session_id: str) -> bool:
    """#925: the live session sits clean-idle and may be closed to fire."""
    try:
        from .runners.claude import live_session_loop_closeable
    except ImportError:
        return False
    return live_session_loop_closeable(session_id)


async def _close_live_session_for_fire(session_id: str) -> bool:
    """#925: close a clean-idle live session (``loop_fire``) so the
    iteration can resume it. False when the close was refused."""
    try:
        from .runners.claude import close_live_session
    except ImportError:
        return False
    return await close_live_session(session_id, "loop_fire", only_if_idle=True)


def _redundancy_check_interval() -> int:
    """Read the configured redundancy check interval, with a safe fallback."""
    try:
        from .settings import load_settings_if_exists

        result = load_settings_if_exists()
        if result is None:
            return 30
        settings, _ = result
        return int(settings.loop.redundancy_check_interval)
    except Exception:  # noqa: BLE001
        return 30


def _next_cron_fire(expression: str) -> float | None:
    """Compute the next monotonic-clock instant matching ``expression``.

    Walks one minute at a time from ``now + 60s`` (to avoid double-firing
    the current minute) up to a 366-day horizon.  Returns ``None`` if the
    expression never matches in that window (almost certainly malformed).
    """
    if not expression or not expression.strip():
        return None
    fields = expression.strip().split()
    if len(fields) != 5:
        return None
    now_monotonic = time.monotonic()
    now_wallclock_dt = datetime.datetime.now().replace(second=0, microsecond=0)
    horizon_minutes = 366 * 24 * 60
    for i in range(1, horizon_minutes + 1):
        candidate = now_wallclock_dt + datetime.timedelta(minutes=i)
        try:
            if cron_matches(expression, candidate):
                offset_seconds = (candidate - datetime.datetime.now()).total_seconds()
                return now_monotonic + max(0.0, offset_seconds)
        except Exception:  # noqa: BLE001
            return None
    return None


def _as_channel_id(chat_id: int) -> ChannelId:
    return chat_id


# ── Persistence ─────────────────────────────────────────────────────────


def _persist() -> None:
    """Write the current pending entries + do-not-resume sentinel to disk.

    No-op if persistence is disabled (``_STATE_PATH is None``).  Errors are
    logged and swallowed — losing persistence is preferable to crashing the
    bot loop.
    """
    if _STATE_PATH is None:
        return
    _prune_sentinels()
    # #926: additive keys under schema_version 1 — ``do_not_resume`` stays a
    # list so an rc19 reader (rollback) still loads the file.
    payload: dict[str, Any] = {
        "schema_version": 1,
        "entries": [_serialize_entry(e) for e in _PENDING_BY_TOKEN.values()],
        "do_not_resume": sorted(_DO_NOT_RESUME_AT),
        "do_not_resume_at": dict(sorted(_DO_NOT_RESUME_AT.items())),
        "cron_suppressed": {
            sid: dict(ids) for sid, ids in sorted(_CRON_SUPPRESSED.items())
        },
    }
    try:
        atomic_write_json(_STATE_PATH, payload)
    except (OSError, ValueError) as exc:
        logger.warning(
            "loop.persist_failed",
            path=str(_STATE_PATH),
            error=str(exc),
            error_type=exc.__class__.__name__,
        )


def _serialize_entry(entry: _LoopEntry) -> dict[str, Any]:
    """Serialize a ``_LoopEntry`` for JSON persistence.

    ``cancel_event`` and ``generation`` are dropped (re-created on load).
    ``context`` is serialized via its dataclass fields so we can restore
    the project mapping on reload.
    """
    ctx = entry.context
    return {
        "token": entry.token,
        "upstream_cron_id": entry.upstream_cron_id,
        "tool_use_id": entry.tool_use_id,
        "chat_id": entry.chat_id,
        "thread_id": entry.thread_id,
        "kind": entry.kind,
        "cron_expression": entry.cron_expression,
        "delay_seconds": entry.delay_seconds,
        "recurring": entry.recurring,
        "prompt": entry.prompt,
        "fallback_first_user_message": entry.fallback_first_user_message,
        "fire_at_wallclock": entry.fire_at_wallclock,
        "iteration_count": entry.iteration_count,
        "max_iterations": entry.max_iterations,
        "max_total_duration_hours": entry.max_total_duration_hours,
        "created_at_wallclock": entry.created_at_wallclock,
        "expires_at_wallclock": entry.expires_at_wallclock,
        "context_project": ctx.project if ctx is not None else None,
        "context_branch": ctx.branch if ctx is not None else None,
        "context_permission_mode": ctx.permission_mode if ctx is not None else None,
        "engine_override": entry.engine_override,
        "resume_token": entry.resume_token,
        "cancelled": entry.cancelled,
    }


def _deserialize_entry(data: dict[str, Any]) -> _LoopEntry | None:
    """Inverse of ``_serialize_entry``.  Returns ``None`` if the payload is
    invalid.  Re-creates the cancel ``Event`` and recomputes
    ``fire_at_monotonic`` from the persisted wall-clock time (or zero if
    past)."""
    try:
        now_wallclock = time.time()
        now_monotonic = time.monotonic()
        fire_at_wallclock = float(data["fire_at_wallclock"])
        offset = max(0.0, fire_at_wallclock - now_wallclock)
        fire_at_monotonic = now_monotonic + offset
        token = str(data["token"])
        ctx = RunContext(
            project=data.get("context_project"),
            branch=data.get("context_branch"),
            trigger_source=f"loop:{token}",
            permission_mode=data.get("context_permission_mode"),
        )
        return _LoopEntry(
            token=token,
            upstream_cron_id=data.get("upstream_cron_id"),
            tool_use_id=str(data["tool_use_id"]),
            chat_id=int(data["chat_id"]),
            thread_id=data.get("thread_id"),
            kind=data["kind"],
            cron_expression=data.get("cron_expression"),
            delay_seconds=(
                float(data["delay_seconds"])
                if data.get("delay_seconds") is not None
                else None
            ),
            recurring=bool(data["recurring"]),
            prompt=str(data["prompt"]),
            fallback_first_user_message=data.get("fallback_first_user_message"),
            fire_at_monotonic=fire_at_monotonic,
            fire_at_wallclock=fire_at_wallclock,
            iteration_count=int(data.get("iteration_count", 0)),
            max_iterations=int(data.get("max_iterations", 20)),
            max_total_duration_hours=int(data.get("max_total_duration_hours", 4)),
            created_at_wallclock=float(data.get("created_at_wallclock", now_wallclock)),
            expires_at_wallclock=float(
                data.get("expires_at_wallclock", now_wallclock + 7 * 86_400)
            ),
            context=ctx,
            engine_override=data.get("engine_override"),
            resume_token=str(data["resume_token"]),
            cancelled=bool(data.get("cancelled", False)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "loop.restore.entry_invalid",
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        return None


def _restore_from_disk(path: Path) -> None:
    """Read ``path`` and re-arm timers for non-cancelled entries.

    Past ``fire_at_wallclock`` values fire immediately (no catch-up
    multiplier) — mirrors upstream's "no catch-up" semantic.  Cancelled
    entries and the do-not-resume sentinel are preserved.
    """
    if not path.exists():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(
            "loop.restore.read_failed",
            path=str(path),
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        return
    if not isinstance(raw, dict):
        return
    migrated = _restore_sentinels(raw)
    entries = raw.get("entries", [])
    if not isinstance(entries, list):
        if migrated:
            _persist()
        return
    restored = 0
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            continue
        entry = _deserialize_entry(raw_entry)
        if entry is None or entry.cancelled:
            continue
        _PENDING_BY_TOKEN[entry.token] = entry
        _PENDING_BY_CHAT[entry.chat_id].add(entry.token)
        _PENDING_BY_TOOL_USE_ID[entry.tool_use_id] = entry.token
        if entry.upstream_cron_id is not None:
            _PENDING_BY_UPSTREAM_ID[entry.upstream_cron_id] = entry.token
            # #926 D-4: a restored entry the CLI had accepted may still be
            # resurrectable — suppress it if nothing records it yet. Applied
            # even with ``own_schedule = false`` (it protects pre-rc20 jobs).
            if entry.kind == "cron" and entry.upstream_cron_id not in (
                _CRON_SUPPRESSED.get(entry.resume_token) or {}
            ):
                migrated |= _mark_cron_suppressed(
                    entry.resume_token,
                    entry.upstream_cron_id,
                    until=entry.created_at_wallclock + CLI_CRON_MAX_AGE_S,
                    source="restore",
                )
        if _TASK_GROUP is not None:
            _TASK_GROUP.start_soon(_arm_timer, entry.token, entry.generation)
        restored += 1
    if restored:
        logger.info("loop.restored", path=str(path), count=restored)
    if migrated:
        _persist()


def _restore_sentinels(raw: dict[str, Any]) -> bool:
    """#926: load the cancel sentinels and suppression records.

    A v1 file (rc19: a ``do_not_resume`` list, no ``do_not_resume_at``)
    records cancels that always left a CLI job behind, so each session gets
    a cancel time of now **and** a 7-day suppression (``source=restore_v1``,
    logged once per session — §13 amendment 3 accepts the over-suppression
    of ScheduleWakeup-only cancels). Returns True when a migration changed
    state that should be written back."""
    now = time.time()
    migrated = False
    stamped = raw.get("do_not_resume_at")
    if isinstance(stamped, dict):
        for sid, at in stamped.items():
            try:
                _DO_NOT_RESUME_AT[str(sid)] = float(at)
            except (TypeError, ValueError):
                continue
    else:
        legacy = raw.get("do_not_resume", [])
        if isinstance(legacy, list):
            for sid in (str(s) for s in legacy):
                _DO_NOT_RESUME_AT[sid] = now
                _mark_cron_suppressed(
                    sid, "legacy", until=now + CLI_CRON_MAX_AGE_S, source="restore_v1"
                )
                migrated = True
    suppressed = raw.get("cron_suppressed")
    if isinstance(suppressed, dict):
        for sid, ids in suppressed.items():
            if not isinstance(ids, dict):
                continue
            for upstream_id, until in ids.items():
                try:
                    until_f = float(until)
                except (TypeError, ValueError):
                    continue
                if until_f > now:
                    _CRON_SUPPRESSED.setdefault(str(sid), {})[str(upstream_id)] = (
                        until_f
                    )
    _prune_sentinels(now)
    return migrated
