"""Messages waiting on a chat's first start (#1157).

With no saved session a chat's first run has no resume token until its
engine reports the session (``StartedEvent``), so nothing can queue behind it
by token. A prompt for the same session scope that arrives meanwhile waits on
the run's :class:`PendingStart` (``telegram/loop.py``,
``dispatch_prompt_run``) and then queues behind the reported token, instead
of starting a second conversation in parallel.

Such a message has no ``queued`` card yet, so ``/cancel`` and ``/new`` reach
it here: cancelling the starting run (or ``/new`` in its thread) drops the
messages waiting on it (:func:`drop_waiting`) — each is told so — rather
than letting them start a fresh run nobody saw queued.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import anyio

from ..logging import get_logger
from ..model import EngineId, ResumeToken

if TYPE_CHECKING:
    from .topics import ThreadFilter

logger = get_logger(__name__)

type FirstStartKey = tuple[
    tuple[int, int] | None, tuple[int, int | None] | None, EngineId
]


@dataclass(slots=True, eq=False)
class PendingStart:
    """A first run that hasn't saved its session yet.

    ``settled`` is set once the session is saved, when the run ends
    (``resume`` stays ``None`` if it never reported one), or when the waiters
    are dropped (``dropped_by`` names the command). ``thread_id`` is the
    starting message's thread, for ``/cancel`` / ``/new`` scoping (#826).
    """

    chat_id: int
    thread_id: int | None
    resume: ResumeToken | None = None
    settled: anyio.Event = field(default_factory=anyio.Event)
    waiting: int = 0
    dropped_by: str | None = None


_PENDING: dict[FirstStartKey, PendingStart] = {}


def first_start_key(
    topic_key: tuple[int, int] | None,
    chat_session_key: tuple[int, int | None] | None,
    engine: EngineId,
) -> FirstStartKey | None:
    """The saved-session scope a tokenless run starts; ``None`` in stateless
    mode, where every message is its own run by design."""
    if topic_key is None and chat_session_key is None:
        return None
    return (topic_key, chat_session_key, engine)


def get(key: FirstStartKey) -> PendingStart | None:
    return _PENDING.get(key)


def register(
    key: FirstStartKey, *, chat_id: int, thread_id: int | None
) -> PendingStart:
    start = PendingStart(chat_id=chat_id, thread_id=thread_id)
    _PENDING[key] = start
    return start


def release(key: FirstStartKey, start: PendingStart) -> None:
    """Wake the waiters and forget ``start`` (idempotent)."""
    start.settled.set()
    if _PENDING.get(key) is start:
        del _PENDING[key]


def drop_waiting(
    chat_id: int, *, thread_filter: ThreadFilter | None, command: str
) -> int:
    """Drop the messages waiting on this chat's (or thread's) first starts.

    Called when ``/cancel`` or ``/new`` stops the starting run. Each woken
    waiter replies to its own message and never runs. Returns how many
    messages were dropped."""
    dropped = 0
    for key, start in list(_PENDING.items()):
        if start.chat_id != chat_id:
            continue
        if thread_filter is not None and not thread_filter(start.thread_id):
            continue
        if start.waiting:
            start.dropped_by = command
            dropped += start.waiting
        release(key, start)
    if dropped:
        logger.info(
            "prompt.first_start_dropped",
            chat_id=chat_id,
            command=command,
            dropped=dropped,
        )
    return dropped


def dropped_notice(count: int, *, command: str) -> str:
    """The #807 barrier's wording for messages a command dropped."""
    if count == 1:
        return (
            f"🗑️ Dropped 1 message sent just before /{command} — "
            "send it again if you still need it."
        )
    return (
        f"🗑️ Dropped {count} messages sent just before /{command} — "
        "send them again if you still need them."
    )
