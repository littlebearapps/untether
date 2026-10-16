"""#1142: webhook runs never inherit Claude ``bypassPermissions``.

A webhook ``agent_run`` builds its prompt from the request payload, so a
chat's (or ``untether.toml``'s) bypass mode must not reach it: Claude is
downgraded to ``default`` (so #835's unattended deny applies) and a warning
is logged once per trigger and origin. Crons keep today's behaviour and are
flagged by the start-up trigger audit instead, alongside two webhook audit
warnings (no ``project``; Bash/Edit/Write pre-approved in a mode that never
asks, or by an explicit ``allowed_tools`` in any mode, #1145). A Codex webhook
whose mode is unknown runs ``safe``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

import untether.runners.claude as claude_mod
import untether.runners.codex as codex_mod
from untether.context import RunContext
from untether.permission_audit import (
    TriggerEngineDefaults,
    TriggerRisk,
    audit_trigger_permission_risks,
    log_trigger_permission_risks,
    trigger_chat_engine_pairs,
)
from untether.runners.claude import ClaudeRunner
from untether.runners.codex import CodexRunner
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.telegram.chat_prefs import ChatPrefsStore
from untether.telegram.engine_overrides import EngineOverrides
from untether.telegram.loop import (
    _apply_trigger_overrides,
    _resolve_engine_run_options,
)
from untether.triggers.settings import parse_trigger_config

DEFAULT_CHAT = 123
WEBHOOK = "webhook:gh"
CRON = "cron:nightly"


@pytest.fixture(autouse=True)
def _reset_warn_once() -> Any:
    claude_mod._WEBHOOK_BYPASS_WARNED.clear()
    codex_mod._WEBHOOK_UNKNOWN_PM_WARNED.clear()
    codex_mod._UNKNOWN_PM_WARNED.clear()
    yield
    claude_mod._WEBHOOK_BYPASS_WARNED.clear()
    codex_mod._WEBHOOK_UNKNOWN_PM_WARNED.clear()
    codex_mod._UNKNOWN_PM_WARNED.clear()


def _claude(**kwargs: Any) -> ClaudeRunner:
    return ClaudeRunner(claude_cmd="claude", **kwargs)


def _mode_arg(args: list[str]) -> str | None:
    if "--permission-mode" not in args:
        return None
    return args[args.index("--permission-mode") + 1]


def _downgrades(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in logs if e["event"] == "claude.webhook_bypass_downgraded"]


# --- Claude: the runtime guard -------------------------------------------


def test_webhook_chat_bypass_runs_default_and_warns_once() -> None:
    runner = _claude()
    opts = EngineRunOptions(
        permission_mode="bypassPermissions", unattended_trigger=WEBHOOK
    )
    with capture_logs() as logs, apply_run_options(opts):
        first = runner._build_args("hi", None)
        state = runner.new_state("hi", None)
        second = runner._build_args("hi", None)
    assert _mode_arg(first) == "default"
    assert _mode_arg(second) == "default"
    assert "bypassPermissions" not in first
    assert state.unattended_trigger == WEBHOOK
    assert state.unattended_mode == "default"
    assert state.requested_permission_mode == "default"
    assert state.prompting_mode is True
    (warn,) = _downgrades(logs)
    assert warn["log_level"] == "warning"
    assert warn["trigger"] == WEBHOOK
    assert warn["origin"] == "chat"


def test_webhook_toml_bypass_runs_default() -> None:
    runner = _claude(permission_mode="bypassPermissions")
    with (
        capture_logs() as logs,
        apply_run_options(EngineRunOptions(unattended_trigger=WEBHOOK)),
    ):
        args = runner._build_args("hi", None)
    assert _mode_arg(args) == "default"
    (warn,) = _downgrades(logs)
    assert warn["origin"] == "toml"


def test_webhook_drops_dangerously_skip_permissions() -> None:
    runner = _claude(permission_mode="plan", dangerously_skip_permissions=True)
    with (
        capture_logs() as logs,
        apply_run_options(EngineRunOptions(unattended_trigger=WEBHOOK)),
    ):
        args = runner._build_args("hi", None)
        state = runner.new_state("hi", None)
    assert "--dangerously-skip-permissions" not in args
    assert _mode_arg(args) == "default"
    assert state.unattended_mode == "default"
    assert state.requested_permission_mode == "default"
    (warn,) = _downgrades(logs)
    assert warn["origin"] == "dangerously_skip_permissions"


def test_webhook_dangerously_skip_without_mode_uses_control_channel() -> None:
    """No mode + the skip flag ran ``-p`` with every check skipped; a webhook
    gets the control channel in ``default`` instead, so #835 can deny."""
    runner = _claude(dangerously_skip_permissions=True)
    with apply_run_options(EngineRunOptions(unattended_trigger=WEBHOOK)):
        args = runner._build_args("hi", None)
    assert "-p" not in args
    assert "--dangerously-skip-permissions" not in args
    assert _mode_arg(args) == "default"


