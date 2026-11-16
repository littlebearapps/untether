"""AskUserQuestion flow state and helpers (#960).

Moved verbatim out of ``runners/claude.py``. A question is a control request
(it is registered in :mod:`.registry` like any approval) plus the state here:
the pending-question entry a typed reply routes to (scoped by chat, #144),
the multi-question flow its option buttons step through, and a short memo of
flows already answered or withdrawn (#698, #684). Engine-neutral: options may
be Claude's ``{"label": ...}`` dicts or an engine's plain strings
(:func:`option_label`).
"""

from __future__ import annotations

import html
import time
from dataclasses import dataclass, field
from typing import Any

from .registry import (
    _REQUEST_TO_INPUT,
    _REQUEST_TO_SESSION,
    Decision,
    answer_control_request,
)

# A1: Pending AskUserQuestion requests: request_id -> (channel_id, question text)
# When Claude Code asks a question, the user can reply via Telegram text.
# Scoped by channel_id to prevent cross-chat message stealing (#144).
_PENDING_ASK_REQUESTS: dict[str, tuple[int, str]] = {}


@dataclass(slots=True)
class AskQuestionState:
    """Tracks multi-question AskUserQuestion flow state."""

    request_id: str
    channel_id: int
    questions: list[dict[str, Any]]
    current_index: int = 0
    answers: dict[str, str] = field(default_factory=dict)
    awaiting_text: bool = False  # True when "Other" was clicked


# Active AskUserQuestion flows: request_id -> AskQuestionState
_ASK_QUESTION_FLOWS: dict[str, AskQuestionState] = {}

# #698: flows that were answered and torn down: request_id -> (channel_id, ts).
# #550 strips the inline keyboard so late taps can't fire
# ``ask_question.flow_missing``, but that strip is an async outbox edit issued
# *after* the flow is popped — and a tap already in flight cannot be recalled,
# so the losing side of that race is unavoidable (1s on nsd; wider in a busy
# group chat where the edit queues behind other traffic). Remembering the
# answered flow briefly lets a late tap resolve to "already answered" instead
# of looking like an unexplained missing flow.
# #684: values are (channel_id, ts, outcome) — ``outcome`` is ``answered``
# or ``cancelled`` (the CLI withdrew the question), so a late tap can say
# "No longer needed" rather than "Already answered".
_ANSWERED_ASK_FLOWS: dict[str, tuple[int, float, str]] = {}
ANSWERED_ASK_FLOW_TTL_S: float = 300.0
_ANSWERED_ASK_FLOWS_MAX = 32


def pending_control_requests_for_session(session_id: str | None) -> int:
    """Return how many control requests for ``session_id`` are still awaiting
    a response (approval buttons or AskUserQuestion).

    This is the **authoritative** "is this session blocked on the user?"
    signal: :data:`_REQUEST_TO_SESSION` is populated the moment a
    ``control_request`` is intercepted and popped again in
    :func:`write_control_response` (and in the Telegram callback handlers)
    as soon as the request is answered — so an entry means the request is
    genuinely outstanding right now, not merely that one happened earlier.

    #495/#499/#500: the stall detector previously inferred this from
    presentation state (``_has_pending_approval`` — the most recent action's
    ``inline_keyboard`` detail) or from the JSONL ring buffer
    (``_approval_pending`` — then the newest ring entry). Both are
    "most recent thing" heuristics and both go stale during a long approval
    wait: an ExitPlanMode permission request carries no ``inline_keyboard``
    detail, and after hours of waiting the newest ring entry is a stale
    ``user``/``result`` frame. The registry does not go stale. #697: the
    watchdog now consults this registry first, keeping a non-positional
    backward scan of the ring buffer only as a True-only fallback.

    The post-result idle watchdog and the pre-result silence cap already
    consult these same registries to decide whether to defer; this helper
    centralises that query so the three consumers cannot drift apart.
    """
    if not session_id:
        return 0
    pending = sum(1 for v in _REQUEST_TO_SESSION.values() if v == session_id)
    pending += sum(
        1 for k in _PENDING_ASK_REQUESTS if _REQUEST_TO_SESSION.get(k) == session_id
    )
    return pending


