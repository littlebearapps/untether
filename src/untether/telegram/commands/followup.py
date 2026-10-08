"""``/steer`` and ``/queue`` (#775).

- ``/steer <text>`` / ``/queue <text>`` override the follow-up mode for one
  message. The Telegram loop intercepts these before command dispatch and
  sends ``<text>`` down the normal prompt path with the override attached
  (see :mod:`untether.telegram.steer`).
- Bare ``/steer`` / ``/queue`` set the default for this topic (in a forum
  topic) or chat, and reply with the new state. The loop routes these to
  :func:`handle_followup_default_command` (topic-aware); the entry-point
  backends below exist so both commands appear in the Telegram command menu,
  and fall back to the chat-level default if reached directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...commands import CommandBackend, CommandContext, CommandResult
from ...logging import get_logger
from ..chat_prefs import ChatPrefsStore, resolve_prefs_path
from ..followup_mode import FollowupMode, resolve_followup_mode
from ..topic_state import TopicStateStore
from ..topics import _topic_key
from ..types import TelegramIncomingMessage
from .overrides import check_admin_or_private
from .reply import make_reply

if TYPE_CHECKING:
    from ...context import RunContext
    from ..bridge import TelegramBridgeConfig

logger = get_logger(__name__)

_DESCRIPTIONS: dict[FollowupMode, str] = {
    "steer": (
        "messages sent while a Claude run is working go straight into it — "
        "Claude picks them up at its next step."
    ),
    "queue": "messages sent while a run is working wait until it finishes.",
}


def followup_state_text(
    mode: FollowupMode, *, scope: str, engine: str | None = None
) -> str:
    other = "queue" if mode == "steer" else "steer"
    lines = [
        f"follow-up mode **set to** `{mode}` ({scope}) — {_DESCRIPTIONS[mode]}",
        f"one-off: `/{other} <text>` · switch: `/{other}`",
    ]
    if mode == "steer" and engine is not None and engine != "claude":
        lines.append(
            f"note: steer is Claude-only — {engine} runs still queue follow-ups."
        )
    return "\n\n".join(lines)


async def handle_followup_default_command(
    cfg: TelegramBridgeConfig,
    msg: TelegramIncomingMessage,
    command_id: str,
    ambient_context: RunContext | None,
    topic_store: TopicStateStore | None,
    chat_prefs: ChatPrefsStore | None,
    *,
    scope_chat_ids: frozenset[int] | None = None,
) -> None:
    """Bare ``/steer`` / ``/queue``: set the default here and show it."""
    from ..engine_defaults import resolve_engine_for_message

    reply = make_reply(cfg, msg)
    mode: FollowupMode = "steer" if command_id == "steer" else "queue"
    decision = await check_admin_or_private(
        cfg,
        msg,
        missing_sender="cannot verify sender for follow-up settings.",
        failed_member="failed to verify follow-up permissions.",
        denied="changing the follow-up mode is restricted to group admins.",
    )
    if not decision.allowed:
        await reply(text=decision.error_text or "not allowed.")
        return
    tkey = (
        _topic_key(msg, cfg, scope_chat_ids=scope_chat_ids)
        if topic_store is not None
        else None
    )
    if tkey is not None and topic_store is not None:
        await topic_store.set_followup_mode(tkey[0], tkey[1], mode)
        scope = "this topic"
    elif chat_prefs is not None:
        await chat_prefs.set_followup_mode(msg.chat_id, mode)
        scope = "this chat"
    else:
        await reply(text="follow-up settings are unavailable (no config path).")
        return
    engine = None
    try:
        engine = (
            await resolve_engine_for_message(
                runtime=cfg.runtime,
                context=ambient_context,
                explicit_engine=None,
                chat_id=msg.chat_id,
                topic_key=tkey,
                topic_store=topic_store,
                chat_prefs=chat_prefs,
            )
        ).engine
    except Exception:  # noqa: BLE001 — the notice is decoration only
        logger.debug("followup.engine_resolve_failed", exc_info=True)
    logger.info(
        "followup.set",
        chat_id=msg.chat_id,
        thread_id=tkey[1] if tkey is not None else None,
        mode=mode,
    )
    await reply(text=followup_state_text(mode, scope=scope, engine=engine))


class FollowupCommand:
    """Entry-point backend for ``/steer`` / ``/queue`` (command menu +
    chat-level fallback; the Telegram loop normally handles both first)."""

    def __init__(self, mode: FollowupMode, description: str) -> None:
        self.id = mode
        self.description = description
        self._mode: FollowupMode = mode

    async def handle(self, ctx: CommandContext) -> CommandResult | None:
        if ctx.args_text.strip():
            # Reached only if the loop didn't intercept (non-Telegram
            # transport): there is no live run to steer from here.
            return CommandResult(
                text=f"`/{self._mode} <text>` is only available in Telegram chats.",
                notify=False,
            )
        if ctx.config_path is None:
            return CommandResult(
                text="follow-up settings are unavailable (no config path).",
                notify=False,
            )
        prefs = ChatPrefsStore(resolve_prefs_path(ctx.config_path))
        await prefs.set_followup_mode(ctx.message.channel_id, self._mode)
        resolved = await resolve_followup_mode(
            chat_id=ctx.message.channel_id,
            thread_id=None,
            chat_prefs=prefs,
            topic_store=None,
        )
        return CommandResult(
            text=followup_state_text(resolved, scope="this chat"), notify=False
        )


STEER_BACKEND: CommandBackend = FollowupCommand(
    "steer", "Steer: send into the running Claude turn (or set the default)"
)
QUEUE_BACKEND: CommandBackend = FollowupCommand(
    "queue", "Queue: wait for the running turn (or set the default)"
)
