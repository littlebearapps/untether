"""Antigravity "Ask me" mode decision (#980, phase 14 §1-§2).

``decide_antigravity_mode`` is the single place that grants the bypass
flag; ``build_args`` only reads its decision. These pin the truth table,
the unattended rules (REVIEW M3), the init cross-check under bypass and the
user-level MCP check that replaces "the file exists".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import structlog.testing

from untether.antigravity import gate_precheck
from untether.antigravity.gate_server import GateRun
from untether.events import EventFactory
from untether.runners import antigravity as agy
from untether.runners.antigravity import (
    AntigravityRunner,
    AntigravityStreamState,
    GateFacts,
    decide_antigravity_mode,
)
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.utils import antigravity_scan

BYPASS = agy.BYPASS_FLAG
READY = GateFacts(installed=True, listening=True)


@pytest.fixture(autouse=True)
def _fresh() -> None:
    agy._reset_permission_warnings()
    gate_precheck.clear_cache()


@pytest.mark.parametrize(
    ("mode", "gate", "bypass", "gated", "agent", "block"),
    [
        ("workspace", None, False, False, True, None),
        ("workspace", READY, False, False, True, None),
        ("plan", READY, False, False, True, None),
        ("full", None, True, False, False, None),
        ("ask", READY, True, True, False, None),
        ("ask", None, False, False, False, agy.GATE_MISSING_BLOCK),
        ("ask", GateFacts(), False, False, False, agy.GATE_MISSING_BLOCK),
        (
            "ask",
            GateFacts(installed=True, problem="x", listening=True),
            False,
            False,
            False,
            agy.GATE_NOT_READY_BLOCK,
        ),
        (
            "ask",
            GateFacts(installed=True, self_protect="/u", listening=True),
            False,
            False,
            False,
            agy.GATE_SELF_PROTECT_BLOCK,
        ),
        (
            "ask",
            GateFacts(installed=True, foreign=(".agents/hooks.json",), listening=True),
            False,
            False,
            False,
            agy.GATE_FOREIGN_BLOCK,
        ),
        (
            "ask",
            GateFacts(installed=True, listening=False),
            False,
            False,
            False,
            agy.GATE_NOT_READY_BLOCK,
        ),
        ("yolo", READY, False, False, True, None),
    ],
)
def test_decide_mode_truth_table(
    mode: str,
    gate: GateFacts | None,
    bypass: bool,
    gated: bool,
    agent: bool,
    block: str | None,
) -> None:
    decision = decide_antigravity_mode(mode, gate)
    assert decision.bypass is bypass
    assert decision.gated is gated
    assert decision.no_shell_agent is agent
    assert decision.block == block
    assert (decision.refusal is not None) is (block is not None)
    # A refusal never carries the bypass flag.
    if decision.refusal is not None:
        assert not decision.bypass


def test_bypass_only_for_full_or_ready_attended_ask() -> None:
    """Risk 1 (crown jewel): without a ready-gate decision from run_impl,
    build_args never bypasses for Ask me — it fails closed to Workspace."""
    runner = AntigravityRunner()
    with apply_run_options(EngineRunOptions(permission_mode="ask")):
        state = runner.new_state("p", None)
        assert state.decision is not None and not state.decision.bypass
        args = runner.build_args("p", None, state=state)
    assert BYPASS not in args
    assert "--mode=accept-edits" in args
    # A ready-gate decision without the run's own required gate: still no
    # bypass (security review: the flag never travels without the gate).
    with apply_run_options(EngineRunOptions(permission_mode="ask")):
        state = runner.new_state("p", None)
        state.decision = decide_antigravity_mode("ask", READY)
        assert BYPASS not in runner.build_args("p", None, state=state)
        state.gate = GateRun(config_path=None, factory=EventFactory("antigravity"))
        assert BYPASS not in runner.build_args("p", None, state=state)
    # With run_impl's ready-gate decision and its required gate: bypass,
    # default agent, no --mode.
    with apply_run_options(EngineRunOptions(permission_mode="ask")):
        state = runner.new_state("p", None)
        state.decision = decide_antigravity_mode("ask", READY)
        state.gate = GateRun(
            config_path=None, factory=EventFactory("antigravity"), required=True
        )
        args = runner.build_args("p", None, state=state)
    assert BYPASS in args
    assert not [a for a in args if a.startswith(("--mode", "--agent"))]


def test_build_args_reads_only_the_decision() -> None:
    """A decision for another mode (stale state) is ignored, never trusted."""
    runner = AntigravityRunner()
    with apply_run_options(EngineRunOptions(permission_mode="workspace")):
        state = runner.new_state("p", None)
        state.decision = decide_antigravity_mode("ask", READY)
        args = runner.build_args("p", None, state=state)
    assert BYPASS not in args


def test_trigger_level_ask_opt_in_reads_run_context_not_chat_pref() -> None:
    runner = AntigravityRunner()
    trigger_own = EngineRunOptions(
        permission_mode="ask",
        unattended_trigger="cron:x",
        trigger_permission_mode="ask",
    )
    with apply_run_options(trigger_own):
        assert runner._effective_mode() == "ask"
    inherited = EngineRunOptions(permission_mode="ask", unattended_trigger="cron:x")
    with (
        structlog.testing.capture_logs() as logs,
        apply_run_options(inherited),
    ):
        assert runner._effective_mode() == "workspace"
        assert runner._effective_mode() == "workspace"
    warns = [e for e in logs if e["event"] == "antigravity.unattended_ask_downgraded"]
    assert len(warns) == 1 and warns[0]["origin"] == "chat"


def test_toml_ask_downgraded_for_webhook() -> None:
    runner = AntigravityRunner(default_permission_mode="ask")
    with apply_run_options(EngineRunOptions(unattended_trigger="webhook:gh")):
        assert runner._effective_mode() == "workspace"
    # A trigger whose own mode is full doesn't open Ask me (or vice versa).
    with apply_run_options(
        EngineRunOptions(
            permission_mode="ask",
            unattended_trigger="cron:x",
            trigger_permission_mode="full",
        )
    ):
        assert runner._effective_mode() == "workspace"


def test_unknown_stored_mode_fails_closed_to_workspace() -> None:
    runner = AntigravityRunner()
    with apply_run_options(EngineRunOptions(permission_mode="ASK")):
        assert runner._effective_mode() == "workspace"
        assert BYPASS not in runner.build_args("p", None, state=None)


def _state(mode: str, decision: Any) -> AntigravityStreamState:
    return AntigravityStreamState(
        factory=EventFactory("antigravity"), effective_mode=mode, decision=decision
    )


def test_init_cross_check_ask_bypass() -> None:
    """D22.7 under Ask me's bypass: always-proceed is expected; a stricter
    value warns once (agy's own checks also apply); anything else stops."""
    runner = AntigravityRunner()
    ok = _state("ask", decide_antigravity_mode("ask", READY))
    assert runner._check_reported_mode(ok, "always-proceed", None) is None
    with structlog.testing.capture_logs() as logs:
        stricter = _state("ask", decide_antigravity_mode("ask", READY))
        assert runner._check_reported_mode(stricter, "request-review", None) is None
    assert [e for e in logs if e["event"].endswith("bypass_unreported")]
    odd = _state("ask", decide_antigravity_mode("ask", READY))
    refused = runner._check_reported_mode(odd, "proceed-in-sandbox", None)
    assert refused is not None and odd.permission_refused
    assert "proceed-in-sandbox" in (refused[0].error or "")


def test_init_always_proceed_without_flag_still_refused_for_workspace() -> None:
    """The rc1 kill branch is unchanged: Workspace never runs on agy's
    always-proceed setting."""
    runner = AntigravityRunner()
    state = _state("workspace", decide_antigravity_mode("workspace"))
    refused = runner._check_reported_mode(state, "always-proceed", None)
    assert refused is not None and state.permission_refused


# ── user-level MCP: decided by agy, not by the file's presence ─────────────


@pytest.mark.anyio
async def test_empty_user_mcp_config_not_foreign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lba-1: ``~/.gemini/config/mcp_config.json`` exists with no servers.
    agy's own ``agy mcp list`` says so, so Ask me isn't refused for it."""
    calls: list[int] = []

    async def none(runner: Any) -> str:
        calls.append(1)
        return "No MCP servers configured.\n"

    monkeypatch.setattr(gate_precheck, "_probe_mcp", none)
    scan = antigravity_scan.ScanResult(
        root=tmp_path,
        agy={
            gate_precheck.USER_MCP_KEY: (2, "x"),
            gate_precheck.USER_HOOKS_KEY: (2, "y"),
        },
    )
    result = gate_precheck.GatePrecheck(ok=True)
    runner = AntigravityRunner()
    assert await gate_precheck.foreign_config(runner, result, scan) == ()
    assert await gate_precheck.foreign_config(runner, result, scan) == ()
    assert len(calls) == 1  # cached


@pytest.mark.anyio
@pytest.mark.parametrize("out", ["fs: npx server-filesystem /work\n", None])
async def test_configured_or_unknown_user_mcp_is_foreign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, out: str | None
) -> None:
    async def listing(runner: Any) -> str | None:
        return out

    monkeypatch.setattr(gate_precheck, "_probe_mcp", listing)
    scan = antigravity_scan.ScanResult(
        root=tmp_path,
        agy={gate_precheck.USER_MCP_KEY: (2, "x"), ".agents/x.json": (1, "z")},
    )
    found = await gate_precheck.foreign_config(
        AntigravityRunner(),
        gate_precheck.GatePrecheck(ok=True, foreign_sources=("~/hook",)),
        scan,
    )
    assert found == tuple(
        sorted(("~/hook", gate_precheck.USER_MCP_KEY, ".agents/x.json"))
    )


@pytest.mark.anyio
async def test_foreign_hook_file_listed_once(tmp_path: Path) -> None:
    """#1080 (AG-57): agy's listing names a project hook file by its absolute
    path and the scan by its relative one; the refusal listed it twice."""
    hooks = tmp_path / ".agents" / "hooks.json"
    hooks.parent.mkdir()
    hooks.write_text("{}")
    scan = antigravity_scan.ScanResult(
        root=tmp_path, agy={".agents/hooks.json": (1, "z")}
    )
    found = await gate_precheck.foreign_config(
        AntigravityRunner(),
        gate_precheck.GatePrecheck(ok=True, foreign_sources=(str(hooks),)),
        scan,
    )
    assert found == (".agents/hooks.json",)


def test_build_runner_approval_timeout(tmp_path: Path) -> None:
    """``[antigravity] approval_timeout_s``: whole seconds, 60-3000."""
    from untether.config import ConfigError

    cfg = tmp_path / "untether.toml"
    assert agy.build_runner({}, cfg).approval_timeout_s is None
    assert agy.build_runner({"approval_timeout_s": 120}, cfg).approval_timeout_s == 120
    for bad in (10, 5000, "60", True, 90.5):
        with pytest.raises(ConfigError, match="approval_timeout_s"):
            agy.build_runner({"approval_timeout_s": bad}, cfg)
