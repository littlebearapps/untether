from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EngineRunOptions:
    model: str | None = None
    reasoning: str | None = None
    permission_mode: str | None = None
    ask_questions: bool | None = None
    diff_preview: bool | None = None
    show_api_cost: bool | None = None
    show_subscription_usage: bool | None = None
    show_resume_line: bool | None = None
    budget_enabled: bool | None = None
    budget_auto_cancel: bool | None = None
    # #289 — per-chat /loop and ScheduleWakeup observation toggle.  ``None``
    # means "follow global ``[loop] enabled``"; True/False is an explicit
    # per-chat override set via ``/config → 🔁 Loop mode``.
    loop_enabled: bool | None = None
    # Native, runner-specific attachments. Telegram populates this only after
    # resolving the effective engine to Codex; other runners ignore the field.
    image_paths: tuple[str, ...] = ()
    # #416 — a stored reasoning level the engine no longer allows, dropped by
    # ``drop_unsupported_reasoning`` at resolution time. Only carries the
    # dropped value to the executor's one-line note; runners never read it.
    ignored_reasoning: str | None = None


# Permission modes the Claude Code CLI accepts for ``--permission-mode``.
#
# Derived from **CLI 2.1.228** (verified on lba-1, 2026-08-12) by reading
# ``claude --help`` and then spawn-probing every value.  Two quirks the help
# text alone would get wrong (#742):
#
#   * ``manual`` is a documented *alias* for ``default`` (CLI v2.1.200+).
#     ``--help`` lists ``manual`` in place of ``default``, but the binary
#     accepts both — a probe of ``--permission-mode default`` exits 0.
#   * an unknown value exits 1 with commander's "Allowed choices are ..."
#     usage error, which ``tests/test_claude_permission_modes.py`` parses to
#     detect drift automatically.
#
# Semantics (docs.claude.com "Choose a permission mode", read 2026-08-12):
#   default / manual  — reads only; everything else prompts
#   acceptEdits       — reads, in-scope file edits, common filesystem commands
#   plan              — research only; edits blocked until the plan is approved
#   auto              — classifier-gated; approves routine work, hard-denies
#                       exfiltration.  Becomes the CLI default for new Pro/Max/
#                       Team sessions on 2026-08-14
#   dontAsk           — auto-denies anything that would prompt; only
#                       pre-approved tools run
#   bypassPermissions — skips all checks
CLAUDE_CLI_PERMISSION_MODES: frozenset[str] = frozenset(
    {
        "default",
        "manual",
        "plan",
        "auto",
        "acceptEdits",
        "dontAsk",
        "bypassPermissions",
    }
)

# Untether-only sugar: CLI ``plan`` **plus** auto-approval of the
# ``ExitPlanMode`` control request, so a plan-mode run never stalls waiting
# for a Telegram tap.  Spelled ``auto`` before 0.35.5rc8, which shadowed the
# CLI's own ``auto`` mode and made it unreachable (#741).
CLAUDE_PLAN_AUTO_MODE = "plan-auto"

# The pre-0.35.5rc8 spelling of :data:`CLAUDE_PLAN_AUTO_MODE`.  Still present
# in ``chat_prefs.json`` on deployed hosts; rewritten there exactly once, at
# first load, by ``ChatPrefsStore._migrate_permission_modes_locked``.  The
# one-shot guard matters: after the rename a user can legitimately *choose*
# ``auto`` from ``/planmode`` or ``/config``, so a per-read rewrite would make
# the CLI's own mode unreachable through the UI.  In TOML-authored config the
# value is NOT rewritten — it now means the CLI's own ``auto``.
# ``build_runtime_spec`` logs one aggregated
# ``claude.permission_mode.auto_semantics_changed`` WARN at startup, and again
# whenever a config reload changes the set of affected entries (engine config
# and crons); see ``untether.permission_audit`` (#751).  Sunset: 0.36.0.
LEGACY_CLAUDE_PLAN_AUTO_MODE = "auto"

# Canonical per-engine permission_mode value sets. Used by trigger config
# validators and by the Claude engine-config loader to reject typos at parse
# time, while staying forward-compatible for engines not yet listed (the
# validator accepts any non-empty string for those).
# Extending this dict requires auditing the runner to ensure each value maps to
# a defined CLI / protocol outcome — see issues #331 (Codex + Gemini completion)
# and #332 (full cross-engine extension).
VALID_PERMISSION_MODES_BY_ENGINE: dict[str, frozenset[str]] = {
    "claude": CLAUDE_CLI_PERMISSION_MODES | {CLAUDE_PLAN_AUTO_MODE},
}


def claude_cli_permission_mode(mode: str | None) -> str | None:
    """Map an Untether permission mode onto the value the CLI accepts.

    Only :data:`CLAUDE_PLAN_AUTO_MODE` is Untether-invented; every other value
    is a genuine CLI mode and passes through untouched.  Before #741 this
    function's job was done inline by ``"plan" if mode == "auto" else mode``,
    which silently swallowed the CLI's own ``auto``.
    """
    if mode is None:
        return None
    if mode == CLAUDE_PLAN_AUTO_MODE:
        return "plan"
    return mode


