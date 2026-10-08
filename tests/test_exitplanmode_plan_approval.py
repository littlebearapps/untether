"""#793: only an APPROVED ExitPlanMode plan is re-shown as "📋 Plan (approved)".

The #508 re-emit used to capture the plan body from every ExitPlanMode call,
whatever the user decided, so a denied plan — or the CLI's stale plan input
that repeats a denied plan — reached the final answer labelled "approved".
The body is now recorded per control ``request_id`` and promoted only when
that request is approved: the Telegram Approve button, the ``plan-auto``
rubber stamp, or the post-outline ``_DISCUSS_APPROVED`` auto-approve.

The plan FILE is the source of truth: on plan-file CLIs (2.1.284 dev-bot
transcript) the plan-file Write and ExitPlanMode are issued in one message and
``input.plan`` is read before the Write lands, so it carries the previous plan.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from untether.commands import CommandContext
from untether.model import CompletedEvent, ResumeToken, TurnEvent
from untether.runners import claude as claude_mod
from untether.runners.claude import (
    _ACTIVE_RUNNERS,
    _DISCUSS_APPROVED,
    _HANDLED_REQUESTS,
    _OUTLINE_PENDING,
    _PLAN_EXIT_APPROVED,
    _REQUEST_TO_INPUT,
    _REQUEST_TO_SESSION,
    _REQUEST_TO_TOOL_NAME,
    _SESSION_BG_STATE,
    _SESSION_STDIN,
    ENGINE,
    ClaudeRunner,
    ClaudeStreamState,
    _cleanup_session_registries,
    send_claude_control_response,
    translate_claude_event,
)
from untether.schemas import claude as claude_schema
from untether.telegram.commands.claude_control import (
    _DISCUSS_FEEDBACK_REFS,
    ClaudeControlCommand,
)
from untether.transport import MessageRef

pytestmark = pytest.mark.anyio

SID = "sess-793"
LABEL = "📋 Plan (approved):"
PLAN_C2 = "## Plan C2\n\n1. Create deny-me.txt\n2. Write 'hello' into it\n"
PLAN_C1B = "## Plan C1b\n\n1. Create approve-me.txt\n2. Write 'ok' into it\n"


@pytest.fixture(autouse=True)
def _clear_registries():
    def _wipe() -> None:
        _ACTIVE_RUNNERS.clear()
        _SESSION_STDIN.clear()
        _SESSION_BG_STATE.clear()
        _REQUEST_TO_SESSION.clear()
        _REQUEST_TO_INPUT.clear()
        _REQUEST_TO_TOOL_NAME.clear()
        _HANDLED_REQUESTS.clear()
        _PLAN_EXIT_APPROVED.clear()
        _DISCUSS_APPROVED.clear()
        _OUTLINE_PENDING.clear()
        _DISCUSS_FEEDBACK_REFS.clear()

    _wipe()
    yield
    _wipe()


def _live_session(session_id: str = SID) -> tuple[ClaudeStreamState, AsyncMock]:
    """A registered run: state reachable by session id, like run_impl does."""
    state = ClaudeStreamState()
    state.factory.started(ResumeToken(engine=ENGINE, value=session_id), title="claude")
    runner = ClaudeRunner(claude_cmd="claude")
    stdin = AsyncMock()
    _ACTIVE_RUNNERS[session_id] = (runner, 0.0)
    _SESSION_STDIN[session_id] = stdin
    _SESSION_BG_STATE[session_id] = state
    return state, stdin


def _feed(state: ClaudeStreamState, payload: dict[str, Any]) -> list[Any]:
    session_id = state.factory.resume.value if state.factory.resume else SID
    event = claude_schema.decode_stream_json_line(
        json.dumps({"uuid": "u", "session_id": session_id, **payload}).encode()
    )
    return translate_claude_event(
        event, title="claude", state=state, factory=state.factory
    )


def _exit_plan_mode(state: ClaudeStreamState, request_id: str, plan: str) -> list:
    # The assistant tool_use precedes the control_request, as on the wire.
    _feed(
        state,
        {
            "type": "assistant",
            "message": {
                "id": f"msg_{request_id}",
                "role": "assistant",
                "model": "claude",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"tu_{request_id}",
                        "name": "ExitPlanMode",
                        "input": {"plan": plan},
                    }
                ],
            },
        },
    )
    return _feed(
        state,
        {
            "type": "control_request",
            "request_id": request_id,
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "ExitPlanMode",
                "input": {"plan": plan},
            },
        },
    )


def _final(state: ClaudeStreamState, answer: str = "Done.") -> str:
    events = _feed(
        state,
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 100,
            "duration_api_ms": 50,
            "num_turns": 2,
            "result": answer,
        },
    )
    completed = [e for e in events if isinstance(e, CompletedEvent)]
    assert len(completed) == 1
    return completed[0].answer


async def _tap(action: str, request_id: str) -> None:
    executor = AsyncMock()
    executor.send = AsyncMock(return_value=MessageRef(channel_id=1, message_id=99))
    ctx = CommandContext(
        command="claude_control",
        text=f"claude_control:{action}:{request_id}",
        args_text=f"{action}:{request_id}",
        args=(f"{action}:{request_id}",),
        message=MessageRef(channel_id=1, message_id=1),
        reply_to=None,
        reply_text=None,
        config_path=None,
        plugin_config=None,  # type: ignore[arg-type]
        runtime=None,  # type: ignore[arg-type]
        executor=executor,
    )
    await ClaudeControlCommand().handle(ctx)


# ── Telegram Approve / Deny ────────────────────────────────────────────────


async def test_tool_use_alone_does_not_mark_a_plan_approved() -> None:
    state, _ = _live_session()
    _exit_plan_mode(state, "req-1", PLAN_C2)
    assert state.last_exitplanmode_plan is None
    assert LABEL not in _final(state)


async def test_denied_plan_is_never_prepended() -> None:
    """#793 C2: Deny → the plan was (correctly) not executed, and the final
    must not present it as approved."""
    state, stdin = _live_session()
    _exit_plan_mode(state, "req-c2", PLAN_C2)
    await _tap("deny", "req-c2")
    sent = json.loads(stdin.send.call_args[0][0].decode())
    assert sent["response"]["response"]["behavior"] == "deny"
    answer = _final(state)
    assert LABEL not in answer
    assert "deny-me.txt" not in answer
    assert state.exitplanmode_plans == {}


async def test_approved_plan_is_prepended() -> None:
    state, _ = _live_session()
    _exit_plan_mode(state, "req-c1b", PLAN_C1B)
    await _tap("approve", "req-c1b")
    answer = _final(state)
    assert answer.startswith(LABEL)
    assert "approve-me.txt" in answer


async def test_deny_then_approve_shows_only_the_approved_plan() -> None:
    state, _ = _live_session()
    _exit_plan_mode(state, "req-c2", PLAN_C2)
    await _tap("deny", "req-c2")
    _exit_plan_mode(state, "req-c1b", PLAN_C1B)
    await _tap("approve", "req-c1b")
    answer = _final(state)
    assert LABEL in answer
    assert "approve-me.txt" in answer
    assert "deny-me.txt" not in answer


async def test_latest_approved_request_wins() -> None:
    state, _ = _live_session()
    _exit_plan_mode(state, "req-a", PLAN_C2)
    await _tap("approve", "req-a")
    _exit_plan_mode(state, "req-b", PLAN_C1B)
    await _tap("approve", "req-b")
    answer = _final(state)
    assert "approve-me.txt" in answer and "deny-me.txt" not in answer


async def test_stale_input_repeating_a_denied_plan_is_not_labelled_approved() -> None:
    """#793, no plan file known (fallback path): the input repeats a plan the
    user denied. Approving that request must not re-show the denied plan as
    approved — the prepend is skipped and the quirk is logged."""
    state, _ = _live_session()
    _exit_plan_mode(state, "req-c2", PLAN_C2)
    await _tap("deny", "req-c2")
    _exit_plan_mode(state, "req-c1b", PLAN_C2)  # stale: byte-identical
    with capture_logs() as logs:
        await _tap("approve", "req-c1b")
    answer = _final(state)
    assert LABEL not in answer
    assert "deny-me.txt" not in answer
    stale = [e for e in logs if e["event"] == "claude.plan.stale_input"]
    assert len(stale) == 1
    assert stale[0]["log_level"] == "info"
    assert stale[0]["request_id"] == "req-c1b"
    assert stale[0]["session_id"] == SID
    assert stale[0]["plan_source"] == "input"
    assert stale[0]["reason"] == "matches_rejected"


async def test_stale_input_also_clears_an_earlier_approved_plan() -> None:
    """The latest approval is what the agent is executing; an earlier approved
    body must not stand in for the stale one."""
    state, _ = _live_session()
    _exit_plan_mode(state, "req-a", PLAN_C1B)
    await _tap("approve", "req-a")
    _exit_plan_mode(state, "req-b", PLAN_C2)
    await _tap("deny", "req-b")
    _exit_plan_mode(state, "req-c", PLAN_C2)
    await _tap("approve", "req-c")
    assert LABEL not in _final(state)


# ── automatic approvals ────────────────────────────────────────────────────


async def test_plan_auto_rubber_stamp_labels_the_plan_approved() -> None:
    state, _ = _live_session()
    state.auto_approve_exit_plan_mode = True
    assert _exit_plan_mode(state, "req-auto", PLAN_C1B) == []
    assert "req-auto" in state.auto_approve_queue
    answer = _final(state)
    assert answer.startswith(LABEL) and "approve-me.txt" in answer


async def test_discuss_approved_auto_approve_labels_the_plan_approved() -> None:
    state, _ = _live_session()
    _DISCUSS_APPROVED.add(SID)
    assert _exit_plan_mode(state, "req-da", PLAN_C1B) == []
    assert "req-da" in state.auto_approve_queue
    answer = _final(state)
    assert answer.startswith(LABEL) and "approve-me.txt" in answer


# ── procedural denials are not rejections ──────────────────────────────────


async def test_pause_and_outline_then_approve_same_plan_is_approved() -> None:
    """ "Pause & Outline Plan" denies the request only to get the outline
    written — it is not a rejection. On plan-file CLIs the next ExitPlanMode
    carries the same plan input; approving it IS an approval of that plan."""
    state, _ = _live_session()
    _exit_plan_mode(state, "req-1", PLAN_C1B * 5)  # >= outline threshold
    await _tap("discuss", "req-1")
    assert SID in _OUTLINE_PENDING
    events = _exit_plan_mode(state, "req-2", PLAN_C1B * 5)
    assert events and events[0].action.detail["request_type"] == "DiscussApproval"
    with capture_logs() as logs:
        await _tap("approve", "req-2")
    assert not [e for e in logs if e["event"] == "claude.plan.stale_input"]
    answer = _final(state)
    assert answer.startswith(LABEL) and "approve-me.txt" in answer


async def test_lets_discuss_then_approve_same_plan_is_approved() -> None:
    state, _ = _live_session()
    _exit_plan_mode(state, "req-1", PLAN_C1B)
    await _tap("chat", "req-1")
    _exit_plan_mode(state, "req-2", PLAN_C1B)
    await _tap("approve", "req-2")
    assert _final(state).startswith(LABEL)


async def test_outline_guard_auto_deny_drops_the_plan() -> None:
    state, _ = _live_session()
    _OUTLINE_PENDING.add(SID)
    _exit_plan_mode(state, "req-g", "short plan")  # < outline threshold
    assert state.auto_deny_queue and state.auto_deny_queue[0][0] == "req-g"
    assert state.exitplanmode_plans == {}
    assert LABEL not in _final(state)


async def test_timed_out_request_drops_the_plan_without_rejecting_it() -> None:
    state, _ = _live_session()
    _exit_plan_mode(state, "req-old", PLAN_C1B)
    event, _ts = state.pending_control_requests["req-old"]
    stale_ts = time.time() - claude_mod.CONTROL_REQUEST_TIMEOUT_SECONDS - 5
    state.pending_control_requests["req-old"] = (event, stale_ts)
    # The next interactive request sweeps the expired one.
    _exit_plan_mode(state, "req-new", PLAN_C1B)
    assert [rid for rid, _ in state.auto_deny_queue] == ["req-old"]
    assert "req-old" not in state.exitplanmode_plans
    assert LABEL not in _final(state)
    # A timeout is not the user rejecting the plan: approving the identical
    # plan afterwards is a real approval.
    await _tap("approve", "req-new")
    assert _final(state).startswith(LABEL)


# ── scoping / cleanup ──────────────────────────────────────────────────────


async def test_approval_in_one_session_never_touches_another() -> None:
    state_a, _ = _live_session("sess-A")
    state_b, _ = _live_session("sess-B")
    _exit_plan_mode(state_a, "req-a", PLAN_C2)
    _exit_plan_mode(state_b, "req-b", PLAN_C1B)
    await _tap("approve", "req-a")
    assert state_a.last_exitplanmode_plan == PLAN_C2
    assert state_b.last_exitplanmode_plan is None
    assert "req-b" in state_b.exitplanmode_plans


async def test_late_approve_after_session_end_promotes_nothing() -> None:
    state, _ = _live_session()
    _exit_plan_mode(state, "req-late", PLAN_C1B)
    _cleanup_session_registries(SID)
    assert SID not in _SESSION_BG_STATE
    assert await send_claude_control_response("req-late", approved=True) is False
    assert state.last_exitplanmode_plan is None


async def test_plan_decision_is_turn_scoped() -> None:
    """A plan approved in one live turn is not re-shown by the next turn."""
    state, _ = _live_session()
    state.live_mode = True
    _exit_plan_mode(state, "req-1", PLAN_C1B)
    await _tap("approve", "req-1")
    assert _final(state).startswith(LABEL)
    events = _feed(state, {"type": "system", "subtype": "init", "model": "claude"})
    assert events  # turn 2 opened
    assert state.last_exitplanmode_plan is None
    events = _feed(
        state,
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 100,
            "duration_api_ms": 50,
            "num_turns": 1,
            "result": "Background check finished.",
        },
    )
    turn_done = [
        e for e in events if isinstance(e, TurnEvent) and e.phase == "completed"
    ]
    assert turn_done and LABEL not in (turn_done[0].answer or "")


# ── the plan file is the source of truth (lagging input.plan) ──────────────


def _plans_dir(tmp_path: Path) -> Path:
    plans = tmp_path / ".claude" / "plans"
    plans.mkdir(parents=True)
    return plans


def _plan_message(
    state: ClaudeStreamState,
    request_id: str,
    *,
    tool: str,
    tool_input: dict[str, Any],
    epm_input: str,
    parent_tool_use_id: str | None = None,
) -> list:
    """One assistant message carrying the plan-file write AND ExitPlanMode
    (as observed on CLI 2.1.284), then the ExitPlanMode control_request whose
    ``input.plan`` is whatever the CLI read — possibly the previous plan."""
    payload: dict[str, Any] = {
        "type": "assistant",
        "message": {
            "id": f"msg_{request_id}",
            "role": "assistant",
            "model": "claude",
            "content": [
                {
                    "type": "tool_use",
                    "id": f"tu_w_{request_id}",
                    "name": tool,
                    "input": tool_input,
                },
                {
                    "type": "tool_use",
                    "id": f"tu_{request_id}",
                    "name": "ExitPlanMode",
                    "input": {"plan": epm_input},
                },
            ],
        },
    }
    if parent_tool_use_id is not None:
        payload["parent_tool_use_id"] = parent_tool_use_id
    _feed(state, payload)
    return _feed(
        state,
        {
            "type": "control_request",
            "request_id": request_id,
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "ExitPlanMode",
                "input": {"plan": epm_input},
            },
        },
    )


PLAN_OLD = "# Plan: create approve-me.txt (rc12-it C1b)\n"
PLAN_A = "# Plan: create deny-me.txt (rc13-793a)\n"
PLAN_B = "# Plan: create approve-me.txt (rc13-793b)\n"


async def test_lagging_input_approved_plan_comes_from_the_plan_file(
    tmp_path: Path,
) -> None:
    """#793 live repro (CLI 2.1.284): write A + EPM(input=old) → Deny;
    write B + EPM(input=A) → Approve. The final must show B, never A."""
    plan_file = str(_plans_dir(tmp_path) / "quiet-bubble.md")
    state, _ = _live_session()
    with capture_logs() as logs:
        _plan_message(
            state,
            "req-a",
            tool="Write",
            tool_input={"file_path": plan_file, "content": PLAN_A},
            epm_input=PLAN_OLD,
        )
        await _tap("deny", "req-a")
        _plan_message(
            state,
            "req-b",
            tool="Write",
            tool_input={"file_path": plan_file, "content": PLAN_B},
            epm_input=PLAN_A,
        )
        await _tap("approve", "req-b")
    answer = _final(state)
    assert answer.startswith(LABEL)
    assert "rc13-793b" in answer
    assert "deny-me.txt" not in answer
    stale = [e for e in logs if e["event"] == "claude.plan.stale_input"]
    assert [(e["request_id"], e["decision"]) for e in stale] == [
        ("req-a", "denied"),
        ("req-b", "approved"),
    ]
    assert all(e["log_level"] == "info" for e in stale)
    assert stale[1]["plan_source"] == "write"
    assert stale[1]["input_chars"] == len(PLAN_A)
    assert stale[1]["file_chars"] == len(PLAN_B)


async def test_plan_auto_with_lagging_input_uses_the_written_plan(
    tmp_path: Path,
) -> None:
    """plan-auto approves at control_request time, possibly before the Write
    reaches disk — the Write's own content is still the right body."""
    plan_file = str(_plans_dir(tmp_path) / "p.md")  # never written to disk
    state, _ = _live_session()
    state.auto_approve_exit_plan_mode = True
    _plan_message(
        state,
        "req-auto",
        tool="Write",
        tool_input={"file_path": plan_file, "content": PLAN_B},
        epm_input=PLAN_A,
    )
    answer = _final(state)
    assert "rc13-793b" in answer and "deny-me.txt" not in answer


