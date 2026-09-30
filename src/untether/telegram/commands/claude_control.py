"""Command backend for handling Claude Code control requests (approve/deny buttons)."""

from __future__ import annotations

from ...commands import CommandBackend, CommandContext, CommandResult
from ...logging import get_logger
from ...runner_bridge import delete_outline_messages, register_ephemeral_message
from ...runners.claude import (
    _ACTIVE_RUNNERS,
    _DISCUSS_APPROVED,
    _OUTLINE_PENDING,
    _REQUEST_TO_SESSION,
    _REQUEST_TO_TOOL_NAME,
    ControlLookup,
    ControlRequestStatus,
    ControlSendResult,
    HandledControl,
    claim_control_request,
    classify_control_request,
    mark_outline_pending,
    mark_request_handled,
    new_control_claim_owner,
    plan_approved_feedback,
    release_control_claims,
    respond_to_control_request,
)
from ...transport import MessageRef
from .ask_question import _ALREADY_ANSWERED_TOAST

logger = get_logger(__name__)

# Tracks the "📋 Asked Claude Code to outline the plan" message ref per session,
# so the post-outline approve/deny can edit it instead of sending a 2nd message.
_DISCUSS_FEEDBACK_REFS: dict[str, MessageRef] = {}


_DISCUSS_DENY_MESSAGE = (
    "STOP. Do NOT call ExitPlanMode yet.\n\n"
    "The user clicked 'Pause & Outline Plan' in Telegram. This is a direct user instruction.\n\n"
    "The user is on a mobile device (Telegram bridge). They can ONLY see your assistant text "
    "messages — tool calls, thinking blocks, file contents, and terminal UI are invisible. "
    "It does not matter what you already know, have planned, or previously wrote in thinking. "
    "The user did NOT see it. You must write the plan as visible text so they can read it "
    "on their phone.\n\n"
    "YOUR IMMEDIATE NEXT ACTION — write a plan outline as a visible assistant message:\n"
    "- Every file you will create or modify (full paths)\n"
    "- What specific changes in each file\n"
    "- The execution order and any key decisions or risks\n"
    "- At least 15 lines of visible text\n\n"
    "ONLY after writing the outline, call ExitPlanMode. The system will show Approve/Deny "
    "buttons to the user. Wait for them to respond.\n\n"
    "WARNING: If you call ExitPlanMode without writing the outline first, it WILL be "
    "automatically rejected. Write the outline, then call ExitPlanMode."
)

_DENY_MESSAGE = (
    "User denied via Telegram (Untether bridge). They cannot see your tool calls "
    "or terminal UI — only your assistant text messages are visible to them. "
    "Explain what you were about to do and ask how they'd like to proceed, "
    "as a visible message in the chat."
)

_EXIT_PLAN_DENY_MESSAGE = (
    "User DENIED your plan via Telegram (Untether bridge). "
    "They do NOT want you to proceed with this plan. "
    "Do NOT call ExitPlanMode again. Instead, ask the user "
    "what they'd like changed, as a visible message in the chat."
)

_CHAT_DENY_MESSAGE = (
    "The user clicked 'Let's discuss' on your plan outline in Telegram. "
    "They want to talk about the plan before deciding.\n\n"
    "Ask the user what they'd like to discuss or change about the plan, "
    "as a visible message in the chat. Do NOT call ExitPlanMode — "
    "wait for the user to respond first."
)

_EARLY_TOASTS: dict[str, str] = {
    "approve": "Approved",
    "deny": "Denied",
    "discuss": "Outlining plan...",
    "chat": "Let's discuss...",
}

# #685: a tap on a request that is no longer pending says so, instead of
# toasting success for a tap that writes nothing.
_NO_LONGER_NEEDED_TOAST = "No longer needed"
_EXPIRED_TOAST = "This request has expired"
_NOT_FOUND_TEXT = "⚠️ Control request not found or session ended"
_CANCELLED_TEXT = "⏹️ Claude Code withdrew this request — nothing to answer."