def test_warning_is_once_per_trigger_and_origin() -> None:
    runner = _claude()
    with capture_logs() as logs:
        for trigger in (WEBHOOK, WEBHOOK, "webhook:other"):
            opts = EngineRunOptions(
                permission_mode="bypassPermissions", unattended_trigger=trigger
            )
            with apply_run_options(opts):
                runner._build_args("hi", None)
    assert [w["trigger"] for w in _downgrades(logs)] == [WEBHOOK, "webhook:other"]


def test_cron_keeps_inherited_bypass() -> None:
    """Crons keep today's behaviour in 0.36.1 (the audit warns instead)."""
    runner = _claude(dangerously_skip_permissions=True)
    opts = EngineRunOptions(
        permission_mode="bypassPermissions", unattended_trigger=CRON
    )
    with capture_logs() as logs, apply_run_options(opts):
        args = runner._build_args("hi", None)
    assert _mode_arg(args) == "bypassPermissions"
    assert "--dangerously-skip-permissions" in args
    assert _downgrades(logs) == []


def test_attended_bypass_unchanged() -> None:
    runner = _claude()
    opts = EngineRunOptions(permission_mode="bypassPermissions")
    with capture_logs() as logs, apply_run_options(opts):
        args = runner._build_args("hi", None)
    assert _mode_arg(args) == "bypassPermissions"
    assert _downgrades(logs) == []


def test_webhook_own_bypass_is_honoured() -> None:
    """Only the trigger's own setting can grant bypass (webhooks get one in
    #1122); it arrives as ``trigger_permission_mode``."""
    runner = _claude()
    opts = EngineRunOptions(
        permission_mode="bypassPermissions",
        trigger_permission_mode="bypassPermissions",
        unattended_trigger=WEBHOOK,
    )
    with capture_logs() as logs, apply_run_options(opts):
        args = runner._build_args("hi", None)
    assert _mode_arg(args) == "bypassPermissions"
    assert _downgrades(logs) == []


@pytest.mark.parametrize("mode", ["acceptEdits", "plan", "auto", "dontAsk"])
def test_webhook_other_modes_unchanged(mode: str) -> None:
    runner = _claude()
    opts = EngineRunOptions(permission_mode=mode, unattended_trigger=WEBHOOK)
    with capture_logs() as logs, apply_run_options(opts):
        args = runner._build_args("hi", None)
    assert _mode_arg(args) == mode
    assert _downgrades(logs) == []


@pytest.mark.anyio
async def test_webhook_in_planmode_bypass_chat_spawns_default(tmp_path: Path) -> None:
    """Acceptance: the whole run_job resolution path — the chat's /planmode
    bypass, the webhook context, the runner argv."""
    prefs = ChatPrefsStore(tmp_path / "prefs.json")
    await prefs.set_engine_override(
        DEFAULT_CHAT, "claude", EngineOverrides(permission_mode="bypassPermissions")
    )
    resolved = await _resolve_engine_run_options(
        DEFAULT_CHAT, None, "claude", chat_prefs=prefs, topic_store=None
    )
    webhook_opts = _apply_trigger_overrides(
        resolved, RunContext(trigger_source=WEBHOOK), engine="claude"
    )
    cron_opts = _apply_trigger_overrides(
        resolved, RunContext(trigger_source=CRON), engine="claude"
    )
    runner = _claude()
    with capture_logs() as logs:
        with apply_run_options(webhook_opts):
            webhook_args = runner._build_args("payload", None)
        with apply_run_options(cron_opts):
            cron_args = runner._build_args("scheduled", None)
    assert _mode_arg(webhook_args) == "default"
    assert _mode_arg(cron_args) == "bypassPermissions"
    assert len(_downgrades(logs)) == 1