async def test_edited_plan_file_is_read_from_disk(tmp_path: Path) -> None:
    plan_path = _plans_dir(tmp_path) / "p.md"
    plan_path.write_text(PLAN_B)
    state, _ = _live_session()
    with capture_logs() as logs:
        _plan_message(
            state,
            "req-e",
            tool="Edit",
            tool_input={
                "file_path": str(plan_path),
                "old_string": "rc13-793a",
                "new_string": "rc13-793b",
            },
            epm_input=PLAN_A,
        )
        await _tap("approve", "req-e")
    assert "rc13-793b" in _final(state)
    stale = [e for e in logs if e["event"] == "claude.plan.stale_input"]
    assert stale and stale[0]["plan_source"] == "file"


async def test_matching_input_logs_nothing(tmp_path: Path) -> None:
    plan_file = str(_plans_dir(tmp_path) / "p.md")
    state, _ = _live_session()
    with capture_logs() as logs:
        _plan_message(
            state,
            "req-ok",
            tool="Write",
            tool_input={"file_path": plan_file, "content": PLAN_B},
            epm_input=PLAN_B,
        )
        await _tap("approve", "req-ok")
    assert "rc13-793b" in _final(state)
    assert not [e for e in logs if e["event"] == "claude.plan.stale_input"]