# #685: what the first resolution did, for the silent "already answered" line.
# Shared with #684 (cancelled / superseded).
_PRIOR_LABELS: dict[str, str] = {
    "approve": "approved",
    "deny": "denied",
    "discuss": "outline requested",
    "chat": "discussion requested",
    "auto": "answered automatically",
    "answer": "answered",
    "timeout": "timed out after 5 min (auto-denied)",
    "superseded": "replaced by the outlined plan",
}


def _prior_label(prior: HandledControl | None) -> str:
    if prior is None or prior.action is None:
        return "answered"
    return _PRIOR_LABELS.get(prior.action, "answered")


def _is_expired(prior: HandledControl | None) -> bool:
    return prior is not None and prior.outcome == "expired"


def _toast_for(lookup: ControlLookup, action: str) -> str | None:
    """Early toast for a tap on a request in state ``lookup`` (#685)."""
    match lookup.status:
        case ControlRequestStatus.PENDING:
            return _EARLY_TOASTS.get(action)
        case ControlRequestStatus.CANCELLED:
            return _NO_LONGER_NEEDED_TOAST
        case ControlRequestStatus.NOT_FOUND:
            return _EXPIRED_TOAST
        case _:  # IN_FLIGHT / ALREADY_HANDLED
            if _is_expired(lookup.prior):
                return _EXPIRED_TOAST
            return _ALREADY_ANSWERED_TOAST


def _already_handled_result(
    status: ControlRequestStatus,
    prior: HandledControl | None,
    request_id: str,
    action: str,
) -> CommandResult:
    """The silent line for a tap on a request that was already resolved (#685).

    An expected user race (double tap, a tap that lost to the keyboard strip),
    so INFO and ``notify=False`` — mirrors #698's ask-flow late tap. It never
    logs ``claude_control.sent``: that line means a response was written.
    """
    logger.info(
        "claude_control.already_handled",
        request_id=request_id,
        action=action,
        status=str(status),
        first_action=prior.action if prior is not None else None,
        outcome=prior.outcome if prior is not None else None,
    )
    if status is ControlRequestStatus.CANCELLED:
        text = _CANCELLED_TEXT
    elif _is_expired(prior):
        text = f"ℹ️ {_EXPIRED_TOAST} — {_prior_label(prior)}"
    else:
        text = f"ℹ️ {_ALREADY_ANSWERED_TOAST} — {_prior_label(prior)}"
    return CommandResult(text=text, notify=False)


def _not_found_result(
    request_id: str, action: str, reason: str | None
) -> CommandResult:
    logger.warning(
        "claude_control.not_found",
        request_id=request_id,
        action=action,
        reason=reason,
    )
    return CommandResult(text=_NOT_FOUND_TEXT, notify=True)


def _unsent_result(
    result: ControlSendResult, request_id: str, action: str
) -> CommandResult:
    """Result for a tap whose response was not written (#685) — or was
    written after the CLI withdrew the request, so it was ignored (#684)."""
    if result.status is ControlRequestStatus.NOT_FOUND:
        return _not_found_result(request_id, action, result.reason)
    if result.status is not ControlRequestStatus.PENDING:
        return _already_handled_result(result.status, result.prior, request_id, action)
    # Ours to answer, but the session was gone or the write failed.
    logger.warning(
        "claude_control.failed",
        request_id=request_id,
        action=action,
        reason=result.reason,
    )
    return CommandResult(text=_NOT_FOUND_TEXT, notify=True)


