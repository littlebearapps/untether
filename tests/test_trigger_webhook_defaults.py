"""#1118 / #1122: webhook run options and safe defaults.

Webhooks get the same run options as crons (one ``TriggerRunOptions`` base,
one ``build_run_context``), a webhook's own ``permission_mode`` needs an
explicit engine and an unsafe one an explicit opt-in, ``allowed_tools``
replaces Claude's engine-wide list, and ``[triggers] webhook_defaults =
"strict"`` applies the safe defaults (compat warns instead).
"""

from __future__ import annotations

import asyncio
from dataclasses import fields as dc_fields
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from structlog.testing import capture_logs

from untether.config_reload_notification import format_strict_webhook_changes_notice
from untether.context import RunContext
from untether.permission_audit import (
    TriggerEngineDefaults,
    audit_trigger_permission_risks,
)
from untether.runners.claude import ClaudeRunner
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.telegram.loop import (
    STRICT_WEBHOOK_DEFAULT_MODES,
    _apply_trigger_overrides,
)
from untether.triggers.dispatcher import build_run_context
from untether.triggers.manager import TriggerManager
from untether.triggers.server import build_webhook_app
from untether.triggers.settings import (
    CronConfig,
    StrictWebhookChange,
    TriggerRunOptions,
    WebhookConfig,
    parse_trigger_config_isolated,
    strict_webhook_changes,
    warn_strict_webhook_changes,
)

LONG_SECRET = "s" * 32


def _hook(**kw: Any) -> dict[str, Any]:
    return {
        "id": "gh",
        "path": "/hooks/gh",
        "auth": "none",
        "prompt_template": "Payload: {{text}}",
        **kw,
    }


def _webhook(**kw: Any) -> WebhookConfig:
    return WebhookConfig.model_validate(_hook(**kw))


def _parse(webhooks: list[dict[str, Any]], **block: Any):
    return parse_trigger_config_isolated(
        {"enabled": True, "webhooks": webhooks, **block}
    )


# --- settings: webhook run options (#1118 N1/N2, #1122) --------------------


def test_webhook_accepts_cron_run_options() -> None:
    hook = _webhook(
        engine="claude",
        permission_mode="auto",
        model="sonnet",
        reasoning="LOW",
        allowed_tools=[" Read ", "Bash(git:*)"],
    )
    assert (hook.permission_mode, hook.model, hook.reasoning) == (
        "auto",
        "sonnet",
        "low",
    )
    assert hook.allowed_tools == ["Read", "Bash(git:*)"]


@pytest.mark.parametrize(
    "extra",
    [
        {"model": "sonnet"},
        {"engine": "claude", "permission_mode": "auto"},
        {"engine": "claude", "allowed_tools": []},
    ],
)
def test_run_options_on_non_agent_webhook_are_invalid(extra: dict[str, Any]) -> None:
    parsed = _parse(
        [
            _hook(
                action="notify_only",
                message_template="hi",
                prompt_template=None,
                **extra,
            )
        ]
    )
    assert not parsed.settings.webhooks
    (bad,) = parsed.invalid
    assert "only applies when action='agent_run'" in bad.error


def test_webhook_permission_mode_needs_explicit_engine() -> None:
    (bad,) = _parse([_hook(permission_mode="default")]).invalid
    assert "needs an explicit engine" in bad.error


@pytest.mark.parametrize(
    ("engine", "mode"),
    [("claude", "bypassPermissions"), ("antigravity", "full"), ("codex", "auto")],
)
def test_unsafe_webhook_mode_needs_opt_in(engine: str, mode: str) -> None:
    (bad,) = _parse([_hook(engine=engine, permission_mode=mode)]).invalid
    assert "allow_unsafe_permission_mode = true" in bad.error
    ok = _webhook(
        engine=engine, permission_mode=mode, allow_unsafe_permission_mode=True
    )
    assert ok.permission_mode == mode


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"engine": "claude"},
        {"engine": "claude", "permission_mode": "default"},
        {"engine": "codex", "permission_mode": "safe"},
    ],
)
def test_opt_in_without_unsafe_mode_is_invalid(extra: dict[str, Any]) -> None:
    (bad,) = _parse([_hook(allow_unsafe_permission_mode=True, **extra)]).invalid
    assert "needs an explicit engine and an unsafe permission_mode" in bad.error


