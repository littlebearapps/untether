"""Keep a session scope's follow-ups in the order they arrived (#1164).

Between the forward coalescer handing a prompt over and the prompt taking its
place (queued behind its session, waiting on the chat's first start, steered
into a live run, or starting a run) there are several awaits: engine
defaults, the steer attempt and its "queued instead" notice, the stored
session lookup, the ``queued`` card. A Telegram send there takes 1-3 s in a
group, and the notice goes to only one message per run, so a later follow-up
used to overtake an earlier one and the finals came out of order.

Each prompt takes a :class:`Ticket` before its first await
(:func:`take`, in dispatch order) and waits for its turn
(:func:`wait_turn`) before any of those steps; :func:`done` hands the turn
to the next prompt once this one has its place. Scope = the chat, thread
and session owner (the per-sender key in groups), so another member's
message or another topic is never held.

A prompt keeps its turn while it waits on a first start (``first_start``):
the ones behind it would wait on the same start, and keeping the turn wakes
them in arrival order.

A prompt still in line has no ``queued`` card and isn't waiting on a first
start, so ``/cancel`` and ``/new`` reach it here (:func:`drop_waiting`): every
prompt that hasn't started its turn yet is dropped and told so when its turn
comes, like the #807 barrier's and ``first_start``'s dropped messages. One
that has started (sending its ``queued`` card, say) carries on as before.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import anyio

from ..logging import get_logger

if TYPE_CHECKING:
    from .topics import ThreadFilter

logger = get_logger(__name__)

# ``(chat_id, thread_id, owner)``: ``drop_waiting`` matches the first two.
type Scope = tuple[Any, ...]


@dataclass(slots=True, eq=False)
class Ticket:
    scope: Scope
    turn: anyio.Event = field(default_factory=anyio.Event)
    started: bool = False
    finished: bool = False
    dropped_by: str | None = None


_LINES: dict[Scope, deque[Ticket]] = {}


def take(scope: Scope) -> Ticket:
    """Join ``scope``'s line. Call it before the prompt's first await."""
    ticket = Ticket(scope=scope)
    line = _LINES.setdefault(scope, deque())
    line.append(ticket)
    if line[0] is ticket:
        ticket.turn.set()
    return ticket


async def wait_turn(ticket: Ticket | None) -> str | None:
    """Wait until every prompt that arrived earlier in the scope is done
    (no-op for an unordered prompt). Returns the command (``"cancel"`` /
    ``"new"``) that dropped the prompt while it was in line, else None."""
    if ticket is None:
        return None
    await ticket.turn.wait()
    ticket.started = True
    return ticket.dropped_by


def done(ticket: Ticket | None) -> None:
    """Leave the line (idempotent): the prompt has its place, or gave up."""
    if ticket is None or ticket.finished:
        return
    ticket.finished = True
    line = _LINES.get(ticket.scope)
    if line is None:
        return
    head = line[0] is ticket
    try:
        line.remove(ticket)
    except ValueError:
        return
    if not line:
        del _LINES[ticket.scope]
    elif head:
        line[0].turn.set()


def drop_waiting(
    chat_id: int, *, thread_filter: ThreadFilter | None, command: str
) -> int:
    """Drop the prompts in this chat's (or thread's) lines that haven't
    started their turn yet; each replies when its turn comes and never runs.
    Returns how many were dropped."""
    dropped = 0
    for scope, line in _LINES.items():
        if scope[0] != chat_id:
            continue
        if thread_filter is not None and not thread_filter(scope[1]):
            continue
        for ticket in line:
            if not ticket.started and ticket.dropped_by is None:
                ticket.dropped_by = command
                dropped += 1
    if dropped:
        logger.info(
            "prompt.dropped_in_line",
            chat_id=chat_id,
            command=command,
            dropped=dropped,
        )
    return dropped
