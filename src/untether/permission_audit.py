"""Config-time audit of Claude permission modes (#751).

Runs from :func:`untether.runtime_loader.build_runtime_spec`, i.e. once at
startup and on every config reload, against the **parsed** config graph: the
validated ``[engines.claude] permission_mode`` (read from the router's Claude
runner) and the ``[[triggers.crons]]`` entries, each resolved to its engine
the same way :meth:`TransportRuntime.resolve_engine` does at dispatch.

Three findings, each logged as one WARN per change:

* ``claude.permission_mode.auto_semantics_changed`` — ``auto`` in engine
  config or on a cron. Its meaning changed in 0.35.5rc8 (#741): it used to be
  Untether's plan-mode sugar (now ``plan-auto``) and is now Claude Code's own
  classifier-gated mode. Log-only (Decision 1); **sunset 0.37.0** (Decision 2;
  moved from 0.36.0 by #947 so 0.35.4 upgraders still see it).
* ``trigger.unattended_approval_risk`` (``phase=config``) — a cron whose
  explicit mode waits for a Telegram tap (``default`` / ``manual`` /
  ``acceptEdits`` for a tool approval, ``plan`` for the plan approval).
  Spent ``run_once`` crons are skipped: they no longer fire (Decision 10).
  Crons that inherit a mode are covered at dispatch time instead
  (``telegram/loop.py``, Decision 3).
* ``trigger.cron.permission_mode_invalid`` — a cron with no ``engine`` whose
  resolved engine is Claude and whose mode Claude does not accept. The cron
  validator can't catch it (it doesn't know the default engine), and a
  ``ConfigError`` would disable every trigger, so this is a WARN (Decision 6).

#1142 adds a trigger security audit (bottom of this module), run from the
Telegram loop at startup and on each trigger reload because it needs each
trigger chat's ``/planmode``: ``trigger.cron.inherits_bypass``,
``trigger.webhook.no_project`` and ``trigger.webhook.preapproved_tools``.

The audit function is pure; :func:`log_permission_audit` owns the dedupe: a
finding set is logged when it differs from the last one logged, so an
unrelated reload is silent, a reload that adds an entry re-emits the full
list with ``reason="reload"``, and a reload that clears every entry resets
silently. TOML is never rewritten.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .logging import get_logger
from .runners.run_options import (
    CLAUDE_PLAN_AUTO_MODE,
    LEGACY_CLAUDE_PLAN_AUTO_MODE,
    VALID_PERMISSION_MODES_BY_ENGINE,
    claude_tap_waits_for,
    is_claude_prompting_mode,
)

if TYPE_CHECKING:
    from .triggers.settings import TriggersSettings

logger = get_logger(__name__)

CLAUDE_ENGINE = "claude"
ENGINE_CONFIG_ENTRY = "engines.claude"
# nsd runs ~20 deliberate `auto` crons: keep the event bounded, `count` has
# the true total.
MAX_LOGGED_ENTRIES = 50

AUTO_SEMANTICS_NOTE = (
    "permission_mode = 'auto' now selects Claude Code's own auto mode"
    " (classifier-gated, no plan gate). Set"
    f" permission_mode = '{CLAUDE_PLAN_AUTO_MODE}' to keep the previous"
    " behaviour (plan mode + auto-approved ExitPlanMode)."
)

# (engine override, project alias) -> engine id
EngineResolver = Callable[[str | None, str | None], str]


@dataclass(frozen=True, slots=True)
class PermissionAudit:
    """What :func:`audit_claude_permission_modes` found."""

    # "engines.claude" and/or "triggers.crons[<id>]"
    auto_entries: tuple[str, ...] = ()
    # ("cron:<id>", mode)
    unattended: tuple[tuple[str, str], ...] = ()
    # ("cron:<id>", mode)
    invalid: tuple[tuple[str, str], ...] = ()
    # #836: ("cron:<id>", engine mode) — a Claude cron with no
    # ``permission_mode`` while the engine default asks for a tap. It takes
    # the chat's /planmode first (unknowable here), so this is a nudge to set
    # the mode explicitly; only the startup message shows it (not logged —
    # the dispatch-time WARN sees the real resolved mode).
    inherited: tuple[tuple[str, str], ...] = ()

    @property
    def empty(self) -> bool:
        return not (
            self.auto_entries or self.unattended or self.invalid or self.inherited
        )


def make_engine_resolver(
    *, default_engine: str, project_engines: Mapping[str, str | None]
) -> EngineResolver:
    """Mirror :meth:`TransportRuntime.resolve_engine` for a trigger:
    explicit ``engine`` → the project's ``default_engine`` → the default."""

    def resolve(engine: str | None, project: str | None) -> str:
        if engine is not None:
            return engine
        if project is None:
            return default_engine
        return project_engines.get(project) or default_engine

    return resolve


