"""Codex app-server approval cards (#963): card shapes, taps → Codex's own
decisions, withdrawal, the diff cache, sub-agents, unattended runs and run end.

Request shapes come from the 0.171.0 token probes (``P-A-0.171.0.jsonl``: a
command escalation with ``availableDecisions`` on a stable connection;
``P-B-0.171.0.jsonl``: ``item/started`` fileChange ~5 ms before its request,
``add`` diffs are the content, ``update`` diffs a unified hunk).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from structlog.testing import capture_logs

from tests.test_codex_app_server_runner import THREAD, Fake, _completed, _runner
from untether.interactive.registry import (
    ControlRequestStatus,
    Decision,
    classify_control_request,
    respond,
)
from untether.model import ActionEvent
from untether.runners import codex_app_server as gate
from untether.runners import codex_approvals as approvals
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.schemas import codex_app_server as cas
from untether.utils.paths import reset_run_base_dir, set_run_base_dir

SUB_THREAD = "01a1f1dc-09ac-7ec3-851e-ef00717cb6a2"
ASK = EngineRunOptions(permission_mode="ask-writes")


@pytest.fixture
def project(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    token = set_run_base_dir(proj)
    try:
        yield proj
    finally:
        reset_run_base_dir(token)


@pytest.fixture(autouse=True)
def _supported(monkeypatch: pytest.MonkeyPatch) -> None:
    async def supported(cmd: str, resume: Any) -> None:
        return None

    monkeypatch.setattr(gate, "app_server_unsupported_event", supported)


# --- request shapes (P-A / P-B, 0.171.0) --------------------------------------


def _command_request(rid: int = 0, thread: str = THREAD, **extra: Any) -> dict:
    params = {
        "kind": "command",
        "threadId": thread,
        "turnId": "turn-1",
        "itemId": "exec-1",
        "startedAtMs": 1,
        "environmentId": "local",
        "reason": "May I retry curl outside the sandbox?",
        "command": "/usr/bin/zsh -lc 'curl -sI https://example.com'",
        "cwd": "/tmp/repo",
        "commandActions": [
            {"type": "unknown", "command": "curl -sI https://example.com"}
        ],
        "proposedExecpolicyAmendment": ["curl", "-sI", "https://example.com"],
        "availableDecisions": [
            "accept",
            {
                "acceptWithExecpolicyAmendment": {
                    "execpolicy_amendment": ["curl", "-sI", "https://example.com"]
                }
            },
            "cancel",
        ],
        **extra,
    }
    return {
        "id": rid,
        "method": "item/commandExecution/requestApproval",
        "params": params,
    }


def _file_item(item_id: str, thread: str = THREAD, diff: str = "hello\n") -> dict:
    return {
        "method": "item/started",
        "params": {
            "threadId": thread,
            "turnId": "turn-1",
            "item": {
                "type": "fileChange",
                "id": item_id,
                "changes": [
                    {
                        "path": "/tmp/repo/notes.txt",
                        "kind": {"type": "add"},
                        "diff": diff,
                    }
                ],
                "status": "inProgress",
            },
        },
    }


def _file_request(rid: int, item_id: str, thread: str = THREAD) -> dict:
    return {
        "id": rid,
        "method": "item/fileChange/requestApproval",
        "params": {
            "threadId": thread,
            "turnId": "turn-1",
            "itemId": item_id,
            "startedAtMs": 1,
            "reason": None,
            "grantRoot": None,
        },
    }


def _permissions_request(rid: int) -> dict:
    return {
        "id": rid,
        "method": "item/permissions/requestApproval",
        "params": {
            "threadId": THREAD,
            "turnId": "turn-1",
            "itemId": "p1",
            "cwd": "/tmp/repo",
            "startedAtMs": 1,
            "reason": "needs the network",
            "permissions": {"network": {"enabled": True}},
        },
    }


# --- driving a run --------------------------------------------------------------


def _cards(events: list[Any]) -> list[ActionEvent]:
    return [
        e
        for e in events
        if isinstance(e, ActionEvent)
        and e.phase == "started"
        and isinstance(e.action.detail.get("inline_keyboard"), dict)
    ]


def _closed(events: list[Any]) -> list[ActionEvent]:
    return [
        e
        for e in events
        if isinstance(e, ActionEvent)
        and e.phase == "completed"
        and e.action.id.startswith("codex.approval.")
    ]


def _callbacks(card: ActionEvent) -> list[str]:
    return [
        b["callback_data"].split(":")[1]
        for row in card.action.detail["inline_keyboard"]["buttons"]
        for b in row
    ]


async def _drive(
    fake: Fake,
    taps: list[str | None],
    options: EngineRunOptions | None = ASK,
) -> list[Any]:
    """Run the fake; tap each card in turn (None = leave it)."""
    events: list[Any] = []
    pending = list(taps)
    with apply_run_options(options), anyio.fail_after(20):
        async for evt in _runner(fake).run("hello", None):
            events.append(evt)
            if evt in _cards([evt]) and pending:
                action = pending.pop(0)
                if action is not None:
                    rid = evt.action.detail["request_id"]
                    sent = await respond(rid, Decision(action != "deny", action))
                    assert sent.sent, sent
    return events


def _replies(fake: Fake, rid: int) -> list[dict]:
    return [m for m in fake.received() if m.get("id") == rid and "method" not in m]


# --- tap → Codex's decision ------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("action", "wire"),
    [
        ("approve", "accept"),
        ("deny", "decline"),  # D2, P-A: accepted although not listed
        ("stop", "cancel"),
        (
            "always",
            {
                "acceptWithExecpolicyAmendment": {
                    "execpolicy_amendment": ["curl", "-sI", "https://example.com"]
                }
            },
        ),
    ],
)
async def test_tap_sends_decision(
    tmp_path: Path, project: Path, action: str, wire: Any
) -> None:
    fake = Fake(tmp_path, approvals=[{"request": _command_request(5)}])
    with capture_logs() as logs:
        events = await _drive(fake, [action])
    assert _replies(fake, 5) == [{"id": 5, "result": {"decision": wire}}]
    (card,) = _cards(events)
    (closed,) = _closed(events)
    assert closed.action.id == card.action.id
    assert closed.action.title.startswith(approvals.ANSWER_TITLES[action])
    assert "curl -sI https://example.com" in closed.action.title
    assert _completed(events).ok
    names = [e["event"] for e in logs]
    assert "codex.app_server.approval" in names
    answered = [e for e in logs if e["event"] == "codex.app_server.approval.answered"]
    assert answered[0]["action"] == action


@pytest.mark.anyio
async def test_file_change_session_and_diff_from_the_cached_item(
    tmp_path: Path, project: Path
) -> None:
    fake = Fake(
        tmp_path,
        approvals=[{"before": [_file_item("fc1")], "request": _file_request(3, "fc1")}],
    )
    events = await _drive(fake, ["session"])
    assert _replies(fake, 3) == [{"id": 3, "result": {"decision": "acceptForSession"}}]
    (card,) = _cards(events)
    assert card.action.title.startswith("Permission Request [codex] - Edit files")
    assert "+ hello" in card.action.title
    assert _callbacks(card) == ["approve", "deny", "session", "stop"]
    assert _closed(events)[0].action.title.startswith("🔁 Allowed for session")


@pytest.mark.anyio
async def test_permissions_grant_scope(tmp_path: Path, project: Path) -> None:
    fake = Fake(
        tmp_path,
        approvals=[
            {"request": _permissions_request(1)},
            {"request": _permissions_request(2)},
            {"request": _permissions_request(3)},
        ],
    )
    await _drive(fake, ["approve", "session", "deny"])
    granted = {"network": {"enabled": True}}
    assert _replies(fake, 1)[0]["result"] == {"permissions": granted, "scope": "turn"}
    assert _replies(fake, 2)[0]["result"] == {
        "permissions": granted,
        "scope": "session",
    }
    assert _replies(fake, 3)[0]["result"] == {"permissions": {}, "scope": "turn"}


@pytest.mark.anyio
async def test_reply_only_after_the_tap(tmp_path: Path, project: Path) -> None:
    fake = Fake(tmp_path, approvals=[{"request": _command_request(9)}])
    seen_before_tap: list[int] = []
    with apply_run_options(ASK), anyio.fail_after(20):
        async for evt in _runner(fake).run("hello", None):
            if evt in _cards([evt]):
                await anyio.sleep(0.2)  # the stub is waiting; nothing written
                seen_before_tap.append(len(_replies(fake, 9)))
                await respond(
                    evt.action.detail["request_id"], Decision(True, "approve")
                )
    assert seen_before_tap == [0]
    assert len(_replies(fake, 9)) == 1


# --- withdrawal, run end, process death -----------------------------------------


@pytest.mark.anyio
async def test_resolved_unanswered_withdraws_the_card(
    tmp_path: Path, project: Path
) -> None:
    fake = Fake(
        tmp_path, approvals=[{"request": _command_request(4), "withdraw": True}]
    )
    events = await _drive(fake, [None])
    (card,) = _cards(events)
    rid = card.action.detail["request_id"]
    (closed,) = _closed(events)
    assert closed.action.title.startswith("⏹️ Withdrawn by Codex")
    assert not closed.ok
    assert classify_control_request(rid).status is not ControlRequestStatus.PENDING
    sent = await respond(rid, Decision(True, "approve"))
    assert not sent.sent
    assert _replies(fake, 4) == []


@pytest.mark.anyio
async def test_answered_card_is_not_withdrawn(tmp_path: Path, project: Path) -> None:
    fake = Fake(tmp_path, approvals=[{"request": _command_request(6)}])
    with capture_logs() as logs:
        await _drive(fake, ["approve"])
    assert not any(e["event"] == "codex.app_server.approval.withdrawn" for e in logs)


@pytest.mark.anyio
async def test_process_death_ends_the_session_and_a_late_tap_writes_nothing(
    tmp_path: Path, project: Path
) -> None:
    fake = Fake(tmp_path, approvals=[{"request": _command_request(2), "exit": True}])
    events = await _drive(fake, [None])
    (card,) = _cards(events)
    rid = card.action.detail["request_id"]
    assert not _completed(events).ok
    assert approvals._RUNS == {}
    assert classify_control_request(rid).status is ControlRequestStatus.NOT_FOUND
    sent = await respond(rid, Decision(True, "approve"))
    assert not sent.sent


# --- sub-agents and the diff cache ------------------------------------------------


@pytest.mark.anyio
async def test_subagent_request_card_and_child_item_cached_without_a_row(
    tmp_path: Path, project: Path
) -> None:
    fake = Fake(
        tmp_path,
        approvals=[
            {
                "before": [_file_item("child-fc", thread=SUB_THREAD)],
                "request": _file_request(8, "child-fc", thread=SUB_THREAD),
            }
        ],
    )
    events = await _drive(fake, ["approve"])
    (card,) = _cards(events)
    assert card.action.title.startswith(
        "Permission Request [codex] - Edit files (sub-agent)"
    )
    assert "+ hello" in card.action.title
    assert _replies(fake, 8) == [{"id": 8, "result": {"decision": "accept"}}]
    # The child's item/started gives no row of its own.
    assert not [
        e for e in events if isinstance(e, ActionEvent) and e.action.id == "child-fc"
    ]


def _state() -> Any:
    runner = gate.CodexAppServerRunner(codex_cmd="codex", extra_args=[])
    state = runner.new_state("p", None)
    state.thread_id = THREAD
    return runner, state


def test_patch_updated_replaces_the_cached_diff_and_completion_drops_it() -> None:
    runner, state = _state()
    started = cas.decode_notification("item/started", _file_item("x")["params"])
    runner._cache_file_change(state, "item/started", started)
    assert state.file_changes["x"][0]["diff"] == "hello\n"
    patch = cas.decode_notification(
        "item/fileChange/patchUpdated",
        {
            "threadId": THREAD,
            "turnId": "turn-1",
            "itemId": "x",
            "changes": [
                {
                    "path": "/tmp/repo/notes.txt",
                    "kind": {"type": "update", "move_path": None},
                    "diff": "@@ -1 +1,2 @@\n hello\n+world\n",
                }
            ],
        },
    )
    runner._cache_file_change(state, "item/fileChange/patchUpdated", patch)
    assert "+world" in state.file_changes["x"][0]["diff"]
    done = cas.decode_notification("item/completed", _file_item("x")["params"])
    runner._cache_file_change(state, "item/completed", done)
    assert "x" not in state.file_changes


# --- card shapes (pure) -----------------------------------------------------------


def _card(method: str, params: dict, **kw: Any) -> approvals.Card:
    decoded = cas.decode_request(method, params)
    assert decoded is not None
    return approvals.build_card(
        "cx_test",
        method,
        decoded,
        subagent=kw.pop("subagent", False),
        file_changes=kw.pop("file_changes", None),
        diff_preview=kw.pop("diff_preview", None),
    )


def _actions(card: approvals.Card) -> list[str]:
    return [b["callback_data"].split(":")[1] for row in card.buttons for b in row]


def test_plain_command_without_available_decisions_uses_codex_default_set() -> None:
    req = _command_request()["params"]
    del req["availableDecisions"], req["proposedExecpolicyAmendment"]
    card = _card(approvals.COMMAND_APPROVAL, req)
    assert _actions(card) == ["approve", "deny", "stop"]
    assert card.decisions == {
        "approve": {"decision": "accept"},
        "deny": {"decision": "decline"},
        "stop": {"decision": "cancel"},
    }
    assert card.title.startswith("Permission Request [codex] - Run command")
    assert "$ `curl -sI https://example.com`" in card.title
    assert "zsh -lc" not in card.title  # commandActions, not the wrapper


def test_command_with_amendment_fallback_offers_always() -> None:
    req = _command_request()["params"]
    del req["availableDecisions"]
    card = _card(approvals.COMMAND_APPROVAL, req)
    assert _actions(card) == ["approve", "deny", "always", "stop"]
    always = card.buttons[1][0]["text"]
    assert "curl -sI https://example.com" in always and "on this host" in always


def test_network_prompt_card() -> None:
    req = _command_request()["params"]
    del req["availableDecisions"], req["proposedExecpolicyAmendment"]
    req["networkApprovalContext"] = {"host": "example.com", "protocol": "https"}
    req["proposedNetworkPolicyAmendments"] = [
        {"host": "example.com", "action": "allow"}
    ]
    card = _card(approvals.COMMAND_APPROVAL, req)
    assert card.title.startswith(
        "Permission Request [codex] - Network access to `example.com` (https)"
    )
    assert "curl" not in card.title.splitlines()[0]
    assert _actions(card) == ["approve", "deny", "session", "always", "stop"]
    assert card.decisions["always"] == {
        "decision": {
            "applyNetworkPolicyAmendment": {
                "network_policy_amendment": {"host": "example.com", "action": "allow"}
            }
        }
    }


def test_write_stdin_title() -> None:
    req = _command_request(kind="writeStdin")["params"]
    card = _card(approvals.COMMAND_APPROVAL, req)
    assert card.title.startswith(
        "Permission Request [codex] - Send input to a running command"
    )


def test_available_decisions_win_and_unknown_values_are_skipped() -> None:
    req = _command_request()["params"]
    req["availableDecisions"] = [
        "acceptForSession",
        {"somethingNew": {}},
        "brandNewDecision",
        "cancel",
    ]
    card = _card(approvals.COMMAND_APPROVAL, req)
    assert set(card.decisions) == {"approve", "session", "deny", "stop"}
    assert "always" not in _actions(card)
    rendered = str(card.buttons)
    assert "somethingNew" not in rendered and "brandNewDecision" not in rendered


def test_multiline_prefix_is_never_offered() -> None:
    req = _command_request()["params"]
    req["availableDecisions"] = [
        "accept",
        {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["a\nb"]}},
    ]
    assert "always" not in _card(approvals.COMMAND_APPROVAL, req).decisions


def test_file_change_card_diff_preview_off_hides_the_diff() -> None:
    changes = _file_item("f")["params"]["item"]["changes"]
    params = _file_request(1, "f")["params"]
    on = _card(approvals.FILE_APPROVAL, params, file_changes=changes)
    off = _card(
        approvals.FILE_APPROVAL, params, file_changes=changes, diff_preview=False
    )
    assert "+ hello" in on.title
    assert "+ hello" not in off.title and "notes.txt" in off.title


def test_file_change_update_delete_and_move() -> None:
    changes = [
        {
            "path": "/tmp/repo/a.txt",
            "kind": {"type": "update", "move_path": None},
            "diff": "@@ -1 +1,2 @@\n hello\n+world\n",
        },
        {"path": "/tmp/repo/old.txt", "kind": {"type": "delete"}, "diff": ""},
        {
            "path": "/tmp/repo/b.txt",
            "kind": {"type": "update", "move_path": "/tmp/repo/c.txt"},
            "diff": "",
        },
    ]
    card = _card(
        approvals.FILE_APPROVAL, _file_request(1, "f")["params"], file_changes=changes
    )
    assert "+world" in card.title and "@@" not in card.title
    assert "🗑 /tmp/repo/old.txt" in card.title
    assert "→" in card.title


def test_permissions_card() -> None:
    card = _card(approvals.PERMISSIONS_APPROVAL, _permissions_request(1)["params"])
    assert card.title.startswith("Permission Request [codex] - Extra permissions")
    assert "Network: on" in card.title
    assert _actions(card) == ["approve", "deny", "session"]


def test_card_text_is_redacted() -> None:
    req = _command_request()["params"]
    req["commandActions"] = [
        {"type": "unknown", "command": "curl https://x.test/?token=SECRET123"}
    ]
    req["reason"] = "fetch https://x.test/?key=SECRET456"
    card = _card(approvals.COMMAND_APPROVAL, req)
    assert "SECRET123" not in card.title and "SECRET456" not in card.title


def test_subagent_suffix() -> None:
    card = _card(
        approvals.COMMAND_APPROVAL, _command_request()["params"], subagent=True
    )
    assert card.title.splitlines()[0].endswith("(sub-agent)")


def test_callback_data_fits_telegram() -> None:
    rid = approvals.new_request_id()
    assert len(f"ia:session:{rid}".encode()) <= 64


# --- unattended runs ----------------------------------------------------------------


@pytest.mark.anyio
async def test_unattended_request_is_declined_with_one_lock_row(
    tmp_path: Path, project: Path
) -> None:
    fake = Fake(
        tmp_path,
        notifications=[_command_request(1), _command_request(2)],
    )
    options = EngineRunOptions(
        permission_mode="ask-writes", unattended_trigger="cron:nightly"
    )
    with capture_logs() as logs:
        events = await _drive(fake, [], options=options)
    assert not _cards(events)
    assert _replies(fake, 1) == [{"id": 1, "result": {"decision": "decline"}}]
    assert _replies(fake, 2) == [{"id": 2, "result": {"decision": "decline"}}]
    rows = [e for e in events if isinstance(e, ActionEvent)]
    assert [r.action.id for r in rows] == ["codex.unattended_deny"]
    denials = [e for e in logs if e["event"] == "permission.unattended_deny"]
    assert [d["trigger_source"] for d in denials] == ["cron:nightly"] * 2
    usage = _completed(events).usage or {}
    assert usage["unattended"] == {
        "trigger": "cron:nightly",
        "mode": "ask-writes",
        "denied": {"command": 2},
    }


# --- the ia: command backend ---------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("action", "approved", "toast"),
    [("session", True, "Allowed for session"), ("stop", False, "Stopped")],
)
async def test_ia_session_and_stop_reach_the_responder(
    action: str, approved: bool, toast: str
) -> None:
    from untether.interactive.registry import drop_session, register
    from untether.telegram.commands.claude_control import IA_BACKEND

    seen: list[Decision] = []

    class Responder:
        engine = "codex"
        callback_prefix = "ia"

        def is_alive(self, session_key: str) -> bool:
            return True

        async def respond(self, request_id: str, key: str, decision: Decision) -> bool:
            seen.append(decision)
            return True

    rid = approvals.new_request_id()
    register(rid, session_key="codex:t-ia", responder=Responder(), tool_name="command")
    try:
        assert IA_BACKEND.early_answer_toast(f"{action}:{rid}") == toast
        ctx = MagicMock()
        ctx.args_text = f"{action}:{rid}"
        ctx.message.channel_id = None
        ctx.callback_query_id = None
        ctx.executor = MagicMock(send=AsyncMock(), edit=AsyncMock())
        result = await IA_BACKEND.handle(ctx)
    finally:
        drop_session("codex:t-ia")
    assert seen == [Decision(approved=approved, action=action)]
    assert result is not None and "Codex" in result.text


@pytest.mark.anyio
async def test_stop_tap_ends_with_the_feedback_hint(
    tmp_path: Path, project: Path
) -> None:
    # `cancel` interrupts the turn (schema: "the turn will also be immediately
    # interrupted"); the final says how to go on, not "Codex turn interrupted".
    fake = Fake(
        tmp_path, approvals=[{"request": _command_request(7)}], status="interrupted"
    )
    events = await _drive(fake, ["stop"])
    done = _completed(events)
    assert not done.ok
    assert done.error == gate._STOPPED_BY_CARD
