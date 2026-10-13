"""Engine-neutral approval / question registry (#960).

The registry moved out of ``runners/claude.py`` with no behaviour change. The
~950 test references to its stores go through ``runners.claude``, so the
first guard is identity: every name ``runners.claude`` re-exports must be the
very object ``interactive`` owns (a copy would pass import and split state).
"""

from __future__ import annotations

import untether.runners.claude as claude_mod
from untether.interactive import questions, registry

REGISTRY_NAMES = (
    "CONTROL_REQUEST_TIMEOUT_SECONDS",
    "ControlLookup",
    "ControlRequestSnapshot",
    "ControlRequestStatus",
    "ControlSendResult",
    "HandledControl",
    "InflightClaim",
    "_CANCELLED_DURING_WRITE",
    "_DIRECT_CLAIM_SEQ",
    "_HANDLED_REQUESTS",
    "_HANDLED_REQUESTS_MAX",
    "_INFLIGHT_CONTROL_RESPONSES",
    "_REQUEST_TO_CHANNEL",
    "_REQUEST_TO_CHANNEL_MAX",
    "_REQUEST_TO_INPUT",
    "_REQUEST_TO_ORIGINATOR",
    "_REQUEST_TO_SESSION",
    "_REQUEST_TO_TOOL_NAME",
    "_TERMINAL_CONTROL_OUTCOMES",
    "_bind_request_channel",
    "claim_control_request",
    "classify_control_request",
    "control_request_origin",
    "control_request_originator",
    "mark_request_handled",
    "new_control_claim_owner",
    "release_control_claims",
)


QUESTION_NAMES = (
    "ANSWERED_ASK_FLOW_TTL_S",
    "AskQuestionState",
    "_ANSWERED_ASK_FLOWS",
    "_ANSWERED_ASK_FLOWS_MAX",
    "_ASK_QUESTION_FLOWS",
    "_PENDING_ASK_REQUESTS",
    "_record_answered_ask_flow",
    "format_question_message",
    "get_ask_question_flow",
    "get_ask_question_flow_by_id",
    "get_pending_ask_request",
    "get_question_option_buttons",
    "pending_control_requests_for_session",
    "recently_answered_ask_flow",
    "recently_resolved_ask_flow",
)


def test_interactive_reexports_are_same_objects() -> None:
    for name in REGISTRY_NAMES:
        assert getattr(claude_mod, name) is getattr(registry, name), name
    for name in QUESTION_NAMES:
        assert getattr(claude_mod, name) is getattr(questions, name), name


def test_option_label_accepts_str_and_dict() -> None:
    # Claude: dicts with a label (and the old fallback when it is missing).
    assert questions.option_label({"label": "Red", "description": "x"}, 0) == "Red"
    assert questions.option_label({"description": "x"}, 2) == "Option 3"
    # Antigravity's ask_question: plain strings.
    assert questions.option_label("Blue", 1) == "Blue"


def test_option_buttons_identical_for_dict_and_str_options() -> None:
    def flow(options: list[object]) -> questions.AskQuestionState:
        return questions.AskQuestionState(
            request_id="r",
            channel_id=1,
            questions=[{"question": "?", "options": options}],
        )

    dicts = questions.get_question_option_buttons(
        flow([{"label": "Red"}, {"label": "Green"}])
    )
    strings = questions.get_question_option_buttons(flow(["Red", "Green"]))
    assert dicts == strings
    assert dicts == [
        [{"text": "Red", "callback_data": "aq:opt:0"}],
        [{"text": "Green", "callback_data": "aq:opt:1"}],
        [{"text": "Other (type reply)", "callback_data": "aq:other"}],
    ]
