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
    clear_discuss_cooldown,
    send_claude_control_response,
    set_discuss_cooldown,
)
from ...transport import MessageRef

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


class ClaudeControlCommand:
    """Command backend for Claude Code permission approval/denial."""

    id = "claude_control"
    description = "Handle Claude Code permission requests"
    answer_early = True

    @staticmethod
    def early_answer_toast(args_text: str) -> str | None:
        """Return a toast string for immediate callback answering, or None."""
        action = args_text.split(":", 1)[0].lower() if args_text else ""
        return _EARLY_TOASTS.get(action)

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

        if action == "discuss":
            # Grab session_id before send_claude_control_response deletes it
            session_id = _REQUEST_TO_SESSION.get(request_id)

            # Deny with a message asking Claude Code to outline the plan
            success = await send_claude_control_response(
                request_id, approved=False, deny_message=_DISCUSS_DENY_MESSAGE
            )
            if not success:
                logger.warning(
                    "claude_control.failed",
                    request_id=request_id,
                    action=action,
                )
                return CommandResult(
                    text="⚠️ Control request not found or session ended",
                    notify=True,
                )

            # Start rate-limiting cooldown so rapid ExitPlanMode retries are auto-denied
            if session_id:
                set_discuss_cooldown(session_id)

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
            # Clean up the synthetic request registration
            _REQUEST_TO_SESSION.pop(request_id, None)

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
                clear_discuss_cooldown(session_id)
                logger.info(
                    "claude_control.discuss_plan_approved",
                    session_id=session_id,
                )
                action_text = "✅ Plan approved — Claude Code will proceed"
            else:
                _OUTLINE_PENDING.discard(session_id)
                clear_discuss_cooldown(session_id)
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

        # Grab session_id before send_claude_control_response deletes it
        session_id = _REQUEST_TO_SESSION.get(request_id)

        # Send control response via the public API
        if not approved:
            tool_name = _REQUEST_TO_TOOL_NAME.get(request_id, "")
            deny_message = (
                _EXIT_PLAN_DENY_MESSAGE
                if tool_name == "ExitPlanMode"
                else _DENY_MESSAGE
            )
        else:
            deny_message = None
        success = await send_claude_control_response(
            request_id, approved, deny_message=deny_message
        )

        if not success:
            logger.warning(
                "claude_control.failed",
                request_id=request_id,
                approved=approved,
            )
            return CommandResult(
                text="⚠️ Control request not found or session ended",
                notify=True,
            )

        # Clear any discuss cooldown on explicit approve/deny
        had_outline = False
        if session_id:
            clear_discuss_cooldown(session_id)
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
                    "✅ Plan approved — Claude Code will proceed"
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

        action_text = "✅ Approved" if approved else "❌ Denied"
        logger.info(
            "claude_control.sent",
            request_id=request_id,
            approved=approved,
        )

        return CommandResult(
            text=f"{action_text} permission request",
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
            _REQUEST_TO_SESSION.pop(request_id, None)

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
            clear_discuss_cooldown(session_id)
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
        session_id = _REQUEST_TO_SESSION.get(request_id)

        success = await send_claude_control_response(
            request_id, approved=False, deny_message=_CHAT_DENY_MESSAGE
        )
        if not success:
            logger.warning(
                "claude_control.failed",
                request_id=request_id,
                action="chat",
            )
            return CommandResult(
                text="⚠️ Control request not found or session ended",
                notify=True,
            )

        if session_id:
            clear_discuss_cooldown(session_id)
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
