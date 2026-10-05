from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol

type ChannelId = int | str
type MessageId = int | str
type ThreadId = int | str


@dataclass(frozen=True, slots=True)
class MessageRef:
    channel_id: ChannelId
    message_id: MessageId
    raw: Any | None = field(default=None, compare=False, hash=False)
    thread_id: ThreadId | None = field(default=None, compare=False, hash=False)
    sender_id: int | None = field(default=None, compare=False, hash=False)


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    text: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SendOptions:
    reply_to: MessageRef | None = None
    notify: bool = True
    replace: MessageRef | None = None
    thread_id: ThreadId | None = None


# #823: what an outgoing Telegram call is for (``progress``, ``final``,
# ``bg_status``, ``approval_surface`` …), for its error log lines only. A
# message_id alone can't say which surface a failed edit was.
_MESSAGE_KIND: ContextVar[str | None] = ContextVar(
    "untether_message_kind", default=None
)


@contextmanager
def message_kind(kind: str | None) -> Iterator[None]:
    """Label the transport calls made inside the block (#823)."""
    token = _MESSAGE_KIND.set(kind)
    try:
        yield
    finally:
        _MESSAGE_KIND.reset(token)


def current_message_kind() -> str | None:
    return _MESSAGE_KIND.get()


class Transport(Protocol):
    async def close(self) -> None: ...

    async def send(
        self,
        *,
        channel_id: ChannelId,
        message: RenderedMessage,
        options: SendOptions | None = None,
    ) -> MessageRef | None: ...

    async def edit(
        self,
        *,
        ref: MessageRef,
        message: RenderedMessage,
        wait: bool = True,
    ) -> MessageRef | None: ...

    async def delete(self, *, ref: MessageRef) -> bool: ...
