"""#785 part 2: short wake-turn acks fold into the #777 status message.

End to end with a REAL ``handle_message`` + ``ClaudeRunner`` against
``tests/fake_clis/fake_claude_live.py`` (``multi_agent_acks`` models the nsd
blogs session: two background agents, each finish answered twice by the CLI).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.test_live_session_harness import _drive, _progress, _watchdog
from untether.session_quarantine import QuarantineStore, set_quarantine_store

pytestmark = pytest.mark.anyio

_ENV = ("FAKE_CLAUDE_SCENARIO", "FAKE_CLAUDE_WAKE_S", "FAKE_CLAUDE_ACK_TOOL")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    set_quarantine_store(QuarantineStore(tmp_path / "q.json"))
    _watchdog(monkeypatch)
    yield
    set_quarantine_store(None)
    for key in _ENV:
        os.environ.pop(key, None)


def _status_ref(transport):
    return next(
        c["ref"]
        for c in transport.send_calls
        if c["message"].text.startswith("⏳ background")
    )


def _status_text(transport) -> str:
    ref = _status_ref(transport)
    edits = [c["message"].text for c in transport.edit_calls if c["ref"] == ref]
    return edits[-1]


def _sent(transport, needle: str) -> list[dict]:
    return [c for c in transport.send_calls if needle in c["message"].text]


async def test_ack_turns_fold_and_only_the_report_pushes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _progress(monkeypatch, show_background_tasks=True, consolidate_wake_turns=True)
    transport = await _drive("multi_agent_acks", wake_s=0.6)

    # The first finish's two turns (ack while sweep two runs, then the
    # task's own restatement) never become messages of their own…
    assert _sent(transport, "Sweep one is back") == []
    assert _sent(transport, "a1 finished (again)") == []
    # …they are on sweep one's row of the status message, in full.
    final = _status_text(transport)
    lines = final.splitlines()
    assert lines[0] == "✅ all 2 background tasks done"
    a1 = lines.index(next(ln for ln in lines if ln.startswith("✅ bg a1 ·")))
    assert lines[a1 + 1] == "   ↳ Sweep one is back; waiting on sweep two."
    assert lines[a1 + 2] == "   ↳ a1 finished (again)."

    # The compiled report (long) breaks out as a normal pushed message.
    report = _sent(transport, "REPORT: all findings compiled.")
    assert len(report) == 1
    assert report[0]["options"].notify is True
    assert "Background task finished — bg a2" in report[0]["message"].text
    # Its restatement folds onto sweep two's row — no second push.
    assert _sent(transport, "a2 finished (again)") == []
    assert "   ↳ a2 finished (again)." in lines

    # One user message → the answer, the status message and the report.
    pushed = [c for c in transport.send_calls if c["options"].notify]
    assert len(pushed) == 1


async def test_tool_using_ack_breaks_out(monkeypatch: pytest.MonkeyPatch) -> None:
    _progress(monkeypatch, show_background_tasks=True, consolidate_wake_turns=True)
    os.environ["FAKE_CLAUDE_ACK_TOOL"] = "1"
    transport = await _drive("multi_agent_acks", wake_s=0.6)
    ack = _sent(transport, "Sweep one is back")
    assert len(ack) == 1
    assert ack[0]["options"].notify is True
    # The restatement is still just an ack: it folds.
    assert _sent(transport, "a1 finished (again)") == []
    assert "   ↳ a1 finished (again)." in _status_text(transport).splitlines()


async def test_consolidation_off_keeps_rc12_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _progress(monkeypatch, show_background_tasks=True, consolidate_wake_turns=False)
    transport = await _drive("multi_agent_acks", wake_s=0.6)
    for needle in (
        "Sweep one is back",
        "a1 finished (again)",
        "REPORT: all findings compiled.",
        "a2 finished (again)",
    ):
        assert len(_sent(transport, needle)) == 1, needle
    # The status message still tracks the tasks, with no ack lines.
    final = _status_text(transport)
    assert final.startswith("✅ all 2 background tasks done")
    assert "↳" not in final


async def test_no_status_message_means_nothing_folds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consolidation needs the #777 status message to fold into."""
    _progress(monkeypatch, show_background_tasks=False, consolidate_wake_turns=True)
    transport = await _drive("multi_agent_acks", wake_s=0.6)
    assert len(_sent(transport, "Sweep one is back")) == 1
    assert len(_sent(transport, "a1 finished (again)")) == 1
    assert not any(
        c["message"].text.startswith("⏳ background") for c in transport.send_calls
    )


@pytest.mark.parametrize("task_end", ["mid", "after"])
async def test_single_agent_pair_pushes_once(
    monkeypatch: pytest.MonkeyPatch, task_end: str
) -> None:
    """One background agent, answered twice by the CLI (#785). ``mid``: the
    first turn is retro-attributed and is the last task — it pushes; its
    restatement folds onto the row. ``after``: the first turn raced the
    task's end, so it can't be known as the last one and folds as an ack; the
    restatement then carries the batch's one push, and the earlier ack is
    filed under the task's row."""
    _progress(monkeypatch, show_background_tasks=True, consolidate_wake_turns=True)
    os.environ["FAKE_CLAUDE_TASK_END"] = task_end
    transport = await _drive("agent_wake_unknown_first", wake_s=0.6)
    os.environ.pop("FAKE_CLAUDE_TASK_END", None)
    pushed = [
        c
        for c in transport.send_calls
        if c["options"].notify and "Background task finished" in c["message"].text
    ]
    assert len(pushed) == 1
    assert "bg a1" in pushed[0]["message"].text
    lines = _status_text(transport).splitlines()
    assert lines[0] == "✅ background task done"
    row = lines.index(next(ln for ln in lines if ln.startswith("✅ bg a1 ·")))
    folded = "The sweep has finished" if task_end == "mid" else "The sweep is back"
    assert lines[row + 1] == f"   ↳ {folded}"
    assert not any(ln.startswith("💬") for ln in lines)


async def test_quiet_batch_report_pushes_even_if_already_announced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dev-bot regression (#785): every earlier wake turn folded, and the
    shell task's end was paired with the last folded ``unknown`` ack, so its
    report turn arrived ``already_announced`` (no push) — a long report
    breaking out of a batch that had never pushed must push."""
    _progress(monkeypatch, show_background_tasks=True, consolidate_wake_turns=True)
    transport = await _drive("quiet_batch_report", wake_s=0.6)
    for ack in ("a1 is back.", "a2 is back.", "Still waiting on the shell job."):
        assert _sent(transport, ack) == [], ack
    report = _sent(transport, "REPORT: all three jobs finished cleanly.")
    assert len(report) == 1
    assert report[0]["options"].notify is True
    pushed = [c for c in transport.send_calls if c["options"].notify]
    assert pushed == report
    assert _status_text(transport).startswith("✅ all 3 background tasks done")
