"""Steer follow-ups into a running Claude session (#775).

The loop calls :func:`maybe_steer` once per prompt, right before the prompt
would be queued behind a session (``dispatch_prompt_run``, i.e. after the
forward coalescer has produced the final prompt text). When the resolved
follow-up mode is ``steer`` and the chat's session is a live Claude process
still accepting steers, the text is written into it immediately and the user
gets a ``↪️ Steered into the current run`` reply; otherwise the caller carries
on down the unchanged queue path.

Only plain text and voice transcripts steer — files, media groups, forwards
and commands always queue (the caller passes ``steerable=False``). A pending
AskUserQuestion wins: plain text answers it before this is reached, and a
voice note is not steered past it either.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, Literal

from ..logging import get_logger
from ..model import ResumeToken
from ..transport import MessageRef, RenderedMessage, SendOptions
from .followup_mode import FollowupMode, normalize_followup_mode, resolve_followup_mode

if TYPE_CHECKING:
    from .bridge import TelegramBridgeConfig
    from .chat_prefs import ChatPrefsStore
    from .topic_state import TopicStateStore

logger = get_logger(__name__)

FOLLOWUP_COMMAND_IDS: frozenset[str] = frozenset({"steer", "queue"})

STEERED_ACK = "\N{RIGHTWARDS ARROW WITH HOOK}\N{VARIATION SELECTOR-16} Steered into the current run."

FallbackReason = Literal["engine", "no_live", "closing"]

# Default-mode fallback notices are sent once per (chat, thread, reason, run)
# so a chat set to steer isn't nagged on every message (#775).
_NOTICE_CAP = 256
_NOTICED: OrderedDict[tuple[Any, ...], None] = OrderedDict()


def split_followup_command(
    command_id: str | None, args_text: str | None
) -> tuple[FollowupMode, str] | None:
    """``/steer <text>`` / ``/queue <text>`` → (mode, text); None otherwise
    (other commands, or the bare form that sets the default)."""
    if command_id not in FOLLOWUP_COMMAND_IDS:
        return None
    text = (args_text or "").strip()
    mode = normalize_followup_mode(command_id)
    if not text or mode is None:
        return None
    return mode, text


def fallback_notice(reason: FallbackReason, engine: str) -> str:
    arrow = "\N{RIGHTWARDS ARROW WITH HOOK}\N{VARIATION SELECTOR-16}"
    if reason == "engine":
        return f"{arrow} Steer isn't supported on {engine} — queued instead."
    if reason == "closing":
        return f"{arrow} The current run is ending — queued instead."
    return f"{arrow} No live Claude run to steer — queued instead."


def _active_run_ref(
    running_tasks: Mapping[MessageRef, Any], chat_id: int, thread_id: int | None
) -> MessageRef | None:
    """The progress ref of a run doing work in this chat/topic, if any (a
    live session idling between turns doesn't count)."""
    from ..runner_bridge import running_task_is_live_idle

    for ref, task in running_tasks.items():
        if ref.channel_id != chat_id:
            continue
        if thread_id is not None and ref.thread_id not in (None, thread_id):
            continue
        done = getattr(task, "done", None)
        if done is not None and done.is_set():
            continue
        if running_task_is_live_idle(task):
            continue
        return ref
    return None


def _should_notice(key: tuple[Any, ...]) -> bool:
    if key in _NOTICED:
        return False
    _NOTICED[key] = None
    while len(_NOTICED) > _NOTICE_CAP:
        _NOTICED.popitem(last=False)
    return True


async def _send(
    cfg: TelegramBridgeConfig,
    *,
    chat_id: int,
    user_msg_id: int,
    thread_id: int | None,
    text: str,
) -> None:
    try:
        await cfg.exec_cfg.transport.send(
            channel_id=chat_id,
            message=RenderedMessage(text=text),
            options=SendOptions(
                reply_to=MessageRef(
                    channel_id=chat_id, message_id=user_msg_id, thread_id=thread_id
                ),
                notify=False,
                thread_id=thread_id,
            ),
        )
    except Exception:  # noqa: BLE001 — a notice must never break dispatch
        logger.debug("steer.notice_failed", exc_info=True)


async def maybe_steer(
    cfg: TelegramBridgeConfig,
    *,
    chat_id: int,
    user_msg_id: int,
    thread_id: int | None,
    topic_thread_id: int | None,
    prompt_text: str,
    engine: str,
    resume_token: ResumeToken | None,
    override: str | None,
    resolve_token: Callable[[], Awaitable[ResumeToken | None]] | None = None,
    running_tasks: Mapping[MessageRef, Any],
    chat_prefs: ChatPrefsStore | None,
    topic_store: TopicStateStore | None,
    run_options: Callable[[ResumeToken], Awaitable[object]] | None = None,
) -> bool:
    """Steer ``prompt_text`` into the chat's live Claude session if the
    resolved follow-up mode says so. Returns True when it was written (the
    caller must not also queue it); False → carry on queueing."""
    explicit = normalize_followup_mode(override)
    mode = explicit or await resolve_followup_mode(
        chat_id=chat_id,
        thread_id=topic_thread_id,
        chat_prefs=chat_prefs,
        topic_store=topic_store,
        default=getattr(cfg, "followup_mode", None),
    )
    if mode != "steer":
        return False
    if resume_token is None and resolve_token is not None:
        # Only looked up once steer is wanted — the queue path resolves the
        # session itself, so the common case pays nothing extra.
        resume_token = await resolve_token()

    target_engine = resume_token.engine if resume_token is not None else engine
    reason: FallbackReason | None = None
    session_id: str | None = None
    if target_engine != "claude":
        reason = "engine"
    elif resume_token is None or resume_token.is_continue or not resume_token.value:
        reason = "no_live"
    else:
        session_id = resume_token.value

    if session_id is not None:
        from ..runners.claude import (
            get_live_session,
            get_pending_ask_request,
            steer_into_session,
        )

        if get_pending_ask_request(channel_id=chat_id) is not None:
            # The CLI is blocked on the question; an answer, not a steer, is
            # what it needs. Plain text already answered it upstream.
            logger.info("steer.skipped", chat_id=chat_id, reason="ask_pending")
            return False
        live = get_live_session(session_id)
        if live is None:
            reason = "no_live"
        else:
            options: object = None
            has_options = run_options is not None
            if run_options is not None:
                try:
                    options = await run_options(resume_token)  # type: ignore[arg-type]
                except Exception:  # noqa: BLE001 — unknown options: let queue decide
                    logger.warning("steer.options_resolve_failed", exc_info=True)
                    return False
            from ..runner_bridge import (
                pop_followup_anchor,
                register_followup_anchor,
            )

            command_uuid = str(uuid.uuid4())
            # If the CLI runs the steer as its own turn (written after the
            # turn's last tool call, F6) that turn replies to this message.
            # Folded mid-turn, the runner reports it absorbed and the bridge
            # drops the anchor; if the session dies first, the run-end sweep
            # tells the user it wasn't run.
            register_followup_anchor(
                command_uuid,
                session_id=session_id,
                reply_to=MessageRef(
                    channel_id=chat_id, message_id=user_msg_id, thread_id=thread_id
                ),
                placeholder=None,
            )
            kwargs: dict[str, Any] = {"command_uuid": command_uuid}
            if has_options:
                kwargs["run_options"] = options
            outcome = await steer_into_session(session_id, prompt_text, **kwargs)
            if outcome == "steered":
                logger.info(
                    "steer.written",
                    chat_id=chat_id,
                    user_msg_id=user_msg_id,
                    session_id=session_id,
                    command_uuid=command_uuid,
                    explicit=explicit is not None,
                    text_len=len(prompt_text),
                )
                await _send(
                    cfg,
                    chat_id=chat_id,
                    user_msg_id=user_msg_id,
                    thread_id=thread_id,
                    text=STEERED_ACK,
                )
                return True
            pop_followup_anchor(command_uuid)
            if outcome == "options_changed":
                # The queue path closes the idle process and resumes with the
                # new settings (and says so) — nothing to add here.
                logger.info("steer.fallback", chat_id=chat_id, reason="options_changed")
                return False
            reason = "closing" if outcome == "window_closed" else "no_live"

    assert reason is not None
    active = _active_run_ref(running_tasks, chat_id, thread_id)
    logger.info(
        "steer.fallback",
        chat_id=chat_id,
        user_msg_id=user_msg_id,
        reason=reason,
        engine=target_engine,
        explicit=explicit is not None,
        busy=active is not None,
    )
    if explicit is not None:
        notify = True
    elif active is None:
        # Default steer mode with nothing running: the message simply runs.
        notify = False
    else:
        notify = _should_notice(
            (chat_id, thread_id, reason, target_engine, active.message_id)
        )
    if notify:
        await _send(
            cfg,
            chat_id=chat_id,
            user_msg_id=user_msg_id,
            thread_id=thread_id,
            text=fallback_notice(reason, target_engine),
        )
    return False