def _claim_synthetic(
    ctx: CommandContext, request_id: str, action: str
) -> CommandResult | None:
    """Claim and resolve a synthetic ``da:<session>`` button (#685).

    These buttons answer nothing on the wire (the underlying ExitPlanMode was
    already auto-denied); the verdict is applied in Untether. So "was it still
    pending?" must be checked here, before acting — otherwise a Deny → Approve
    double tap re-applied the verdict and turned the deny into an approval.
    Everything between the check and the resolution is synchronous, so a
    concurrent tap can't slip in. Returns the result to send when the button
    was already resolved, or ``None`` when this tap now owns it.
    """
    channel_id = ctx.message.channel_id
    owner = ctx.callback_query_id or new_control_claim_owner()
    lookup = claim_control_request(
        request_id, action=action, owner=owner, channel_id=channel_id
    )
    if lookup.status is ControlRequestStatus.NOT_FOUND:
        return _not_found_result(request_id, action, lookup.reason)
    if lookup.status is not ControlRequestStatus.PENDING:
        return _already_handled_result(lookup.status, lookup.prior, request_id, action)
    # Resolve before the first await: pop the registration and record the
    # verdict. #683: marking it handled also lets the reconcile loop complete
    # the synthetic claude.discuss_approve.N action it belongs to.
    _REQUEST_TO_SESSION.pop(request_id, None)
    mark_request_handled(request_id, action=action, channel_id=channel_id)
    release_control_claims(owner)
    return None


