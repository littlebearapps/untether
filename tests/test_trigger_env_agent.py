"""#1121: per-trigger ``env`` and Claude ``agent`` for crons and webhooks."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

import untether.runners.claude as claude_mod
from untether.context import RunContext, attended_context
from untether.runners.claude import ClaudeRunner, ClaudeStreamState
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.telegram.loop import _apply_trigger_overrides
from untether.triggers.dispatcher import build_run_context
from untether.triggers.settings import (
    CronConfig,
    WebhookConfig,
    parse_trigger_config_isolated,
)
from untether.utils.env_policy import apply_run_env
from untether.utils.paths import reset_run_context, set_run_context


def _cron(**kw: Any) -> CronConfig:
    return CronConfig.model_validate(
        {"id": "desk", "schedule": "0 9 * * *", "prompt": "go", **kw}
    )


# --- config validation -----------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "PATH",
        "HOME",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
        "PYTHONPATH",
        "NODE_OPTIONS",
        "ANTHROPIC_API_KEY",
        "GITHUB_TOKEN",
        "UNTETHER_RUN_ID",
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "HTTPS_PROXY",
        "BASH_ENV",
        "GIT_SSH_COMMAND",
    ],
)
def test_reserved_env_names_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError, match="reserved"):
        _cron(env={name: "x"})


@pytest.mark.parametrize("name", ["lower", "1ROLE", "A-B", ""])
def test_bad_env_names_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError, match="invalid env name"):
        _cron(env={name: "x"})


@pytest.mark.parametrize("value", ["{{payload.x}}", "a\x00b"])
def test_templated_or_nul_values_are_rejected_without_echoing(value: str) -> None:
    with pytest.raises(ValidationError) as exc:
        _cron(env={"APP_ROLE": value})
    assert "APP_ROLE" in str(exc.value)
    assert value not in str(exc.value)


def test_env_and_agent_accepted_for_claude() -> None:
    cron = _cron(engine="claude", env={"APP_SESSION_ROLE": "qa"}, agent="plug:qa-desk")
    assert cron.env == {"APP_SESSION_ROLE": "qa"}
    assert cron.agent == "plug:qa-desk"
    # env alone works on any engine.
    assert _cron(engine="codex", env={"APP_ROLE": "qa"}).env == {"APP_ROLE": "qa"}


@pytest.mark.parametrize("engine", [None, "codex", "opencode", "pi", "antigravity"])
def test_agent_needs_claude(engine: str | None) -> None:
    with pytest.raises(ValidationError, match='agent needs engine = "claude"'):
        _cron(engine=engine, agent="qa-desk")


@pytest.mark.parametrize("agent", ["-p", "qa desk", "--dangerously-skip-permissions"])
def test_bad_agent_values_are_rejected(agent: str) -> None:
    with pytest.raises(ValidationError, match="invalid agent"):
        _cron(engine="claude", agent=agent)


def test_non_agent_webhook_rejects_env_and_agent() -> None:
    with pytest.raises(ValidationError, match="only applies when action='agent_run'"):
        WebhookConfig.model_validate(
            {
                "id": "w",
                "path": "/w",
                "auth": "none",
                "action": "notify_only",
                "message_template": "hi",
                "env": {"APP_ROLE": "qa"},
            }
        )


def test_bad_env_skips_only_that_entry() -> None:
    parsed = parse_trigger_config_isolated(
        {
            "enabled": True,
            "crons": [
                {"id": "ok", "schedule": "0 9 * * *", "prompt": "x"},
                {
                    "id": "bad",
                    "schedule": "0 9 * * *",
                    "prompt": "x",
                    "env": {"PATH": "/tmp/evil"},
                },
            ],
        }
    )
    assert [c.id for c in parsed.settings.crons] == ["ok"]
    (invalid,) = parsed.invalid
    assert invalid.trigger_id == "bad"
    assert "/tmp/evil" not in invalid.error


# --- plumbing -------------------------------------------------------------


def test_build_run_context_forwards_env_and_agent() -> None:
    ctx = build_run_context(
        _cron(engine="claude", env={"B_ROLE": "2", "A_ROLE": "1"}, agent="qa")
    )
    assert ctx.env == (("A_ROLE", "1"), ("B_ROLE", "2"))
    assert ctx.agent == "qa"
    # A human reply to that run keeps neither.
    assert attended_context(ctx) == RunContext(project=None)


def test_agent_reaches_run_options_env_never_logged() -> None:
    ctx = RunContext(
        trigger_source="cron:desk", agent="qa", env=(("APP_ROLE", "sekrit"),)
    )
    with capture_logs() as logs:
        opts = _apply_trigger_overrides(EngineRunOptions(), ctx, engine="claude")
    assert opts is not None and opts.agent == "qa"
    assert "sekrit" not in repr(logs)


def test_apply_run_env_adds_trigger_env_for_trigger_runs_only() -> None:
    env = (("APP_ROLE", "qa"), ("SERVICE_KEY", "trigger"))
    ctx = RunContext(trigger_source="cron:desk", run_id="r", env=env)
    with capture_logs() as logs:
        out = apply_run_env({"PATH": "/bin", "SERVICE_KEY": "service"}, ctx)
    assert out is not None
    assert out["APP_ROLE"] == "qa"
    # Never replaces a name the service passed through.
    assert out["SERVICE_KEY"] == "service"
    assert "qa" not in repr(logs) and "trigger" not in repr(
        [e.get("names") for e in logs]
    )
    # A reply / /at with the same env (not unattended) gets nothing.
    attended = RunContext(trigger_source="at:1", env=env)
    assert "APP_ROLE" not in (apply_run_env({"PATH": "/bin"}, attended) or {})


def test_claude_args_carry_agent_only_for_that_run() -> None:
    runner = ClaudeRunner(
        claude_cmd="claude", extra_args=["--agent", "old", "--chrome"]
    )
    with apply_run_options(EngineRunOptions(agent="qa-desk")):
        trigger_args = runner._build_args("hi", None)
    attended_args = runner._build_args("hi", None)
    assert "--agent=qa-desk" in trigger_args
    assert "old" not in trigger_args and "--chrome" in trigger_args
    assert "--agent=qa-desk" not in attended_args
    assert attended_args.count("--agent") == 1  # engine-wide one kept


def test_env_audit_expects_trigger_env_names(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    def fake_audit(pid: int, **kw: Any) -> list[str]:
        seen.update(kw)
        return []

    monkeypatch.setattr(claude_mod, "audit_proc_env", fake_audit)
    monkeypatch.setattr(claude_mod, "load_settings_if_exists", lambda: None)
    state = ClaudeStreamState()
    state.pid = 1234
    token = set_run_context(
        RunContext(trigger_source="cron:desk", env=(("APP_ROLE", "qa"),))
    )
    try:
        claude_mod._maybe_audit_env(state, "sess")
    finally:
        reset_run_context(token)
    assert "APP_ROLE" in seen["expected_extras"]
