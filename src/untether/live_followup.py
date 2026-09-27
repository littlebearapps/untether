"""Follow-up injection into live engine sessions (#776 phase 06).

``ThreadScheduler`` calls :func:`inject_live_followup` for every queued
resume job before waiting on the owning process to exit. When the target
Claude session is still live and accepting input, the message is written
into that process — queued until its current turn ends — instead of
``--resume``-ing a new process after the old one exits. That removes the
fresh-session diverts and double executions of #647/#776 for the common
case; anything else (no live process, closing, other engines) falls back to
the unchanged resume path.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from .logging import get_logger
from .scheduler import ThreadJob
from .transport import MessageRef

logger = get_logger(__name__)


OptionsFor = Callable[[ThreadJob], Awaitable[Any]]


async def inject_live_followup(
    job: ThreadJob, *, options_for: OptionsFor | None = None
) -> bool:
    token = job.resume_token
    if token.engine != "claude":
        return False
    from .runner_bridge import pop_followup_anchor, register_followup_anchor
    from .runners.claude import (
        get_live_session,
        inject_when_idle,
        is_session_accepting,
    )

    session_id = token.value
    if not is_session_accepting(session_id):
        live = get_live_session(session_id)
        logger.debug(
            "claude.live_session.inject_skipped",
            session_id=session_id,
            live=live is not None,
            closing=bool(live and live.closing),
        )
        return False
    if options_for is not None:
        live = get_live_session(session_id)
        try:
            wanted = await options_for(job)
        except Exception:  # noqa: BLE001 — unknown options: don't inject
            logger.warning("claude.live_session.options_resolve_failed", exc_info=True)
            return False
        if live is not None and wanted != live.state.spawn_run_options:
            # The chat's settings changed since this process started
            # (/planmode, /model, reasoning, /config toggles): a follow-up
            # written into it would run with the old ones. Close it (once
            # idle) so the message resumes a fresh process instead.
            from .runners.claude import close_live_session

            closed = await close_live_session(
                session_id, "options_changed", notice=True, only_if_idle=True
            )
            logger.info(
                "claude.live_session.options_changed",
                session_id=session_id,
                closed=closed,
            )
            return False
    command_uuid = str(uuid.uuid4())
    register_followup_anchor(
        command_uuid,
        session_id=session_id,
        reply_to=MessageRef(
            channel_id=job.chat_id,
            message_id=job.user_msg_id,
            thread_id=job.thread_id,
        ),
        placeholder=job.progress_ref,
    )
    ok = await inject_when_idle(session_id, job.text, command_uuid=command_uuid)
    if not ok:
        pop_followup_anchor(command_uuid)
        logger.info(
            "claude.live_session.inject_unavailable",
            session_id=session_id,
            chat_id=job.chat_id,
            user_msg_id=job.user_msg_id,
        )
        return False
    logger.info(
        "claude.live_session.injected",
        session_id=session_id,
        command_uuid=command_uuid,
        chat_id=job.chat_id,
        user_msg_id=job.user_msg_id,
        source="scheduler",
    )
    return True