# --- #1159: where an unattended run's mode came from -----------------------


@pytest.mark.parametrize(
    ("runner_kwargs", "opts", "source"),
    [
        (
            {},
            EngineRunOptions(permission_mode="acceptEdits", unattended_trigger=WEBHOOK),
            "chat",
        ),
        (
            {"permission_mode": "acceptEdits"},
            EngineRunOptions(unattended_trigger=WEBHOOK),
            "engine_config",
        ),
        (
            {"permission_mode": "acceptEdits"},
            EngineRunOptions(
                permission_mode="plan",
                trigger_permission_mode="plan",
                unattended_trigger=CRON,
            ),
            "trigger",
        ),
        (
            {},
            EngineRunOptions(
                permission_mode="bypassPermissions", unattended_trigger=WEBHOOK
            ),
            "webhook_guard",
        ),
        (
            {"permission_mode": "bypassPermissions"},
            EngineRunOptions(unattended_trigger=WEBHOOK),
            "webhook_guard",
        ),
        (
            {"permission_mode": "plan", "dangerously_skip_permissions": True},
            EngineRunOptions(unattended_trigger=WEBHOOK),
            "webhook_guard",
        ),
    ],
)
def test_1159_new_state_records_the_modes_source(
    runner_kwargs: dict[str, Any], opts: EngineRunOptions, source: str
) -> None:
    runner = _claude(**runner_kwargs)
    with apply_run_options(opts):
        state = runner.new_state("hi", None)
    assert state.unattended_mode_source == source


def test_1159_attended_run_has_no_mode_source() -> None:
    runner = _claude(permission_mode="acceptEdits")
    with apply_run_options(EngineRunOptions(permission_mode="plan")):
        state = runner.new_state("hi", None)
    assert state.unattended_mode_source is None


# --- Codex: unknown mode on a webhook runs safe ----------------------------


def _codex_args(opts: EngineRunOptions) -> list[str]:
    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    with apply_run_options(opts):
        return runner.build_args("hi", None, state=runner.new_state("hi", None))


def test_codex_webhook_unknown_mode_runs_read_only() -> None:
    opts = EngineRunOptions(permission_mode="plan", unattended_trigger=WEBHOOK)
    with capture_logs() as logs:
        first = _codex_args(opts)
        second = _codex_args(opts)
    for args in (first, second):
        assert args[args.index("--sandbox") + 1] == "read-only"
    warns = [e for e in logs if e["event"] == "codex.webhook_unknown_mode_safe"]
    assert len(warns) == 1
    assert warns[0]["trigger"] == WEBHOOK
    assert warns[0]["mode"] == "plan"
    assert warns[0]["log_level"] == "warning"
    assert not [e for e in logs if e["event"] == "codex.permission_mode.unknown"]


def test_codex_webhook_safe_meta() -> None:
    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    opts = EngineRunOptions(permission_mode="plan", unattended_trigger=WEBHOOK)
    with apply_run_options(opts):
        assert runner._permission_mode() == "safe"
    with apply_run_options(EngineRunOptions(permission_mode="plan")):
        assert runner._permission_mode() == "plan"


@pytest.mark.parametrize(
    "opts",
    [
        EngineRunOptions(permission_mode="plan", unattended_trigger=CRON),
        EngineRunOptions(permission_mode="plan"),
        EngineRunOptions(permission_mode="auto", unattended_trigger=WEBHOOK),
        EngineRunOptions(unattended_trigger=WEBHOOK),
    ],
    ids=["cron-unknown", "attended-unknown", "webhook-full-auto", "webhook-none"],
)
def test_codex_other_runs_unchanged(opts: EngineRunOptions) -> None:
    assert "--sandbox" not in _codex_args(opts)


