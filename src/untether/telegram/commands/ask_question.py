"""Command backend for AskUserQuestion option button callbacks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...commands import CommandBackend, CommandContext, CommandResult
from ...logging import get_logger
from ...transport import MessageRef, RenderedMessage, SendOptions, Transport

if TYPE_CHECKING:
    from ...runners.claude import AskQuestionState

logger = get_logger(__name__)

_EARLY_TOASTS: dict[str, str] = {
    "opt": "Selected",
    "other": "Type your reply...",
}

# #698: shown for a tap that lands after the flow was answered and torn down.
_ALREADY_ANSWERED_TOAST = "Already answered"
# #684: shown when the CLI withdrew the question (control_cancel_request).
_NO_LONGER_NEEDED_TOAST = "No longer needed"


def _resolved_toast(outcome: str) -> str:
    return (
        _NO_LONGER_NEEDED_TOAST if outcome == "cancelled" else _ALREADY_ANSWERED_TOAST
    )


async def send_next_ask_question_message(
    transport: Transport,
    *,
    chat_id: int,
    user_msg_id: int,
    thread_id: int | None,
    flow: AskQuestionState,
    notify: bool = True,
) -> None:
    """Send the next question in a multi-question AskUserQuestion flow.

    Used by the text-reply continuation path (after the user clicks "Other"
    and types an answer). The callback-button path edits the existing message
    in-place via ``ctx.executor.edit`` instead.

    Regression: prior to #488 the loop dispatcher constructed the
    ``RenderedMessage`` correctly but passed it to ``send_plain`` which
    expects a ``str``, causing a ``TypeError`` and crashing the whole
    Untether process.
    """
    from ...runners.claude import format_question_message, get_question_option_buttons

    # #713: this message is sent with parse_mode="HTML", so the agent-authored
    # question text must be escaped or a stray `<svg>` fails the whole send.
    msg_text = format_question_message(flow, escape_html=True)
    buttons = get_question_option_buttons(flow)
    await transport.send(
        channel_id=chat_id,
        message=RenderedMessage(
            text=msg_text,
            extra={
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": buttons},
            },
        ),
        options=SendOptions(
            reply_to=MessageRef(channel_id=chat_id, message_id=user_msg_id),
            notify=notify,
            thread_id=thread_id,
        ),
    )


class AskQuestionCommand:
    """Command backend for AskUserQuestion option selection."""

    id = "aq"
    description = "Handle AskUserQuestion option buttons"
    answer_early = True

    @staticmethod
    def early_answer_toast(
        args_text: str, *, channel_id: int | None = None
    ) -> str | None:
        from ...runners.claude import get_ask_question_flow, recently_resolved_ask_flow

        action = args_text.split(":", 1)[0].lower() if args_text else ""
        # #698: the early answer fires before `handle` runs, so this toast is
        # the only feedback a late tap on an already-answered keyboard gets.
        # Don't tell the user "Selected" for a tap that will do nothing.
        # #715: both lookups are channel-scoped. Unscoped, a chat with no
        # outstanding question would see another chat's live flow and toast
        # "Selected" for a tap that `handle` then rejects.
        if get_ask_question_flow(channel_id=channel_id) is None:
            resolved = recently_resolved_ask_flow(channel_id=channel_id)
            if resolved is not None:
                return _resolved_toast(resolved[1])
        return _EARLY_TOASTS.get(action)

    async def handle(self, ctx: CommandContext) -> CommandResult | None:
        from ...runner_bridge import advance_ask_action_model, clear_ask_action_model
        from ...runners.claude import (
            answer_ask_question_with_options,
            format_question_message,
            get_ask_question_flow,
            get_question_option_buttons,
            recently_resolved_ask_flow,
        )

        parts = ctx.args_text.split(":", 1)
        action = parts[0].lower() if parts else ""

        # #715: scope the lookup to the chat the tap came from. The resolver
        # returns the FIRST flow in the registry when unscoped, so with two
        # concurrent AskUserQuestion flows a tap in chat B was answered
        # against whichever iterated first — silently recording B's option
        # index as A's answer, because the callback data (`aq:opt:N`) is
        # positional and so never fails. #698 already channel-scoped the
        # sibling `recently_answered_ask_flow` lookup for exactly this
        # reason; the live-flow lookup one line up was left unscoped.
        channel_id = ctx.message.channel_id
        flow = get_ask_question_flow(channel_id=channel_id)
        if flow is None:
            # #698: an option tap that lost the race against the (async, outbox-
            # queued) keyboard strip is an expected user race, not an unexplained
            # missing flow — INFO, and say something true.
            resolved = recently_resolved_ask_flow(channel_id=channel_id)
            if resolved is not None:
                answered, outcome = resolved
                logger.info(
                    "ask_question.flow_already_answered",
                    action=action,
                    request_id=answered,
                    outcome=outcome,
                )
                return CommandResult(text=_resolved_toast(outcome), notify=False)
            logger.warning("ask_question.flow_missing", action=action)
            return CommandResult(text="No active question", notify=False)

        if action == "opt":
            # Option selected: "opt:N"
            option_idx_str = parts[1] if len(parts) > 1 else "0"
            try:
                option_idx = int(option_idx_str)
            except ValueError:
                logger.warning(
                    "ask_question.option_parse_failed",
                    request_id=flow.request_id,
                    raw_value=option_idx_str,
                )
                return CommandResult(
                    text="That option is no longer valid.", notify=True
                )

            # #710: read the current question only after bounds-checking it.
            # The index is incremented below and separated from the flow
            # teardown by an `await`, so two callbacks racing that window left
            # the second one indexing past the end — an ERROR-level traceback
            # (`callback.failed`, a signature the issue watcher auto-files on)
            # plus a failure toast, for an action that in fact succeeded.
            # Treat out-of-range as the already-answered case so both
            # orderings of a double-tap produce the same truthful outcome.
            if flow.current_index >= len(flow.questions):
                logger.info(
                    "ask_question.flow_already_answered",
                    action=action,
                    request_id=flow.request_id,
                )
                return CommandResult(text=_ALREADY_ANSWERED_TOAST, notify=False)

            # Get the selected option label
            current_q = flow.questions[flow.current_index]
            options = current_q.get("options", [])
            if 0 <= option_idx < len(options):
                selected_label = options[option_idx].get(
                    "label", f"Option {option_idx + 1}"
                )
            else:
                selected_label = f"Option {option_idx + 1}"

            # Record the answer
            question_key = current_q.get(
                "question", f"Question {flow.current_index + 1}"
            )
            flow.answers[question_key] = selected_label
            flow.current_index += 1

            # Check if there are more questions
            if flow.current_index < len(flow.questions):
                # Render next question by editing the message.
                # #713: the same question needs two different encodings. The
                # model title is rendered through render_markdown (which
                # escapes tags itself), so it takes the RAW text; the edit
                # below is parse_mode="HTML", so it takes the escaped text.
                # Passing the escaped string to both would double-escape and
                # show the user a literal `&lt;svg&gt;` on the next heartbeat.
                msg_text = format_question_message(flow)
                msg_html = format_question_message(flow, escape_html=True)
                buttons = get_question_option_buttons(flow)
                # #709: advance the MODEL first. The edit below is what the
                # user sees immediately; without this the next 30s heartbeat
                # re-renders from the tracker's intercept-time title and
                # keyboard and clobbers it, showing Q1's text and Q1's option
                # labels while Q2 is outstanding. Updating the model makes
                # that re-render a no-op instead of a regression.
                if not advance_ask_action_model(
                    flow.request_id, title=msg_text, buttons=buttons
                ):
                    logger.debug(
                        "ask_question.model_not_tracked",
                        request_id=flow.request_id,
                    )
                msg = RenderedMessage(
                    text=msg_html,
                    extra={
                        "parse_mode": "HTML",
                        "reply_markup": {"inline_keyboard": buttons},
                    },
                )
                await ctx.executor.edit(ctx.message, msg)
                return None
            else:
                # All questions answered — send structured response.
                # #709: drop the keyboard from the MODEL before the await, so
                # a heartbeat landing mid-teardown can't repaint the answered
                # question's buttons. This is the model-side half of the #550
                # keyboard strip below, and it narrows #698's race window.
                request_id = flow.request_id
                advance_ask_action_model(
                    request_id, title="✅ All questions answered", buttons=None
                )
                success = await answer_ask_question_with_options(request_id)
                clear_ask_action_model(request_id)
                if success:
                    # Strip the inline keyboard from the final question message
                    # so the user can no longer click buttons that would fire
                    # `ask_question.flow_missing` warnings. We use a short
                    # completion text because `flow.current_index` is now past
                    # the end of `flow.questions` (so `format_question_message`
                    # would IndexError) and `MessageRef` does not carry the
                    # original text.
                    cleared = RenderedMessage(
                        text="✅ All questions answered",
                        extra={
                            "parse_mode": "HTML",
                            "reply_markup": {"inline_keyboard": []},
                        },
                    )
                    try:
                        await ctx.executor.edit(ctx.message, cleared)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "ask_question.keyboard_clear_failed",
                            request_id=flow.request_id,
                            error=str(exc),
                        )

                    answer_lines = []
                    for question, answer in flow.answers.items():
                        answer_lines.append(f"Q: {question}\nA: {answer}")
                    answers_summary = "\n\n".join(answer_lines)
                    return CommandResult(
                        text=f"Answers sent:\n\n{answers_summary}",
                        notify=True,
                    )
                # Preserve buttons on failure so the user can retry.
                return CommandResult(
                    text="Failed to send answers — session may have ended",
                    notify=True,
                )

        elif action == "other":
            # "Other" clicked — switch to text input mode
            flow.awaiting_text = True
            return CommandResult(
                text="Type your answer as a reply...",
                notify=False,
            )

        return None


BACKEND: CommandBackend = AskQuestionCommand()