def audit_claude_permission_modes(
    *,
    engine_mode: str | None,
    triggers: TriggersSettings | None,
    resolve_engine: EngineResolver,
    spent_cron_ids: Collection[str] = (),
) -> PermissionAudit:
    """Pure: no I/O, no logging. Crons count only when triggers are enabled."""
    auto_entries: list[str] = []
    unattended: list[tuple[str, str]] = []
    invalid: list[tuple[str, str]] = []
    inherited: list[tuple[str, str]] = []
    engine_waits = engine_mode is not None and claude_tap_waits_for(engine_mode)
    if engine_mode == LEGACY_CLAUDE_PLAN_AUTO_MODE:
        auto_entries.append(ENGINE_CONFIG_ENTRY)
    allowed = VALID_PERMISSION_MODES_BY_ENGINE[CLAUDE_ENGINE]
    if triggers is not None and triggers.enabled:
        spent = set(spent_cron_ids)
        for cron in triggers.crons:
            mode = cron.permission_mode
            if mode is None:
                if (
                    engine_waits
                    and engine_mode is not None
                    and resolve_engine(cron.engine, cron.project) == CLAUDE_ENGINE
                    and not (cron.run_once and cron.id in spent)
                ):
                    inherited.append((f"cron:{cron.id}", engine_mode))
                continue
            if resolve_engine(cron.engine, cron.project) != CLAUDE_ENGINE:
                continue
            trigger = f"cron:{cron.id}"
            if mode not in allowed:
                # With an explicit `engine = "claude"` the cron validator
                # already rejected it; only the resolved case reaches here.
                invalid.append((trigger, mode))
                continue
            if mode == LEGACY_CLAUDE_PLAN_AUTO_MODE:
                auto_entries.append(f"triggers.crons[{cron.id}]")
            if claude_tap_waits_for(mode) is not None and not (
                cron.run_once and cron.id in spent
            ):
                unattended.append((trigger, mode))
    return PermissionAudit(
        auto_entries=tuple(auto_entries),
        unattended=tuple(unattended),
        invalid=tuple(invalid),
        inherited=tuple(inherited),
    )


# #836: the startup-message line. One constant per wording so the CHANGELOG,
# docs and tests can't disagree; the fallback is for a build without #835.
UNATTENDED_LINE_LABEL = "unattended approvals (auto-denied)"
UNATTENDED_LINE_LABEL_WAITS = "unattended approvals (wait for a tap)"


def format_unattended_entries(
    audit: PermissionAudit, *, limit: int = 3, label: str = UNATTENDED_LINE_LABEL
) -> str | None:
    """#836: ``_<label>:_ `cron:a (default), cron:b (inherits plan)` +N more``.

    Explicit tap-waiting crons first, then crons that inherit a tap-waiting
    engine default. ``None`` when there is nothing to show. Cron ids are
    free-form: backticks become ``'`` so the inline code span can't break,
    and ``+N more`` sits outside it.
    """
    items = [f"{trigger} ({mode})" for trigger, mode in audit.unattended]
    items += [f"{trigger} (inherits {mode})" for trigger, mode in audit.inherited]
    if not items:
        return None
    shown = [item.replace("`", "'") for item in items[:limit]]
    more = len(items) - len(shown)
    suffix = f" +{more} more" if more > 0 else ""
    return f"_{label}:_ `{', '.join(shown)}`{suffix}"


# Last finding set logged per kind (process-wide; startup + reloads).
_LAST_LOGGED: dict[str, frozenset[Any]] = {}


