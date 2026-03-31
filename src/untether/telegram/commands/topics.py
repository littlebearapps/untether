from __future__ import annotations

from typing import TYPE_CHECKING

from ...context import RunContext
from ...logging import get_logger
from ...markdown import MarkdownParts
from ...runner_bridge import RunningTasks
from ...transport import RenderedMessage, SendOptions
from ...transport_runtime import TransportRuntime
from ..chat_prefs import ChatPrefsStore
from ..chat_sessions import ChatSessionStore
from ..context import (
    _format_context,
    _format_ctx_status,
    _merge_topic_context,
    _parse_project_branch_args,
    _usage_ctx_set,
    _usage_topic,
)
from ..files import split_command_args
from ..render import prepare_telegram
from ..topic_state import TopicStateStore
from ..topics import (
    _maybe_rename_topic,
    _topic_key,
    _topic_title,
    _topics_chat_project,
    _topics_command_error,
)
from ..types import TelegramIncomingMessage
from .reply import make_reply

if TYPE_CHECKING:
    from ..bridge import TelegramBridgeConfig

logger = get_logger(__name__)


def _cancel_chat_tasks(
    chat_id: int,
    running_tasks: RunningTasks | None,
) -> int:
    """Cancel all running tasks for a chat.

    Returns the number of tasks cancelled.
    """
    cancelled = 0
    if running_tasks:
        for ref, task in running_tasks.items():
            if ref.channel_id == chat_id and not task.cancel_requested.is_set():
                task.cancel_requested.set()
                cancelled += 1
    return cancelled


async def _handle_ctx_command(
    cfg: TelegramBridgeConfig,
    msg: TelegramIncomingMessage,
    args_text: str,
    store: TopicStateStore,
    *,
    resolved_scope: str | None = None,
    scope_chat_ids: frozenset[int] | None = None,
) -> None:
    reply = make_reply(cfg, msg)
    error = _topics_command_error(
        cfg,
        msg.chat_id,
        resolved_scope=resolved_scope,
        scope_chat_ids=scope_chat_ids,
    )
    if error is not None:
        await reply(text=error)
        return
    chat_project = _topics_chat_project(cfg, msg.chat_id)
    tkey = _topic_key(msg, cfg, scope_chat_ids=scope_chat_ids)
    if tkey is None:
        await reply(text="this command only works inside a topic.")
        return
    tokens = split_command_args(args_text)
    action = tokens[0].lower() if tokens else "show"
    if action in {"show", ""}:
        snapshot = await store.get_thread(*tkey)
        bound = snapshot.context if snapshot is not None else None
        ambient = _merge_topic_context(chat_project=chat_project, bound=bound)
        resolved = cfg.runtime.resolve_message(
            text="",
            reply_text=msg.reply_to_text,
            chat_id=msg.chat_id,
            ambient_context=ambient,
        )
        text = _format_ctx_status(
            cfg=cfg,
            runtime=cfg.runtime,
            bound=bound,
            resolved=resolved.context,
            context_source=resolved.context_source,
            snapshot=snapshot,
            chat_project=chat_project,
        )
        await reply(text=text)
        return
    if action == "set":
        rest = " ".join(tokens[1:])
        context, error = _parse_project_branch_args(
            rest,
            runtime=cfg.runtime,
            require_branch=False,
            chat_project=chat_project,
        )
        if error is not None:
            await reply(
                text=f"error:\n{error}\n{_usage_ctx_set(chat_project=chat_project)}",
            )
            return
        if context is None:
            await reply(
                text=f"error:\n{_usage_ctx_set(chat_project=chat_project)}",
            )
            return
        await store.set_context(*tkey, context)
        await _maybe_rename_topic(
            cfg,
            store,
            chat_id=tkey[0],
            thread_id=tkey[1],
            context=context,
        )
        await reply(
            text=f"topic bound to `{_format_context(cfg.runtime, context)}`",
        )
        return
    if action == "clear":
        await store.clear_context(*tkey)
        await reply(text="topic binding cleared.")
        return
    await reply(
        text="unknown `/ctx` command. use `/ctx`, `/ctx set`, or `/ctx clear`.",
    )