# --- the start-up trigger audit --------------------------------------------


def _triggers(crons: list[dict[str, Any]] = (), webhooks: list[dict[str, Any]] = ()):
    return parse_trigger_config(
        {"enabled": True, "crons": list(crons), "webhooks": list(webhooks)}
    )


def _cron(cron_id: str = "nightly", **kw: Any) -> dict[str, Any]:
    return {"id": cron_id, "schedule": "0 3 * * *", "prompt": "hi", **kw}


def _hook(hook_id: str = "gh", **kw: Any) -> dict[str, Any]:
    base = {
        "id": hook_id,
        "path": f"/hooks/{hook_id}",
        "auth": "none",
        "prompt_template": "Payload: {{text}}",
    }
    return {**base, **kw}


def _audit(
    triggers: Any,
    *,
    defaults: TriggerEngineDefaults | None = None,
    chat_modes: dict[tuple[int, str], str | None] | None = None,
    engine: str = "claude",
    spent: tuple[str, ...] = (),
) -> tuple[TriggerRisk, ...]:
    modes = chat_modes or {}
    return audit_trigger_permission_risks(
        triggers,
        default_chat_id=DEFAULT_CHAT,
        resolve_engine=lambda override, _project: override or engine,
        defaults=defaults or TriggerEngineDefaults(),
        chat_mode=lambda chat_id, eng: modes.get((chat_id, eng)),
        spent_cron_ids=spent,
    )


def _events(risks: tuple[TriggerRisk, ...], event: str) -> list[TriggerRisk]:
    return [r for r in risks if r.event == event]


def test_audit_cron_inheriting_chat_bypass() -> None:
    risks = _audit(
        _triggers([_cron()]),
        chat_modes={(DEFAULT_CHAT, "claude"): "bypassPermissions"},
    )
    (risk,) = _events(risks, "trigger.cron.inherits_bypass")
    assert risk.trigger == CRON
    assert risk.engine == "claude"
    assert risk.mode == "bypassPermissions"
    assert risk.origin == "chat"
    assert risk.chat_id == DEFAULT_CHAT


def test_audit_cron_uses_its_own_chat_id() -> None:
    risks = _audit(
        _triggers([_cron(chat_id=-100777)]),
        chat_modes={(-100777, "claude"): "bypassPermissions"},
    )
    (risk,) = _events(risks, "trigger.cron.inherits_bypass")
    assert risk.chat_id == -100777


def test_audit_cron_inheriting_toml_bypass() -> None:
    risks = _audit(
        _triggers([_cron()]),
        defaults=TriggerEngineDefaults(claude_mode="bypassPermissions"),
    )
    (risk,) = _events(risks, "trigger.cron.inherits_bypass")
    assert risk.origin == "engine_config"


def test_audit_cron_dangerously_skip_overrides_own_mode() -> None:
    risks = _audit(
        _triggers([_cron(permission_mode="plan")]),
        defaults=TriggerEngineDefaults(claude_skip_permissions=True),
    )
    (risk,) = _events(risks, "trigger.cron.inherits_bypass")
    assert risk.origin == "dangerously_skip_permissions"


def test_audit_cron_own_bypass_and_safe_modes_are_quiet() -> None:
    risks = _audit(
        _triggers(
            [
                _cron("own", permission_mode="bypassPermissions"),
                _cron("chatplan"),
            ]
        ),
        chat_modes={(DEFAULT_CHAT, "claude"): "plan"},
    )
    assert _events(risks, "trigger.cron.inherits_bypass") == []


def test_audit_spent_run_once_cron_is_skipped() -> None:
    risks = _audit(
        _triggers([_cron(run_once=True)]),
        defaults=TriggerEngineDefaults(claude_mode="bypassPermissions"),
        spent=("nightly",),
    )
    assert _events(risks, "trigger.cron.inherits_bypass") == []


