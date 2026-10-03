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
from ..logging import get_logger
from ..transport import MessageRef, RenderedMessage, SendOptions, Transport

if TYPE_CHECKING:
    from .bridge import TelegramBridgeConfig
    from .types import TelegramCallbackQuery

logger = get_logger(__name__)

EXPIRED_TOAST = "This button has expired"
RUNNING_TOAST = "Running once"
RUNNING_NOTE = "\N{BLACK RIGHT-POINTING TRIANGLE}\N{VARIATION SELECTOR-16} Running once despite the daily budget."


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
    source = _skipped_source(context)
    logger.warning(
        "cost_budget.run_blocked",
        scope="per_day",
        chat_id=chat_id,
        thread_id=thread_id,
        trigger=source or (context.trigger_source if context else None),
        attended=source is None,
        daily_cost=round(daily, 4),
        budget=limit,
    )
    if progress_ref is not None:
        # A resumed prompt already showed "queued"; it isn't going to run.
        with contextlib.suppress(Exception):
            await transport.delete(ref=progress_ref)
    text = daily_block_text(daily, limit, skipped=source)
    extra: dict[str, object] = {}
    if source is None:
        token = register_pending_run(chat_id, rerun, notice=text)
        extra["reply_markup"] = {
            "inline_keyboard": [
                [
                    {
                        "text": RUN_ANYWAY_LABEL,
                        "callback_data": run_anyway_callback_data(token),
                    }
                ]
            ]
        }
    try:
        await transport.send(
            channel_id=chat_id,
            message=RenderedMessage(text=text, extra=extra),
            options=SendOptions(
                reply_to=MessageRef(channel_id=chat_id, message_id=user_msg_id),
                # A skipped trigger replies to its own (already pushed)
                # "Scheduled" announcement; a refused prompt pushes.
                notify=source is None,
                thread_id=thread_id,
            ),
        )
    except Exception:  # noqa: BLE001 — a failed notice must not crash the worker
        logger.warning("cost_budget.notice_failed", chat_id=chat_id, exc_info=True)


def _skipped_source(context: RunContext | None) -> str | None:
    """The trigger to name in a skip notice, or None for a person's prompt.

    Crons and webhooks (nobody there) and ``/loop`` fires (they repeat on
    their own) are skipped with a notice and no button — one per fire,
    replying to the fire's own announcement; a prompt — including an ``/at``
    run someone scheduled — gets **Run anyway**.
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
