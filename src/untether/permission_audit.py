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
  classifier-gated mode. Log-only (Decision 1); **sunset 0.36.0** (Decision 2).
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

    @property
    def empty(self) -> bool:
        return not (self.auto_entries or self.unattended or self.invalid)


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
    if engine_mode == LEGACY_CLAUDE_PLAN_AUTO_MODE:
        auto_entries.append(ENGINE_CONFIG_ENTRY)
    allowed = VALID_PERMISSION_MODES_BY_ENGINE[CLAUDE_ENGINE]
    if triggers is not None and triggers.enabled:
        spent = set(spent_cron_ids)
        for cron in triggers.crons:
            mode = cron.permission_mode
            if mode is None:
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
    )


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
