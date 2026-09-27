"""#778: per-session cost ledger — Claude's total_cost_usd is session-cumulative."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import anyio
import pytest

from untether import runner_bridge as rb
from untether.session_costs import (
    SessionCostLedger,
    get_session_cost_ledger,
)


def test_ledger_roundtrip_persists_and_prunes(tmp_path: Path) -> None:
    path = tmp_path / "session_costs.json"
    ledger = SessionCostLedger.load(path)
    ledger.record("claude", "s1", 1.5, resumed=False)
    data = json.loads(path.read_text())
    assert data["claude:s1"]["cost"] == 1.5
    data["claude:old"] = {"cost": 9.0, "ts": time.time() - 90 * 24 * 3600}
    path.write_text(json.dumps(data))
    reloaded = SessionCostLedger.load(path)
    assert reloaded.last("claude", "s1") == 1.5
    assert reloaded.last("claude", "old") is None


def test_corrupt_ledger_file_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "session_costs.json"
    path.write_text("{not json")
    assert SessionCostLedger.load(path).last("claude", "x") is None


def test_delta_for_resumed_session_uses_ledger() -> None:
    ledger = SessionCostLedger(path=None)
    ledger.record("claude", "s", 3.23, resumed=False)
    result = ledger.record("claude", "s", 3.48, resumed=True)
    assert result.delta == pytest.approx(0.25)
    assert result.source == "ledger"


def test_new_session_baseline_zero() -> None:
    result = SessionCostLedger(path=None).record("claude", "s", 0.4, resumed=False)
    assert (result.delta, result.source) == (0.4, "new_session")


def test_first_sight_resumed_session_records_raw_and_flags() -> None:
    ledger = SessionCostLedger(path=None)
    first = ledger.record("claude", "s", 11.08, resumed=True)
    assert (first.delta, first.source) == (11.08, "baseline_unknown")
    second = ledger.record("claude", "s", 11.71, resumed=True)
    assert second.delta == pytest.approx(0.63)


def test_seeded_baseline_from_resume_guard() -> None:
    result = SessionCostLedger(path=None).record(
        "claude", "s", 0.0686, resumed=True, baseline=0.0611
    )
    assert result.delta == pytest.approx(0.0075)
    assert result.source == "baseline"


def test_cumulative_going_backwards_never_negative() -> None:
    ledger = SessionCostLedger(path=None)
    ledger.record("claude", "s", 5.0, resumed=False)
    assert ledger.record("claude", "s", 4.0, resumed=True).delta == 0.0


def test_staging_evidence_sequence_sums_to_real_spend() -> None:
    """The 7ff32810 staging sequence: raw recording summed ~44; the real
    spend is the last cumulative minus the first run's baseline."""
    ledger = SessionCostLedger(path=None)
    totals = [3.2273, 3.4819, 3.7075, 11.0821, 11.7071, 12.7719]
    deltas = [
        ledger.record("claude", "7ff", t, resumed=i > 0).delta
        for i, t in enumerate(totals)
    ]
    assert sum(deltas) == pytest.approx(12.7719)


def test_apply_cost_delta_rewrites_claude_usage_only() -> None:
    usage = {"total_cost_usd": 2.0, "num_turns": 3}
    rb._apply_cost_delta("claude", "s", usage, resumed=False)
    out = rb._apply_cost_delta(
        "claude", "s", {"total_cost_usd": 2.5, "num_turns": 1}, resumed=True
    )
    assert out["total_cost_usd"] == pytest.approx(0.5)
    assert out["session_total_cost_usd"] == 2.5
    assert out["num_turns"] == 1
    codex = {"total_cost_usd": 1.0}
    assert rb._apply_cost_delta("codex", "s", codex, resumed=True) is codex


def test_apply_cost_delta_uses_seeded_baseline() -> None:
    out = rb._apply_cost_delta(
        "claude",
        "fresh-proc",
        {"total_cost_usd": 0.0686, "session_cost_baseline": 0.0611},
        resumed=True,
    )
    assert out["total_cost_usd"] == pytest.approx(0.0075)


# ── end to end through the real bridge ──────────────────────────────────────


@pytest.mark.anyio
async def test_live_followup_turn_budget_sees_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live session with a follow-up turn: budget/footer/outlier receive the
    per-turn delta, and the ledger ends at the session's cumulative total."""
    from tests.test_live_session_harness import _drive, _watchdog
    from untether.runners import claude as claude_mod
    from untether.runners.claude import write_user_message

    _watchdog(monkeypatch)
    seen: list[float] = []
    real_check = rb._check_cost_budget

    def _spy(usage: dict[str, Any] | None):
        if usage is not None:
            seen.append(usage["total_cost_usd"])
        return real_check(usage)

    monkeypatch.setattr(rb, "_check_cost_budget", _spy)
    sid = "fake-live-session"

    async def _inject_once_idle() -> None:
        with anyio.fail_after(20):
            while True:
                live = claude_mod.get_live_session(sid)
                if live is not None and live.idle:
                    break
                await anyio.sleep(0.02)
        assert await write_user_message(sid, "again", command_uuid="cmd-778")

    async with anyio.create_task_group() as tg:
        tg.start_soon(_inject_once_idle)
        await _drive("followup")
    assert seen == [pytest.approx(0.01), pytest.approx(0.01)]
    assert get_session_cost_ledger().last("claude", sid) == pytest.approx(0.02)
