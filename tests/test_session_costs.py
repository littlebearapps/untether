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
    token_counts,
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


# --- #419: per-session token ledger ------------------------------------------


def _tok(inp: int, out: int, **extra: int) -> dict[str, int]:
    return {"input_tokens": inp, "output_tokens": out, **extra}


def test_token_ledger_new_session_delta_equals_total() -> None:
    ledger = SessionCostLedger(path=None)
    r = ledger.record_tokens(
        "codex", "t", _tok(100, 10), scope="thread_cumulative", resumed=False
    )
    assert r.source == "new_session"
    assert r.delta == _tok(100, 10)
    assert r.cumulative == _tok(100, 10)
    assert r.runs == 1


def test_token_ledger_resumed_uses_previous_total() -> None:
    ledger = SessionCostLedger(path=None)
    ledger.record_tokens(
        "codex", "t", _tok(100, 10), scope="thread_cumulative", resumed=False
    )
    r = ledger.record_tokens(
        "codex", "t", _tok(250, 30), scope="thread_cumulative", resumed=True
    )
    assert (r.delta["input_tokens"], r.delta["output_tokens"]) == (150, 20)
    assert r.source == "ledger"
    assert r.runs == 2


def test_token_ledger_triangular_overcount_fixed() -> None:
    ledger = SessionCostLedger(path=None)
    deltas = [
        ledger.record_tokens(
            "codex", "t", _tok(total, 1), scope="thread_cumulative", resumed=i > 0
        ).delta["input_tokens"]
        for i, total in enumerate([100, 250, 420])
    ]
    assert sum(deltas) == 420  # raw summing would give 770


def test_token_ledger_first_sight_resumed_is_baseline_unknown() -> None:
    from structlog.testing import capture_logs

    ledger = SessionCostLedger(path=None)
    with capture_logs() as logs:
        r = ledger.record_tokens(
            "codex", "t", _tok(900, 9), scope="thread_cumulative", resumed=True
        )
    assert r.source == "baseline_unknown"
    assert r.delta == _tok(900, 9)
    assert any(e["event"] == "usage.token_baseline_unknown" for e in logs)


def test_token_ledger_zero_total_keeps_baseline() -> None:
    ledger = SessionCostLedger(path=None)
    ledger.record_tokens(
        "codex", "t", _tok(100, 5), scope="thread_cumulative", resumed=False
    )
    zero = ledger.record_tokens(
        "codex",
        "t",
        _tok(0, 0, cached_input_tokens=0, reasoning_output_tokens=0),
        scope="thread_cumulative",
        resumed=True,
    )
    assert zero.source == "zero_total"
    assert not any(zero.delta.values())
    tokens = ledger.session_tokens("codex", "t")
    assert tokens is not None and tokens.runs == 1  # no write
    r = ledger.record_tokens(
        "codex", "t", _tok(130, 6), scope="thread_cumulative", resumed=True
    )
    assert r.delta["input_tokens"] == 30


def test_token_ledger_field_going_backwards_keeps_max_baseline() -> None:
    ledger = SessionCostLedger(path=None)
    ledger.record_tokens(
        "codex", "t", _tok(100, 10), scope="thread_cumulative", resumed=False
    )
    back = ledger.record_tokens(
        "codex", "t", _tok(80, 12), scope="thread_cumulative", resumed=True
    )
    assert (back.delta["input_tokens"], back.delta["output_tokens"]) == (0, 2)
    assert back.cumulative == _tok(100, 12)
    r = ledger.record_tokens(
        "codex", "t", _tok(130, 15), scope="thread_cumulative", resumed=True
    )
    assert (r.delta["input_tokens"], r.delta["output_tokens"]) == (30, 3)


def test_token_ledger_external_spend_lands_in_next_delta() -> None:
    """Tokens spent on the thread outside Untether land in the next delta,
    as ``ledger`` (plan #419 §4.4) — exact in aggregate, not per run."""
    ledger = SessionCostLedger(path=None)
    ledger.record_tokens(
        "codex", "t", _tok(100, 1), scope="thread_cumulative", resumed=False
    )
    r = ledger.record_tokens(
        "codex", "t", _tok(400, 1), scope="thread_cumulative", resumed=True
    )
    assert (r.delta["input_tokens"], r.source) == (300, "ledger")


def test_token_ledger_per_run_scope_accumulates() -> None:
    ledger = SessionCostLedger(path=None)
    ledger.record_tokens("opencode", "s", _tok(5, 3), scope="per_run", resumed=False)
    r = ledger.record_tokens("opencode", "s", _tok(2, 1), scope="per_run", resumed=True)
    assert r.source == "per_run"
    assert r.delta == _tok(2, 1)
    assert r.cumulative == _tok(7, 4)
    assert r.runs == 2


def test_token_ledger_ignores_non_int_and_bool_values() -> None:
    ledger = SessionCostLedger(path=None)
    r = ledger.record_tokens(
        "codex",
        "t",
        {"input_tokens": 10, "output_tokens": True, "cached_input_tokens": "5"},
        scope="thread_cumulative",
        resumed=False,
    )
    assert r.delta == {"input_tokens": 10}


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (
            {"total_cost_usd": 0.1, "usage": {"input_tokens": 5, "output_tokens": 2}},
            {"input_tokens": 5, "output_tokens": 2},
        ),
        (
            {
                "input_tokens": 10,
                "cached_input_tokens": 4,
                "cache_write_input_tokens": 1,
                "output_tokens": 3,
                "reasoning_output_tokens": 2,
            },
            {
                "input_tokens": 10,
                "cached_input_tokens": 4,
                "cache_write_input_tokens": 1,
                "output_tokens": 3,
                "reasoning_output_tokens": 2,
            },
        ),
        ({"total_cost_usd": 1.0}, None),
        (
            {"input_tokens": 7, "output_tokens": "x", "cached_input_tokens": False},
            {"input_tokens": 7},
        ),
        ({"input_tokens": 0, "output_tokens": 0}, None),
        (
            {"input_tokens": -4, "output_tokens": 3},
            {"input_tokens": 0, "output_tokens": 3},
        ),
        (None, None),
    ],
)
def test_token_counts_shapes(usage: Any, expected: Any) -> None:
    assert token_counts(usage) == expected


