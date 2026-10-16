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

A message is held from the moment it is routed, not only once it waits: it
takes a :class:`Route` before its first await (engine defaults, a steer
attempt and its "queued instead" notice can take a second), and a first
start dropped after that point drops it too when it gets to the wait
(:func:`dropped_since`). Otherwise a ``/new`` sent just after it would miss
it and it would start the new conversation instead.
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

# Drops that a message still on its way to the wait must see: key →
# (generation of the drop, command). Kept only while a route taken before the
# drop is open, so it stays empty when nothing is in flight.
_generation = 0
_DROPPED: dict[FirstStartKey, tuple[int, str]] = {}
_OPEN_ROUTES: dict[int, int] = {}


@dataclass(slots=True, eq=False)
class Route:
    """A prompt on its way to the first-start wait (see the module doc)."""

    generation: int
    closed: bool = False


def route() -> Route:
    """Mark a prompt routed. Call it before the prompt's first await and
    :func:`close_route` once it has passed the wait (or given up)."""
    _OPEN_ROUTES[_generation] = _OPEN_ROUTES.get(_generation, 0) + 1
    return Route(generation=_generation)


def close_route(route_: Route) -> None:
    """Forget ``route_`` (idempotent) and the drops no open route predates."""
    if route_.closed:
        return
    route_.closed = True
    count = _OPEN_ROUTES.get(route_.generation, 0) - 1
    if count > 0:
        _OPEN_ROUTES[route_.generation] = count
    else:
        _OPEN_ROUTES.pop(route_.generation, None)
    oldest = min(_OPEN_ROUTES, default=None)
    for key, (generation, _) in list(_DROPPED.items()):
        if oldest is None or generation <= oldest:
            del _DROPPED[key]


def dropped_since(route_: Route, key: FirstStartKey) -> str | None:
    """The command (``"cancel"`` / ``"new"``) that dropped ``key``'s first
    start after ``route_`` was taken, or None. A message that finds no first
    start to wait on checks this: had it got there sooner it would have been
    waiting, and dropped with the others."""
    record = _DROPPED.get(key)
    if record is not None and record[0] > route_.generation:
        return record[1]
    return None


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
    messages were dropped (a message still on its way to the wait is not
    counted here: it finds the drop through :func:`dropped_since`)."""
    global _generation
    dropped = 0
    for key, start in list(_PENDING.items()):
        if start.chat_id != chat_id:
            continue
        if thread_filter is not None and not thread_filter(start.thread_id):
            continue
        if start.waiting:
            start.dropped_by = command
            dropped += start.waiting
        if _OPEN_ROUTES:
            _generation += 1
            _DROPPED[key] = (_generation, command)
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
