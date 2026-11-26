"""Codex ``/compact`` on the app-server transport (#964, phase 964-1).

Codex's ``/compact`` is a TUI command; over app-server the text would reach
the model. A bare ``/compact`` (or ``/compact@bot``) in a chat with a saved
Codex session now resumes the thread and sends the native
``thread/compact/start`` in place of ``turn/start``; the compact turn's
``contextCompaction`` item gives the 🗜️ row and a completed turn answers
``🗜️ Context compacted``. With no session nothing spawns. ``/compact <text>``
stays an ordinary prompt (D6), and the exec transport is unchanged.

Shapes follow the 964-0 probe P-C (codex-cli 0.171.0): ``{}``, then
``turn/started`` → ``contextCompaction`` item → ``thread/tokenUsage/updated``
→ ``turn/completed``; the resume's own usage update arrives first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from tests import test_codex_app_server_runner as _rt
from tests.test_codex_app_server_runner import (
    THREAD,
    Fake,
    _completed,
    _run,
    _runner,
)
from untether.model import ActionEvent, ResumeToken
from untether.runner import PRESPAWN_BLOCKED_KEY
from untether.runner_bridge import (
    _PLAIN_PRESPAWN_BLOCKS,
    _apply_preamble,
    _compaction_empty_body,
    _compaction_manual_success,
)
from untether.runners import codex_app_server as gate
from untether.runners.codex import CodexRunner

RESUME = ResumeToken(engine="codex", value=THREAD)

# The runner suite's fixtures (fake gate, run base dir, gate cache reset).
project = _rt.project
gate_calls = _rt.gate_calls
_fresh_cache = _rt._fresh_cache


def _usage(turn: str, last: int) -> dict:
    def breakdown(n: int) -> dict:
        return {"totalTokens": n, "inputTokens": n, "outputTokens": 0}

    return {
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": THREAD,
            "turnId": turn,
            "tokenUsage": {
                "total": breakdown(79282),
                "last": breakdown(last),
                "modelContextWindow": 475000,
            },
        },
    }


def _compaction(method: str) -> dict:
    return {
        "method": method,
        "params": {
            "threadId": THREAD,
            "turnId": "turn-c",
            "item": {"type": "contextCompaction", "id": "cc1"},
        },
    }


# The P-C sequence: the resume's usage, then the compact turn.
P_C = {
    "after_thread": [_usage("turn-0", 23667)],
    "compact_notifications": [
        _compaction("item/started"),
        _compaction("item/completed"),
        _usage("turn-c", 16282),
    ],
}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "prompt", ["/compact", "  /compact  ", "/compact@untether_bot"]
)
async def test_compact_sends_thread_compact_start_and_marks_manual_success(
    tmp_path: Path, project: Path, gate_calls: list[str], prompt: str
) -> None:
    fake = Fake(tmp_path, **P_C)
    with capture_logs() as logs:
        events = await _run(_runner(fake), resume=RESUME, prompt=prompt)
    assert fake.requests("thread/compact/start") == [{"threadId": THREAD}]
    assert fake.requests("turn/start") == []
    assert [
        e["thread_id"] for e in logs if e["event"] == "codex.app_server.compact_started"
    ] == [THREAD]
    titles = [
        (e.phase, e.action.title)
        for e in events
        if isinstance(e, ActionEvent) and e.action.id == "cc1"
    ]
    assert [phase for phase, _ in titles] == ["started", "completed"]
    done = _completed(events)
    assert done.ok and done.resume == RESUME
    usage = done.usage or {}
    compaction = usage["compaction"]
    assert compaction == {
        "count": 1,
        "trigger": "manual",
        "manual_success": True,
        "pre_tokens": 23667,
        "post_tokens": 16282,
    }
    assert _compaction_manual_success(usage)
    assert _compaction_empty_body(compaction) == (
        "\N{COMPRESSION}\N{VARIATION SELECTOR-16} Context compacted"
        " · 24k → 16k tokens (manual)"
    )


@pytest.mark.anyio
async def test_compact_without_usage_omits_token_counts(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, compact_notifications=P_C["compact_notifications"][:2])
    done = _completed(await _run(_runner(fake), resume=RESUME, prompt="/compact"))
    assert done.ok
    assert (done.usage or {})["compaction"] == {
        "count": 1,
        "trigger": "manual",
        "manual_success": True,
    }


@pytest.mark.anyio
async def test_compact_without_session_is_refused_before_spawn(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path)
    with capture_logs() as logs:
        events = await _run(_runner(fake), prompt="/compact")
    assert fake.received() == []
    assert gate_calls == []  # not even the support probe
    done = _completed(events)
    assert not done.ok and done.resume is None
    assert (done.usage or {})[PRESPAWN_BLOCKED_KEY] == "compact_no_conversation"
    assert "compact_no_conversation" in _PLAIN_PRESPAWN_BLOCKS
    assert "Codex" in (done.error or "")
    assert "Antigravity" not in (done.error or "")
    assert [e for e in logs if e["event"] == "codex.app_server.compact_no_conversation"]


@pytest.mark.anyio
async def test_compact_with_text_is_a_normal_turn(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path)
    done = _completed(
        await _run(_runner(fake), resume=RESUME, prompt="/compact keep the API notes")
    )
    assert done.ok and done.answer == "done"
    assert fake.requests("thread/compact/start") == []
    [turn] = fake.requests("turn/start")
    assert turn["input"] == [{"type": "text", "text": "/compact keep the API notes"}]
    assert "compaction" not in (done.usage or {})


@pytest.mark.anyio
async def test_compact_continue_resumes_the_listed_thread_then_compacts(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, listed=[THREAD], **P_C)
    cont = ResumeToken(engine="codex", value="", is_continue=True)
    done = _completed(await _run(_runner(fake), resume=cont, prompt="/compact"))
    assert done.ok
    assert fake.requests("thread/compact/start") == [{"threadId": THREAD}]


def test_compact_skips_preamble_on_codex() -> None:
    assert _apply_preamble("/compact", "codex", transport="app-server") == "/compact"
    assert (
        _apply_preamble("/compact@untether_bot", "codex", transport="app-server")
        == "/compact@untether_bot"
    )
    # D6: with text it's an ordinary prompt, so it keeps the preamble.
    assert (
        _apply_preamble("/compact x", "codex", transport="app-server") != "/compact x"
    )
    # Exec transport: /compact stays text for the model, as before.
    assert _apply_preamble("/compact", "codex", transport="exec") != "/compact"
    assert _apply_preamble("/compact", "codex") != "/compact"


def test_exec_transport_compact_unchanged() -> None:
    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    state = runner.new_state("/compact", None)
    assert runner.stdin_payload("/compact", None, state=state) == b"/compact"
    assert "exec" in runner.build_args("/compact", None, state=state)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "scenario",
    [
        {"status": "failed", "turn_error": {"message": "compaction failed"}},
        {"status": "interrupted"},
        {"compact_error": "cannot compact right now"},
    ],
    ids=["failed", "interrupted", "rpc_error"],
)
async def test_compact_failed_turn_is_error(
    tmp_path: Path, project: Path, gate_calls: list[str], scenario: dict[str, Any]
) -> None:
    fake = Fake(tmp_path, **scenario)
    done = _completed(await _run(_runner(fake), resume=RESUME, prompt="/compact"))
    assert not done.ok
    assert not _compaction_manual_success(done.usage)


def test_compact_texts_are_plain() -> None:
    assert gate.NOTHING_TO_COMPACT_TEXT.startswith("Nothing to compact")
    assert "`" not in gate.NOTHING_TO_COMPACT_TEXT
