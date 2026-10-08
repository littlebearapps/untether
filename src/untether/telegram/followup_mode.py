"""Follow-up mode (#775): what a message sent during a live Claude run does.

``queue`` (default) holds the message until the running turn ends, then runs
it as its own turn (#776's live injection, or ``--resume`` once the process
has exited). ``steer`` writes it into the running session straight away:
Claude reads it at the next tool boundary and folds it into the turn it is
already running (probe F5), or — after the turn's last tool call — runs it as
the next turn in the same process (F6).

Resolution mirrors :func:`listen_mode.resolve_listen_mode`: topic override →
chat default → ``[transports.telegram] followup_mode`` → ``queue``. Stored in
the chat/topic prefs, deliberately NOT in ``EngineOverrides`` (whose
rebuild-by-hand sites drop unknown fields).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .chat_prefs import ChatPrefsStore
    from .topic_state import TopicStateStore

FollowupMode = Literal["queue", "steer"]

FOLLOWUP_MODES: tuple[FollowupMode, ...] = ("queue", "steer")
DEFAULT_FOLLOWUP_MODE: FollowupMode = "queue"


def normalize_followup_mode(value: str | None) -> FollowupMode | None:
    """``"queue"``/``"steer"`` (case/whitespace-insensitive) or ``None``."""
    if value is None:
        return None
    value = value.strip().lower()
    if value == "steer":
        return "steer"
    if value == "queue":
        return "queue"
    return None


async def resolve_followup_mode(
    *,
    chat_id: int,
    thread_id: int | None,
    chat_prefs: ChatPrefsStore | None,
    topic_store: TopicStateStore | None,
    default: str | None = None,
) -> FollowupMode:
    if topic_store is not None and thread_id is not None:
        topic_mode = await topic_store.get_followup_mode(chat_id, thread_id)
        if topic_mode is not None:
            return topic_mode
    if chat_prefs is not None:
        chat_mode = await chat_prefs.get_followup_mode(chat_id)
        if chat_mode is not None:
            return chat_mode
    return normalize_followup_mode(default) or DEFAULT_FOLLOWUP_MODE
