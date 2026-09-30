from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import anyio

from ...commands import CommandContext, get_command
from ...config import ConfigError
from ...logging import get_logger
from ...model import EngineId, ResumeToken
from ...runner_bridge import RunningTasks, register_ephemeral_message
from ...runners.run_options import EngineRunOptions
from ...scheduler import ThreadScheduler
from ...transport import MessageRef, RenderedMessage, SendOptions
from ...utils.error_display import user_safe_error
from ..files import split_command_args
from ..types import TelegramCallbackQuery, TelegramIncomingMessage
from .executor import _TelegramCommandExecutor

if TYPE_CHECKING:
    from ..bridge import TelegramBridgeConfig

logger = get_logger(__name__)


def _parse_callback_data(data: str) -> tuple[str, str]:
    """Parse callback data into command_id and args_text.

    Format: command_id:args... -> (command_id, args...)
    """
    parts = data.split(":", 1)
    command_id = parts[0].lower()
    if not command_id:
        logger.warning("callback.parse_failed", data=data[:64])
    args_text = parts[1] if len(parts) > 1 else ""
    return command_id, args_text


def _early_answer_toast(
    backend: object,
    args_text: str,
    chat_id: int,
    claim_owner: str | None = None,
) -> str | None:
    """Call a backend's ``early_answer_toast`` hook, passing the chat it fired in.

    #715: the toast is chosen from registry state (is there a live flow? was
    one just answered?), and that state is per-chat — so a hook that cannot
    see which chat tapped can only answer globally, and in a fleet running
    concurrent chats it answers about someone else's run.

    #685: ``claim_owner`` (the callback query id) lets a hook reserve the
    request synchronously, before the first ``await`` of the dispatch, so two
    concurrent taps can't both toast success. A hook that reserves must also
    expose ``release_early_claim(owner)``; the dispatch ``finally`` calls it.

    ``early_answer_toast`` is an internal duck-typed hook, not part of the
    ``CommandBackend`` Protocol, so a backend may still carry an older
    ``(args_text, *, channel_id)`` or ``(args_text)`` signature. Fall back to
    them rather than letting a ``TypeError`` escape: this runs before
    ``backend.handle`` inside the dispatch ``try``, and an exception here would
    take out the whole callback — a strictly worse outcome than a slightly
    less specific toast.
    """
    hook = getattr(backend, "early_answer_toast", None)
    if hook is None:
        return None
    if claim_owner is not None:
        try:
            return hook(args_text, channel_id=chat_id, claim_owner=claim_owner)
        except TypeError:
            pass
    try:
        return hook(args_text, channel_id=chat_id)
    except TypeError:
        return hook(args_text)


def _release_early_claim(backend: object, owner: str | None) -> None:
    """Release any claim ``owner``'s early toast reserved (#685). Never raises."""
    if backend is None or owner is None:
        return
    release = getattr(backend, "release_early_claim", None)
    if release is None:
        return
    try:
        release(owner)
    except Exception:  # noqa: BLE001
        logger.debug("callback.release_claim_failed", exc_info=True)