def _parse_chat_ctx_args(
    args_text: str,
    *,
    runtime: TransportRuntime,
    default_project: str | None,
) -> tuple[RunContext | None, str | None]:
    tokens = split_command_args(args_text)
    if not tokens:
        return None, _usage_ctx_set(chat_project=None)
    if len(tokens) > 2:
        return None, "too many arguments"
    project_token: str | None = None
    branch: str | None = None
    first = tokens[0]
    if first.startswith("@"):
        branch = first[1:] or None
    else:
        project_token = first
        if len(tokens) == 2:
            second = tokens[1]
            if not second.startswith("@"):
                return None, "branch must be prefixed with @"
            branch = second[1:] or None
    project_key: str | None = None
    if project_token is None:
        if default_project is None:
            return None, "project is required"
        project_key = default_project
    else:
        project_key = runtime.normalize_project_key(project_token)
        if project_key is None:
            return None, f"unknown project {project_token!r}"
    return RunContext(project=project_key, branch=branch), None


async def _handle_chat_ctx_command(
    cfg: TelegramBridgeConfig,
    msg: TelegramIncomingMessage,
    args_text: str,
    chat_prefs: ChatPrefsStore | None,
) -> None:
    reply = make_reply(cfg, msg)
    if chat_prefs is None:
        await reply(text="chat context unavailable; config path is not set.")
        return

    tokens = split_command_args(args_text)
    action = tokens[0].lower() if tokens else "show"
    if action in {"show", ""}:
        bound = await chat_prefs.get_context(msg.chat_id)
        resolved = cfg.runtime.resolve_message(
            text="",
            reply_text=msg.reply_to_text,
            chat_id=msg.chat_id,
            ambient_context=bound,
        )
        source = resolved.context_source
        if bound is not None and resolved.context_source == "ambient":
            source = "bound"
        lines = [
            f"bound ctx: {_format_context(cfg.runtime, bound)}",
            f"resolved ctx: {_format_context(cfg.runtime, resolved.context)} (source: {source})",
        ]
        if bound is None:
            ctx_usage = (
                _usage_ctx_set(chat_project=None).removeprefix("usage: ").strip()
            )
            lines.append(f"note: no bound context — bind with {ctx_usage}")
        await reply(text="\n".join(lines))
        return
    if action == "set":
        rest = " ".join(tokens[1:])
        context, error = _parse_chat_ctx_args(
            rest,
            runtime=cfg.runtime,
            default_project=cfg.runtime.default_project,
        )
        if error is not None:
            await reply(
                text=f"error:\n{error}\n{_usage_ctx_set(chat_project=None)}",
            )
            return
        if context is None:
            await reply(text=f"error:\n{_usage_ctx_set(chat_project=None)}")
            return
        await chat_prefs.set_context(msg.chat_id, context)
        await reply(
            text=f"chat bound to `{_format_context(cfg.runtime, context)}`",
        )
        return
    if action == "clear":
        await chat_prefs.clear_context(msg.chat_id)
        await reply(text="chat context cleared.")
        return
    await reply(
        text="unknown `/ctx` command. use `/ctx`, `/ctx set`, or `/ctx clear`.",
    )


