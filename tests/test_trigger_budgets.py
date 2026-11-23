"""#1125: per-trigger run budgets (``max_turns``, ``max_budget_usd``,
``max_elapsed_s``)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import anyio
import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

import untether.runner_bridge as bridge
from tests.test_exec_bridge import FakeTransport
from untether import budget_gate
from untether.context import RunContext, attended_context
from untether.markdown import MarkdownPresenter
from untether.runner_bridge import (
    ExecBridgeConfig,
    IncomingMessage,
    handle_message,
    trigger_elapsed_limit,
    trigger_journal_outcome,
)
from untether.runners.claude import (
    _SESSION_BG_STATE,
    _SESSION_STDIN,
    ClaudeRunner,
    ClaudeStreamState,
    interrupt_pending_turn,
)
from untether.runners.mock import ScriptRunner, Wait
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.telegram.loop import _apply_trigger_overrides
from untether.triggers import journal
from untether.triggers.dispatcher import build_run_context
from untether.triggers.settings import (
    CronConfig,
    WebhookConfig,
    parse_trigger_config_isolated,
)
from untether.utils.antigravity_argv import AgyArgvError, agy_flag


def _cron(**kw: Any) -> CronConfig:
    return CronConfig.model_validate(
        {"id": "watch", "schedule": "0 9 * * *", "prompt": "go", **kw}
    )


# --- config validation -----------------------------------------------------


def test_budgets_accepted_for_claude() -> None:
    cron = _cron(engine="claude", max_turns=3, max_budget_usd=0.05, max_elapsed_s=45)
    assert (cron.max_turns, cron.max_budget_usd, cron.max_elapsed_s) == (3, 0.05, 45)
    # A whole number of dollars is fine too.
    assert _cron(engine="claude", max_budget_usd=1).max_budget_usd == 1.0


@pytest.mark.parametrize("key", ["max_turns", "max_budget_usd"])
@pytest.mark.parametrize("engine", [None, "codex", "opencode", "pi", "antigravity"])
def test_claude_only_budgets_need_engine_claude(key: str, engine: str | None) -> None:
    with pytest.raises(ValidationError, match=f'{key} needs engine = "claude"'):
        _cron(engine=engine, **{key: 1})


@pytest.mark.parametrize("engine", [None, "codex", "opencode", "pi", "antigravity"])
def test_max_elapsed_works_on_every_engine(engine: str | None) -> None:
    assert _cron(engine=engine, max_elapsed_s=30).max_elapsed_s == 30


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("max_turns", 0),
        ("max_turns", "3"),
        ("max_turns", True),
        ("max_budget_usd", 0),
        ("max_budget_usd", -1.0),
        ("max_budget_usd", True),
        ("max_budget_usd", float("inf")),
        ("max_elapsed_s", 29),
        ("max_elapsed_s", 45.5),
    ],
)
def test_bad_budget_values_are_rejected(key: str, value: Any) -> None:
    with pytest.raises(ValidationError, match=key):
        _cron(engine="claude", **{key: value})


def test_non_agent_webhook_rejects_budgets() -> None:
    with pytest.raises(ValidationError, match="max_elapsed_s only applies"):
        WebhookConfig.model_validate(
            {
                "id": "w",
                "path": "/w",
                "auth": "none",
                "action": "notify_only",
                "message_template": "hi",
                "max_elapsed_s": 60,
            }
        )


def test_bad_budget_skips_only_that_entry() -> None:
    parsed = parse_trigger_config_isolated(
        {
            "enabled": True,
            "crons": [
                {"id": "ok", "schedule": "0 9 * * *", "prompt": "x"},
                {"id": "bad", "schedule": "0 9 * * *", "prompt": "x", "max_turns": 2},
            ],
        }
    )
    assert [c.id for c in parsed.settings.crons] == ["ok"]
    (invalid,) = parsed.invalid
    assert invalid.trigger_id == "bad"
    assert 'max_turns needs engine = "claude"' in invalid.error


# --- plumbing ---------------------------------------------------------------


def test_budgets_reach_context_and_run_options_not_human_turns() -> None:
    ctx = build_run_context(
        _cron(engine="claude", max_turns=2, max_budget_usd=0.5, max_elapsed_s=60)
    )
    assert (ctx.max_turns, ctx.max_budget_usd, ctx.max_elapsed_s) == (2, 0.5, 60)
    opts = _apply_trigger_overrides(EngineRunOptions(), ctx, engine="claude", log=False)
    assert opts is not None
    assert (opts.max_turns, opts.max_budget_usd, opts.max_elapsed_s) == (2, 0.5, 60)
    # A reply to that run keeps none of them.
    assert attended_context(ctx) == RunContext()


def test_webhook_budgets_forward_like_crons() -> None:
    webhook = WebhookConfig.model_validate(
        {
            "id": "w",
            "path": "/w",
            "auth": "none",
            "prompt_template": "x",
            "engine": "claude",
            "max_turns": 4,
            "max_elapsed_s": 90,
        }
    )
    ctx = build_run_context(webhook)
    assert (ctx.max_turns, ctx.max_elapsed_s) == (4, 90)


def test_claude_args_carry_native_caps_only_for_that_run() -> None:
    runner = ClaudeRunner(claude_cmd="claude", extra_args=["--max-turns", "99"])
    with apply_run_options(
        EngineRunOptions(
            permission_mode="auto", max_turns=3, max_budget_usd=0.05, max_elapsed_s=60
        )
    ):
        args = runner._build_args("hi", None)
    assert "--max-turns=3" in args
    assert "--max-budget-usd=0.05" in args
    # The trigger's cap comes after an engine-wide one, so it wins.
    assert args.index("--max-turns=3") > args.index("--max-turns")
    # Wall-clock is the bridge's job: no Claude flag for it.
    assert not any("elapsed" in a or "timeout" in a for a in args)
    attended = runner._build_args("hi", None)
    assert not any(
        a.startswith(("--max-turns=", "--max-budget-usd=")) for a in attended
    )


def test_agy_print_timeout_flag() -> None:
    assert agy_flag("--print-timeout", 60) == "--print-timeout=60s"
    for bad in (0, -5, True, "60", 1.5):
        with pytest.raises(AgyArgvError):
            agy_flag("--print-timeout", bad)


# --- run-wide guard (live-session wake turns) --------------------------------


def test_trigger_run_stop_turns_and_cost() -> None:
    opts = EngineRunOptions(max_turns=3, max_budget_usd=0.10)
    assert budget_gate.trigger_run_stop(0.05, 2, opts) is None
    assert budget_gate.trigger_run_stop(0.05, 3, opts) == ("trigger_turns", 3.0, 3.0)
    assert budget_gate.trigger_run_stop(0.10, 1, opts) == (
        "trigger_budget",
        0.10,
        0.10,
    )
    assert budget_gate.trigger_run_stop(9.0, 99, EngineRunOptions()) is None
    # Applies with no [cost_budget] at all.
    assert budget_gate.run_stop(0.0, opts, run_turns=5) == ("trigger_turns", 5.0, 3.0)


def test_trigger_run_stop_text() -> None:
    assert budget_gate.run_stop_text(("trigger_turns", 4.0, 3.0)) == (
        "\N{OCTAGONAL SIGN} Stopped: the run used 4 turns, this trigger's max_turns is 3"
    )
    assert "max_budget_usd $0.10" in budget_gate.run_stop_text(
        ("trigger_budget", 0.12, 0.10)
    )


def test_run_turns_accumulate_across_results() -> None:
    state: dict[str, Any] = {"run_cost": 0.0, "sid": None, "closed": False}
    bridge._add_budget_run_cost(state, {"num_turns": 2, "total_cost_usd": 0.01})
    bridge._add_budget_run_cost(state, {"num_turns": 1})
    bridge._add_budget_run_cost(state, {"num_turns": True})  # never a bool
    assert state["run_turns"] == 3
    assert state["run_cost"] == pytest.approx(0.01)


def test_live_session_stops_after_the_turn_that_used_the_run(monkeypatch) -> None:
    import untether.runners.claude as claude_mod

    stopped: list[str] = []
    monkeypatch.setattr(
        claude_mod, "get_live_session", lambda sid: SimpleNamespace(closing=False)
    )
    monkeypatch.setattr(claude_mod, "stop_live_session_input", stopped.append)
    state: dict[str, Any] = {"run_cost": 0.0, "run_turns": 0, "sid": None}
    with apply_run_options(EngineRunOptions(max_turns=3)):
        bridge._add_budget_run_cost(state, {"num_turns": 2})
        assert bridge._decide_budget_stop(state, "s1") is None
        bridge._add_budget_run_cost(state, {"num_turns": 1})  # a wake turn
        with capture_logs() as logs:
            text = bridge._decide_budget_stop(state, "s1")
    assert text is not None and "max_turns is 3" in text
    assert stopped == ["s1"] and state["sid"] == "s1"
    assert any(e["event"] == "cost_budget.run_stopped" for e in logs)


# --- journal outcomes ---------------------------------------------------------


@pytest.mark.parametrize(
    ("usage", "ok", "stop", "expected"),
    [
        ({"subtype": "error_max_turns"}, False, None, "max_turns"),
        ({"subtype": "error_max_budget_usd"}, False, None, "max_budget"),
        ({"terminal_reason": "aborted_streaming"}, True, "max_elapsed", "max_elapsed"),
        ({"terminal_reason": "aborted_tools"}, True, None, "cancelled"),
        ({"limit": "max_elapsed"}, False, None, "max_elapsed"),  # agy
        ({"subtype": "success"}, True, None, "ok"),
        (None, False, None, "error"),
    ],
)
def test_trigger_journal_outcome(
    usage: dict | None, ok: bool, stop: str | None, expected: str
) -> None:
    assert trigger_journal_outcome(usage, ok=ok, stop_reason=stop) == expected


# --- wall-clock -----------------------------------------------------------------


def test_elapsed_limit_per_engine(monkeypatch) -> None:
    ctx = RunContext(trigger_source="cron:w", max_elapsed_s=60)
    assert trigger_elapsed_limit("codex", ctx) == 60.0
    assert trigger_elapsed_limit("codex", RunContext()) is None
    assert trigger_elapsed_limit("codex", None) is None
    # agy stops itself (--print-timeout); the bridge is the backstop.
    assert trigger_elapsed_limit("antigravity", ctx) == 90.0
    monkeypatch.setattr(
        bridge,
        "_load_watchdog_settings",
        lambda: SimpleNamespace(live_session_max_s=600.0),
    )
    assert trigger_elapsed_limit("claude", ctx) == 60.0
    with capture_logs() as logs:
        clamped = trigger_elapsed_limit(
            "claude", RunContext(trigger_source="cron:w", max_elapsed_s=90000)
        )
    assert clamped == 600.0
    (warn,) = [e for e in logs if e["event"] == "trigger.max_elapsed.clamped"]
    assert warn["live_session_max_s"] == 600.0


@pytest.mark.anyio
async def test_interrupt_any_turn_needs_no_pending_request() -> None:
    sid = "sess-1125"
    fake_stdin = AsyncMock()
    _SESSION_STDIN[sid] = fake_stdin
    _SESSION_BG_STATE[sid] = ClaudeStreamState()
    try:
        assert await interrupt_pending_turn(sid, grace_s=0.05) is False
        fake_stdin.send.assert_not_awaited()
        assert await interrupt_pending_turn(sid, grace_s=0.05, any_turn=True) is False
        (call,) = fake_stdin.send.await_args_list
        assert json.loads(call.args[0])["request"] == {"subtype": "interrupt"}
    finally:
        _SESSION_BG_STATE.pop(sid, None)
        _SESSION_STDIN.pop(sid, None)


@pytest.mark.anyio
async def test_max_elapsed_stops_the_run_and_journals_it(tmp_path, monkeypatch) -> None:
    journal.init_journal(tmp_path / "untether.toml")
    monkeypatch.setattr(bridge, "trigger_elapsed_limit", lambda engine, ctx: 0.05)
    transport = FakeTransport()
    runner = ScriptRunner([Wait(anyio.Event())], engine="pi", resume_value="s-1125")
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=True
    )
    ctx = RunContext(trigger_source="cron:watch", run_id="rid", max_elapsed_s=30)
    try:
        with capture_logs() as logs, anyio.fail_after(5):
            await handle_message(
                cfg,
                runner=runner,
                incoming=IncomingMessage(channel_id=1, message_id=10, text="go"),
                resume_token=None,
                context=ctx,
                running_tasks={},
            )
    finally:
        journal.reset_journal()
    last = (transport.edit_calls or transport.send_calls)[-1]["message"].text
    assert "time limit" in last
    assert any(e["event"] == "trigger.max_elapsed.reached" for e in logs)
    (line,) = [
        json.loads(raw)
        for raw in (tmp_path / journal.JOURNAL_FILENAME).read_text().splitlines()
    ]
    assert line["outcome"] == "max_elapsed" and line["run_id"] == "rid"


@pytest.mark.anyio
async def test_no_limit_no_timer(monkeypatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(
        bridge,
        "trigger_elapsed_limit",
        lambda engine, ctx: calls.append(ctx) or None,
    )
    hold = anyio.Event()
    runner = ScriptRunner([Wait(hold)], engine="pi", resume_value="s-2")
    cfg = ExecBridgeConfig(
        transport=FakeTransport(), presenter=MarkdownPresenter(), final_notify=True
    )
    running_tasks: dict = {}

    async def run() -> None:
        await handle_message(
            cfg,
            runner=runner,
            incoming=IncomingMessage(channel_id=1, message_id=10, text="go"),
            resume_token=None,
            running_tasks=running_tasks,
        )

    async with anyio.create_task_group() as tg:
        tg.start_soon(run)
        await anyio.sleep(0.1)
        task = running_tasks[next(iter(running_tasks))]
        assert task.stop_reason is None and not task.cancel_requested.is_set()
        task.cancel_requested.set()
    assert calls == [None]


# --- Antigravity: native --print-timeout ----------------------------------------


def test_agy_argv_uses_print_timeout_for_a_trigger_limit() -> None:
    from untether.runners.antigravity import AntigravityRunner

    runner = AntigravityRunner(antigravity_cmd="agy")
    state = runner.new_state("hi", None)
    with apply_run_options(EngineRunOptions(max_elapsed_s=60)):
        args = runner.build_args("hi", None, state=state)
    assert "--print-timeout=60s" in args and "0" not in args
    plain = runner.build_args("hi", None, state=runner.new_state("hi", None))
    i = plain.index("--print-timeout")
    assert plain[i + 1] == "0"


@pytest.mark.anyio
@pytest.mark.parametrize("limit", [None, 60])
async def test_agy_print_timeout_success_is_a_time_limit_stop(
    limit: int | None, tmp_path, monkeypatch
) -> None:
    from tests import test_antigravity_errors as agy_tests
    from untether.runners import antigravity as agy
    from untether.utils.paths import reset_run_base_dir, set_run_base_dir

    monkeypatch.setattr(agy, "_probe_agy_version", agy._run_agy_version)
    monkeypatch.setenv("UNTETHER_FAKE_AGY_TIME_SCALE", "1")
    agy_tests._script(
        tmp_path,
        monkeypatch,
        "err:[agy] print timeout after 60s\nsleep:0.3\n"
        f"out:{agy_tests._INIT}\n"
        f"out:{agy_tests._result('SUCCESS', response='Partial')}\n",
    )
    proj = tmp_path / "proj"
    proj.mkdir()
    token = set_run_base_dir(proj)
    try:
        with apply_run_options(EngineRunOptions(max_elapsed_s=limit)):
            done = agy_tests._done(await agy_tests._run())
    finally:
        reset_run_base_dir(token)
    if limit is None:
        assert done.ok is True  # not our limit: unchanged
        return
    assert done.ok is False
    assert done.error == agy.PRINT_TIMEOUT_TEXT
    assert done.answer == "Partial"
    assert (done.usage or {}).get("limit") == "max_elapsed"
