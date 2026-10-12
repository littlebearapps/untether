"""Follow-up injection into live engine sessions (#776 phase 06).

``ThreadScheduler`` calls :func:`inject_live_followup` for every queued
resume job before waiting on the owning process to exit. When the target
session is still live and accepting input, the message is written into that
process — queued until its current turn ends — instead of ``--resume``-ing a
new process after the old one exits. That removes the fresh-session diverts
and double executions of #647/#776 for the common case; anything else (no
live process, closing, an engine with no live-session backend) falls back to
the unchanged resume path.

The engine's registry is reached through :mod:`untether.live_sessions`
(#977); Claude is the only backend today.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .live_sessions import live_backend
from .logging import get_logger
from .scheduler import ThreadJob
from .transport import MessageRef

logger = get_logger(__name__)


OptionsFor = Callable[[ThreadJob], Awaitable[Any]]
# #996: the working directory ``job`` would run in (its context's resolved
# project/branch cwd; None = Untether's own cwd). Raises when it can't be
# resolved (unknown project, worktree error).
CwdFor = Callable[[ThreadJob], Awaitable[Path | None]]


async def inject_live_followup(
    job: ThreadJob,
    *,
    options_for: OptionsFor | None = None,
    cwd_for: CwdFor | None = None,
) -> bool:
    token = job.resume_token
    backend = live_backend(token.engine)
    if backend is None or not backend.enabled():
        return False
    from .runner_bridge import register_followup_anchor, settle_followup_anchor

    # Log events keep the engine's own namespace (``claude.live_session.*``).
    ns = f"{backend.log_ns}.live_session"
    session_id = token.value
    if not backend.is_accepting(session_id):
        live = backend.get(session_id)
        logger.debug(
            f"{ns}.inject_skipped",
            session_id=session_id,
            live=live is not None,
            closing=bool(live and live.closing),
        )
        return False
    close_reason = backend.pre_inject_close_reason(session_id)
    if close_reason is not None:
        # #383 (Claude, ``plan_rearm_failed``): the CLI refused to go back
        # into plan mode — close the live process once idle so this message
        # resumes a fresh one spawned with --permission-mode plan (same
        # fallback as options_changed).
        closed = await backend.close(
            session_id, close_reason, notice=False, only_if_idle=True
        )
        logger.info(
            f"{ns}.{close_reason}_closed",
            session_id=session_id,
            closed=closed,
        )
        return False
    wanted: Any = None
    if options_for is not None:
        # Both read before the await, off the same process: its spawn
        # options never change, and a process that replaces it meanwhile
        # must not be compared against.
        live = backend.get(session_id)
        spawn_options = backend.spawn_options(session_id) if live is not None else None
        try:
            wanted = await options_for(job)
        except Exception:  # noqa: BLE001 — unknown options: don't inject
            logger.warning(f"{ns}.options_resolve_failed", exc_info=True)
            return False
        if live is not None and wanted != spawn_options:
            # The chat's settings changed since this process started
            # (/planmode, /model, reasoning, /config toggles): a follow-up
            # written into it would run with the old ones. Close it (once
            # idle) so the message resumes a fresh process instead.
            closed = await backend.close(
                session_id, "options_changed", notice=True, only_if_idle=True
            )
            logger.info(
                f"{ns}.options_changed",
                session_id=session_id,
                closed=closed,
            )
            return False
    if cwd_for is not None:
        live = backend.get(session_id)
        spawn_cwd = backend.spawn_cwd(session_id) if live is not None else None
        resolve_error: str | None = None
        try:
            wanted_cwd: Path | None = await cwd_for(job)
        except Exception as exc:  # noqa: BLE001 — unresolvable: don't inject
            wanted_cwd = None
            resolve_error = exc.__class__.__name__
        if live is not None and (resolve_error is not None or wanted_cwd != spawn_cwd):
            # #996: the chat's project/branch changed since this process
            # started (/ctx set, /ctx clear, a topic rebind): a follow-up
            # written into it would run in the old directory — possibly the
            # wrong repo. Close it (once idle, like options_changed) so the
            # message resumes the session in the new directory. A context
            # that no longer resolves takes the same path, so the resume
            # reports the error instead of writing into the old cwd.
            closed = await backend.close(
                session_id, "options_changed", notice=True, only_if_idle=True
            )
            # Mid-turn the close is refused and the pump retries every tick:
            # only the close itself is worth an INFO line.
            log = logger.info if closed else logger.debug
            log(
                f"{ns}.context_changed",
                session_id=session_id,
                chat_id=job.chat_id,
                source="scheduler",
                spawn_cwd=None if spawn_cwd is None else str(spawn_cwd),
                cwd=None if wanted_cwd is None else str(wanted_cwd),
                resolve_error=resolve_error,
                closed=closed,
            )
            return False
    from .budget_gate import BUDGET_STOP_REASON, daily_gate

    if daily_gate(wanted) is not None:
        # #896: a follow-up written here would start a turn past the daily
        # budget. End the session once idle; the message then takes the
        # resume path, where the daily gate refuses it (with Run anyway).
        closed = await backend.close(
            session_id, BUDGET_STOP_REASON, notice=True, only_if_idle=True
        )
        logger.info(
            f"{ns}.budget_refused",
            session_id=session_id,
            chat_id=job.chat_id,
            closed=closed,
        )
        return False
    command_uuid = str(uuid.uuid4())
    # #921: in flight until settled — the run-end sweep leaves it to us, so a
    # cancel racing this wait can't say "send it again" for a message that is
    # about to be re-dispatched below.
    register_followup_anchor(
        command_uuid,
        session_id=session_id,
        reply_to=MessageRef(
            channel_id=job.chat_id,
            message_id=job.user_msg_id,
            thread_id=job.thread_id,
        ),
        placeholder=job.progress_ref,
        in_flight=True,
        engine=backend.engine,
    )
    ok = False
    try:
        ok = await backend.inject_when_idle(
            session_id, job.text, command_uuid=command_uuid
        )
    finally:
        settle_followup_anchor(command_uuid, written=ok)
    if not ok:
        logger.info(
            f"{ns}.inject_unavailable",
            session_id=session_id,
            chat_id=job.chat_id,
            user_msg_id=job.user_msg_id,
        )
        return False
    logger.info(
        f"{ns}.injected",
        session_id=session_id,
        command_uuid=command_uuid,
        chat_id=job.chat_id,
        user_msg_id=job.user_msg_id,
        source="scheduler",
    )
    return True
