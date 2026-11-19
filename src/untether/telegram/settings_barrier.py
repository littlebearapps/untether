"""A prompt waits for the settings commands sent before it in its chat (#1265).

Commands run alongside message handling, and a settings change awaits a
Telegram admin lookup (#1042) and the prefs store before it lands, so a
prompt sent a second or two after ``/planmode auto`` used to run in the old
mode. A settings command registers here synchronously, in update order, when
it is dispatched (:func:`start_soon`); a prompt waits for every one still in
flight in its chat that was sent before it (:func:`wait_before`, by Telegram
message id, which only grows within a chat). A prompt sent before the command
never waits on it. The wait is bounded so a stuck command can't hold prompts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import anyio

from ..logging import get_logger

logger = get_logger(__name__)

# Commands (typed or tapped) that change what the chat's next prompt runs
# with: engine, model, reasoning, permission mode, context, follow-up mode,
# listen mode, verbosity and the /config pages.
SETTINGS_COMMANDS = frozenset(
    {
        "agent",
        "config",
        "ctx",
        "listen",
        "model",
        "planmode",
        "queue",
        "reasoning",
        "steer",
        "trigger",
        "verbose",
    }
)
WAIT_S = 15.0

_IN_FLIGHT: dict[int, dict[int, anyio.Event]] = {}


class _TaskGroup(Protocol):
    def start_soon(self, func: Callable[..., Awaitable[Any]], *args: Any) -> None: ...


def start_soon(
    task_group: _TaskGroup,
    chat_id: int,
    message_id: int,
    func: Callable[..., Awaitable[Any]],
    *args: Any,
) -> None:
    """Start a settings command, registered before this returns."""
    done = anyio.Event()
    _IN_FLIGHT.setdefault(chat_id, {})[message_id] = done

    async def _run() -> None:
        try:
            await func(*args)
        finally:
            done.set()
            chat = _IN_FLIGHT.get(chat_id)
            if chat is not None and chat.get(message_id) is done:
                del chat[message_id]
                if not chat:
                    del _IN_FLIGHT[chat_id]

    task_group.start_soon(_run)


class Guarded:
    """A task group whose ``start_soon`` goes through :func:`start_soon`."""

    __slots__ = ("_chat_id", "_message_id", "_task_group")

    def __init__(self, task_group: _TaskGroup, chat_id: int, message_id: int) -> None:
        self._task_group = task_group
        self._chat_id = chat_id
        self._message_id = message_id

    def start_soon(self, func: Callable[..., Awaitable[Any]], *args: Any) -> None:
        start_soon(self._task_group, self._chat_id, self._message_id, func, *args)


async def wait_before(chat_id: int, message_id: int) -> None:
    """Wait for the chat's settings commands sent before *message_id*."""
    pending = [
        done for mid, done in _IN_FLIGHT.get(chat_id, {}).items() if mid < message_id
    ]
    if not pending:
        return
    started = anyio.current_time()
    with anyio.move_on_after(WAIT_S) as scope:
        for done in pending:
            await done.wait()
    logger.info(
        "prompt.settings_wait",
        chat_id=chat_id,
        commands=len(pending),
        waited_s=round(anyio.current_time() - started, 2),
        timed_out=scope.cancelled_caught,
    )
