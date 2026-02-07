"""Command backend for handling Claude Code control requests (approve/deny buttons)."""

from __future__ import annotations

from ...commands import CommandBackend, CommandContext, CommandResult
from ...logging import get_logger
from ...runners.claude import send_claude_control_response

logger = get_logger(__name__)


class ClaudeControlCommand:
    """Command backend for Claude Code permission approval/denial."""

    id = "claude_control"
    description = "Handle Claude Code permission requests"

    async def handle(self, ctx: CommandContext) -> CommandResult | None:
        """Handle callback from approve/deny buttons.

        Args:
            ctx: Command context with args_text="approve:request_id" or "deny:request_id"

        Returns:
            CommandResult with feedback message, or None
        """
        # Parse args: "approve:request_id" or "deny:request_id"
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

        if action not in ("approve", "deny"):
            logger.warning(
                "claude_control.unknown_action",
                action=action,
                request_id=request_id,
            )
            return CommandResult(
                text=f"Unknown action: {action}",
                notify=False,
            )

        approved = action == "approve"

        # Send control response via the public API
        success = await send_claude_control_response(request_id, approved)

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