@pytest.mark.parametrize(
    ("engine", "mode", "error"),
    [
        ("codex", "plan", "allowed values: ['auto', 'safe']"),
        ("opencode", "plan", "has no permission_mode a webhook can set"),
        ("claude", "yolo", "unknown permission_mode 'yolo'"),
    ],
)
def test_webhook_mode_validated_per_engine(engine: str, mode: str, error: str) -> None:
    (bad,) = _parse([_hook(engine=engine, permission_mode=mode)]).invalid
    assert error in bad.error


@pytest.mark.parametrize("model", [CronConfig, WebhookConfig])
def test_allowed_tools_needs_engine_claude(model: type) -> None:
    base = (
        {"id": "c", "schedule": "0 3 * * *", "prompt": "hi"}
        if model is CronConfig
        else _hook()
    )
    with pytest.raises(ValueError, match='allowed_tools needs engine = "claude"'):
        model.model_validate({**base, "allowed_tools": ["Read"]})
    with pytest.raises(ValueError, match='allowed_tools needs engine = "claude"'):
        model.model_validate({**base, "engine": "codex", "allowed_tools": []})
    assert model.model_validate({**base, "engine": "claude", "allowed_tools": []})


@pytest.mark.parametrize("entry", ["", "-x", "Bash,Edit", "Ba\x00sh"])
def test_allowed_tools_entries_validated(entry: str) -> None:
    with pytest.raises(ValueError, match="invalid allowed_tools entry"):
        _webhook(engine="claude", allowed_tools=["Read", entry])


def test_run_options_are_one_shared_base() -> None:
    assert issubclass(CronConfig, TriggerRunOptions)
    assert issubclass(WebhookConfig, TriggerRunOptions)


# --- settings: webhook_defaults (#1118 N4, S3/S4) --------------------------


def test_webhook_defaults_default_is_compat() -> None:
    assert _parse([]).settings.webhook_defaults == "compat"


def test_bad_webhook_defaults_fails_the_block() -> None:
    with pytest.raises(ValueError, match="webhook_defaults"):
        _parse([], webhook_defaults="lax")


def test_strict_skips_projectless_agent_webhook_only() -> None:
    hooks = [
        _hook(),
        _hook(id="ok", path="/hooks/ok", project="app"),
        _hook(
            id="note",
            path="/hooks/note",
            action="notify_only",
            message_template="hi",
            prompt_template=None,
        ),
    ]
    strict = _parse(hooks, webhook_defaults="strict")
    assert [w.id for w in strict.settings.webhooks] == ["ok", "note"]
    (bad,) = strict.invalid
    assert bad.trigger_id == "gh" and "project is required" in bad.error
    assert not _parse(hooks).invalid  # compat: all load


@pytest.mark.parametrize(("length", "skipped"), [(31, True), (32, False)])
def test_strict_short_secret_boundary(length: int, skipped: bool) -> None:
    hook = _hook(project="app", auth="bearer", secret="s" * length)
    strict = _parse([hook], webhook_defaults="strict")
    assert bool(strict.invalid) is skipped
    if skipped:
        assert "at least 32 characters" in strict.invalid[0].error
        assert "s" * 8 not in strict.invalid[0].error
    assert not _parse([hook]).invalid


def test_strict_webhook_changes_lists_each_change() -> None:
    changes = strict_webhook_changes(_webhook(auth="bearer", secret="short"))
    assert changes[0] == "skipped: no project"
    assert any("safe mode" in c for c in changes)
    assert "no tools pre-approved for Claude" in changes
    assert changes[-1] == "skipped: secret under 32 characters"
    configured = _webhook(
        project="app", engine="claude", permission_mode="default", allowed_tools=[]
    )
    assert strict_webhook_changes(configured) == ()
    codex = _webhook(project="app", engine="codex", permission_mode="safe")
    assert strict_webhook_changes(codex) == ()


def test_warn_strict_webhook_changes_once_and_never_in_strict() -> None:
    warned: set[StrictWebhookChange] = set()
    compat = _parse([_hook(project="app")]).settings
    with capture_logs() as logs:
        first = warn_strict_webhook_changes(compat, warned=warned)
        again = warn_strict_webhook_changes(compat, warned=warned)
    assert [c.webhook_id for c in first] == ["gh"] and again == []
    (log,) = [e for e in logs if e["event"] == "trigger.webhook.strict_defaults_change"]
    assert log["log_level"] == "warning"
    assert log["changes_in"] == "0.37.0"
    strict = _parse([_hook(project="app")], webhook_defaults="strict").settings
    assert warn_strict_webhook_changes(strict, warned=set()) == []


