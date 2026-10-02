from __future__ import annotations

from dataclasses import dataclass


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


# #751 / #835: trigger sources with nobody present to answer a Telegram
# prompt. `at:` is excluded on purpose: a human scheduled it from the chat and
# is around to tap (#751 Decision 7). `loop:` re-fires follow a human's /loop.
UNATTENDED_TRIGGER_PREFIXES: tuple[str, ...] = ("cron:", "webhook:")


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