def test_audit_antigravity_cron_inheriting_full() -> None:
    risks = _audit(
        _triggers([_cron()]),
        defaults=TriggerEngineDefaults(antigravity_mode="full"),
        engine="antigravity",
    )
    (risk,) = _events(risks, "trigger.cron.inherits_bypass")
    assert risk.engine == "antigravity"
    assert risk.mode == "full"
    assert risk.origin == "engine_config"
    # agy's own guard already runs it as Workspace (REVIEW M3).
    assert risk.outcome == "runs_as_workspace"


def test_audit_claude_cron_outcome_keeps_bypass() -> None:
    (risk,) = _audit(
        _triggers([_cron()]),
        defaults=TriggerEngineDefaults(claude_mode="bypassPermissions"),
    )
    assert risk.outcome == "runs_with_bypass"


def test_audit_webhook_without_project() -> None:
    risks = _audit(
        _triggers(
            webhooks=[
                _hook("bare"),
                _hook("scoped", project="myapp"),
                _hook(
                    "note",
                    action="notify_only",
                    message_template="hi",
                    prompt_template=None,
                ),
            ]
        ),
        defaults=TriggerEngineDefaults(claude_mode="default"),
    )
    assert [r.trigger for r in _events(risks, "trigger.webhook.no_project")] == [
        "webhook:bare"
    ]


def test_audit_webhook_non_prompting_mode_with_preapproved_tools() -> None:
    risks = _audit(
        _triggers(webhooks=[_hook(project="myapp")]),
        defaults=TriggerEngineDefaults(
            claude_mode=None, claude_allowed_tools=("Bash", "Read", "Edit", "Write")
        ),
    )
    (risk,) = _events(risks, "trigger.webhook.preapproved_tools")
    assert risk.trigger == WEBHOOK
    assert risk.tools == ("Bash", "Edit", "Write")
    assert risk.mode is None
    assert risk.origin == "engine_config"


def test_audit_webhook_chat_mode_wins_over_engine_config() -> None:
    risks = _audit(
        _triggers(webhooks=[_hook(project="myapp")]),
        defaults=TriggerEngineDefaults(
            claude_mode="default", claude_allowed_tools=("Bash(*)", "Read")
        ),
        chat_modes={(DEFAULT_CHAT, "claude"): "auto"},
    )
    (risk,) = _events(risks, "trigger.webhook.preapproved_tools")
    assert risk.mode == "auto"
    assert risk.origin == "chat"
    assert risk.tools == ("Bash(*)",)


@pytest.mark.parametrize(
    ("defaults", "chat_mode", "engine"),
    [
        # prompting mode: the implicit allowlist isn't sent (#749)
        (TriggerEngineDefaults(claude_allowed_tools=("Bash",)), "default", "claude"),
        # bypass is downgraded to default on webhooks (#1142), which drops
        # the implicit allowlist (an explicit one is flagged, #1145)
        (
            TriggerEngineDefaults(claude_allowed_tools=("Bash",)),
            "bypassPermissions",
            "claude",
        ),
        (
            TriggerEngineDefaults(
                claude_allowed_tools=("Bash",), claude_skip_permissions=True
            ),
            "auto",
            "claude",
        ),
        # nothing risky pre-approved, or narrowed to a command pattern
        (TriggerEngineDefaults(claude_allowed_tools=("Read", "Glob")), None, "claude"),
        (TriggerEngineDefaults(claude_allowed_tools=("Bash(git:*)",)), None, "claude"),
        # not Claude
        (TriggerEngineDefaults(claude_allowed_tools=("Bash",)), None, "codex"),
    ],
    ids=[
        "prompting",
        "bypass-downgraded",
        "skip-downgraded",
        "harmless",
        "narrowed",
        "codex",
    ],
)
def test_audit_webhook_preapproved_quiet_cases(
    defaults: TriggerEngineDefaults, chat_mode: str | None, engine: str
) -> None:
    risks = _audit(
        _triggers(webhooks=[_hook(project="myapp")]),
        defaults=defaults,
        chat_modes={(DEFAULT_CHAT, engine): chat_mode},
        engine=engine,
    )
    assert _events(risks, "trigger.webhook.preapproved_tools") == []


