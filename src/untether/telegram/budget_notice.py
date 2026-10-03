"""Telegram side of the daily budget gate (#896).

``refuse_run`` posts the "Daily budget reached" notice for a refused run (with
a one-shot **Run anyway** button for attended chats); the button's callback is
routed here by ``loop.route_update`` (``budget:run:<token>``).
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ..budget_gate import (
    RUN_ANYWAY_LABEL,
    RUN_ANYWAY_PREFIX,
    claim_pending_run,
    daily_block_text,
    register_pending_run,
    run_anyway_callback_data,
)
from ..context import RunContext, unattended_trigger
from ..cost_tracker import _today as budget_day
from ..logging import get_logger
from ..transport import MessageRef, RenderedMessage, SendOptions, Transport

if TYPE_CHECKING:
    from .bridge import TelegramBridgeConfig
    from .types import TelegramCallbackQuery

logger = get_logger(__name__)

EXPIRED_TOAST = "This button has expired"
RUNNING_TOAST = "Running once"
RUNNING_NOTE = "\N{BLACK RIGHT-POINTING TRIANGLE}\N{VARIATION SELECTOR-16} Running once despite the daily budget."


# (chat_id, trigger source) → the budget day its skip notice was sent on.
# Unattended refusals repeat on every /loop fire and cron tick until
# midnight; the chat hears about each trigger once a day (each refusal is
# still logged). In-memory: a restart may repeat one notice, which is fine.
_SKIP_NOTICED: dict[tuple[int, str], str] = {}


def _first_skip_today(chat_id: int, source: str) -> bool:
    """True (and remembered) the first time ``source`` is skipped in
    ``chat_id`` on the budget's current day (host local, like the total)."""
    today = budget_day()
    for key in [k for k, day in _SKIP_NOTICED.items() if day != today]:
        del _SKIP_NOTICED[key]
    key = (chat_id, source)
    if _SKIP_NOTICED.get(key) == today:
        return False
    _SKIP_NOTICED[key] = today
    return True


async def skip_unattended_run(
    transport: Transport,
    *,
    chat_id: int,
    context: RunContext | None,
    daily: float,
    limit: float,
    reply_to_msg_id: int | None = None,
    thread_id: int | None = None,
    stage: str = "run",
) -> None:
    """Refuse a cron / webhook / loop fire: log it, and tell the chat once a
    day per trigger (no button — nobody is there to tap it)."""
    source = _skipped_source(context) or (
        context.trigger_source if context is not None else None
    )
    label = source or "a scheduled run"
    first = _first_skip_today(chat_id, label)
    logger.warning(
        "cost_budget.run_blocked",
        scope="per_day",
        chat_id=chat_id,
        thread_id=thread_id,
        trigger=source,
        attended=False,
        stage=stage,
        notice_sent=first,
        daily_cost=round(daily, 4),
        budget=limit,
    )
    if not first:
        return
    await _send_notice(
        transport,
        chat_id=chat_id,
        thread_id=thread_id,
        reply_to_msg_id=reply_to_msg_id,
        text=daily_block_text(daily, limit, skipped=label),
        extra={},
        # A skipped trigger is quiet, like its "Scheduled" announcement.
        notify=False,
    )


