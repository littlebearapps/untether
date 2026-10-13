"""Approve / Deny / Let's discuss on an Antigravity plan card (#981).

The card is posted after a Plan first turn's final (``utils/antigravity_plan``
builds it; the bridge sends it). agy's plan turn has already ended, so every
answer acts on the *conversation*, not on a waiting process:

- **Approve** resumes the conversation in Workspace mode with
  ``APPROVE_PROMPT`` — a queued follow-up in this chat, so it waits for and
  holds the session like a reply would (P-H3c).
- **Deny** runs nothing; the conversation stays resumable.
- **Let's discuss** runs nothing either: the user's next message is a plan
  turn on the same conversation (the chat is still in Plan first), which
  rewrites the plan and posts a new card.

Callback data: ``agy_plan:<approve|deny|discuss>:<request id>``.
"""

from __future__ import annotations

from ...commands import CommandBackend, CommandContext, CommandResult
from ...logging import get_logger
from ...model import ResumeToken
from ...transport import MessageRef, RenderedMessage
from ...utils import antigravity_plan
from .ask_question import _ALREADY_ANSWERED_TOAST

logger = get_logger(__name__)

ENGINE = "antigravity"

_EARLY_TOASTS = {
    "approve": "Approved",
    "deny": "Denied",
    "discuss": "Let's discuss…",
}
_EXPIRED_TOAST = "This plan has expired"
_SUPERSEDED_TOAST = "A newer plan replaced this one"

_EXPIRED_TEXT = (
    "⚠️ This plan card has expired (or Untether restarted) — send a message "
    "to keep planning, then approve the new plan."
)
_SUPERSEDED_TEXT = "ℹ️ A newer plan replaced this one — use the latest card."
_ALREADY_TEXT = f"ℹ️ {_ALREADY_ANSWERED_TOAST}"
_UNAVAILABLE_TEXT = "⚠️ Untether couldn't start the approved run here — try again."
_MOVED_TEXT = (
    "⚠️ This chat's project directory changed since the plan was written, so "
    "Untether won't carry it out — send a message to plan again."
)


def _parse(args_text: str) -> tuple[str, str] | None:
    action, _, request_id = (args_text or "").partition(":")
    action = action.lower()
    if action not in antigravity_plan.ACTIONS or not request_id:
        return None
    return action, request_id


def _refusal_toast(reason: str) -> str:
    if reason == "superseded":
        return _SUPERSEDED_TOAST
    if reason == "expired":
        return _EXPIRED_TOAST
    return _ALREADY_ANSWERED_TOAST


def _refusal_text(reason: str) -> str:
    if reason == "superseded":
        return _SUPERSEDED_TEXT
    if reason == "expired":
        return _EXPIRED_TEXT
    return _ALREADY_TEXT