def get_pending_ask_request(
    channel_id: int | None = None,
) -> tuple[str, str] | None:
    """Return the oldest pending AskUserQuestion for *channel_id*, or None.

    When *channel_id* is provided, only requests from that channel are
    returned — preventing cross-chat message stealing (#144).
    """
    for request_id, (ch, question) in _PENDING_ASK_REQUESTS.items():
        if channel_id is not None and ch != channel_id:
            continue
        return request_id, question
    return None


async def answer_ask_question(request_id: str, answer: str) -> bool:
    """Answer a pending AskUserQuestion by denying with the user's response.

    The deny message contains the user's answer so Claude Code reads it and
    continues with that information. ``Decision.answer`` carries the raw
    answer for an engine that words its own reply.
    """
    _PENDING_ASK_REQUESTS.pop(request_id, None)
    deny_message = (
        f"The user answered your question via Telegram:\n\n"
        f'"{answer}"\n\n'
        f"Use this answer and continue. Do not call AskUserQuestion again "
        f"for this same question."
    )
    return await answer_control_request(
        request_id,
        Decision(
            approved=False,
            action="answer",
            deny_message=deny_message,
            answer=answer,
        ),
    )


async def answer_ask_question_with_options(request_id: str) -> bool:
    """Send a structured answer for an AskUserQuestion flow with collected answers.

    Approves the request with updatedInput containing the answers dict.
    """
    flow = _ASK_QUESTION_FLOWS.pop(request_id, None)
    _PENDING_ASK_REQUESTS.pop(request_id, None)
    if flow is None:
        return False
    _record_answered_ask_flow(request_id, flow.channel_id)

    # Update the stored input to include answers
    stored_input = _REQUEST_TO_INPUT.get(request_id)
    if stored_input is not None:
        stored_input["answers"] = flow.answers

    return await answer_control_request(
        request_id, Decision(approved=True, action="answer", answers=flow.answers)
    )


def _record_answered_ask_flow(
    request_id: str, channel_id: int, *, outcome: str = "answered"
) -> None:
    """Remember that *request_id*'s AskUserQuestion flow was answered (#698)
    — or withdrawn by the CLI (``outcome="cancelled"``, #684)."""
    now = time.monotonic()
    for rid, (_ch, ts, _outcome) in list(_ANSWERED_ASK_FLOWS.items()):
        if now - ts > ANSWERED_ASK_FLOW_TTL_S:
            del _ANSWERED_ASK_FLOWS[rid]
    # Re-recording moves the entry to the end, so insertion order stays
    # oldest → newest (the size cap below evicts from the front).
    _ANSWERED_ASK_FLOWS.pop(request_id, None)
    _ANSWERED_ASK_FLOWS[request_id] = (channel_id, now, outcome)
    while len(_ANSWERED_ASK_FLOWS) > _ANSWERED_ASK_FLOWS_MAX:
        _ANSWERED_ASK_FLOWS.pop(next(iter(_ANSWERED_ASK_FLOWS)))


def recently_answered_ask_flow(channel_id: int | None = None) -> str | None:
    """Return the request_id of a just-answered AskUserQuestion flow, or None.

    #698: lets the callback handler tell a late tap on an already-answered
    keyboard ("Already answered", INFO) apart from a genuinely unexplained
    missing flow (WARNING). Scoped by channel so one chat's late tap cannot
    claim another chat's answer.
    """
    resolved = recently_resolved_ask_flow(channel_id)
    return resolved[0] if resolved is not None else None


