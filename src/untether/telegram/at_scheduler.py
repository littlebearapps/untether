"""One-shot delayed-run scheduler for the ``/at`` command (#288).

Users send ``/at 30m <prompt>`` in Telegram; ``AtCommand.handle`` calls
:func:`schedule_delayed_run` which spawns an anyio task that sleeps for
the requested duration, then dispatches a run via the ``run_job`` closure
registered via :func:`install`.

State is process-local and not persisted — a restart cancels all pending
delays. This is explicitly documented and matches the "fire-and-forget"
intent of the feature (the issue body calls this acceptable). The /cancel
command can drop pending /at timers via :func:`cancel_pending_for_chat`.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import anyio
from anyio.abc import TaskGroup

from ..logging import get_logger
from ..transport import ChannelId, RenderedMessage, SendOptions, Transport

logger = get_logger(__name__)

__all__ = [
    "MAX_DELAY_SECONDS",
    "MIN_DELAY_SECONDS",
    "PER_CHAT_LIMIT",
    "active_count",
    "cancel_pending_for_chat",
    "install",
    "pending_for_chat",
    "schedule_delayed_run",
    "uninstall",
]

# 60s minimum mirrors ScheduleWakeup / Untether cron granularity.
MIN_DELAY_SECONDS = 60
# 24h maximum — beyond this users probably want a cron.
MAX_DELAY_SECONDS = 86_400
# Per-chat cap to prevent runaway scheduling.
PER_CHAT_LIMIT = 20

RunJobFn = Callable[..., Awaitable[None]]


@dataclass(slots=True)
class _PendingAt:
    token: str
    chat_id: int
    thread_id: int | None
    prompt: str
    delay_s: int
    scheduled_at: float  # monotonic time when user called /at
    fire_at: float  # monotonic time when the run will fire
    cancel_scope: anyio.CancelScope
    fired: bool = field(default=False)


_TASK_GROUP: TaskGroup | None = None
_RUN_JOB: RunJobFn | None = None
_TRANSPORT: Transport | None = None
_DEFAULT_CHAT_ID: int | None = None
_PENDING: dict[str, _PendingAt] = {}


def install(
    task_group: TaskGroup,
    run_job: RunJobFn,
    transport: Transport,
    default_chat_id: int,
) -> None:
    """Register the task group and run_job closure used by the scheduler.

    Called from ``telegram.loop.run_main_loop`` once the task group is
    open and ``run_job`` has been defined.
    """
    global _TASK_GROUP, _RUN_JOB, _TRANSPORT, _DEFAULT_CHAT_ID
    _TASK_GROUP = task_group
    _RUN_JOB = run_job
    _TRANSPORT = transport
    _DEFAULT_CHAT_ID = int(default_chat_id)


def uninstall() -> None:
    """Clear installed references — tests and graceful shutdown use this."""
    global _TASK_GROUP, _RUN_JOB, _TRANSPORT, _DEFAULT_CHAT_ID
    _TASK_GROUP = None
    _RUN_JOB = None
    _TRANSPORT = None
    _DEFAULT_CHAT_ID = None
    _PENDING.clear()


class AtSchedulerError(Exception):
    """Raised when /at scheduling cannot proceed."""


def schedule_delayed_run(
    chat_id: int,
    thread_id: int | None,
    delay_s: int,
    prompt: str,
) -> str:
    """Start a background task that fires a run after ``delay_s`` seconds.

    Returns a token identifying the pending delay so callers can record or
    cancel it. Raises :class:`AtSchedulerError` if the scheduler is not
    installed, the delay is out of range, or the per-chat cap is reached.
    """
    if _TASK_GROUP is None or _RUN_JOB is None or _TRANSPORT is None:
        raise AtSchedulerError("/at scheduler not installed")
    if delay_s < MIN_DELAY_SECONDS or delay_s > MAX_DELAY_SECONDS:
        raise AtSchedulerError(
            f"delay must be between {MIN_DELAY_SECONDS}s and {MAX_DELAY_SECONDS}s"
        )
    if sum(1 for p in _PENDING.values() if p.chat_id == chat_id) >= PER_CHAT_LIMIT:
        raise AtSchedulerError(
            f"per-chat limit of {PER_CHAT_LIMIT} pending /at delays reached"
        )
    token = secrets.token_hex(6)
    now = time.monotonic()
    scope = anyio.CancelScope()
    entry = _PendingAt(
        token=token,
        chat_id=chat_id,
        thread_id=thread_id,
        prompt=prompt,
        delay_s=delay_s,
        scheduled_at=now,
        fire_at=now + delay_s,
        cancel_scope=scope,
    )
    _PENDING[token] = entry
    _TASK_GROUP.start_soon(_run_delayed, token)
    logger.info("at.scheduled", chat_id=chat_id, token=token, delay_s=delay_s)
    return token


async def _run_delayed(token: str) -> None:
    """Sleep until fire_at, then dispatch a run via run_job."""
    entry = _PENDING.get(token)
    if entry is None:
        return
    with entry.cancel_scope:
        try:
            await anyio.sleep(entry.delay_s)
        except anyio.get_cancelled_exc_class():
            logger.info("at.cancelled", chat_id=entry.chat_id, token=token)
            _PENDING.pop(token, None)
            raise
        entry.fired = True
        # Pop before firing so /cancel can no longer see it as pending.
        _PENDING.pop(token, None)

    assert _RUN_JOB is not None and _TRANSPORT is not None
    # Send a notification so run_job has a message_id to reply to,
    # mirroring TriggerDispatcher._dispatch.
    label = f"\N{ALARM CLOCK} Running scheduled prompt ({entry.delay_s}s after /at)"
    try:
        notify_ref = await _TRANSPORT.send(
            channel_id=_as_channel_id(entry.chat_id),
            message=RenderedMessage(text=label),
            options=SendOptions(notify=False),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "at.notify_failed",
            chat_id=entry.chat_id,
            token=token,
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        return
    if notify_ref is None:
        logger.error("at.notify_failed", chat_id=entry.chat_id, token=token)
        return

    logger.info(
        "at.firing",
        chat_id=entry.chat_id,
        token=token,
        delay_s=entry.delay_s,
    )
    try:
        await _RUN_JOB(
            entry.chat_id,
            notify_ref.message_id,
            entry.prompt,
            None,  # resume_token
            None,  # context
            entry.thread_id,
            None,  # chat_session_key
            None,  # reply_ref
            None,  # on_thread_known
            None,  # engine_override
            None,  # progress_ref
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "at.run_failed",
            chat_id=entry.chat_id,
            token=token,
            error=str(exc),
            error_type=exc.__class__.__name__,
        )


def _as_channel_id(chat_id: int) -> ChannelId:
    return chat_id


def cancel_pending_for_chat(chat_id: int) -> int:
    """Cancel all pending /at delays for ``chat_id``.

    Returns the number of delays cancelled. Delays that have already
    fired (``fired=True``) run as part of the normal running_tasks set
    and are unaffected.
    """
    cancelled = 0
    for token in list(_PENDING):
        entry = _PENDING.get(token)
        if entry is None or entry.chat_id != chat_id or entry.fired:
            continue
        entry.cancel_scope.cancel()
        _PENDING.pop(token, None)
        cancelled += 1
    if cancelled:
        logger.info("at.cancelled_for_chat", chat_id=chat_id, count=cancelled)
    return cancelled


def active_count() -> int:
    """Return the number of pending /at delays currently sleeping."""
    return sum(1 for p in _PENDING.values() if not p.fired)


def pending_for_chat(chat_id: int) -> list[_PendingAt]:
    """Return a snapshot of pending /at entries for ``chat_id`` (test/inspection aid)."""
    return [p for p in _PENDING.values() if p.chat_id == chat_id and not p.fired]