async def _handle_new_command(
    cfg: TelegramBridgeConfig,
    msg: TelegramIncomingMessage,
    store: TopicStateStore,
    *,
    resolved_scope: str | None = None,
    scope_chat_ids: frozenset[int] | None = None,
    running_tasks: RunningTasks | None = None,
) -> None:
    reply = make_reply(cfg, msg)
    error = _topics_command_error(
        cfg,
        msg.chat_id,
        resolved_scope=resolved_scope,
        scope_chat_ids=scope_chat_ids,
    )
    if error is not None:
        await reply(text=error)
        return
    tkey = _topic_key(msg, cfg, scope_chat_ids=scope_chat_ids)
    if tkey is None:
        await reply(text="this command only works inside a topic.")
        return
    cancelled = _cancel_chat_tasks(msg.chat_id, running_tasks)
    if cancelled:
        logger.info("new.cancelled_running", chat_id=msg.chat_id, count=cancelled)
    await store.clear_sessions(*tkey)
    label = "cancelled run and cleared" if cancelled else "cleared"
    await reply(text=f"\N{BROOM} {label} stored sessions for this topic.")


async def _handle_chat_new_command(
    cfg: TelegramBridgeConfig,
    msg: TelegramIncomingMessage,
    store: ChatSessionStore,
    session_key: tuple[int, int | None] | None,
    running_tasks: RunningTasks | None = None,
) -> None:
    reply = make_reply(cfg, msg)
    cancelled = _cancel_chat_tasks(msg.chat_id, running_tasks)
    if cancelled:
        logger.info("new.cancelled_running", chat_id=msg.chat_id, count=cancelled)
    if session_key is None and not cancelled:
        await reply(text="no stored sessions to clear for this chat.")
        return
    if session_key is not None:
        await store.clear_sessions(session_key[0], session_key[1])
    label = "cancelled run and cleared" if cancelled else "cleared"
    if msg.chat_type == "private":
        text = f"\N{BROOM} {label} stored sessions for this chat."
    else:
        text = f"\N{BROOM} {label} stored sessions for you in this chat."
    await reply(text=text)


async def _handle_topic_command(
    cfg: TelegramBridgeConfig,
    msg: TelegramIncomingMessage,
    args_text: str,
    store: TopicStateStore,
    *,
    resolved_scope: str | None = None,
    scope_chat_ids: frozenset[int] | None = None,
) -> None:
    reply = make_reply(cfg, msg)
    error = _topics_command_error(
        cfg,
        msg.chat_id,
        resolved_scope=resolved_scope,
        scope_chat_ids=scope_chat_ids,
    )
    if error is not None:
        await reply(text=error)
        return
    chat_project = _topics_chat_project(cfg, msg.chat_id)
    context, error = _parse_project_branch_args(
        args_text,
        runtime=cfg.runtime,
        require_branch=True,
        chat_project=chat_project,
    )
    if error is not None or context is None:
        usage = _usage_topic(chat_project=chat_project)
        text = f"error:\n{error}\n{usage}" if error else usage
        await reply(text=text)
        return
    title = _topic_title(runtime=cfg.runtime, context=context)
    existing = await store.find_thread_for_context(msg.chat_id, context)
    stale_thread_id: int | None = None
    if existing is not None:
        updated = await cfg.bot.edit_forum_topic(
            chat_id=msg.chat_id,
            message_thread_id=existing,
            name=title,
        )
        if updated:
            await reply(
                text=f"topic already exists for {_format_context(cfg.runtime, context)} "
                "in this chat.",
            )
            return
        stale_thread_id = existing
    created = await cfg.bot.create_forum_topic(msg.chat_id, title)
    if created is None:
        await reply(text="failed to create topic.")
        return
    thread_id = created.message_thread_id
    if stale_thread_id is not None:
        await store.delete_thread(msg.chat_id, stale_thread_id)
    await store.set_context(
        msg.chat_id,
        thread_id,
        context,
        topic_title=title,
    )
    await reply(text=f"created topic `{title}`.")
    bound_text = f"topic bound to `{_format_context(cfg.runtime, context)}`"
    rendered_text, entities = prepare_telegram(MarkdownParts(header=bound_text))
    await cfg.exec_cfg.transport.send(
        channel_id=msg.chat_id,
        message=RenderedMessage(text=rendered_text, extra={"entities": entities}),
        options=SendOptions(thread_id=thread_id),
    )