def test_audit_disabled_triggers_are_quiet() -> None:
    triggers = parse_trigger_config({"enabled": False, "crons": [_cron()]})
    assert (
        _audit(
            triggers, defaults=TriggerEngineDefaults(claude_mode="bypassPermissions")
        )
        == ()
    )


def test_trigger_chat_engine_pairs() -> None:
    triggers = _triggers(
        [_cron(), _cron("other", chat_id=-1)],
        [_hook(), _hook("note", action="notify_only", message_template="x")],
    )
    pairs = trigger_chat_engine_pairs(
        triggers,
        default_chat_id=DEFAULT_CHAT,
        resolve_engine=lambda override, _project: override or "claude",
    )
    assert pairs == {(DEFAULT_CHAT, "claude"), (-1, "claude")}


def test_log_trigger_permission_risks_once() -> None:
    risks = _audit(
        _triggers([_cron()], [_hook("bare")]),
        defaults=TriggerEngineDefaults(
            claude_mode="bypassPermissions", claude_allowed_tools=("Bash",)
        ),
    )
    warned: set[TriggerRisk] = set()
    with capture_logs() as logs:
        first = log_trigger_permission_risks(risks, warned=warned, reason="startup")
        second = log_trigger_permission_risks(risks, warned=warned, reason="reload")
    assert len(first) == len(risks) == 2
    assert second == []
    events = {e["event"]: e for e in logs}
    cron = events["trigger.cron.inherits_bypass"]
    assert cron["log_level"] == "warning"
    assert cron["cron_id"] == "nightly"
    assert cron["origin"] == "engine_config"
    assert cron["outcome"] == "runs_with_bypass"
    assert cron["reason"] == "startup"
    hook = events["trigger.webhook.no_project"]
    assert hook["webhook_id"] == "bare"
    assert hook["log_level"] == "warning"


# --- #1145: an explicit allowlist is sent in prompting modes too ------------
#
# #749 drops the *implicit* default allowlist in prompting modes but sends an
# explicit `[engines.claude] allowed_tools` in every mode, so a webhook in a
# `default` / `acceptEdits` chat, or one downgraded from bypass (#1142), still
# has Bash pre-approved. The audit must flag those, and only those.

EXPLICIT_BASH = ("Bash", "Read")


@pytest.mark.parametrize(
    ("chat_mode", "claude_mode", "skip", "mode", "origin", "outcome"),
    [
        (
            "bypassPermissions",
            None,
            False,
            "bypassPermissions",
            "chat",
            "runs_as_default",
        ),
        (
            None,
            "bypassPermissions",
            False,
            "bypassPermissions",
            "engine_config",
            "runs_as_default",
        ),
        (
            "auto",
            None,
            True,
            "bypassPermissions",
            "dangerously_skip_permissions",
            "runs_as_default",
        ),
        ("acceptEdits", None, False, "acceptEdits", "chat", None),
        ("default", None, False, "default", "chat", None),
        (None, "default", False, "default", "engine_config", None),
        ("manual", None, False, "manual", "chat", None),
    ],
    ids=[
        "bypass-chat-downgraded",
        "bypass-toml-downgraded",
        "skip-downgraded",
        "acceptEdits",
        "default",
        "default-toml",
        "manual",
    ],
)
def test_audit_webhook_explicit_allowlist_flagged_in_prompting_modes(
    chat_mode: str | None,
    claude_mode: str | None,
    skip: bool,
    mode: str,
    origin: str,
    outcome: str | None,
) -> None:
    risks = _audit(
        _triggers(webhooks=[_hook(project="myapp")]),
        defaults=TriggerEngineDefaults(
            claude_mode=claude_mode,
            claude_skip_permissions=skip,
            claude_allowed_tools=EXPLICIT_BASH,
            claude_allowed_tools_explicit=True,
        ),
        chat_modes={(DEFAULT_CHAT, "claude"): chat_mode},
    )
    (risk,) = _events(risks, "trigger.webhook.preapproved_tools")
    assert risk.trigger == WEBHOOK
    assert risk.tools == ("Bash",)
    assert risk.mode == mode
    assert risk.origin == origin
    assert risk.outcome == outcome
    assert risk.chat_id == DEFAULT_CHAT


