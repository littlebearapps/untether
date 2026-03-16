"""Command backend for AskUserQuestion option button callbacks."""

from __future__ import annotations

from ...commands import CommandBackend, CommandContext, CommandResult
from ...logging import get_logger
from ...transport import RenderedMessage

logger = get_logger(__name__)

_EARLY_TOASTS: dict[str, str] = {
    "opt": "Selected",
    "other": "Type your reply...",
}


class AskQuestionCommand:
    """Command backend for AskUserQuestion option selection."""

    id = "aq"
    description = "Handle AskUserQuestion option buttons"
    answer_early = True

    @staticmethod
    def early_answer_toast(args_text: str) -> str | None:
        action = args_text.split(":", 1)[0].lower() if args_text else ""
        return _EARLY_TOASTS.get(action)

    async def handle(self, ctx: CommandContext) -> CommandResult | None:
        from ...runners.claude import (
            answer_ask_question_with_options,
            format_question_message,
            get_ask_question_flow,
            get_question_option_buttons,
        )

        parts = ctx.args_text.split(":", 1)
        action = parts[0].lower() if parts else ""

        flow = get_ask_question_flow()
        if flow is None:
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
                # Render next question by editing the message
                msg_text = format_question_message(flow)
                buttons = get_question_option_buttons(flow)
                msg = RenderedMessage(
                    text=msg_text,
                    extra={
                        "parse_mode": "HTML",
                        "reply_markup": {"inline_keyboard": buttons},
                    },
                )
                await ctx.executor.edit(ctx.message, msg)
                return None
            else:
                # All questions answered — send structured response
                success = await answer_ask_question_with_options(flow.request_id)
                if success:
                    answer_lines = []
                    for question, answer in flow.answers.items():
                        answer_lines.append(f"Q: {question}\nA: {answer}")
                    answers_summary = "\n\n".join(answer_lines)
                    return CommandResult(
                        text=f"Answers sent:\n\n{answers_summary}",
                        notify=True,
                    )
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