def test_strict_changes_notice_text() -> None:
    text = format_strict_webhook_changes_notice(
        [StrictWebhookChange("gh_push", ("skipped: no project",))]
    )
    assert text.startswith(
        "⚠️ Trigger safety: 1 webhook relies on defaults that change in 0.37.0"
    )
    assert "gh\\_push" in text  # escaped for the markdown renderer
    assert 'webhook_defaults = "strict"' in text


# --- dispatch: one builder, cron/webhook parity (#1122) --------------------

_PARITY_VALUES: dict[str, Any] = {
    "permission_mode": "auto",
    "model": "sonnet",
    "reasoning": "low",
    "allowed_tools": ["Read"],
}


@pytest.mark.parametrize("name", sorted(_PARITY_VALUES))
def test_build_run_context_parity(name: str) -> None:
    # Every run option except `engine` (routing, not a RunContext field).
    run_option_fields = set(TriggerRunOptions.model_fields) - {"engine"}
    assert run_option_fields == set(_PARITY_VALUES)
    opts = {"engine": "claude", "project": "app", name: _PARITY_VALUES[name]}
    cron = CronConfig.model_validate(
        {"id": "x", "schedule": "0 3 * * *", "prompt": "hi", **opts}
    )
    hook = _webhook(id="x", **opts)
    cron_ctx, hook_ctx = build_run_context(cron), build_run_context(hook)
    assert cron_ctx.trigger_source == "cron:x"
    assert hook_ctx.trigger_source == "webhook:x"
    ignore = {"trigger_source", "run_id"}
    for f in dc_fields(RunContext):
        if f.name not in ignore:
            assert getattr(cron_ctx, f.name) == getattr(hook_ctx, f.name), f.name
    assert cron_ctx.allowed_tools in (None, ("Read",))


def test_build_run_context_strict_only_for_webhooks() -> None:
    cron = CronConfig.model_validate(
        {"id": "c", "schedule": "0 3 * * *", "prompt": "hi"}
    )
    assert build_run_context(cron, strict=True).strict_webhook is False
    assert build_run_context(_webhook(), strict=True).strict_webhook is True
    assert build_run_context(_webhook()).strict_webhook is False


# --- applier: strict defaults per engine (#1118 S1/S2/S5) ------------------


def _strict_ctx(**kw: Any) -> RunContext:
    return RunContext(trigger_source="webhook:gh", strict_webhook=True, **kw)


@pytest.mark.parametrize(
    ("engine", "mode"), sorted(STRICT_WEBHOOK_DEFAULT_MODES.items())
)
def test_strict_webhook_gets_engine_safe_mode(engine: str, mode: str) -> None:
    chat = EngineRunOptions(permission_mode="plan")
    opts = _apply_trigger_overrides(chat, _strict_ctx(), engine=engine, log=False)
    assert opts is not None
    assert opts.permission_mode == mode
    assert opts.allowed_tools == (() if engine == "claude" else None)


def test_strict_keeps_the_webhooks_own_mode_and_tools() -> None:
    ctx = _strict_ctx(permission_mode="auto", allowed_tools=("Read",))
    opts = _apply_trigger_overrides(None, ctx, engine="claude", log=False)
    assert opts is not None
    assert (opts.permission_mode, opts.allowed_tools) == ("auto", ("Read",))


def test_strict_leaves_other_engines_alone() -> None:
    opts = _apply_trigger_overrides(None, _strict_ctx(), engine="pi", log=False)
    assert opts is not None
    assert opts.permission_mode is None and opts.allowed_tools is None


def test_compat_webhook_keeps_chat_options() -> None:
    ctx = RunContext(trigger_source="webhook:gh")
    chat = EngineRunOptions(permission_mode="plan")
    opts = _apply_trigger_overrides(chat, ctx, engine="claude", log=False)
    assert opts is not None
    assert opts.permission_mode == "plan" and opts.allowed_tools is None


def test_webhook_override_logs_use_webhook_names() -> None:
    ctx = RunContext(trigger_source="webhook:gh", model="sonnet")
    with capture_logs() as logs:
        _apply_trigger_overrides(None, ctx, engine="claude")
    assert [e["event"] for e in logs] == ["trigger.webhook.model_override"]


# --- Claude argv (#1118 acceptance) ----------------------------------------


def _claude_args(opts: EngineRunOptions, **runner_kw: Any) -> list[str]:
    runner = ClaudeRunner(claude_cmd="claude", **runner_kw)
    with apply_run_options(opts):
        return runner._build_args("payload prompt", None)