def reset_permission_audit_state() -> None:
    """Tests: forget what was logged."""
    _LAST_LOGGED.clear()


def _changed(kind: str, items: tuple[Any, ...]) -> bool:
    fingerprint = frozenset(items)
    if not fingerprint:
        _LAST_LOGGED.pop(kind, None)
        return False
    if _LAST_LOGGED.get(kind) == fingerprint:
        return False
    _LAST_LOGGED[kind] = fingerprint
    return True


def log_permission_audit(
    audit: PermissionAudit, *, config_path: Path | str | None, reason: str
) -> None:
    """Log each finding kind whose set changed since it was last logged."""
    path = str(config_path) if config_path is not None else None
    if _changed("auto", audit.auto_entries):
        logger.warning(
            "claude.permission_mode.auto_semantics_changed",
            entries=list(audit.auto_entries[:MAX_LOGGED_ENTRIES]),
            count=len(audit.auto_entries),
            reason=reason,
            config_path=path,
            note=AUTO_SEMANTICS_NOTE,
        )
    if _changed("unattended", audit.unattended):
        logger.warning(
            "trigger.unattended_approval_risk",
            phase="config",
            entries=[
                {
                    "trigger": trigger,
                    "mode": mode,
                    "waits_for": claude_tap_waits_for(mode),
                }
                for trigger, mode in audit.unattended[:MAX_LOGGED_ENTRIES]
            ],
            count=len(audit.unattended),
            reason=reason,
            config_path=path,
            note=(
                "these crons run in a mode that asks for a Telegram tap, so"
                " those approvals will be auto-denied (#835); set"
                " permission_mode to plan-auto, auto, dontAsk or"
                " bypassPermissions, or pre-approve the tools"
            ),
        )
    if _changed("invalid", audit.invalid):
        allowed = sorted(VALID_PERMISSION_MODES_BY_ENGINE[CLAUDE_ENGINE])
        for trigger, mode in audit.invalid:
            logger.warning(
                "trigger.cron.permission_mode_invalid",
                trigger=trigger,
                mode=mode,
                allowed=allowed,
                reason=reason,
                config_path=path,
                note="the cron resolves to Claude, which will reject this mode at spawn",
            )


def audit_runtime_permission_modes(
    *,
    raw_triggers: object,
    engine_mode: str | None,
    resolve_engine: EngineResolver,
    config_path: Path,
    reason: str,
) -> PermissionAudit:
    """The ``build_runtime_spec`` entry point: parse, audit, log.

    A trigger section that fails to parse skips the cron checks
    (``triggers.init_failed`` owns that error); engine config is still
    audited. Never raises for bad config.
    """
    triggers: TriggersSettings | None = None
    if isinstance(raw_triggers, dict):
        # Lazy: keep runtime_loader import-light and avoid a cycle through
        # the triggers package (same posture as triggers/settings.py).
        from .triggers.settings import parse_trigger_config

        try:
            triggers = parse_trigger_config(raw_triggers)
        except (ValueError, TypeError):
            triggers = None
    spent: Collection[str] = ()
    if triggers is not None and any(c.run_once for c in triggers.crons):
        from .triggers.run_once_state import load_fired_state, resolve_state_path

        spent = set(load_fired_state(resolve_state_path(config_path)))
    audit = audit_claude_permission_modes(
        engine_mode=engine_mode,
        triggers=triggers,
        resolve_engine=resolve_engine,
        spent_cron_ids=spent,
    )
    log_permission_audit(audit, config_path=config_path, reason=reason)
    return audit


# ── #1142: trigger security audit (startup + every trigger reload) ──────────
#
# Run from ``telegram/loop.py`` beside #894's chat-fallback warning, where the
# trigger's chat and that chat's ``/planmode`` are known. Warnings only: in
# 0.36.1 nothing is rejected (the breaking half is #1118, 0.36.2).

CLAUDE_BYPASS_MODE = "bypassPermissions"
ANTIGRAVITY_ENGINE = "antigravity"
ANTIGRAVITY_FULL_MODE = "full"
# Pre-approving any of these on a webhook lets a payload-built prompt run
# shell commands or change files with nobody asked.
RISKY_PREAPPROVED_TOOLS: tuple[str, ...] = ("Bash", "Edit", "Write")