@pytest.mark.parametrize(
    ("chat_mode", "claude_mode", "skip"),
    [
        ("bypassPermissions", None, False),
        (None, "bypassPermissions", False),
        ("auto", None, True),
        ("acceptEdits", None, False),
        ("default", None, False),
        ("manual", None, False),
    ],
    ids=["bypass-chat", "bypass-toml", "skip", "acceptEdits", "default", "manual"],
)
def test_audit_webhook_implicit_allowlist_quiet_in_prompting_modes(
    chat_mode: str | None, claude_mode: str | None, skip: bool
) -> None:
    risks = _audit(
        _triggers(webhooks=[_hook(project="myapp")]),
        defaults=TriggerEngineDefaults(
            claude_mode=claude_mode,
            claude_skip_permissions=skip,
            claude_allowed_tools=("Bash", "Read", "Edit", "Write"),
            claude_allowed_tools_explicit=False,
        ),
        chat_modes={(DEFAULT_CHAT, "claude"): chat_mode},
    )
    assert _events(risks, "trigger.webhook.preapproved_tools") == []


def test_audit_webhook_explicit_allowlist_without_risky_tools_quiet() -> None:
    risks = _audit(
        _triggers(webhooks=[_hook(project="myapp")]),
        defaults=TriggerEngineDefaults(
            claude_allowed_tools=("Read", "Bash(git:*)"),
            claude_allowed_tools_explicit=True,
        ),
        chat_modes={(DEFAULT_CHAT, "claude"): "acceptEdits"},
    )
    assert _events(risks, "trigger.webhook.preapproved_tools") == []


def test_log_webhook_preapproved_tools_note_names_explicit_allowlist() -> None:
    explicit = TriggerEngineDefaults(
        claude_allowed_tools=EXPLICIT_BASH, claude_allowed_tools_explicit=True
    )
    prompting_risks = _audit(
        _triggers(webhooks=[_hook("edits", project="a")]),
        defaults=explicit,
        chat_modes={(DEFAULT_CHAT, "claude"): "acceptEdits"},
    )
    auto_risks = _audit(
        _triggers(webhooks=[_hook("auto", project="b")]),
        defaults=explicit,
        chat_modes={(DEFAULT_CHAT, "claude"): "auto"},
    )
    with capture_logs() as logs:
        log_trigger_permission_risks(prompting_risks, warned=set(), reason="startup")
        log_trigger_permission_risks(auto_risks, warned=set(), reason="startup")
    prompting, autonomous = (
        e for e in logs if e["event"] == "trigger.webhook.preapproved_tools"
    )
    assert prompting["log_level"] == "warning"
    assert prompting["webhook_id"] == "edits"
    assert prompting["mode"] == "acceptEdits"
    assert prompting["tools"] == ["Bash"]
    assert "explicitly" in prompting["note"]
    assert "even in prompting modes" in prompting["note"]
    # A mode that never asks keeps the original wording.
    assert autonomous["mode"] == "auto"
    assert "never asks" in autonomous["note"]
    assert "explicitly" not in autonomous["note"]


def test_log_webhook_downgraded_bypass_reports_outcome() -> None:
    risks = _audit(
        _triggers(webhooks=[_hook(project="a")]),
        defaults=TriggerEngineDefaults(
            claude_allowed_tools=EXPLICIT_BASH, claude_allowed_tools_explicit=True
        ),
        chat_modes={(DEFAULT_CHAT, "claude"): "bypassPermissions"},
    )
    with capture_logs() as logs:
        log_trigger_permission_risks(risks, warned=set(), reason="reload")
    (event,) = (e for e in logs if e["event"] == "trigger.webhook.preapproved_tools")
    assert event["mode"] == "bypassPermissions"
    assert event["origin"] == "chat"
    assert event["outcome"] == "runs_as_default"
    assert event["reason"] == "reload"
    assert "explicitly" in event["note"]