def is_claude_plan_auto(mode: str | None) -> bool:
    """True when *mode* arms Untether's ExitPlanMode rubber stamp.

    Deliberately false for the CLI's ``auto``: that mode has no plan gate to
    rubber-stamp, and conflating the two is the bug #741 fixes.
    """
    return mode == CLAUDE_PLAN_AUTO_MODE


# Modes whose stated purpose is to ask the user before acting.  Untether must
# neither pre-approve their tools at stage 5 (``--allowedTools``) nor
# blanket-approve their stage-6 ``canUseTool`` requests (#749).
_CLAUDE_PROMPTING_MODES: frozenset[str] = frozenset(
    {"default", "manual", "acceptEdits"}
)


def is_claude_prompting_mode(mode: str | None) -> bool:
    """True when *mode* promises the user a permission prompt (#749).

    Enumerated rather than derived, so adding a CLI mode is a deliberate
    classification rather than an accident of a predicate.  Membership drives
    two things: whether ``--allowedTools`` is sent, and whether the control
    handler gates every tool or only ``ExitPlanMode``/``AskUserQuestion``.

    ``plan`` and ``plan-auto`` are **False** despite plan mode being
    interactive.  Probe G (2026-08-13, CLI 2.1.228) showed plan mode blocks a
    ``Write`` *internally* — the file was never created and no ``can_use_tool``
    ever surfaced — so it does not need Untether's gate to be safe.  Probes H/I
    showed the opposite: dropping the allowlist there turns every ``Read`` into
    a stage-6 round-trip Untether approves anyway.  Gating plan mode would buy
    no safety and cost an approval button per tool in the fleet's most-used
    mode.  See docs/findings/2026-08-13-claude-permission-modes.md.
    **Probe G no longer holds on CLI 2.1.285**: plan mode raises a
    ``can_use_tool`` (``decision_reason_type: "mode"``) for a non-plan-file
    ``Write`` instead of blocking it, and this classification lets stage 6
    approve it — see the 2026-09-30 findings addenda (#383, §Q3a). Open
    issue; not changed by #751.

    ``auto``, ``dontAsk`` and ``bypassPermissions`` are False because each
    resolves permissions elsewhere: ``auto`` at the stage-4 classifier,
    ``dontAsk`` by auto-denying, ``bypassPermissions`` by skipping checks.

    ``None`` (no mode configured) is False — that run takes the legacy ``-p``
    path with no control channel at all, so no stage-6 request can arrive.
    """
    return mode in _CLAUDE_PROMPTING_MODES


# Modes in which an unattended run (cron / webhook) stops and waits for a
# Telegram tap nobody is there to give (#751): the prompting modes wait for a
# tool approval, and ``plan`` waits for the ``ExitPlanMode`` approval, which
# is never auto-approved.  ``plan-auto`` is excluded on purpose — its whole
# point is that the plan gate is rubber-stamped.  Derived from
# ``_CLAUDE_PROMPTING_MODES`` so the two cannot drift silently.
CLAUDE_TAP_REQUIRED_MODES: frozenset[str] = _CLAUDE_PROMPTING_MODES | {"plan"}


def claude_tap_waits_for(mode: str | None) -> str | None:
    """What an unattended run in *mode* would wait for, or ``None`` (#751)."""
    if mode not in CLAUDE_TAP_REQUIRED_MODES:
        return None
    return "plan approval" if mode == "plan" else "tool approval"


def normalise_claude_cli_mode(mode: str | None) -> str | None:
    """The mode the CLI reports in ``system/init.permissionMode`` for *mode*.

    ``plan-auto`` → ``plan`` (Untether sugar) and ``manual`` → ``default``
    (a CLI alias: probe P1b, CLI 2.1.285 — ``--permission-mode manual``
    reports ``default``).  Every other value is returned unchanged, so a
    requested and a reported mode compare equal exactly when the CLI honoured
    the request (#751).
    """
    cli_mode = claude_cli_permission_mode(mode)
    if cli_mode == "manual":
        return "default"
    return cli_mode


_RUN_OPTIONS: ContextVar[EngineRunOptions | None] = ContextVar(
    "untether.engine_run_options", default=None
)


def get_run_options() -> EngineRunOptions | None:
    return _RUN_OPTIONS.get()


def set_run_options(options: EngineRunOptions | None) -> Token:
    return _RUN_OPTIONS.set(options)


def reset_run_options(token: Token) -> None:
    _RUN_OPTIONS.reset(token)


@contextmanager
def apply_run_options(options: EngineRunOptions | None) -> Iterator[None]:
    token = set_run_options(options)
    try:
        yield
    finally:
        reset_run_options(token)
