"""Ask me's approval card through the real gate socket (#980, phase 14 §4-§6).

The hook's side of the protocol is spoken by ``Harness`` (the gate server
tests' helper); answers go through the engine-neutral registry exactly as a
Telegram tap does (``respond`` / the ``ia:`` command backend).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
import structlog.testing

from tests.antigravity_gate_support import short_dir
from tests.test_antigravity_gate_server import CHAT, CID, Harness, _wait_for
from untether.antigravity import gate_policy, gate_server
from untether.antigravity.gate_server import GateRun
from untether.events import EventFactory
from untether.interactive import (
    Decision,
    classify_control_request,
    pending_control_requests_for_session,
    respond,
)
from untether.interactive.registry import (
    _REQUEST_TO_ORIGINATOR,
    ControlRequestStatus,
)
from untether.model import ActionEvent
from untether.runners import antigravity as agy
from untether.utils.paths import (
    reset_run_channel_id,
    reset_run_sender_id,
    set_run_channel_id,
    set_run_sender_id,
)

CHILD = "child-0001-conversation"


@pytest.fixture(autouse=True)
def _runtime_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_dir()))
    gate_server._reset_for_tests()
    gate_policy._reset_for_tests()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "README.md").write_text("hello\n")
    return proj


async def _harness(project: Path, **kwargs: Any) -> AsyncIterator[Harness]:
    gate = GateRun(
        config_path=Path("/x/untether.toml"),
        factory=EventFactory("antigravity"),
        wait_s=kwargs.pop("wait_s", 60),
        required=True,
        project_root=project,
        channel_id=CHAT,
        **kwargs,
    )
    await gate.open()
    gate.set_conversation(CID)
    h = Harness(gate)
    send, receive = anyio.create_memory_object_stream[Any](32)
    chat = set_run_channel_id(CHAT)
    sender = set_run_sender_id(77)
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(gate.serve, send)

            async def collect() -> None:
                async with receive:
                    async for evt in receive:
                        h.events.append(evt)

            tg.start_soon(collect)
            yield h
            await gate.aclose()
            tg.cancel_scope.cancel()
    finally:
        reset_run_sender_id(sender)
        reset_run_channel_id(chat)


@pytest.fixture
async def harness(project: Path) -> AsyncIterator[Harness]:
    async for h in _harness(project):
        yield h


def _call(tool: str, args: dict[str, Any], step: int = 2, cid: str = CID) -> dict:
    return {
        "conversationId": cid,
        "stepIdx": step,
        "toolCall": {"name": tool, "args": args},
    }


def _cards(h: Harness) -> list[ActionEvent]:
    return [
        e
        for e in h.events
        if isinstance(e, ActionEvent)
        and e.phase == "started"
        and e.action.detail.get("request_type") == "PreToolUse"
    ]


def _done(h: Harness) -> list[ActionEvent]:
    return [
        e
        for e in h.events
        if isinstance(e, ActionEvent)
        and e.phase == "completed"
        and e.action.id.startswith("antigravity.approval.")
    ]


async def _held_call(
    h: Harness, payload: dict[str, Any], decision: Decision | None
) -> dict[str, Any] | None:
    """Call the hook, wait for the card, answer it (or not)."""
    out: dict[str, Any] = {}

    async def call() -> None:
        out["v"] = await h.call("PreToolUse", payload)

    async with anyio.create_task_group() as tg:
        before = len(_cards(h))
        tg.start_soon(call)
        await _wait_for(lambda: len(_cards(h)) > before)
        if decision is not None:
            rid = _cards(h)[-1].action.detail["request_id"]
            await respond(rid, decision)
    return out["v"]


SHELL = {"CommandLine": "git status", "Cwd": "", "toolSummary": "Check status"}


@pytest.mark.anyio
async def test_approve_returns_allow(harness: Harness) -> None:
    with structlog.testing.capture_logs() as logs:
        out = await _held_call(
            harness, _call("run_command", SHELL), Decision(True, "approve")
        )
    assert out == {"decision": "allow"}
    card = _cards(harness)[0]
    assert card.action.kind == "warning"
    assert card.action.title.startswith(
        "Permission Request [agy] - shell command\n$ `git status`"
    )
    assert card.action.detail["tool_name"] == "run_command"
    rows = card.action.detail["inline_keyboard"]["buttons"]
    assert [b["text"] for row in rows for b in row] == [
        "✅ Approve",
        "❌ Deny",
        gate_server.ALWAYS_LABEL,
    ]
    assert all(b["callback_data"].startswith("ia:") for row in rows for b in row)
    assert _done(harness)[0].action.title == "✅ Approved: shell command"
    decision = next(log for log in logs if log["event"] == "antigravity.gate.decision")
    assert (decision["decision"], decision["tool"], decision["source"]) == (
        "allow",
        "run_command",
        "user",
    )
    assert pending_control_requests_for_session(harness.gate.session_key) == 0


@pytest.mark.anyio
async def test_deny_returns_reason_and_no_retry_text(harness: Harness) -> None:
    out = await _held_call(
        harness,
        _call("run_command", SHELL),
        Decision(False, "deny", deny_message="Claude's text, ignored"),
    )
    assert out == {"decision": "deny", "reason": gate_server.DENY_TEXT}
    assert "Don't retry the same call" in gate_server.DENY_TEXT
    assert _done(harness)[0].action.title == "❌ Denied: shell command"
    assert _done(harness)[0].ok is False


@pytest.mark.anyio
async def test_timeout_denies_and_strips_keyboard(project: Path) -> None:
    async for h in _harness(project, wait_s=1):
        with structlog.testing.capture_logs() as logs:
            out = await _held_call(h, _call("run_command", SHELL), None)
        assert out is not None and out["decision"] == "deny"
        assert out["reason"] == gate_server.timeout_text(1)
        done = _done(h)[0]
        assert done.action.title.startswith("⏱️ Timed out: denied after 1 s")
        rid = _cards(h)[0].action.detail["request_id"]
        lookup = classify_control_request(rid)
        assert lookup.status is ControlRequestStatus.ALREADY_HANDLED
        assert lookup.prior is not None and lookup.prior.outcome == "expired"
        assert [
            log["source"] for log in logs if log["event"] == "antigravity.gate.decision"
        ] == ["timeout"]


def test_timeout_text_minutes() -> None:
    assert "15 min" in gate_server.timeout_text(900)
    assert "90 s" in gate_server.timeout_text(90)


@pytest.mark.anyio
async def test_reminder_snapshot_lists_the_held_card(harness: Harness) -> None:
    """The bridge's reminder / stall / unanswerable monitors read the
    engine state's ``control_request_snapshot`` (#684, #919)."""
    state = agy.AntigravityStreamState(
        factory=EventFactory("antigravity"), gate=harness.gate
    )

    async def call() -> None:
        await harness.call("PreToolUse", _call("run_command", SHELL))

    async with anyio.create_task_group() as tg:
        tg.start_soon(call)
        await _wait_for(lambda: bool(_cards(harness)))
        assert state.awaiting_user_approval()
        (snap,) = state.control_request_snapshot()
        assert snap.kind == "tool" and snap.tool_name == "run_command"
        await respond(
            _cards(harness)[0].action.detail["request_id"], Decision(True, "approve")
        )
    assert not state.awaiting_user_approval()