CRON_INHERITS_BYPASS = "trigger.cron.inherits_bypass"
WEBHOOK_NO_PROJECT = "trigger.webhook.no_project"
WEBHOOK_PREAPPROVED_TOOLS = "trigger.webhook.preapproved_tools"

_RISK_NOTES: dict[str, str] = {
    CRON_INHERITS_BYPASS: (
        "this cron inherits a mode that skips approvals from the chat's"
        " permission mode or untether.toml (Claude keeps it; Antigravity runs it as"
        " Workspace); set permission_mode on the cron to make the choice"
        " explicit (a future release stops crons inheriting it, #1118)"
    ),
    WEBHOOK_NO_PROJECT: (
        "this agent_run webhook has no project, so it runs in Untether's own"
        " working directory; set project to the repo it should work in"
        " (#1118 makes this an error)"
    ),
    WEBHOOK_PREAPPROVED_TOOLS: (
        "this agent_run webhook runs in a mode that never asks while these"
        " tools are pre-approved, so a prompt built from the request payload"
        " can run commands or edit files; narrow [engines.claude]"
        " allowed_tools or use a prompting mode"
    ),
}


@dataclass(frozen=True, slots=True)
class TriggerEngineDefaults:
    """Engine-config permission settings a trigger run falls back to."""

    claude_mode: str | None = None
    claude_skip_permissions: bool = False
    claude_allowed_tools: tuple[str, ...] = ()
    antigravity_mode: str | None = None


@dataclass(frozen=True, slots=True)
class TriggerRisk:
    """One #1142 finding; hashable so the caller can log each once."""

    event: str
    trigger: str  # "cron:<id>" / "webhook:<id>"
    engine: str | None = None
    mode: str | None = None
    origin: str | None = None  # chat / engine_config / dangerously_skip_permissions
    chat_id: int | None = None
    tools: tuple[str, ...] = ()
    outcome: str | None = None


# (chat_id, engine) -> that chat's /planmode for the engine, or None
ChatModeLookup = Callable[[int, str], str | None]


def _agent_triggers(
    triggers: TriggersSettings,
) -> list[tuple[str, Any]]:
    """Crons and ``agent_run`` webhooks, as ``(kind, config)``."""
    found: list[tuple[str, Any]] = [("cron", cron) for cron in triggers.crons]
    found += [
        ("webhook", hook) for hook in triggers.webhooks if hook.action == "agent_run"
    ]
    return found


def trigger_chat_engine_pairs(
    triggers: TriggersSettings,
    *,
    default_chat_id: int,
    resolve_engine: EngineResolver,
) -> set[tuple[int, str]]:
    """The ``(chat_id, engine)`` pairs whose chat preference the audit reads.

    A trigger run has no topic, so only the chat-level ``/planmode`` applies
    (``run_job`` passes ``thread_id=None``)."""
    return {
        (
            trigger.chat_id or default_chat_id,
            resolve_engine(trigger.engine, trigger.project),
        )
        for _kind, trigger in _agent_triggers(triggers)
    }


def _preapproved_risky_tools(allowed: tuple[str, ...]) -> tuple[str, ...]:
    """Allowlist entries that pre-approve Bash/Edit/Write outright (a bare
    name or ``Name(*)``; a command pattern like ``Bash(git:*)`` is narrower)."""
    risky = {
        spelling
        for tool in RISKY_PREAPPROVED_TOOLS
        for spelling in (tool, f"{tool}(*)")
    }
    return tuple(name for entry in allowed if (name := entry.strip()) in risky)