def test_strict_dontask_chat_sends_no_allowlist() -> None:
    """A webhook in a dontAsk chat with no allowed_tools spawns without
    --allowedTools under strict (compat still sends the engine list)."""
    ctx = _strict_ctx(permission_mode="dontAsk")
    opts = _apply_trigger_overrides(None, ctx, engine="claude", log=False)
    assert opts is not None
    args = _claude_args(opts, allowed_tools=["Bash", "Read"])
    assert "--allowedTools" not in args
    compat = _apply_trigger_overrides(
        None,
        RunContext(trigger_source="webhook:gh", permission_mode="dontAsk"),
        engine="claude",
        log=False,
    )
    assert compat is not None
    assert "--allowedTools" in _claude_args(compat, allowed_tools=["Bash", "Read"])


def test_strict_no_mode_uses_control_channel_with_prompt_on_stdin() -> None:
    opts = _apply_trigger_overrides(None, _strict_ctx(), engine="claude", log=False)
    assert opts is not None
    args = _claude_args(opts)
    assert args[args.index("--permission-mode") + 1] == "default"
    assert args[args.index("--permission-prompt-tool") + 1] == "stdio"
    assert "--" not in args and "-p" not in args
    assert "payload prompt" not in args


def test_trigger_allowed_tools_replace_engine_list() -> None:
    opts = EngineRunOptions(
        permission_mode="default",
        unattended_trigger="webhook:gh",
        allowed_tools=("Read",),
    )
    args = _claude_args(opts, allowed_tools=["Bash"])
    # Explicit, so sent even in a prompting mode (#749 rule).
    assert args[args.index("--allowedTools") + 1] == "Read"


# --- audit (#1118 N3) ------------------------------------------------------


def _audit(settings: Any, **defaults: Any):
    return audit_trigger_permission_risks(
        settings,
        default_chat_id=1,
        resolve_engine=lambda override, _project: override or "claude",
        defaults=TriggerEngineDefaults(**defaults),
        chat_mode=lambda _chat, _engine: None,
    )


def test_audit_reads_the_webhooks_own_tools_and_strict() -> None:
    risky = ("Bash", "Read")
    own = _parse(
        [
            _hook(
                project="app",
                engine="claude",
                permission_mode="auto",
                allowed_tools=["Bash"],
            )
        ]
    ).settings
    (risk,) = _audit(own, claude_allowed_tools=())
    assert risk.tools == ("Bash",) and risk.origin == "trigger"
    own_safe = _parse(
        [
            _hook(
                project="app", engine="claude", permission_mode="auto", allowed_tools=[]
            )
        ]
    ).settings
    assert _audit(own_safe, claude_allowed_tools=risky) == ()
    strict = _parse([_hook(project="app")], webhook_defaults="strict").settings
    assert (
        _audit(strict, claude_allowed_tools=risky, claude_allowed_tools_explicit=True)
        == ()
    )
    compat = _parse([_hook(project="app")]).settings
    assert _audit(compat, claude_allowed_tools=risky)


# --- server: strict read per request from the manager (hot reload) ---------


@pytest.mark.anyio
async def test_server_passes_strict_from_the_manager() -> None:
    seen: list[bool] = []

    class _Dispatcher:
        async def dispatch_webhook(self, wh: Any, prompt: str, *, strict: bool) -> None:
            seen.append(strict)

    settings = _parse([_hook(project="app")]).settings
    manager = TriggerManager(settings)
    app = build_webhook_app(settings, _Dispatcher(), manager=manager)  # type: ignore[arg-type]
    async with TestClient(TestServer(app)) as cl:
        assert (await cl.post("/hooks/gh", json={"text": "x"})).status == 202
        await asyncio.sleep(0.05)
        manager.update(
            _parse([_hook(project="app")], webhook_defaults="strict").settings
        )
        assert (await cl.post("/hooks/gh", json={"text": "x"})).status == 202
        await asyncio.sleep(0.05)
    assert seen == [False, True]


# --- loop: the one Telegram notice -----------------------------------------


@pytest.mark.anyio
async def test_loop_notice_sent_only_for_new_changes() -> None:
    from tests.telegram_fakes import FakeTransport, make_cfg
    from untether.telegram.loop import _notify_strict_webhook_changes

    transport = FakeTransport()
    cfg = make_cfg(transport)
    await _notify_strict_webhook_changes(cfg, [])
    assert transport.send_calls == []
    await _notify_strict_webhook_changes(
        cfg, [StrictWebhookChange("gh", ("skipped: no project",))]
    )
    (call,) = transport.send_calls
    assert call["message"].text.startswith("⚠️ Trigger safety: 1 webhook")
    assert call["options"].notify is True
