"""Untether-side scheduler for /loop and ScheduleWakeup (#289).

Claude Code's session-scoped scheduler dies when the ``claude --print``
subprocess exits — verified empirically against ``claude`` 2.1.129/2.1.132
in `docs/plans/2026-05-06-289-loop-and-cron-interception.md` (Probe 1).
This module observes ``CronCreate`` / ``ScheduleWakeup`` / ``CronDelete``
tool_use events at the JSONL layer (wired in :mod:`untether.runners.claude`),
captures the user's intent (cron expression + prompt OR delay + prompt),
and at each fire interval spawns ``claude --resume <session_id>`` with the
original prompt re-issued as a fresh user turn.

State is persisted to ``active_loops.json`` (sibling to the config file)
via :func:`untether.utils.json_state.atomic_write_json` so loops survive
Untether restarts.

Default OFF — opt-in per-chat via ``/config → 🔁 Loop mode``. When the
toggle is OFF (the default) the observer never reaches this module so
behaviour matches the pre-#289 baseline.
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
from anyio.abc import TaskGroup

from .context import RunContext
from .logging import get_logger
from .model import EngineId, ResumeToken
from .transport import ChannelId, RenderedMessage, SendOptions, Transport
from .triggers.cron import cron_matches
from .utils.json_state import atomic_write_json

logger = get_logger(__name__)

__all__ = [
    "STATE_FILENAME",
    "LoopSchedulerError",
    "active_count",
    "bind_upstream_id",
    "cancel_by_token",
    "cancel_by_upstream_id",
    "cancel_pending_for_chat",
    "install",
    "is_do_not_resume",
    "mark_do_not_resume",
    "next_fire_for_session",
    "pending_for_chat",
    "register_pending_cron",
    "register_pending_wakeup",
    "uninstall",
]

STATE_FILENAME = "active_loops.json"

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

# Sessions that have been cancelled via /cancel — the do-not-resume sentinel
# (issue #289 design doc §5c).  ``_fire`` refuses to spawn for any session in
# this set so an upstream session-scoped cron that survives in the JSONL
# transcript can never be re-fired by us if the user cancels.  Persisted to
# disk alongside _PENDING entries.
_DO_NOT_RESUME: set[str] = set()


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
    _DO_NOT_RESUME.clear()


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
    """Shared body for ``register_pending_cron`` / ``register_pending_wakeup``."""
    assert _TASK_GROUP is not None  # caller guards
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


def bind_upstream_id(tool_use_id: str, upstream_id: str) -> None:
    """Bind the upstream 8-char cron ID to a previously-registered entry.

    Called from the tool_result decode site after parsing the result text
    via the ``\\bjob ([0-9a-f]{8})\\b`` regex.  No-op if no matching entry
    (e.g. registration was rejected or the master toggle was off).
    """
    token = _PENDING_BY_TOOL_USE_ID.get(tool_use_id)
    if token is None:
        return
    entry = _PENDING_BY_TOKEN.get(token)
    if entry is None:
        return
    entry.upstream_cron_id = upstream_id
    _PENDING_BY_UPSTREAM_ID[upstream_id] = token
    _persist()


# ── Cancellation ────────────────────────────────────────────────────────


def cancel_by_token(token: str) -> bool:
    """Cancel a single loop by its Untether-side token.  Returns ``True``
    if a matching pending entry was cancelled, ``False`` otherwise.
    """
    entry = _PENDING_BY_TOKEN.get(token)
    if entry is None or entry.cancelled:
        return False
    entry.cancelled = True
    entry.cancel_event.set()
    _drop_indexes(entry)
    _DO_NOT_RESUME.add(entry.resume_token)
    _persist()
    logger.info(
        "loop.cancelled",
        token=token,
        chat_id=entry.chat_id,
        session=entry.resume_token,
        reason="user_cancel",
        iterations_completed=entry.iteration_count,
    )
    return True


def cancel_by_upstream_id(upstream_id: str) -> bool:
    """Cancel a loop by its upstream 8-char cron ID (CronDelete observed)."""
    token = _PENDING_BY_UPSTREAM_ID.get(upstream_id)
    if token is None:
        return False
    return cancel_by_token(token)


def cancel_pending_for_chat(chat_id: int) -> int:
    """Cancel all pending loops for ``chat_id``.  Returns count cancelled."""
    cancelled = 0
    for token in list(_PENDING_BY_CHAT.get(chat_id, ())):
        if cancel_by_token(token):
            cancelled += 1
    if cancelled:
        logger.info("loop.cancelled_for_chat", chat_id=chat_id, count=cancelled)
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
    """Return ``True`` if ``session_id`` has the do-not-resume sentinel set.

    The fire path consults this before spawning a ``--resume`` subprocess
    so cancelled loops cannot be revived even if the upstream session-scoped
    cron survives in the JSONL transcript.  ``/continue`` is a separate
    user-initiated action and does NOT consult this set (handover default).
    """
    return session_id in _DO_NOT_RESUME


def mark_do_not_resume(session_id: str) -> None:
    """Mark ``session_id`` as do-not-resume.  Idempotent.  Persisted."""
    if session_id in _DO_NOT_RESUME:
        return
    _DO_NOT_RESUME.add(session_id)
    _persist()


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
    2. Drop-on-busy: if another run is in flight for our chat, log and
       skip.  Mirrors upstream's "no catch-up" semantic.
    3. Race avoidance: if the originating subprocess is still alive, sleep
       ``redundancy_check_interval`` and re-arm.
    4. Honour the do-not-resume sentinel.
    5. Spawn the iteration via :func:`_spawn_loop_iteration`.
    6. Re-arm next fire (recurring) or expire (one-shot).
    """
    entry = _PENDING_BY_TOKEN.get(token)
    if entry is None or entry.cancelled:
        return
    now_wallclock = time.time()
    if now_wallclock >= entry.expires_at_wallclock:
        _expire(entry, reason="expired_7d")
        return
    if entry.iteration_count >= entry.max_iterations:
        _expire(entry, reason="max_iterations")
        return
    if (
        now_wallclock - entry.created_at_wallclock
        >= entry.max_total_duration_hours * 3600
    ):
        _expire(entry, reason="max_total_duration")
        return
    if is_do_not_resume(entry.resume_token):
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
    # Race avoidance — skip if the originating subprocess is still alive
    # (control_request awaiting Telegram input, or any other reason).
    if _is_session_alive_safe(entry.resume_token):
        logger.info(
            "loop.fire_skipped_subprocess_alive",
            token=token,
            session=entry.resume_token,
        )
        await _redundancy_sleep_then_retry(token)
        return
    await _spawn_loop_iteration(entry)
    _rearm_or_expire(entry)


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
            options=SendOptions(notify=False),
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


async def _redundancy_sleep_then_retry(token: str) -> None:
    """Sleep redundancy_check_interval then re-fire.  Bounded to avoid
    runaway spinning if the subprocess never exits."""
    interval = _redundancy_check_interval()
    await anyio.sleep(interval)
    if _TASK_GROUP is not None:
        _TASK_GROUP.start_soon(_fire, token)


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
    payload: dict[str, Any] = {
        "schema_version": 1,
        "entries": [_serialize_entry(e) for e in _PENDING_BY_TOKEN.values()],
        "do_not_resume": sorted(_DO_NOT_RESUME),
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
    do_not_resume = raw.get("do_not_resume", [])
    if isinstance(do_not_resume, list):
        _DO_NOT_RESUME.update(str(s) for s in do_not_resume)
    entries = raw.get("entries", [])
    if not isinstance(entries, list):
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
        if _TASK_GROUP is not None:
            _TASK_GROUP.start_soon(_arm_timer, entry.token, entry.generation)
        restored += 1
    if restored:
        logger.info("loop.restored", path=str(path), count=restored)