@pytest.mark.anyio
async def test_originator_recorded_for_approval_cards(harness: Harness) -> None:
    """#388: the card is bound to the run's chat and originator, so
    ``approval_originator_only`` refuses another user's tap."""

    async def call() -> None:
        await harness.call("PreToolUse", _call("run_command", SHELL))

    async with anyio.create_task_group() as tg:
        tg.start_soon(call)
        await _wait_for(lambda: bool(_cards(harness)))
        rid = _cards(harness)[0].action.detail["request_id"]
        assert _REQUEST_TO_ORIGINATOR[rid] == 77
        from untether.telegram.approval_originator import callback_originator_mismatch

        assert callback_originator_mismatch("ia", f"always:{rid}", CHAT, 78) == (
            rid,
            77,
        )
        assert callback_originator_mismatch("ia", f"approve:{rid}", CHAT, 77) is None
        await respond(rid, Decision(True, "approve"))


@pytest.mark.anyio
async def test_always_allow_exact_command_and_cwd_only(harness: Harness) -> None:
    with structlog.testing.capture_logs() as logs:
        out = await _held_call(
            harness, _call("run_command", SHELL), Decision(True, "always")
        )
        assert out == {"decision": "allow"}
        assert _done(harness)[0].action.title == "✅ Always allowed: shell command"
        # The same command and cwd: no card.
        again = await harness.call("PreToolUse", _call("run_command", SHELL, step=4))
        assert again == {"decision": "allow"}
        assert len(_cards(harness)) == 1
        # A different command asks again.
        other = {**SHELL, "CommandLine": "git push"}
        out = await _held_call(
            harness, _call("run_command", other, step=6), Decision(False, "deny")
        )
        assert out is not None and out["decision"] == "deny"
    granted = [log for log in logs if log["event"] == "antigravity.gate.always_granted"]
    assert granted and "git status" not in str(granted)
    assert len(granted[0]["command_sha"]) == 12


@pytest.mark.anyio
async def test_always_allow_never_unattended(harness: Harness) -> None:
    gate_policy.grant(CHAT, CID, SHELL)
    harness.gate.unattended_trigger = "cron:nightly"
    out = await harness.call("PreToolUse", _call("run_command", SHELL))
    assert out is not None and out["decision"] == "deny"
    assert "unattended" in out["reason"]


