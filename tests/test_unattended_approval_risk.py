"""#751 — dispatch-time warning for unattended runs in tap-waiting modes.

A cron or webhook that reaches Claude in `default` / `manual` /
`acceptEdits` (tool approval) or `plan` (plan approval) waits for a Telegram
tap nobody is there to give. The config audit only sees explicit cron modes;
this check sees the real resolved mode (cron → chat pref → engine config).
"""

from __future__ import annotations

from typing import Any

import pytest
from structlog.testing import capture_logs

from untether.context import RunContext
from untether.runners.run_options import EngineRunOptions
from untether.telegram import loop as loop_mod
from untether.telegram.loop import _note_unattended_approval_risk


@pytest.fixture(autouse=True)
def _reset():
    loop_mod._UNATTENDED_RISK_WARNED.clear()
    yield
    loop_mod._UNATTENDED_RISK_WARNED.clear()


def _risk(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in logs if e["event"] == "trigger.unattended_approval_risk"]


def _no_default() -> str | None:
    raise AssertionError("engine default must not be read when a mode is known")


def test_751_dispatch_warns_for_cron_in_prompting_mode_once() -> None:
    ctx = RunContext(trigger_source="cron:x", permission_mode="default")
    opts = EngineRunOptions(permission_mode="default")
    with capture_logs() as logs:
        _note_unattended_approval_risk(ctx, "claude", opts, _no_default)
        _note_unattended_approval_risk(ctx, "claude", opts, _no_default)
        ctx2 = RunContext(trigger_source="cron:x", permission_mode="plan")
        _note_unattended_approval_risk(
            ctx2, "claude", EngineRunOptions(permission_mode="plan"), _no_default
        )
    events = _risk(logs)
    assert [(e["mode"], e["source"], e["waits_for"]) for e in events] == [
        ("default", "cron", "tool approval"),
        ("plan", "cron", "plan approval"),
    ]
    assert all(e["phase"] == "dispatch" and e["trigger"] == "cron:x" for e in events)
    assert all(e["log_level"] == "warning" for e in events)


def test_751_dispatch_uses_engine_default_when_no_override() -> None:
    ctx = RunContext(trigger_source="cron:y")
    with capture_logs() as logs:
        _note_unattended_approval_risk(ctx, "claude", None, lambda: "plan")
    (event,) = _risk(logs)
    assert event["source"] == "engine_config"
    assert event["mode"] == "plan"


def test_751_dispatch_webhook_chat_pref_acceptEdits_warns() -> None:
    ctx = RunContext(trigger_source="webhook:gh")
    opts = EngineRunOptions(permission_mode="acceptEdits")
    with capture_logs() as logs:
        _note_unattended_approval_risk(ctx, "claude", opts, _no_default)
    (event,) = _risk(logs)
    assert (event["trigger"], event["mode"], event["source"]) == (
        "webhook:gh",
        "acceptEdits",
        "chat_pref",
    )


@pytest.mark.parametrize(
    ("ctx", "engine", "mode"),
    [
        (None, "claude", "default"),
        (RunContext(), "claude", "default"),  # interactive
        (RunContext(trigger_source="at:123"), "claude", "default"),
        (RunContext(trigger_source="loop:tok"), "claude", "default"),
        (RunContext(trigger_source="cron:x"), "codex", "default"),
        (RunContext(trigger_source="cron:x"), "claude", "plan-auto"),
        (RunContext(trigger_source="cron:x"), "claude", "auto"),
        (RunContext(trigger_source="cron:x"), "claude", "dontAsk"),
        (RunContext(trigger_source="cron:x"), "claude", "bypassPermissions"),
    ],
)
def test_751_dispatch_negative(ctx, engine, mode) -> None:
    with capture_logs() as logs:
        _note_unattended_approval_risk(
            ctx, engine, EngineRunOptions(permission_mode=mode), _no_default
        )
    assert _risk(logs) == []


def test_751_dispatch_no_mode_anywhere_is_silent() -> None:
    with capture_logs() as logs:
        _note_unattended_approval_risk(
            RunContext(trigger_source="cron:z"), "claude", None, lambda: None
        )
    assert _risk(logs) == []


def test_751_dispatch_engine_default_failure_never_raises() -> None:
    def boom() -> str | None:
        raise RuntimeError("no runner")

    with capture_logs() as logs:
        _note_unattended_approval_risk(
            RunContext(trigger_source="cron:z"), "claude", None, boom
        )
    assert _risk(logs) == []


def test_751_dispatch_dedupe_set_is_bounded() -> None:
    cap = loop_mod._UNATTENDED_RISK_WARNED_MAX
    opts = EngineRunOptions(permission_mode="default")
    for i in range(cap + 5):
        _note_unattended_approval_risk(
            RunContext(trigger_source=f"cron:c{i}"), "claude", opts, _no_default
        )
    assert len(loop_mod._UNATTENDED_RISK_WARNED) <= cap


@pytest.mark.anyio
async def test_751_run_job_wires_the_dispatch_check(tmp_path, monkeypatch) -> None:
    """`run_job` calls the check after the cron override is applied."""
    from tests.telegram_fakes import (
        FakeBot,
        FakeTransport,
        _empty_projects,
        _make_router,
    )
    from untether.markdown import MarkdownPresenter
    from untether.runner_bridge import ExecBridgeConfig
    from untether.runners.mock import Return, ScriptRunner
    from untether.telegram import at_scheduler
    from untether.telegram.bridge import TelegramBridgeConfig
    from untether.transport_runtime import TransportRuntime

    seen: list[tuple[Any, ...]] = []
    real = loop_mod._note_unattended_approval_risk

    def spy(context, engine, run_options, engine_default_mode):
        seen.append((context.trigger_source, engine, run_options.permission_mode))
        return real(context, engine, run_options, engine_default_mode)

    monkeypatch.setattr(loop_mod, "_note_unattended_approval_risk", spy)
    transport = FakeTransport()
    runner = ScriptRunner([Return(answer="ok")], engine="claude")
    runtime = TransportRuntime(
        router=_make_router(runner),
        projects=_empty_projects(),
        config_path=tmp_path / "untether.toml",
    )
    cfg = TelegramBridgeConfig(
        bot=FakeBot(),
        runtime=runtime,
        chat_id=123,
        startup_msg="",
        exec_cfg=ExecBridgeConfig(
            transport=transport, presenter=MarkdownPresenter(), final_notify=True
        ),
        forward_coalesce_s=0.0,
        media_group_debounce_s=0.0,
    )

    async def poller(_cfg):
        run_job = at_scheduler._RUN_JOB
        assert run_job is not None
        await run_job(
            123,
            1,
            "hi",
            None,
            RunContext(trigger_source="cron:wired", permission_mode="default"),
        )
        return
        yield  # pragma: no cover — makes this an async generator

    with capture_logs() as logs:
        await loop_mod.run_main_loop(cfg, poller)
    assert seen == [("cron:wired", "claude", "default")]
    (event,) = _risk(logs)
    assert (event["trigger"], event["source"]) == ("cron:wired", "cron")
    assert len(runner.calls) == 1