def test_record_cost_preserves_token_fields_and_vice_versa() -> None:
    ledger = SessionCostLedger(path=None)
    ledger.record_tokens(
        "x", "s", _tok(10, 1), scope="thread_cumulative", resumed=False
    )
    ledger.record("x", "s", 0.5, resumed=False)
    tokens = ledger.session_tokens("x", "s")
    assert tokens is not None and tokens.totals == _tok(10, 1)
    ledger.record_tokens("x", "s", _tok(20, 2), scope="thread_cumulative", resumed=True)
    assert ledger.last("x", "s") == 0.5


def test_last_returns_none_for_token_only_entry() -> None:
    ledger = SessionCostLedger(path=None)
    ledger.record_tokens(
        "codex", "t", _tok(10, 1), scope="thread_cumulative", resumed=False
    )
    assert ledger.last("codex", "t") is None


def test_session_tokens_roundtrip_persists_across_reload(tmp_path: Path) -> None:
    path = tmp_path / "session_costs.json"
    ledger = SessionCostLedger.load(path)
    ledger.record_tokens(
        "codex", "t", _tok(100, 10), scope="thread_cumulative", resumed=False
    )
    ledger.record_tokens(
        "codex", "t", _tok(250, 30), scope="thread_cumulative", resumed=True
    )
    reloaded = SessionCostLedger.load(path)
    tokens = reloaded.session_tokens("codex", "t")
    assert tokens is not None
    assert tokens.totals == _tok(250, 30)
    assert tokens.last_run == _tok(150, 20)
    assert tokens.runs == 2
    assert tokens.last_source == "ledger"
    assert tokens.updated_ts > 0
    assert reloaded.session_tokens("codex", "missing") is None


def test_legacy_rc14_file_loads_and_claude_delta_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "session_costs.json"
    path.write_text(json.dumps({"claude:s": {"cost": 3.23, "ts": time.time()}}))
    ledger = SessionCostLedger.load(path)
    assert ledger.session_tokens("claude", "s") is None
    result = ledger.record("claude", "s", 3.48, resumed=True)
    assert result.delta == pytest.approx(0.25)
    assert result.source == "ledger"
    data = json.loads(path.read_text())
    assert set(data["claude:s"]) == {"cost", "ts"}


def test_apply_token_delta_codex_rewrites_flat_fields() -> None:
    first = {
        "input_tokens": 100,
        "cached_input_tokens": 40,
        "output_tokens": 10,
        "reasoning_output_tokens": 4,
    }
    rb._apply_token_delta("codex", "t", first, resumed=False)
    usage = {
        "input_tokens": 250,
        "cached_input_tokens": 90,
        "output_tokens": 30,
        "reasoning_output_tokens": 9,
    }
    snapshot = dict(usage)
    out = rb._apply_token_delta("codex", "t", usage, resumed=True)
    assert out is not usage
    assert usage == snapshot  # input not mutated
    assert out is not None
    assert (out["input_tokens"], out["cached_input_tokens"]) == (150, 50)
    assert (out["output_tokens"], out["reasoning_output_tokens"]) == (20, 5)
    assert out["thread_total_usage"]["input_tokens"] == 250
    assert out["token_delta_source"] == "ledger"


@pytest.mark.parametrize(
    ("engine", "session_id", "usage"),
    [
        ("claude", "s", {"input_tokens": 5, "output_tokens": 1}),
        ("pi", "s", {"input_tokens": 5, "output_tokens": 1}),
        ("codex", None, {"input_tokens": 5, "output_tokens": 1}),
        ("codex", "", {"input_tokens": 5, "output_tokens": 1}),
        ("codex", "s", {"total_cost_usd": 1.0}),
    ],
)
def test_apply_token_delta_passthrough_cases(
    engine: str, session_id: str | None, usage: dict[str, Any]
) -> None:
    out = rb._apply_token_delta(engine, session_id, usage, resumed=False)
    assert out is usage
    assert "token_delta_source" not in usage


def test_apply_token_delta_failure_is_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from structlog.testing import capture_logs

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(get_session_cost_ledger(), "record_tokens", boom)
    usage = {"input_tokens": 5, "output_tokens": 1}
    with capture_logs() as logs:
        out = rb._apply_token_delta("codex", "t", usage, resumed=False)
    assert out is usage
    assert any(e["event"] == "usage.token_delta_failed" for e in logs)


def test_apply_token_delta_opencode_per_run() -> None:
    """#417: OpenCode joins the ledger as ``per_run`` — per-run figures stay
    as reported, the session total is added alongside."""
    first = {"total_cost_usd": 0.01, "usage": {"input_tokens": 5, "output_tokens": 1}}
    rb._apply_token_delta("opencode", "s", first, resumed=False)
    usage = {"usage": {"input_tokens": 7, "output_tokens": 2}}
    out = rb._apply_token_delta("opencode", "s", usage, resumed=True)
    assert out is not usage
    assert out is not None
    assert out["usage"] == {"input_tokens": 7, "output_tokens": 2}
    assert out["session_total_usage"] == {"input_tokens": 12, "output_tokens": 3}
    assert "token_delta_source" not in out
    assert "input_tokens" not in out