async def _dispatch_command(
    cfg: TelegramBridgeConfig,
    msg: TelegramIncomingMessage,
    text: str,
    command_id: str,
    args_text: str,
    running_tasks: RunningTasks,
    scheduler: ThreadScheduler,
    on_thread_known: Callable[[ResumeToken, anyio.Event], Awaitable[None]] | None,
    stateful_mode: bool,
    default_engine_override: EngineId | None,
    engine_overrides_resolver: Callable[[EngineId], Awaitable[EngineRunOptions | None]]
    | None,
) -> None:
    allowlist = cfg.runtime.allowlist
    chat_id = msg.chat_id
    user_msg_id = msg.message_id
    reply_ref = (
        MessageRef(
            channel_id=chat_id,
            message_id=msg.reply_to_message_id,
            thread_id=msg.thread_id,
        )
        if msg.reply_to_message_id is not None
        else None
    )
    executor = _TelegramCommandExecutor(
        exec_cfg=cfg.exec_cfg,
        runtime=cfg.runtime,
        running_tasks=running_tasks,
        scheduler=scheduler,
        on_thread_known=on_thread_known,
        engine_overrides_resolver=engine_overrides_resolver,
        chat_id=chat_id,
        user_msg_id=user_msg_id,
        thread_id=msg.thread_id,
        show_resume_line=cfg.show_resume_line,
        stateful_mode=stateful_mode,
        default_engine_override=default_engine_override,
    )
    message_ref = MessageRef(
        channel_id=chat_id,
        message_id=user_msg_id,
        thread_id=msg.thread_id,
        sender_id=msg.sender_id,
        raw=msg.raw,
    )
    logger.info("command.dispatch", command=command_id, chat_id=chat_id)
    try:
        backend = get_command(command_id, allowlist=allowlist, required=False)
    except ConfigError as exc:
        # #201: don't send raw exception text to Telegram (may include paths,
        # URLs, or exception class names).
        await executor.send(
            f"error: {user_safe_error(exc, fallback='command lookup failed')}",
            reply_to=message_ref,
            notify=True,
        )
        return
    if backend is None:
        logger.warning(
            "command.unknown_command",
            command=command_id,
            chat_id=chat_id,
        )
        return
    try:
        plugin_config = cfg.runtime.plugin_config(command_id)
    except ConfigError as exc:
        await executor.send(
            f"error: {user_safe_error(exc, fallback='plugin config error')}",
            reply_to=message_ref,
            notify=True,
        )
        return
    ctx = CommandContext(
        command=command_id,
        text=text,
        args_text=args_text,
        args=split_command_args(args_text),
        message=message_ref,
        reply_to=reply_ref,
        reply_text=msg.reply_to_text,
        config_path=cfg.runtime.config_path,
        plugin_config=plugin_config,
        runtime=cfg.runtime,
        executor=executor,
        trigger_manager=cfg.trigger_manager,
        default_chat_id=cfg.chat_id,
        file_deny_globs=tuple(cfg.files.deny_globs),
    )
    try:
        result = await backend.handle(ctx)
    except Exception as exc:
        logger.exception(
            "command.failed",
            command=command_id,
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        # #201: user sees a sanitised summary; full exception is in the
        # structlog record above (including error_type).
        await executor.send(
            f"error: {user_safe_error(exc, fallback='command failed')}",
            reply_to=message_ref,
            notify=True,
        )
        return
    logger.debug("command.executed", command=command_id, chat_id=chat_id)
    if result is not None:
        if result.skip_reply:
            reply_to = None
        elif result.reply_to is not None:
            reply_to = result.reply_to
        else:
            reply_to = message_ref
        msg: RenderedMessage | str = result.text
        if result.parse_mode is not None:
            msg = RenderedMessage(
                text=result.text, extra={"parse_mode": result.parse_mode}
            )
        await executor.send(msg, reply_to=reply_to, notify=result.notify)


async def _dispatch_callback(
    cfg: TelegramBridgeConfig,
    msg: TelegramCallbackQuery,
    command_id: str,
    args_text: str,
    thread_id: int | None,
    running_tasks: RunningTasks,
    scheduler: ThreadScheduler,
    on_thread_known: Callable[[ResumeToken, anyio.Event], Awaitable[None]] | None,
    stateful_mode: bool,
    default_engine_override: EngineId | None,
    callback_query_id: str | None = None,
) -> None:
    """Dispatch a callback query to a command backend."""
    # Validate sender in group chats — prevent unauthorised users pressing
    # another user's approval buttons (#192).
    if (
        cfg.allowed_user_ids
        and msg.sender_id is not None
        and msg.sender_id not in cfg.allowed_user_ids
    ):
        logger.warning(
            "callback.sender_not_allowed",
            chat_id=msg.chat_id,
            sender_id=msg.sender_id,
            command=command_id,
        )
        if callback_query_id is not None:
            await cfg.bot.answer_callback_query(
                callback_query_id, text="Not authorised"
            )
        return

    allowlist = cfg.runtime.allowlist
    chat_id = msg.chat_id
    user_msg_id = msg.message_id
    executor = _TelegramCommandExecutor(
        exec_cfg=cfg.exec_cfg,
        runtime=cfg.runtime,
        running_tasks=running_tasks,
        scheduler=scheduler,
        on_thread_known=on_thread_known,
        engine_overrides_resolver=None,
        chat_id=chat_id,
        user_msg_id=user_msg_id,
        thread_id=thread_id,
        show_resume_line=cfg.show_resume_line,
        stateful_mode=stateful_mode,
        default_engine_override=default_engine_override,
    )
    message_ref = MessageRef(
        channel_id=chat_id,
        message_id=user_msg_id,
        thread_id=thread_id,
        sender_id=msg.sender_id,
        raw=msg.raw,
    )
    dispatch_start = time.monotonic()
    logger.info("callback.dispatch", command=command_id, chat_id=chat_id)
    _answered = False
    backend: object | None = None

    # #247: instrument the early-answer path so we can observe actual latency
    # to Telegram's answerCallbackQuery in the field. `BotResponseTimeoutError`
    # on the caller's client happens when we don't answer inside Telegram's
    # 30-second window; the early-answer branch is intended to cover this by
    # firing before backend.handle() runs its slow work (e.g. claude_control
    # writes to the Claude PTY stdin). Log the measured HTTP round-trip as
    # INFO so staging grep can distinguish "we were fast, Telegram was slow"
    # from "we were slow."
    async def _answer_callback(text: str | None = None, *, early: bool = False) -> None:
        nonlocal _answered
        if callback_query_id is not None and not _answered:
            # #546: latency_ms now isolates the HTTP round-trip alone — as of
            # rc19, ``answer_callback_query`` no longer queues behind the
            # per-chat send outbox. The ``queue_wait_ms=0`` field is kept
            # explicit so monitoring dashboards can confirm the queue path
            # was indeed bypassed; if a regression accidentally routes
            # callback answers through the outbox again, this stays at 0
            # and a separate ``telegram.outbox.op.completed`` debug line
            # would appear instead.
            start = time.monotonic()
            await cfg.bot.answer_callback_query(callback_query_id, text=text)
            _answered = True
            now = time.monotonic()
            logger.info(
                "callback.answered",
                command=command_id,
                chat_id=chat_id,
                latency_ms=round((now - start) * 1000, 1),
                queue_wait_ms=0.0,
                total_ms=round((now - dispatch_start) * 1000, 1),
                early=early,
                has_toast=text is not None,
            )

    try:
        try:
            backend = get_command(command_id, allowlist=allowlist, required=False)
        except ConfigError as exc:
            await _answer_callback(
                user_safe_error(exc, fallback="callback lookup failed")
            )
            return
        if backend is None:
            logger.warning(
                "callback.unknown_command",
                command=command_id,
                chat_id=chat_id,
            )
            return
        try:
            plugin_config = cfg.runtime.plugin_config(command_id)
        except ConfigError as exc:
            await _answer_callback(user_safe_error(exc, fallback="plugin config error"))
            return
        # Early callback answering: clear the Telegram spinner immediately.
        # #247: the early-answer branch MUST run before backend.handle() so
        # answerCallbackQuery reaches Telegram before any blocking control-
        # response work. The instrumentation inside _answer_callback records
        # latency_ms (HTTP round-trip) and total_ms (time since dispatch
        # entry); the `early=True` flag lets us split the metric by branch
        # when grepping.
        if getattr(backend, "answer_early", False) and callback_query_id is not None:
            toast = _early_answer_toast(
                backend, args_text, chat_id, claim_owner=callback_query_id
            )
            # Always answer early when the backend opts in, even if the toast
            # is None — clearing the spinner before backend.handle() is the
            # whole point. A None toast just means no toast text will appear.
            await _answer_callback(toast, early=True)

        # For callbacks, text is the full callback data and args come from parsing
        text = msg.data or ""
        ctx = CommandContext(
            command=command_id,
            text=text,
            args_text=args_text,
            args=split_command_args(args_text),
            message=message_ref,
            reply_to=None,  # Callback queries don't have reply context
            reply_text=None,
            config_path=cfg.runtime.config_path,
            plugin_config=plugin_config,
            runtime=cfg.runtime,
            executor=executor,
            trigger_manager=cfg.trigger_manager,
            default_chat_id=cfg.chat_id,
            file_deny_globs=tuple(cfg.files.deny_globs),
            callback_query_id=callback_query_id,
        )
        try:
            result = await backend.handle(ctx)
        except Exception as exc:
            logger.exception(
                "callback.failed",
                command=command_id,
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
            await _answer_callback(user_safe_error(exc, fallback="callback failed"))
            return
        logger.debug("callback.executed", command=command_id, chat_id=chat_id)
        if result is not None:
            cb_msg: RenderedMessage | str = result.text
            if result.parse_mode is not None:
                cb_msg = RenderedMessage(
                    text=result.text, extra={"parse_mode": result.parse_mode}
                )
            rendered = (
                cb_msg
                if isinstance(cb_msg, RenderedMessage)
                else RenderedMessage(text=cb_msg)
            )
            if result.skip_reply:
                # Send without reply_to — bypass executor default which
                # would reply to the callback's message (possibly deleted).
                sent_ref = await cfg.exec_cfg.transport.send(
                    channel_id=chat_id,
                    message=rendered,
                    options=SendOptions(notify=result.notify, thread_id=thread_id),
                )
            else:
                if result.reply_to is not None:
                    reply_to = result.reply_to
                else:
                    reply_to = message_ref
                sent_ref = await executor.send(
                    cb_msg, reply_to=reply_to, notify=result.notify
                )
            # Register feedback message for cleanup when the run finishes.
            if sent_ref is not None and callback_query_id is not None:
                register_ephemeral_message(chat_id, user_msg_id, sent_ref)
    finally:
        # #685: a claim the early toast reserved must never outlive this
        # callback — even when handle raised or was cancelled.
        _release_early_claim(backend, callback_query_id)
        await _answer_callback()
