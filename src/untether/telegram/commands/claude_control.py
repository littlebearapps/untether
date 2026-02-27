"""Command backend for handling Claude Code control requests (approve/deny buttons)."""

from __future__ import annotations

from ...commands import CommandBackend, CommandContext, CommandResult
from ...logging import get_logger
from ...runners.claude import (
    _DISCUSS_APPROVED,
    _REQUEST_TO_SESSION,
    clear_discuss_cooldown,
    send_claude_control_response,
    set_discuss_cooldown,
)

logger = get_logger(__name__)


_DISCUSS_DENY_MESSAGE = (
    "MANDATORY STOP — the user clicked 'Pause & Outline Plan' in Telegram.\n\n"
    "This is a DIRECT USER INSTRUCTION. You MUST comply — this is NOT optional and NOT a system error.\n\n"
    "CONTEXT: The user is on Untether (Telegram bridge). They can ONLY see your assistant text "
    "messages. Tool calls, thinking blocks, file contents, and terminal UI are ALL invisible to them. "
    "If you already wrote a plan in thinking or as tool input, the user DID NOT SEE IT.\n\n"
    "IMPORTANT: Even if you previously wrote a plan outline earlier in this conversation, the user "
    "may NOT have seen it due to a session interruption (usage limit, error, restart). "
    "You MUST write the full outline again as visible text NOW — do not skip or summarise.\n\n"
    "REQUIRED — write a COMPREHENSIVE plan outline as your IMMEDIATE next assistant message:\n"
    "1. Every file you will create or modify (full paths)\n"
    "2. What specific changes you will make in each file\n"
    "3. The order/phases you will execute them in\n"
    "4. Any key decisions, trade-offs, or risks\n"
    "5. What the end result will look like\n\n"
    "The outline MUST be at least 15 lines of VISIBLE TEXT in the chat.\n\n"
    "After writing the outline, call ExitPlanMode. Approve/Deny buttons will appear "
    "in Telegram for the user to approve your plan. Do NOT wait for a text reply — "
    "just call ExitPlanMode and the buttons will handle it."
)

_DENY_MESSAGE = (
    "User denied via Telegram (Untether bridge). They cannot see your tool calls "
    "or terminal UI — only your assistant text messages are visible to them. "
    "Explain what you were about to do and ask how they'd like to proceed, "
    "as a visible message in the chat."
)

_EARLY_TOASTS: dict[str, str] = {
    "approve": "Approved",
    "deny": "Denied",
    "discuss": "Outlining plan...",
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
        """Handle callback from approve/deny/discuss buttons.

        Args:
            ctx: Command context with args_text="approve:request_id",
                 "deny:request_id", or "discuss:request_id"

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

        if action not in ("approve", "deny", "discuss"):
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

            # Deny with a message asking Claude to outline the plan
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
            return CommandResult(
                text="📋 Asked Claude to outline the plan",
                notify=True,
            )

        approved = action == "approve"

        # Handle synthetic discuss-approval buttons (post-outline Approve/Deny)
        if request_id.startswith("da:"):
            session_id = request_id.removeprefix("da:")
            # Clean up the synthetic request registration
            _REQUEST_TO_SESSION.pop(request_id, None)

            if approved:
                _DISCUSS_APPROVED.add(session_id)
                clear_discuss_cooldown(session_id)
                logger.info(
                    "claude_control.discuss_plan_approved",
                    session_id=session_id,
                )
                return CommandResult(
                    text="✅ Plan approved — Claude will proceed",
                    notify=True,
                )
            else:
                clear_discuss_cooldown(session_id)
                logger.info(
                    "claude_control.discuss_plan_denied",
                    session_id=session_id,
                )
                return CommandResult(
                    text="❌ Plan denied — send a follow-up message with feedback",
                    notify=True,
                )

        # Grab session_id before send_claude_control_response deletes it
        session_id = _REQUEST_TO_SESSION.get(request_id)

        # Send control response via the public API
        deny_message = _DENY_MESSAGE if not approved else None
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
        if session_id:
            clear_discuss_cooldown(session_id)

        action_text = "✅ Approved" if approved else "❌ Denied"
        logger.info(
            "claude_control.sent",
            request_id=request_id,
            approved=approved,
        )

        return CommandResult(
            text=f"{action_text} permission request",
            notify=True,
        )


BACKEND: CommandBackend = ClaudeControlCommand()
