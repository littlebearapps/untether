from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class RunContext:
    project: str | None = None
    branch: str | None = None
    # rc4 (#271): trigger_source is set when a run is initiated by a cron
    # or webhook (e.g. "cron:daily-review", "webhook:github-push") so the
    # Telegram meta footer can show the provenance.
    trigger_source: str | None = None
    # #330: per-cron permission_mode override. When a cron with a
    # permission_mode field fires, the dispatcher sets this; run_job
    # applies it on top of the resolved EngineRunOptions so the cron
    # override wins over the chat's /planmode default.
    permission_mode: str | None = None
    # #743: per-cron model / reasoning overrides, applied the same way.
    model: str | None = None
    reasoning: str | None = None
    # #1120: one id per trigger fire (uuid4), exported to the child as
    # ``UNTETHER_RUN_ID`` and bound into the run's log context.
    run_id: str | None = None
    # #1118: a trigger's own Claude allowlist (``()`` = pre-approve nothing).
    allowed_tools: tuple[str, ...] | None = None
    # #1118: a webhook fired under ``[triggers] webhook_defaults = "strict"``;
    # ``_apply_trigger_overrides`` applies the engine's safe defaults.
    strict_webhook: bool = False
    # #1121: a trigger's own env vars (added by ``apply_run_env``, never over
    # an existing name) and its Claude ``--agent``.
    env: tuple[tuple[str, str], ...] = ()
    agent: str | None = None
    # #1125: a trigger's run budgets (Claude turn / cost caps; wall-clock for
    # every engine, enforced by the bridge).
    max_turns: int | None = None
    max_budget_usd: float | None = None
    max_elapsed_s: int | None = None
    # #1220: the quiet-end sentinel when ``quiet_unless_output`` is on.
    quiet_sentinel: str | None = None


# #751 / #835: trigger sources with nobody present to answer a Telegram
# prompt. `at:` is excluded on purpose: a human scheduled it from the chat and
# is around to tap (#751 Decision 7). `loop:` re-fires follow a human's /loop.
UNATTENDED_TRIGGER_PREFIXES: tuple[str, ...] = ("cron:", "webhook:")
# #1142: a webhook's prompt is built from the request payload, so it never
# inherits Claude's bypass mode (crons keep theirs; the audit flags them).
WEBHOOK_TRIGGER_PREFIX = "webhook:"


def unattended_trigger(context: RunContext | None) -> str | None:
    """The trigger source when *context* is an unattended run, else None.

    The single predicate for both the #751 dispatch warning and the #835
    fail-closed enforcement, so the two can't drift. Only dispatchers set
    ``trigger_source``; a Telegram message, reply or directive can't.
    """
    if context is None:
        return None
    source = context.trigger_source
    if source and source.startswith(UNATTENDED_TRIGGER_PREFIXES):
        return source
    return None


def attended_context(context: RunContext | None) -> RunContext | None:
    """*context* with the trigger-only fields cleared (#835).

    For a human message that reuses a trigger run's context — a reply to a
    running cron/webhook turn's progress or wake-turn message. The human
    keeps the run's project/branch but not its provenance or its per-trigger
    ``permission_mode``/``model``/``reasoning``: their turn resolves the
    chat's own options, so it never inherits "unattended" (approvals
    auto-denied) and never lands in the cron's live process, whose spawn
    options no longer match (``options_changed`` → attended resume).
    """
    if context is None:
        return None
    # #1120: generic, so a field added later can't leak into a human turn.
    if replace(context, project=None, branch=None) == RunContext():
        return context
    return RunContext(project=context.project, branch=context.branch)
