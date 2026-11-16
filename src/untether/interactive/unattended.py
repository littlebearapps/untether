"""What an unattended run tells the model when nobody can approve (#835, #960).

Moved verbatim out of ``runners/claude.py``. Cron and webhook runs have no one
to tap a button, so a request that needs a human is denied with this text
instead (the queueing around it stays in each runner).
"""

from __future__ import annotations


def unattended_deny_message(trigger: str, tool: str, reason: str) -> str:
    kind = "webhook" if trigger.startswith("webhook:") else "scheduled"
    head = f"Untether: this is an unattended {kind} run ({trigger}) — "
    if tool == "ExitPlanMode":
        return (
            head + "nobody can approve a plan. Do not call ExitPlanMode again. "
            "Finish by giving the complete plan as your final answer."
        )
    if tool == "AskUserQuestion":
        return (
            head + "nobody can answer questions. Proceed with reasonable "
            "defaults and state your assumptions."
        )
    if reason == "diff_preview":
        what = f"nobody can review this change, so {tool} was denied"
    elif reason == "plan_mode":
        what = (
            f"it runs in plan mode and nobody can approve a plan, so {tool} was denied"
        )
    else:
        what = f"nobody is available to approve {tool}, so it was denied"
    return (
        head + what + ". Continue only with actions that don't need approval; "
        "if the task can't be finished without it, stop and report what you "
        "would have done and why it needs approval."
    )