class AntigravityPlanCommand:
    """Callback backend for the Antigravity plan card."""

    id = "agy_plan"
    description = "Answer an Antigravity plan card"
    answer_early = True
    # #1068: button taps only — not in the command menu.
    callback_only = True

    @staticmethod
    def early_answer_toast(
        args_text: str,
        *,
        channel_id: int | None = None,
        claim_owner: str | None = None,
    ) -> str | None:
        """Claim the card for this tap before the dispatcher's first await
        (#685 parity), so a double tap can't toast — or act — twice."""
        parsed = _parse(args_text)
        if parsed is None:
            return None
        action, request_id = parsed
        try:
            if claim_owner is None:
                plan = antigravity_plan.peek(request_id, channel_id)
                if plan is None:
                    return _EXPIRED_TOAST
                if plan.status in ("pending", "discuss"):
                    return _EARLY_TOASTS[action]
                return _refusal_toast(
                    "superseded" if plan.status == "superseded" else "answered"
                )
            reason, _plan = antigravity_plan.claim(request_id, channel_id, claim_owner)
        except Exception:  # noqa: BLE001 — a toast must never take out the tap
            logger.debug("antigravity.plan.early_toast_failed", exc_info=True)
            return _EARLY_TOASTS[action]
        return _EARLY_TOASTS[action] if reason == "ok" else _refusal_toast(reason)

    @staticmethod
    def release_early_claim(owner: str) -> None:
        antigravity_plan.release(owner)

    async def handle(self, ctx: CommandContext) -> CommandResult | None:
        parsed = _parse(ctx.args_text)
        if parsed is None:
            logger.warning("antigravity.plan.invalid_callback", args_text=ctx.args_text)
            return CommandResult(text="Invalid plan callback", notify=False)
        action, request_id = parsed
        channel_id = ctx.message.channel_id
        owner = ctx.callback_query_id or f"agy-plan-{id(ctx)}"
        reason, plan = antigravity_plan.claim(request_id, channel_id, owner)
        card_ref = MessageRef(
            channel_id=channel_id,
            message_id=ctx.message.message_id,
            thread_id=ctx.message.thread_id,
        )
        if reason != "ok" or plan is None:
            logger.info(
                "antigravity.plan.tap_refused",
                request_id=request_id,
                action=action,
                reason=reason,
                channel_id=channel_id,
            )
            if reason != "claimed" and await self._retire_card(
                ctx, card_ref, plan, reason
            ):
                # #1074: the card now says why and has no buttons; the toast
                # said so too, so nothing more is posted.
                return None
            return CommandResult(text=_refusal_text(reason), notify=False)

        if action == "approve":
            enqueue = getattr(ctx.executor, "enqueue_resume", None)
            if enqueue is None:
                antigravity_plan.release(owner)
                logger.warning(
                    "antigravity.plan.approve_unavailable", request_id=request_id
                )
                return CommandResult(text=_UNAVAILABLE_TEXT, notify=True)
            context = antigravity_plan.approve_context(plan)
            if plan.cwd is not None:
                # Never carry a plan out somewhere else than it was written
                # for (a project's path or worktree can change in between).
                try:
                    cwd = ctx.runtime.resolve_run_cwd(context)
                except Exception:  # noqa: BLE001 — treated as "moved"
                    cwd = None
                if cwd is None or str(cwd) != plan.cwd:
                    antigravity_plan.resolve(request_id, "deny")
                    logger.warning(
                        "antigravity.plan.cwd_changed",
                        request_id=request_id,
                        session_id=plan.conversation_id,
                    )
                    await self._edit_card(ctx, card_ref, plan.name, "deny")
                    return CommandResult(text=_MOVED_TEXT, notify=True)
            antigravity_plan.resolve(request_id, "approve")
            await self._edit_card(ctx, card_ref, plan.name, "approve")
            await enqueue(
                antigravity_plan.APPROVE_PROMPT,
                ResumeToken(engine=ENGINE, value=plan.conversation_id),
                context=context,
                reply_to_message_id=ctx.message.message_id,
                thread_id=plan.thread_id,
            )
            logger.info(
                "antigravity.plan.decision",
                decision="approve",
                path="resume",
                request_id=request_id,
                session_id=plan.conversation_id,
            )
            return None

        antigravity_plan.resolve(request_id, action)
        await self._edit_card(ctx, card_ref, plan.name, action)
        logger.info(
            "antigravity.plan.decision",
            decision=action,
            request_id=request_id,
            session_id=plan.conversation_id,
        )
        return None

    @classmethod
    async def _retire_card(
        cls,
        ctx: CommandContext,
        ref: MessageRef,
        plan: antigravity_plan.PendingPlan | None,
        reason: str,
    ) -> bool:
        """#1074: a tap on a card that can't be answered (replaced by a newer
        plan, expired, already answered) takes its buttons off, so it can't
        be tapped again. A tap still being acted on (``claimed``) is left to
        the tap that holds it."""
        if plan is None:  # expired, restarted, or another chat's card
            text = _EXPIRED_TEXT
        else:
            if reason == "superseded":
                antigravity_plan.mark_retired(plan.request_id)
            text = antigravity_plan.card_text(plan.name, plan.status)
        return await cls._edit_text(ctx, ref, text)

    @classmethod
    async def _edit_card(
        cls, ctx: CommandContext, ref: MessageRef, name: str, decision: str
    ) -> None:
        """The card shows the decision and loses its buttons."""
        await cls._edit_text(ctx, ref, antigravity_plan.card_text(name, decision))

    @staticmethod
    async def _edit_text(ctx: CommandContext, ref: MessageRef, text: str) -> bool:
        try:
            edited = await ctx.executor.edit(
                ref,
                RenderedMessage(
                    text=text,
                    extra={"entities": [], "reply_markup": {"inline_keyboard": []}},
                ),
            )
        except Exception:  # noqa: BLE001 — the decision stands either way
            logger.debug("antigravity.plan.card_edit_failed", exc_info=True)
            return False
        return edited is not None


BACKEND: CommandBackend = AntigravityPlanCommand()
