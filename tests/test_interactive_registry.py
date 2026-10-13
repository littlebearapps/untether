"""Engine-neutral approval / question registry (#960).

The registry moved out of ``runners/claude.py`` with no behaviour change. The
~950 test references to its stores go through ``runners.claude``, so the
first guard is identity: every name ``runners.claude`` re-exports must be the
very object ``interactive`` owns (a copy would pass import and split state).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

import untether.runners.claude as claude_mod
from untether.interactive import preview, questions, registry, unattended
from untether.interactive.registry import (
    ControlRequestStatus,
    Decision,
)
from untether.utils.paths import (
    reset_run_channel_id,
    reset_run_sender_id,
    set_run_channel_id,
    set_run_sender_id,
)

CHAT_A = 111
CHAT_B = 222
USER_A = 9001

_STORES = (
    registry._REQUEST_TO_SESSION,
    registry._REQUEST_TO_INPUT,
    registry._REQUEST_TO_TOOL_NAME,
    registry._REQUEST_TO_CHANNEL,
    registry._REQUEST_TO_ORIGINATOR,
    registry._REQUEST_TO_RESPONDER,
    registry._REQUEST_META,
    registry._HANDLED_REQUESTS,
    registry._INFLIGHT_CONTROL_RESPONSES,
    registry._CANCELLED_DURING_WRITE,
    questions._PENDING_ASK_REQUESTS,
    questions._ASK_QUESTION_FLOWS,
    questions._ANSWERED_ASK_FLOWS,
)


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    for store in _STORES:
        store.clear()
    yield
    for store in _STORES:
        store.clear()


@dataclass
class FakeResponder:
    """A non-Claude engine: records every decision it is asked to deliver."""

    engine: str = "fake"
    callback_prefix: str = "ia"
    alive: bool = True
    written: bool = True
    calls: list[tuple[str, str, Decision]] = field(default_factory=list)
    during_write: Any = None

    def is_alive(self, session_key: str) -> bool:
        return self.alive

    async def respond(
        self, request_id: str, session_key: str, decision: Decision
    ) -> bool:
        self.calls.append((request_id, session_key, decision))
        if self.during_write is not None:
            self.during_write(request_id)
        return self.written


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


def test_moved_helpers_keep_their_claude_names() -> None:
    assert claude_mod._format_diff_preview is preview.format_diff_preview
    assert claude_mod.unattended_deny_message is unattended.unattended_deny_message
    assert claude_mod.answer_ask_question is questions.answer_ask_question
    assert (
        claude_mod.answer_ask_question_with_options
        is questions.answer_ask_question_with_options
    )
    assert claude_mod.respond_to_control_request is registry.respond_to_control_request


# ── registration ────────────────────────────────────────────────────────────


def test_register_always_binds_channel() -> None:
    token = set_run_channel_id(CHAT_A)
    try:
        registry.register("ag-1", session_key="conv", responder=FakeResponder())
    finally:
        reset_run_channel_id(token)
    assert registry._REQUEST_TO_SESSION["ag-1"] == "conv"
    assert registry._REQUEST_TO_CHANNEL["ag-1"] == CHAT_A
    assert registry.control_request_origin("ag-1") == CHAT_A


def test_register_binds_originator_from_run_sender() -> None:
    channel = set_run_channel_id(CHAT_A)
    sender = set_run_sender_id(USER_A)
    try:
        registry.register("ag-1", session_key="conv", responder=FakeResponder())
        registry.register("ag-2", session_key="conv")
    finally:
        reset_run_sender_id(sender)
        reset_run_channel_id(channel)
    assert registry.control_request_originator("ag-1") == USER_A
    assert registry.control_request_originator("ag-2") == USER_A
    # A run without a human sender (cron, webhook) records none.
    registry.register("ag-3", session_key="conv")
    assert registry.control_request_originator("ag-3") is None


def test_register_tool_fields_only_when_named() -> None:
    registry.register("r1", session_key="s")
    assert "r1" not in registry._REQUEST_TO_INPUT
    assert "r1" not in registry._REQUEST_TO_TOOL_NAME
    registry.register("r2", session_key="s", tool_name="Write")
    assert registry._REQUEST_TO_INPUT["r2"] == {}
    assert registry._REQUEST_TO_TOOL_NAME["r2"] == "Write"
    registry.register(
        "r3", session_key="s", tool_name="Edit", tool_input={"file_path": "x"}
    )
    assert registry._REQUEST_TO_INPUT["r3"] == {"file_path": "x"}


def test_classify_channel_mismatch_reads_not_found() -> None:
    token = set_run_channel_id(CHAT_A)
    try:
        registry.register("ag-1", session_key="conv", responder=FakeResponder())
    finally:
        reset_run_channel_id(token)
    # Pending: another chat learns nothing.
    lookup = registry.classify_control_request("ag-1", channel_id=CHAT_B)
    assert lookup.status is ControlRequestStatus.NOT_FOUND
    assert lookup.reason == "channel_mismatch"
    # In flight: same.
    claim = registry.claim_control_request(
        "ag-1", action="approve", owner="tap-1", channel_id=CHAT_A
    )
    assert claim.status is ControlRequestStatus.PENDING
    lookup = registry.classify_control_request("ag-1", channel_id=CHAT_B)
    assert lookup.status is ControlRequestStatus.NOT_FOUND
    registry.release_control_claims("tap-1")
    # Handled in chat A: chat B still reads not found (#715).
    registry._REQUEST_TO_SESSION.pop("ag-1")
    registry.mark_request_handled("ag-1", action="approve", channel_id=CHAT_A)
    lookup = registry.classify_control_request("ag-1", channel_id=CHAT_B)
    assert lookup.status is ControlRequestStatus.NOT_FOUND
    assert lookup.reason == "channel_mismatch"
    lookup = registry.classify_control_request("ag-1", channel_id=CHAT_A)
    assert lookup.status is ControlRequestStatus.ALREADY_HANDLED


def test_claim_serialises_two_taps() -> None:
    registry.register("ag-1", session_key="conv", responder=FakeResponder())
    first = registry.claim_control_request("ag-1", action="approve", owner="tap-1")
    second = registry.claim_control_request("ag-1", action="deny", owner="tap-2")
    assert first.status is ControlRequestStatus.PENDING
    assert second.status is ControlRequestStatus.IN_FLIGHT
    assert second.prior is not None and second.prior.action == "approve"


# ── responders ──────────────────────────────────────────────────────────────


def test_callback_prefix_defaults_to_claude_control() -> None:
    registry.register("req_1", session_key="sid")
    registry.register("ag-1", session_key="conv", responder=FakeResponder())
    assert registry.callback_prefix("req_1") == "claude_control"
    assert registry.callback_prefix("unknown") == "claude_control"
    assert registry.callback_prefix("ag-1") == "ia"


@pytest.mark.anyio
async def test_respond_dispatches_to_registered_responder() -> None:
    fake = FakeResponder()
    registry.register(
        "ag-1",
        session_key="conv",
        responder=fake,
        tool_name="run_command",
        tool_input={"CommandLine": "ls"},
    )
    decision = Decision(approved=True, action="approve")
    result = await registry.respond("ag-1", decision, channel_id=CHAT_A)
    assert result.status is ControlRequestStatus.PENDING
    assert result.sent is True
    assert result.session_id == "conv"
    assert result.tool_name == "run_command"
    assert fake.calls == [("ag-1", "conv", decision)]
    assert "ag-1" not in registry._REQUEST_TO_SESSION
    assert "ag-1" not in registry._REQUEST_TO_RESPONDER
    assert "ag-1" not in registry._INFLIGHT_CONTROL_RESPONSES
    record = registry._HANDLED_REQUESTS["ag-1"]
    assert record is not None and record.action == "approve"
    # A second tap writes nothing.
    again = await registry.respond("ag-1", decision, channel_id=CHAT_A)
    assert again.status is ControlRequestStatus.ALREADY_HANDLED
    assert len(fake.calls) == 1


@pytest.mark.anyio
async def test_respond_session_gone_reads_no_active_session() -> None:
    fake = FakeResponder(alive=False)
    registry.register("ag-1", session_key="conv", responder=fake, tool_name="x")
    result = await registry.respond("ag-1", Decision(approved=False, action="deny"))
    assert result.reason == "no_active_session"
    assert result.sent is False
    assert fake.calls == []
    for store in (
        registry._REQUEST_TO_SESSION,
        registry._REQUEST_TO_TOOL_NAME,
        registry._REQUEST_TO_INPUT,
        registry._REQUEST_TO_RESPONDER,
    ):
        assert "ag-1" not in store


def test_respond_unknown_responder_defaults_to_claude() -> None:
    # A Claude registration (or a test writing the store directly) carries
    # no responder: Claude's answers it, alive iff the session's runner is.
    registry._REQUEST_TO_SESSION["req_1"] = "sid-1"
    responder = registry.responder_for("req_1")
    assert isinstance(responder, claude_mod.ClaudeResponder)
    assert responder.engine == "claude"
    assert registry.session_alive("sid-1") is False
    claude_mod._ACTIVE_RUNNERS["sid-1"] = (object(), 0.0)  # type: ignore[assignment]
    try:
        assert registry.session_alive("sid-1") is True
        assert registry.session_alive("sid-1", request_id="req_1") is True
    finally:
        claude_mod._ACTIVE_RUNNERS.pop("sid-1", None)


@pytest.mark.anyio
async def test_answer_ask_question_routes_through_responder() -> None:
    fake = FakeResponder()
    registry.register("ag-q", session_key="conv", responder=fake, kind="ask")
    questions._PENDING_ASK_REQUESTS["ag-q"] = (CHAT_A, "Colour?")
    assert await questions.answer_ask_question("ag-q", "Blue") is True
    (_rid, _sid, decision) = fake.calls[0]
    assert decision.approved is False
    assert decision.action == "answer"
    assert decision.answer == "Blue"
    assert '"Blue"' in (decision.deny_message or "")
    assert "ag-q" not in questions._PENDING_ASK_REQUESTS


@pytest.mark.anyio
async def test_answer_with_options_passes_answers() -> None:
    fake = FakeResponder()
    registry.register(
        "ag-q", session_key="conv", responder=fake, tool_name="ask_question"
    )
    flow = questions.AskQuestionState(
        request_id="ag-q",
        channel_id=CHAT_A,
        questions=[{"question": "Colour?", "options": ["Red", "Blue"]}],
        answers={"Colour?": "Red"},
    )
    questions._ASK_QUESTION_FLOWS["ag-q"] = flow
    assert await questions.answer_ask_question_with_options("ag-q") is True
    (_rid, _sid, decision) = fake.calls[0]
    assert decision.approved is True
    assert decision.answers == {"Colour?": "Red"}
    assert questions.recently_answered_ask_flow(CHAT_A) == "ag-q"


# ── withdraw / expire / drop_session ───────────────────────────────────────


@pytest.mark.anyio
async def test_withdraw_during_write_reports_cancelled() -> None:
    fake = FakeResponder()
    fake.during_write = lambda rid: registry.withdraw(rid)
    registry.register("ag-1", session_key="conv", responder=fake, tool_name="x")
    result = await registry.respond("ag-1", Decision(approved=True, action="approve"))
    # The engine dropped the request while we were writing: say so (#684).
    assert result.status is ControlRequestStatus.CANCELLED
    record = registry._HANDLED_REQUESTS["ag-1"]
    assert record is not None and record.outcome == "cancelled"
    assert not registry._CANCELLED_DURING_WRITE
    assert not registry._INFLIGHT_CONTROL_RESPONSES


def test_withdraw_idle_retires_and_records_cancelled() -> None:
    registry.register("ag-q", session_key="conv", responder=FakeResponder())
    questions._PENDING_ASK_REQUESTS["ag-q"] = (CHAT_A, "Colour?")
    questions._ASK_QUESTION_FLOWS["ag-q"] = questions.AskQuestionState(
        request_id="ag-q", channel_id=CHAT_A, questions=[{"question": "?"}]
    )
    assert registry.withdraw("ag-q", channel_id=CHAT_A) is False
    assert "ag-q" not in registry._REQUEST_TO_SESSION
    assert "ag-q" not in registry._REQUEST_TO_RESPONDER
    assert "ag-q" not in questions._PENDING_ASK_REQUESTS
    assert "ag-q" not in questions._ASK_QUESTION_FLOWS
    # A late option tap reads "No longer needed".
    assert questions.recently_resolved_ask_flow(CHAT_A) == ("ag-q", "cancelled")
    lookup = registry.classify_control_request("ag-q", channel_id=CHAT_A)
    assert lookup.status is ControlRequestStatus.CANCELLED


def test_expire_is_terminal_against_later_answer() -> None:
    registry.register("ag-1", session_key="conv", responder=FakeResponder())
    assert registry.expire("ag-1", channel_id=CHAT_A) is True
    assert registry.expire("ag-1") is False  # no longer pending
    registry.mark_request_handled("ag-1", action="approve", channel_id=CHAT_A)
    record = registry._HANDLED_REQUESTS["ag-1"]
    assert record is not None
    assert record.outcome == "expired"
    assert record.action == "timeout"


def test_expire_leaves_a_request_being_answered() -> None:
    registry.register("ag-1", session_key="conv", responder=FakeResponder())
    registry.claim_control_request("ag-1", action="approve", owner="tap-1")
    assert registry.expire("ag-1") is False
    assert registry._REQUEST_TO_SESSION["ag-1"] == "conv"


def test_drop_session_clears_all_neutral_stores_and_runs_callbacks() -> None:
    seen: list[str] = []

    def callback(session_key: str) -> str | None:
        seen.append(session_key)
        return "extra" if session_key == "conv" else None

    registry.on_session_dropped(callback)
    registry.on_session_dropped(callback)  # idempotent
    try:
        token = set_run_channel_id(CHAT_A)
        try:
            registry.register(
                "ag-1", session_key="conv", responder=FakeResponder(), tool_name="x"
            )
            registry.register("ag-2", session_key="conv", responder=FakeResponder())
            registry.register("other", session_key="other-conv")
        finally:
            reset_run_channel_id(token)
        registry.claim_control_request("ag-1", action="approve", owner="tap-1")
        registry._CANCELLED_DURING_WRITE.add("ag-1")
        questions._PENDING_ASK_REQUESTS["ag-2"] = (CHAT_A, "?")
        questions._ASK_QUESTION_FLOWS["ag-2"] = questions.AskQuestionState(
            request_id="ag-2", channel_id=CHAT_A, questions=[{"question": "?"}]
        )

        assert registry.drop_session("conv") == ["extra", "requests(2)"]
        assert seen == ["conv"]
        for rid in ("ag-1", "ag-2"):
            for store in _STORES:
                assert rid not in store, (rid, store)
        assert registry._REQUEST_TO_SESSION == {"other": "other-conv"}
        assert registry.drop_session("conv") == ["extra"]
    finally:
        registry._SESSION_DROPPED_CALLBACKS.remove(callback)


def test_claude_cleanup_runs_discuss_feedback_callback() -> None:
    from untether.telegram.commands import claude_control

    claude_control._DISCUSS_FEEDBACK_REFS["sid-x"] = object()  # type: ignore[assignment]
    registry._REQUEST_TO_SESSION["req_1"] = "sid-x"
    claude_mod._cleanup_session_registries("sid-x")
    assert "sid-x" not in claude_control._DISCUSS_FEEDBACK_REFS
    assert "req_1" not in registry._REQUEST_TO_SESSION


def test_snapshot_lists_responder_requests() -> None:
    fake = FakeResponder()
    registry.register(
        "ag-1",
        session_key="conv",
        responder=fake,
        tool_name="run_command",
        tool_input={"CommandLine": "ls"},
    )
    registry.register("ag-q", session_key="conv", responder=fake, kind="ask")
    questions._PENDING_ASK_REQUESTS["ag-q"] = (CHAT_A, "?")
    registry.register("req_1", session_key="conv")  # Claude-style: skipped
    at = registry._REQUEST_META["ag-1"].registered_at
    snaps = registry.snapshot(
        "conv",
        now=at + 5.0,
        input_details=lambda value: f"cmd={(value or {}).get('CommandLine')}",
    )
    by_id = {s.request_id: s for s in snaps}
    assert set(by_id) == {"ag-1", "ag-q"}
    assert by_id["ag-1"].tool_name == "run_command"
    assert by_id["ag-1"].kind == "tool"
    assert by_id["ag-1"].age_s >= 5.0
    assert by_id["ag-1"].input_details == "cmd=ls"
    assert by_id["ag-1"].writer_ok is True
    assert by_id["ag-q"].kind == "ask"
    assert by_id["ag-q"].answerable_by_text is True


# ── callbacks ───────────────────────────────────────────────────────────────


def test_synth_keyboard_uses_request_prefix() -> None:
    from untether.orphan_approvals import synth_keyboard

    registry.register("req_1", session_key="sid")
    registry.register("ag-1", session_key="conv", responder=FakeResponder())
    # Claude ids (and unknown ids) keep exactly the pre-#960 keyboard.
    for rid in ("req_1", "da:sid", "unknown"):
        assert synth_keyboard(rid) == [
            [
                {
                    "text": "✅ Approve",
                    "callback_data": f"claude_control:approve:{rid}",
                },
                {"text": "❌ Deny", "callback_data": f"claude_control:deny:{rid}"},
            ]
        ]
    assert [b["callback_data"] for b in synth_keyboard("ag-1")[0]] == [
        "ia:approve:ag-1",
        "ia:deny:ag-1",
    ]


def test_control_callbacks_in_sees_every_answer_prefix() -> None:
    from untether.runner_bridge import control_callbacks_in
    from untether.transport import RenderedMessage

    rows = [
        [{"text": "a", "callback_data": "claude_control:approve:r1"}],
        [{"text": "b", "callback_data": "aq:opt:0"}],
        [{"text": "c", "callback_data": "ia:deny:ag-1"}],
        [{"text": "d", "callback_data": "cancel:x"}],
    ]
    message = RenderedMessage(
        text="x", extra={"reply_markup": {"inline_keyboard": rows}}
    )
    assert control_callbacks_in(message) == {
        "claude_control:approve:r1",
        "aq:opt:0",
        "ia:deny:ag-1",
    }


def test_ia_tap_from_non_originator_refused() -> None:
    from untether.telegram.approval_originator import callback_originator_mismatch

    channel = set_run_channel_id(CHAT_A)
    sender = set_run_sender_id(USER_A)
    try:
        registry.register("ag-1", session_key="conv", responder=FakeResponder())
    finally:
        reset_run_sender_id(sender)
        reset_run_channel_id(channel)
    for command in ("ia", "claude_control"):
        assert callback_originator_mismatch(command, "approve:ag-1", CHAT_A, 42) == (
            "ag-1",
            USER_A,
        )
        assert (
            callback_originator_mismatch(command, "approve:ag-1", CHAT_A, USER_A)
            is None
        )


def test_interactive_package_imports_no_runner_or_telegram() -> None:
    """``interactive`` sits below the runners and the transport: importing it
    in a fresh interpreter loads no ``untether.runners`` / ``telegram``."""
    import subprocess
    import sys

    code = (
        "import sys\n"
        "import untether.interactive.registry, untether.interactive.questions\n"
        "import untether.interactive.preview, untether.interactive.unattended\n"
        "bad = sorted(m for m in sys.modules if m.startswith("
        "('untether.runners', 'untether.telegram')))\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == ""


def test_recently_resolved_ask_flow_returns_the_newest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1071: a late tap is matched to the chat's newest resolved question,
    not the oldest one still inside the TTL (live: a tap on B7 logged B4)."""
    clock = [1000.0]
    monkeypatch.setattr(questions.time, "monotonic", lambda: clock[0])
    questions._record_answered_ask_flow("q-old", CHAT_A, outcome="cancelled")
    clock[0] += 5
    questions._record_answered_ask_flow("q-other-chat", CHAT_B)
    clock[0] += 5
    questions._record_answered_ask_flow("q-new", CHAT_A)
    assert questions.recently_resolved_ask_flow(CHAT_A) == ("q-new", "answered")
    assert questions.recently_answered_ask_flow(CHAT_A) == "q-new"
    assert questions.recently_resolved_ask_flow(CHAT_B) == ("q-other-chat", "answered")
    assert questions.recently_resolved_ask_flow() == ("q-new", "answered")
    # Re-recording an older id makes it the newest.
    clock[0] += 5
    questions._record_answered_ask_flow("q-old", CHAT_A, outcome="cancelled")
    assert questions.recently_resolved_ask_flow(CHAT_A) == ("q-old", "cancelled")
    # Past the TTL nothing is returned and the entries are pruned.
    clock[0] += questions.ANSWERED_ASK_FLOW_TTL_S + 1
    assert questions.recently_resolved_ask_flow(CHAT_A) is None