@pytest.mark.anyio
async def test_unattended_trigger_ask_denies_ask_list_and_counts_footer(
    harness: Harness, project: Path
) -> None:
    harness.gate.unattended_trigger = "webhook:gh"
    with structlog.testing.capture_logs() as logs:
        first = await harness.call("PreToolUse", _call("run_command", SHELL))
        second = await harness.call(
            "PreToolUse", _call("search_web", {"query": "x"}, step=4)
        )
        edit = await harness.call(
            "PreToolUse",
            _call("write_to_file", {"TargetFile": str(project / "a.txt")}, step=6),
        )
    assert first is not None and first["decision"] == "deny"
    assert "unattended webhook run (webhook:gh)" in first["reason"]
    assert second is not None and second["decision"] == "deny"
    assert edit == {"decision": "allow"}  # the safe list still runs
    assert _cards(harness) == []
    notes = [e for e in harness.events if e.action.kind == "note"]
    assert len(notes) == 2  # one 🔒 row (started + completed), once per run
    usage = harness.gate.unattended_usage()
    assert usage is not None
    assert usage["denied"] == {"shell command": 1, "web search": 1}
    assert "run it from the chat" in usage["hint"]
    denials = [log for log in logs if log["event"] == "permission.unattended_deny"]
    assert [d["tool_name"] for d in denials] == ["run_command", "search_web"]


@pytest.mark.anyio
async def test_subagent_tool_card_names_the_child(harness: Harness) -> None:
    """P-H8: a child's tool call reaches the gate with its own id."""
    harness.gate.add_children([CHILD], role="Researcher")
    out = await _held_call(
        harness, _call("run_command", SHELL, cid=CHILD), Decision(True, "approve")
    )
    assert out == {"decision": "allow"}
    assert "from subagent: Researcher" in _cards(harness)[0].action.title
    # The child messaging its parent is internal: no card.
    report = await harness.call(
        "PreToolUse",
        _call("send_message", {"Recipient": CID, "Message": "done"}, step=4, cid=CHILD),
    )
    assert report == {"decision": "allow"}
    assert len(_cards(harness)) == 1


@pytest.mark.anyio
async def test_unknown_conversation_never_on_the_safe_list(harness: Harness) -> None:
    out = await _held_call(
        harness,
        _call("view_file", {"AbsolutePath": "/etc/hosts"}, cid="someone-else"),
        Decision(False, "deny"),
    )
    assert out is not None and out["decision"] == "deny"
    assert "another agy conversation" in _cards(harness)[0].action.title


@pytest.mark.anyio
async def test_rate_limited_cards_deny(harness: Harness) -> None:
    harness.gate._card_times.extend([1e12] * gate_server.MAX_CARDS_PER_MINUTE)
    out = await harness.call("PreToolUse", _call("run_command", SHELL))
    assert out == {"decision": "deny", "reason": gate_server.TOO_MANY_APPROVALS_TEXT}


@pytest.mark.anyio
async def test_hook_gone_withdraws_the_card(harness: Harness) -> None:
    await harness.call("PreToolUse", _call("run_command", SHELL), close_after_send=True)
    await _wait_for(lambda: bool(_done(harness)))
    assert _done(harness)[0].action.title.startswith("⏹️ Withdrawn")
    rid = _cards(harness)[0].action.detail["request_id"]
    assert classify_control_request(rid).status is ControlRequestStatus.CANCELLED


# ── the ``ia:`` command backend's "Always allow" button ─────────────────────


@pytest.mark.anyio
async def test_ia_always_button_answers_and_toasts(harness: Harness) -> None:
    from untether.telegram.commands.claude_control import IA_BACKEND

    assert IA_BACKEND.early_answer_toast("always:missing") == "This request has expired"

    async def call() -> dict[str, Any] | None:
        return await harness.call("PreToolUse", _call("run_command", SHELL))

    out: dict[str, Any] = {}

    async def run() -> None:
        out["v"] = await call()

    async with anyio.create_task_group() as tg:
        tg.start_soon(run)
        await _wait_for(lambda: bool(_cards(harness)))
        rid = _cards(harness)[0].action.detail["request_id"]
        assert IA_BACKEND.early_answer_toast(f"always:{rid}") == "Always allowed"
        ctx = MagicMock()
        ctx.args_text = f"always:{rid}"
        ctx.message.channel_id = CHAT
        ctx.callback_query_id = None
        ctx.executor = MagicMock(send=AsyncMock(), edit=AsyncMock())
        result = await IA_BACKEND.handle(ctx)
        assert result is not None and "runs without asking" in result.text
    assert out["v"] == {"decision": "allow"}
    assert gate_policy.granted(CHAT, CID, SHELL)
    # A second tap on the spent button reads "already answered".
    ctx.args_text = f"always:{rid}"
    again = await IA_BACKEND.handle(ctx)
    assert again is not None and "approved (always" in again.text


def test_gate_block_text_rows() -> None:
    prefix = "tool call denied by pre-tool hook: "
    assert (
        agy._gate_block_text(prefix + gate_server.DENY_TEXT)
        == "blocked: denied in Telegram"
    )
    assert (
        agy._gate_block_text(prefix + gate_server.timeout_text(900))
        == "blocked: no answer in Telegram in time"
    )
    other = prefix + "probe hook deny: no"
    assert agy._gate_block_text(other) == other
    assert agy._gate_block_text("tool failed") == "tool failed"