def audit_trigger_permission_risks(
    triggers: TriggersSettings | None,
    *,
    default_chat_id: int,
    resolve_engine: EngineResolver,
    defaults: TriggerEngineDefaults,
    chat_mode: ChatModeLookup,
    spent_cron_ids: Collection[str] = (),
) -> tuple[TriggerRisk, ...]:
    """Pure: no I/O, no logging. Nothing when triggers are disabled."""
    if triggers is None or not triggers.enabled:
        return ()
    spent = set(spent_cron_ids)
    risks: list[TriggerRisk] = []
    for kind, trigger in _agent_triggers(triggers):
        source = f"{kind}:{trigger.id}"
        engine = resolve_engine(trigger.engine, trigger.project)
        chat_id = trigger.chat_id or default_chat_id
        pref = chat_mode(chat_id, engine)
        if kind == "cron":
            if trigger.run_once and trigger.id in spent:
                continue
            risk = _cron_bypass_risk(trigger, source, engine, chat_id, pref, defaults)
            if risk is not None:
                risks.append(risk)
            continue
        if trigger.project is None:
            risks.append(
                TriggerRisk(event=WEBHOOK_NO_PROJECT, trigger=source, engine=engine)
            )
        risk = _webhook_tools_risk(source, engine, chat_id, pref, defaults)
        if risk is not None:
            risks.append(risk)
    return tuple(risks)


def _cron_bypass_risk(
    cron: Any,
    source: str,
    engine: str,
    chat_id: int,
    pref: str | None,
    defaults: TriggerEngineDefaults,
) -> TriggerRisk | None:
    own = cron.permission_mode
    if engine == CLAUDE_ENGINE:
        if defaults.claude_skip_permissions:
            # The opt-in key overrides every mode, the cron's own included.
            mode, origin = CLAUDE_BYPASS_MODE, "dangerously_skip_permissions"
        elif own is not None:
            return None
        elif pref is not None:
            mode, origin = pref, "chat"
        else:
            mode, origin = defaults.claude_mode, "engine_config"
        if mode != CLAUDE_BYPASS_MODE:
            return None
        outcome = "runs_with_bypass"
    elif engine == ANTIGRAVITY_ENGINE:
        if own is not None:
            return None
        if pref is not None:
            mode, origin = pref, "chat"
        else:
            mode, origin = defaults.antigravity_mode, "engine_config"
        if mode != ANTIGRAVITY_FULL_MODE:
            return None
        # agy's own guard (REVIEW M3) already runs it as Workspace.
        outcome = "runs_as_workspace"
    else:
        return None
    return TriggerRisk(
        event=CRON_INHERITS_BYPASS,
        trigger=source,
        engine=engine,
        mode=mode,
        origin=origin,
        chat_id=chat_id,
        outcome=outcome,
    )


def _webhook_tools_risk(
    source: str,
    engine: str,
    chat_id: int,
    pref: str | None,
    defaults: TriggerEngineDefaults,
) -> TriggerRisk | None:
    if engine != CLAUDE_ENGINE:
        return None
    if pref is not None:
        mode, origin = pref, "chat"
    else:
        mode, origin = defaults.claude_mode, "engine_config"
    if defaults.claude_skip_permissions or mode == CLAUDE_BYPASS_MODE:
        # The runtime guard runs it in `default`, which prompts (#1142).
        return None
    if is_claude_prompting_mode(mode):
        return None  # the allowlist isn't sent in prompting modes (#749)
    tools = _preapproved_risky_tools(defaults.claude_allowed_tools)
    if not tools:
        return None
    return TriggerRisk(
        event=WEBHOOK_PREAPPROVED_TOOLS,
        trigger=source,
        engine=engine,
        mode=mode,
        origin=origin,
        chat_id=chat_id,
        tools=tools,
    )


def log_trigger_permission_risks(
    risks: Collection[TriggerRisk], *, warned: set[TriggerRisk], reason: str
) -> list[TriggerRisk]:
    """Log each finding not already in *warned* (startup, then reloads that
    change it); returns the newly logged ones."""
    new: list[TriggerRisk] = []
    for risk in risks:
        if risk in warned:
            continue
        warned.add(risk)
        new.append(risk)
        kind, _, trigger_id = risk.trigger.partition(":")
        fields: dict[str, Any] = {
            f"{kind}_id": trigger_id,
            "engine": risk.engine,
            "reason": reason,
            "note": _RISK_NOTES[risk.event],
        }
        if risk.event != WEBHOOK_NO_PROJECT:
            fields.update(mode=risk.mode, origin=risk.origin, chat_id=risk.chat_id)
        if risk.tools:
            fields["tools"] = list(risk.tools)
        if risk.outcome is not None:
            fields["outcome"] = risk.outcome
        logger.warning(risk.event, **fields)
    return new
