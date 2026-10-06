"""Opt-in "only the originator can approve" for multi-user chats (#388).

With ``[transports.telegram] approval_originator_only = true``, a Claude
approval button (``claude_control:``, incl. the orphan-approval surface) or an
AskUserQuestion answer (``aq:`` buttons and typed replies) is accepted only
from the Telegram user whose message started the run. Runs with no human
originator — cron, webhook, ``/at`` and loop fires — stay answerable by any
allowed user. Off by default: every allowed user can answer, as before.

The run's originator is looked up from the triggering message: the loop notes
``(chat_id, message_id) -> sender_id`` for every allowed incoming message and
``_run_engine`` sets the run's sender contextvar from it, which the Claude
runner records against each pending request (``control_request_originator``).
"""

from __future__ import annotations

from collections import OrderedDict

from ..logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "NOT_ORIGINATOR_TEXT",
    "callback_originator_mismatch",
    "message_sender",
    "note_message_sender",
    "request_originator_mismatch",
]

NOT_ORIGINATOR_TEXT = "Only the person who started this run can answer this."

_SENDERS: OrderedDict[tuple[int, int], int] = OrderedDict()
_SENDERS_MAX = 4096


def note_message_sender(chat_id: int, message_id: int, sender_id: int | None) -> None:
    """Remember who sent an incoming message (bounded, oldest evicted)."""
    if sender_id is None:
        return
    key = (chat_id, message_id)
    _SENDERS[key] = sender_id
    _SENDERS.move_to_end(key)
    while len(_SENDERS) > _SENDERS_MAX:
        _SENDERS.popitem(last=False)


def message_sender(chat_id: int, message_id: int) -> int | None:
    """The sender of an incoming message, or None (a bot message — e.g. a
    cron/webhook announcement — or one evicted from the window)."""
    return _SENDERS.get((chat_id, message_id))


def request_originator_mismatch(request_id: str, sender_id: int | None) -> int | None:
    """The originator of a pending request when *sender_id* is someone else,
    else None (same user, or no human originator)."""
    from ..runners.claude import control_request_originator

    originator = control_request_originator(request_id)
    if originator is None or originator == sender_id:
        return None
    return originator


def callback_originator_mismatch(
    command_id: str, args_text: str, chat_id: int, sender_id: int | None
) -> tuple[str, int] | None:
    """``(request_id, originator)`` when a ``claude_control`` / ``aq`` tap
    comes from someone other than the run's originator, else None."""
    if command_id == "claude_control":
        _action, _, request_id = (args_text or "").partition(":")
    elif command_id == "aq":
        from ..runners.claude import get_ask_question_flow

        flow = get_ask_question_flow(channel_id=chat_id)
        if flow is None:
            return None
        request_id = flow.request_id
    else:
        return None
    if not request_id:
        return None
    originator = request_originator_mismatch(request_id, sender_id)
    if originator is None:
        return None
    return request_id, originator