async def refuse_run(
    transport: Transport,
    *,
    chat_id: int,
    user_msg_id: int,
    thread_id: int | None,
    context: RunContext | None,
    progress_ref: MessageRef | None,
    daily: float,
    limit: float,
    rerun: Callable[[], Awaitable[None]],
) -> None:
    """Tell the chat a run was refused by the daily budget gate."""
    if progress_ref is not None:
        # A resumed prompt already showed "queued"; it isn't going to run.
        with contextlib.suppress(Exception):
            await transport.delete(ref=progress_ref)
    source = _skipped_source(context)
    if source is not None:
        await skip_unattended_run(
            transport,
            chat_id=chat_id,
            context=context,
            daily=daily,
            limit=limit,
            # Replies to the fire's own announcement.
            reply_to_msg_id=user_msg_id,
            thread_id=thread_id,
        )
        return
    logger.warning(
        "cost_budget.run_blocked",
        scope="per_day",
        chat_id=chat_id,
        thread_id=thread_id,
        trigger=context.trigger_source if context else None,
        attended=True,
        notice_sent=True,
        daily_cost=round(daily, 4),
        budget=limit,
    )
    text = daily_block_text(daily, limit, skipped=None)
    token = register_pending_run(chat_id, rerun, notice=text)
    extra: dict[str, object] = {
        "reply_markup": {
            "inline_keyboard": [
                [
                    {
                        "text": RUN_ANYWAY_LABEL,
                        "callback_data": run_anyway_callback_data(token),
                    }
                ]
            ]
        }
    }
    await _send_notice(
        transport,
        chat_id=chat_id,
        thread_id=thread_id,
        reply_to_msg_id=user_msg_id,
        text=text,
        extra=extra,
        notify=True,  # a refused prompt pushes
    )


async def _send_notice(
    transport: Transport,
    *,
    chat_id: int,
    thread_id: int | None,
    reply_to_msg_id: int | None,
    text: str,
    extra: dict[str, object],
    notify: bool,
) -> None:
    reply_to = (
        MessageRef(channel_id=chat_id, message_id=reply_to_msg_id)
        if reply_to_msg_id is not None
        else None
    )
    try:
        await transport.send(
            channel_id=chat_id,
            message=RenderedMessage(text=text, extra=extra),
            options=SendOptions(reply_to=reply_to, notify=notify, thread_id=thread_id),
        )
    except Exception:  # noqa: BLE001 — a failed notice must not crash the worker
        logger.warning("cost_budget.notice_failed", chat_id=chat_id, exc_info=True)


def _skipped_source(context: RunContext | None) -> str | None:
    """The trigger to name in a skip notice, or None for a person's prompt.

    Crons and webhooks (nobody there) and ``/loop`` fires (they repeat on
    their own) are skipped with a notice and no button — once a day per
    trigger, replying to the fire's own announcement; a prompt — including
    an ``/at`` run someone scheduled — gets **Run anyway**.
    """
    source = unattended_trigger(context)
    if source is not None:
        return source
    raw = context.trigger_source if context is not None else None
    if raw and raw.startswith("loop:"):
        return raw
    return None


def is_run_anyway_callback(data: str | None) -> bool:
    return bool(data) and data.startswith(RUN_ANYWAY_PREFIX)  # type: ignore[union-attr]


async def handle_budget_run_callback(
    cfg: TelegramBridgeConfig, query: TelegramCallbackQuery
) -> None:
    """**Run anyway**: start the refused run once, in its own chat only."""
    token = (query.data or "")[len(RUN_ANYWAY_PREFIX) :]
    pending = claim_pending_run(token, chat_id=query.chat_id)
    if pending is None:
        logger.info(
            "cost_budget.run_anyway_expired",
            chat_id=query.chat_id,
            message_id=query.message_id,
        )
        with contextlib.suppress(Exception):
            await cfg.bot.answer_callback_query(
                callback_query_id=query.callback_query_id, text=EXPIRED_TOAST
            )
        return
    with contextlib.suppress(Exception):
        await cfg.bot.answer_callback_query(
            callback_query_id=query.callback_query_id, text=RUNNING_TOAST
        )
    logger.warning(
        "cost_budget.run_anyway",
        chat_id=query.chat_id,
        message_id=query.message_id,
        sender_id=query.sender_id,
    )
    notice_ref = MessageRef(channel_id=query.chat_id, message_id=query.message_id)
    text = pending.notice
    with contextlib.suppress(Exception):
        await cfg.exec_cfg.transport.edit(
            ref=notice_ref,
            message=RenderedMessage(
                text=f"{text}\n{RUNNING_NOTE}" if text else RUNNING_NOTE,
                extra={"reply_markup": {"inline_keyboard": []}},
            ),
        )
    await pending.rerun()