def recently_resolved_ask_flow(
    channel_id: int | None = None,
) -> tuple[str, str] | None:
    """Like :func:`recently_answered_ask_flow`, with the outcome (#684).

    Returns ``(request_id, outcome)`` — ``answered`` or ``cancelled`` (the
    CLI withdrew the question) — or None. The **newest** resolution in the
    chat wins (#1071: it used to return the oldest one still in the TTL, so
    a late tap was logged against, and toasted for, an earlier question).
    """
    now = time.monotonic()
    newest: tuple[float, str, str] | None = None
    for rid, (ch, ts, outcome) in list(_ANSWERED_ASK_FLOWS.items()):
        if now - ts > ANSWERED_ASK_FLOW_TTL_S:
            del _ANSWERED_ASK_FLOWS[rid]
            continue
        if channel_id is not None and ch != channel_id:
            continue
        if newest is None or ts >= newest[0]:
            newest = (ts, rid, outcome)
    return None if newest is None else (newest[1], newest[2])


def get_ask_question_flow(
    channel_id: int | None = None,
) -> AskQuestionState | None:
    """Return the active AskUserQuestion flow for *channel_id*, or None."""
    for flow in _ASK_QUESTION_FLOWS.values():
        if channel_id is not None and flow.channel_id != channel_id:
            continue
        return flow
    return None


def get_ask_question_flow_by_id(request_id: str) -> AskQuestionState | None:
    """Return a specific AskUserQuestion flow, or None."""
    return _ASK_QUESTION_FLOWS.get(request_id)


def option_label(opt: Any, index: int) -> str:
    """The button label of a question option (#960).

    Claude sends ``{"label": ..., "description": ...}`` dicts; Antigravity's
    ``ask_question`` sends plain strings. A dict without a label reads
    ``Option N`` as before.
    """
    if isinstance(opt, str):
        return opt
    return opt.get("label", f"Option {index + 1}")


def format_question_message(
    flow: AskQuestionState, *, escape_html: bool = False
) -> str:
    """Format the current question in a flow as a display string.

    The question text is agent-authored free text and routinely contains
    angle brackets — a question about an inline ``<svg>``, a generic like
    ``list<T>``, a shell redirect. It is consumed under two different and
    incompatible rendering contracts:

    * ``escape_html=True`` — the caller builds a ``RenderedMessage`` carrying
      ``parse_mode="HTML"``. Telegram parses the whole body as HTML and
      accepts only a small tag whitelist, so an unescaped ``<svg>`` fails the
      **entire** request with ``400 Bad Request: can't parse entities:
      Unsupported start tag``. Because the ask-flow messages carry the option
      keyboard, losing that edit leaves the run unanswerable from Telegram
      (#713 — same class as the #199 fix in ``commands/auth.py``).
    * the default, ``escape_html=False`` — the caller stores the string as a
      progress action title (``advance_ask_action_model``, #709), which is
      rendered via ``render_markdown`` where markdown-it (``html: False``)
      already neutralises tags. Escaping here as well would double-escape and
      show the user a literal ``&lt;svg&gt;``.

    Only the agent's text is escaped; the bot-authored ``❓ Question N of M:``
    prefix contains no HTML-special characters. ``quote=False`` because
    quotation marks are legal in HTML text content — escaping them would
    surface a literal ``&quot;`` for a merely quoted question.
    """
    q = flow.questions[flow.current_index]
    question_text = q.get("question", "")
    if escape_html:
        question_text = html.escape(question_text, quote=False)
    total = len(flow.questions)
    if total > 1:
        return f"❓ Question {flow.current_index + 1} of {total}: {question_text}"
    return f"❓ {question_text}"


def get_question_option_buttons(flow: AskQuestionState) -> list[list[dict[str, str]]]:
    """Build inline keyboard buttons for the current question's options."""
    q = flow.questions[flow.current_index]
    options = q.get("options", [])
    buttons: list[list[dict[str, str]]] = []
    for i, opt in enumerate(options[:4]):
        label = option_label(opt, i)
        buttons.append([{"text": label, "callback_data": f"aq:opt:{i}"}])
    buttons.append([{"text": "Other (type reply)", "callback_data": "aq:other"}])
    return buttons
