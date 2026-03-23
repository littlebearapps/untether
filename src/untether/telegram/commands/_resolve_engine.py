"""Shared helper for resolving the effective engine in a chat."""

from __future__ import annotations

from ...commands import CommandContext


async def resolve_effective_engine(ctx: CommandContext) -> str:
    """Resolve the effective engine for the current chat.

    Resolution order: chat override → project default → global default.
    """
    from ..chat_prefs import ChatPrefsStore, resolve_prefs_path

    chat_id = ctx.message.channel_id
    global_default = ctx.runtime.default_engine

    chat_override = None
    if ctx.config_path is not None:
        prefs = ChatPrefsStore(resolve_prefs_path(ctx.config_path))
        chat_override = await prefs.get_default_engine(chat_id)

    if chat_override is not None:
        return chat_override

    project_default = None
    context = ctx.runtime.default_context_for_chat(chat_id)
    if context is not None:
        project_default = ctx.runtime.project_default_engine(context)

    return project_default if project_default is not None else global_default