class ClaudeControlCommand:
    """Command backend for Claude Code permission approval/denial."""

    id = "claude_control"
    description = "Handle Claude Code permission requests"
    answer_early = True

    @staticmethod
    def early_answer_toast(
        args_text: str,
        *,
        channel_id: int | None = None,
        claim_owner: str | None = None,
    ) -> str | None:
        """Return a toast string for immediate callback answering, or None.

        #685: the toast reflects the request's real state, not just the
        button's label — ``Already answered`` / ``No longer needed`` /
        ``This request has expired`` for a tap that will write nothing. With
        ``claim_owner`` (the callback query id) a pending request is reserved
        for this tap, synchronously, before the dispatcher's first ``await``;
        a concurrent second tap then sees it in flight. ``channel_id`` scopes
        the resolved-request lookup (#715). Never raises.
        """
        action, _, request_id = (args_text or "").partition(":")
        action = action.lower()
        if action not in _EARLY_TOASTS or not request_id:
            return None
        try:
            if claim_owner is not None:
                lookup = claim_control_request(
                    request_id,
                    action=action,
                    owner=claim_owner,
                    channel_id=channel_id,
                )
            else:
                lookup = classify_control_request(request_id, channel_id=channel_id)
            return _toast_for(lookup, action)
        except Exception:  # noqa: BLE001 — a toast must never take out the tap
            logger.debug("claude_control.early_toast_failed", exc_info=True)
            return _EARLY_TOASTS.get(action)

    @staticmethod
    def release_early_claim(owner: str) -> None:
        """Drop any claim ``owner``'s early toast still holds (#685)."""
        release_control_claims(owner)

    async def handle(self, ctx: CommandContext) -> CommandResult | None:
        """Handle callback from approve/deny/discuss/chat buttons.

        Args:
            ctx: Command context with args_text="approve:request_id",
                 "deny:request_id", "discuss:request_id",
                 or "chat:request_id"

        Returns:
            CommandResult with feedback message, or None
        """
        # Parse args: "action:request_id"
        parts = ctx.args_text.split(":", 1)
        if len(parts) != 2:
            logger.warning(
                "claude_control.invalid_callback",
                args_text=ctx.args_text,
            )
            return CommandResult(
                text="Invalid control callback format",
                notify=False,
            )

        action, request_id = parts
        action = action.lower()

        if action not in ("approve", "deny", "discuss", "chat"):
            logger.warning(
                "claude_control.unknown_action",
                action=action,
                request_id=request_id,
            )
            return CommandResult(
                text=f"Unknown action: {action}",
                notify=False,
            )

        channel_id = ctx.message.channel_id

        if action == "discuss":
            # Deny with a message asking Claude Code to outline the plan.
            # Procedural, not a verdict on the plan (#793): approving the same
            # plan after the outline is a real approval.
            sent = await respond_to_control_request(
                request_id,
                False,
                action=action,
                channel_id=channel_id,
                deny_message=_DISCUSS_DENY_MESSAGE,
                rejects_plan=False,
                claim_owner=ctx.callback_query_id,
            )
            if not sent.sent or sent.status is ControlRequestStatus.CANCELLED:
                return _unsent_result(sent, request_id, action)
            session_id = sent.session_id

            # Arm the outline gate: ExitPlanMode is auto-denied until Claude
            # writes enough visible outline text (#570 retired the extra
            # time-based cooldown this used to arm).
            if session_id:
                mark_outline_pending(session_id)

            logger.info(
                "claude_control.sent",
                request_id=request_id,
                action=action,
            )

            # Send feedback directly and store ref so post-outline approve/deny
            # can edit this message instead of creating a second one.
            ref = await ctx.executor.send(
                "📋 Asked Claude Code to outline the plan",
                notify=True,
            )
            if ref and session_id:
                _DISCUSS_FEEDBACK_REFS[session_id] = ref
                register_ephemeral_message(
                    ctx.message.channel_id, ctx.message.message_id, ref
                )
            return None

        if action == "chat":
            return await self._handle_chat(ctx, request_id)

        approved = action == "approve"

        # Handle synthetic discuss-approval buttons (post-outline Approve/Deny)
        if request_id.startswith("da:"):
            session_id = request_id.removeprefix("da:")
            # #685: classify + claim + resolve before acting, so a second tap
            # (e.g. Deny then Approve) can't re-apply — or flip — the verdict.
            if (done := _claim_synthetic(ctx, request_id, action)) is not None:
                return done

            # Check if session is still alive — it may have ended
            # (context exhaustion) before the user clicked the button
            if session_id not in _ACTIVE_RUNNERS:
                logger.warning(
                    "claude_control.discuss_plan_session_ended",
                    session_id=session_id,
                )
                _DISCUSS_FEEDBACK_REFS.pop(session_id, None)
                return CommandResult(
                    text=(
                        "⚠️ Session has ended — start a new run"
                        " or resume with /claude continue"
                    ),
                    notify=True,
                )

            # Delete outline messages immediately on approve or deny
            await delete_outline_messages(session_id)

            if approved:
                _DISCUSS_APPROVED.add(session_id)
                _OUTLINE_PENDING.discard(session_id)
                logger.info(
                    "claude_control.discuss_plan_approved",
                    session_id=session_id,
                )
                action_text = plan_approved_feedback(session_id)
            else:
                _OUTLINE_PENDING.discard(session_id)
                logger.info(
                    "claude_control.discuss_plan_denied",
                    session_id=session_id,
                )
                action_text = "❌ Plan denied — send a follow-up message with feedback"

            # Edit the discuss feedback message instead of sending a new one
            existing_ref = _DISCUSS_FEEDBACK_REFS.pop(session_id, None)
            if existing_ref:
                try:
                    await ctx.executor.edit(existing_ref, action_text)
                    return None
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "claude_control.discuss_feedback_edit_failed",
                        session_id=session_id,
                        exc_info=True,
                    )
            # Fallback: send as new message if edit failed or no ref stored
            return CommandResult(
                text=action_text,
                notify=True,
                skip_reply=True,
            )

        # Grab the tool name before the write pops it
        tool_name = _REQUEST_TO_TOOL_NAME.get(request_id, "")

        # Send control response via the public API
        if not approved:
            deny_message = (
                _EXIT_PLAN_DENY_MESSAGE
                if tool_name == "ExitPlanMode"
                else _DENY_MESSAGE
            )
        else:
            deny_message = None
        sent = await respond_to_control_request(
            request_id,
            approved,
            action=action,
            channel_id=channel_id,
            deny_message=deny_message,
            claim_owner=ctx.callback_query_id,
        )
        if not sent.sent or sent.status is ControlRequestStatus.CANCELLED:
            # #685: never log claude_control.sent (or say "Approved") for a
            # tap that wrote nothing.
            return _unsent_result(sent, request_id, action)
        session_id = sent.session_id

        # Clear outline-pending state on explicit approve/deny
        had_outline = False
        if session_id:
            _OUTLINE_PENDING.discard(session_id)
            # Delete outline messages when ExitPlanMode is approved/denied.
            # Track whether outlines existed — if the callback originated from
            # an outline message (now deleted), we must skip replying to it.
            from ...runner_bridge import _OUTLINE_REGISTRY

            had_outline = session_id in _OUTLINE_REGISTRY
            await delete_outline_messages(session_id)
            # Try to edit the discuss feedback message for outline-flow
            # approve/deny (when outline was long enough to use real request_id
            # instead of da: prefix).
            existing_ref = _DISCUSS_FEEDBACK_REFS.pop(session_id, None)
            if existing_ref:
                action_text = (
                    plan_approved_feedback(session_id)
                    if approved
                    else "❌ Plan denied — send a follow-up message with feedback"
                )
                try:
                    await ctx.executor.edit(existing_ref, action_text)
                    logger.info(
                        "claude_control.sent",
                        request_id=request_id,
                        approved=approved,
                    )
                    return None
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "claude_control.discuss_feedback_edit_failed",
                        session_id=session_id,
                        exc_info=True,
                    )

        logger.info(
            "claude_control.sent",
            request_id=request_id,
            approved=approved,
        )
        if approved and tool_name == "ExitPlanMode":
            # #383: a plan approval says so, not "permission request".
            feedback = "✅ Plan approved"
        else:
            feedback = (
                f"{'✅ Approved' if approved else '❌ Denied'} permission request"
            )

        return CommandResult(
            text=feedback,
            notify=True,
            skip_reply=had_outline,
        )

    async def _handle_chat(
        self, ctx: CommandContext, request_id: str
    ) -> CommandResult | None:
        """Handle 'Let's discuss' button on post-outline approval."""
        action_text = "💬 Let's discuss — type your feedback"

        # Synthetic da: prefix path (request already auto-denied)
        if request_id.startswith("da:"):
            session_id = request_id.removeprefix("da:")
            # #685: same claim-before-acting guard as the da: approve/deny.
            if (done := _claim_synthetic(ctx, request_id, "chat")) is not None:
                return done

            if session_id not in _ACTIVE_RUNNERS:
                logger.warning(
                    "claude_control.discuss_plan_session_ended",
                    session_id=session_id,
                )
                _DISCUSS_FEEDBACK_REFS.pop(session_id, None)
                return CommandResult(
                    text=(
                        "⚠️ Session has ended — start a new run"
                        " or resume with /claude continue"
                    ),
                    notify=True,
                )

            await delete_outline_messages(session_id)
            _OUTLINE_PENDING.discard(session_id)
            logger.info(
                "claude_control.discuss_plan_chat",
                session_id=session_id,
            )

            existing_ref = _DISCUSS_FEEDBACK_REFS.pop(session_id, None)
            if existing_ref:
                try:
                    await ctx.executor.edit(existing_ref, action_text)
                    return None
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "claude_control.discuss_feedback_edit_failed",
                        session_id=session_id,
                        exc_info=True,
                    )
            return CommandResult(
                text=action_text,
                notify=True,
                skip_reply=True,
            )

        # Hold-open path (real request_id, control request still pending)
        sent = await respond_to_control_request(
            request_id,
            False,
            action="chat",
            channel_id=ctx.message.channel_id,
            deny_message=_CHAT_DENY_MESSAGE,
            rejects_plan=False,  # #793: wants to talk, not a rejection
            claim_owner=ctx.callback_query_id,
        )
        if not sent.sent or sent.status is ControlRequestStatus.CANCELLED:
            return _unsent_result(sent, request_id, "chat")
        session_id = sent.session_id

        if session_id:
            _OUTLINE_PENDING.discard(session_id)
            await delete_outline_messages(session_id)

        logger.info(
            "claude_control.sent",
            request_id=request_id,
            action="chat",
        )

        existing_ref = (
            _DISCUSS_FEEDBACK_REFS.pop(session_id, None) if session_id else None
        )
        if existing_ref:
            try:
                await ctx.executor.edit(existing_ref, action_text)
                return None
            except Exception:  # noqa: BLE001
                logger.debug(
                    "claude_control.discuss_feedback_edit_failed",
                    session_id=session_id,
                    exc_info=True,
                )
        return CommandResult(
            text=action_text,
            notify=True,
            skip_reply=True,
        )


BACKEND: CommandBackend = ClaudeControlCommand()