@pytest.mark.parametrize("explicit", [False, True], ids=["implicit", "explicit"])
@pytest.mark.parametrize("skip", [False, True], ids=["noskip", "skip"])
@pytest.mark.parametrize(
    "claude_mode", [None, "default", "plan", "bypassPermissions"], ids=str
)
@pytest.mark.parametrize(
    "chat_mode",
    [None, "default", "manual", "acceptEdits", "plan", "auto", "bypassPermissions"],
    ids=str,
)
def test_audit_matches_runner_allowlist(
    chat_mode: str | None, claude_mode: str | None, skip: bool, explicit: bool
) -> None:
    """The audit flags a webhook exactly when the runner sends a risky
    ``--allowedTools`` for that webhook's run (#1145: the two can't drift)."""
    runner = _claude(
        permission_mode=claude_mode,
        allowed_tools=list(EXPLICIT_BASH),
        allowed_tools_explicit=explicit,
        dangerously_skip_permissions=skip,
    )
    opts = EngineRunOptions(permission_mode=chat_mode, unattended_trigger=WEBHOOK)
    with capture_logs(), apply_run_options(opts):
        args = runner._build_args("hi", None)
    risks = _audit(
        _triggers(webhooks=[_hook(project="myapp")]),
        defaults=TriggerEngineDefaults(
            claude_mode=claude_mode,
            claude_skip_permissions=skip,
            claude_allowed_tools=EXPLICIT_BASH,
            claude_allowed_tools_explicit=explicit,
        ),
        chat_modes={(DEFAULT_CHAT, "claude"): chat_mode},
    )
    flagged = bool(_events(risks, "trigger.webhook.preapproved_tools"))
    assert flagged == ("--allowedTools" in args)


@pytest.mark.parametrize(
    ("mode", "explicit", "expected"),
    [
        (None, False, True),
        ("plan", False, True),
        ("auto", False, True),
        ("bypassPermissions", False, True),
        ("default", False, False),
        ("manual", False, False),
        ("acceptEdits", False, False),
        ("default", True, True),
        ("manual", True, True),
        ("acceptEdits", True, True),
    ],
)
def test_claude_sends_allowed_tools(
    mode: str | None, explicit: bool, expected: bool
) -> None:
    from untether.runners.run_options import claude_sends_allowed_tools

    assert claude_sends_allowed_tools(mode, explicit=explicit) is expected


@pytest.mark.anyio
async def test_loop_audit_reads_runner_allowed_tools_explicit() -> None:
    """``_audit_trigger_permissions`` passes the runner's explicit flag, so an
    explicit list in an acceptEdits chat is flagged at startup (#1145)."""
    from types import SimpleNamespace

    from untether.telegram.loop import _audit_trigger_permissions

    runner = _claude(
        permission_mode="plan",
        allowed_tools=list(EXPLICIT_BASH),
        allowed_tools_explicit=True,
    )

    class _Prefs:
        async def get_engine_override(self, chat_id: int, engine: str) -> Any:
            return EngineOverrides(permission_mode="acceptEdits")

    runtime = SimpleNamespace(
        engine_ids=("claude",),
        config_path=None,
        resolve_engine=lambda engine_override, context: engine_override or "claude",
        resolve_runner=lambda resume_token, engine_override: SimpleNamespace(
            runner=runner
        ),
    )
    cfg = SimpleNamespace(runtime=runtime, chat_id=DEFAULT_CHAT)
    state = SimpleNamespace(chat_prefs=_Prefs(), trigger_permission_risks_warned=set())
    with capture_logs() as logs:
        await _audit_trigger_permissions(
            cfg,  # type: ignore[arg-type]
            state,  # type: ignore[arg-type]
            _triggers(webhooks=[_hook(project="myapp")]),
            reason="startup",
        )
    assert not [e for e in logs if e["event"] == "trigger.permission_audit.failed"]
    (event,) = (e for e in logs if e["event"] == "trigger.webhook.preapproved_tools")
    assert event["mode"] == "acceptEdits"
    assert event["tools"] == ["Bash"]