async def test_unreadable_plan_file_falls_back_to_input(tmp_path: Path) -> None:
    missing = str(_plans_dir(tmp_path) / "gone.md")
    state, _ = _live_session()
    _plan_message(
        state,
        "req-m",
        tool="Edit",
        tool_input={"file_path": missing, "old_string": "x", "new_string": "y"},
        epm_input=PLAN_B,
    )
    await _tap("approve", "req-m")
    assert "rc13-793b" in _final(state)


async def test_non_plan_file_writes_are_not_tracked(tmp_path: Path) -> None:
    state, _ = _live_session()
    _plan_message(
        state,
        "req-n",
        tool="Write",
        tool_input={"file_path": str(tmp_path / "notes.md"), "content": PLAN_A},
        epm_input=PLAN_B,
    )
    assert state.plan_file_path is None
    await _tap("approve", "req-n")
    assert "rc13-793b" in _final(state)


async def test_subagent_plan_file_write_is_ignored(tmp_path: Path) -> None:
    plan_file = str(_plans_dir(tmp_path) / "p.md")
    state, _ = _live_session()
    _plan_message(
        state,
        "req-s",
        tool="Write",
        tool_input={"file_path": plan_file, "content": PLAN_A},
        epm_input=PLAN_B,
        parent_tool_use_id="toolu_agent",
    )
    assert state.plan_file_path is None


def test_plan_file_symlink_escape_and_oversize_are_not_read(tmp_path: Path) -> None:
    plans = _plans_dir(tmp_path)
    secret = tmp_path / "secret.md"
    secret.write_text("secret")
    link = plans / "link.md"
    link.symlink_to(secret)
    assert claude_mod._read_plan_file(str(link)) is None
    big = plans / "big.md"
    big.write_text("x" * (claude_mod._PLAN_FILE_MAX_BYTES + 1))
    assert claude_mod._read_plan_file(str(big)) is None
    ok = plans / "ok.md"
    ok.write_text(PLAN_B)
    assert claude_mod._read_plan_file(str(ok)) == PLAN_B


def test_plan_file_path_accepts_claude_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "cfg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    assert claude_mod._is_plan_file_path(config / "plans" / "p.md")
    assert not claude_mod._is_plan_file_path(tmp_path / "other" / "plans" / "p.md")
    assert not claude_mod._is_plan_file_path(config / "plans" / "p.txt")
