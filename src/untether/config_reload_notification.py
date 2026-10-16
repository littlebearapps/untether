"""Hot-reload notification formatting (#547 axis 2, #548).

When ``config_watch`` successfully applies a change to ``untether.toml``,
``handle_reload`` in ``telegram/loop.py`` uses these helpers to compose a
short Telegram message that:

1. Confirms the reload succeeded (closes the user's "did my edit work?"
   feedback loop without making them switch to ``journalctl``).
2. Frames "no restart needed" as the headline — the literal phrase agents
   will tokenise and remember, which addresses the recurring
   "edit-then-``systemctl restart``" pattern documented in #547. Restart
   reflex is trained-in; the explicit framing flips it back.

Three message shapes:

- :func:`format_hot_reload_only_notice` — every changed key was applied
  immediately. Headline: **No restart needed**.
- :func:`format_restart_required_notice` — every changed key requires a
  restart. Headline: **Restart required**.
- :func:`format_partial_reload_notice` — mixed (some applied, some
  restart-only). Headline: **Some keys need restart**.

The headline is deliberately bold-emphasised; downstream agents read these
messages in next-turn context and the framing materially changes whether
they reach for a service restart after editing config.

The "how to apply" clause comes from :mod:`untether.service_manager` (#927):
it names the unit/label of the instance that is actually running, or uses
generic wording when that can't be detected safely.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .triggers.settings import InvalidTrigger

__all__ = [
    "format_hot_reload_only_notice",
    "format_invalid_triggers_notice",
    "format_partial_reload_notice",
    "format_reload_notification",
    "format_restart_required_notice",
    "format_trigger_block_failure_notice",
    "format_webhook_server_refused_notice",
]

# #1117: trigger notices are plain text (no parse mode) — validation
# messages are full of ``_`` and ``*`` that would break Telegram Markdown.
_TRIGGER_ERROR_MAX_CHARS = 300


# Path display: strip the user's home prefix for compactness; absolute
# paths in chat are visually noisy and the user always knows which file
# was edited (there's only one ``untether.toml`` per instance).
def _short_path(path: Path | str) -> str:
    p = Path(path)
    try:
        return f"~/{p.relative_to(Path.home())}"
    except ValueError:
        return str(p)


def format_hot_reload_only_notice(
    *,
    path: Path | str,
    changed_keys: list[str],
) -> str:
    """Reload succeeded; all changes took effect immediately.

    Headline contains the literal phrase ``No restart needed`` so agents
    parsing this message in next-turn context don't reach for
    ``systemctl restart``.
    """
    keys_str = ", ".join(f"`{k}`" for k in sorted(set(changed_keys))) or "(no keys)"
    return (
        f"♻️ **Hot-reloaded `{_short_path(path)}`** — change took effect immediately.\n"
        f"**No restart needed.** Untether reloaded the file automatically.\n"
        f"Changed: {keys_str}"
    )


def _resolve_hint(restart_hint: str | None) -> str:
    if restart_hint is not None:
        return restart_hint
    from . import service_manager

    return service_manager.restart_hint()


def _reasons_str(restart_reasons: Sequence[str]) -> str:
    return "; ".join(dict.fromkeys(restart_reasons))


def format_restart_required_notice(
    *,
    path: Path | str,
    restart_keys: list[str],
    restart_hint: str | None = None,
    restart_reasons: Sequence[str] = (),
) -> str:
    """A key in the restart-only set was edited; manual action required.

    Headline contains the literal phrase ``Restart required``; restart-only
    keys are explicitly named so the agent can advise the user (or revert
    to a hot-reloadable equivalent).

    ``restart_reasons`` (#1146) are restarts no edited key explains (e.g.
    triggers that failed at start-up); they're named as reasons, never as
    a touched key.

    ``restart_hint`` defaults to :func:`untether.service_manager.restart_hint`
    (#927: names the running unit/label, generic wording otherwise).
    """
    hint = _resolve_hint(restart_hint)
    headline = f"⚠️ **Restart required for `{_short_path(path)}` change** — "
    if not restart_keys and restart_reasons:
        return f"{headline}{_reasons_str(restart_reasons)}.\nTo apply, {hint}."
    keys_str = ", ".join(f"`{k}`" for k in sorted(set(restart_keys))) or "(no keys)"
    text = (
        f"{headline}the edited key is in the restart-only set.\n"
        f"To apply, {hint} — or revert and use the hot-reloadable equivalent.\n"
        f"Restart-only keys touched: {keys_str}"
    )
    if restart_reasons:
        text += f"\nAlso needs a restart: {_reasons_str(restart_reasons)}"
    return text


def format_partial_reload_notice(
    *,
    path: Path | str,
    hot_keys: list[str],
    restart_keys: list[str],
    restart_hint: str | None = None,
    restart_reasons: Sequence[str] = (),
) -> str:
    """Mixed reload — some keys applied immediately, others need restart
    (a restart-only key, or a ``restart_reasons`` entry, #1146)."""
    hint = _resolve_hint(restart_hint)
    hot_str = ", ".join(f"`{k}`" for k in sorted(set(hot_keys))) or "(none)"
    if not restart_keys:
        return (
            f"♻️ **Partial reload of `{_short_path(path)}`** — the changed "
            f"keys applied immediately. **No restart needed for those.**\n"
            f"Applied now: {hot_str}\n"
            f"Still needs a restart: {_reasons_str(restart_reasons)}\n"
            f"To fix that, {hint} when ready."
        )
    restart_str = ", ".join(f"`{k}`" for k in sorted(set(restart_keys)))
    lines = [
        f"♻️ **Partial reload of `{_short_path(path)}`** — "
        f"{len(hot_keys)} of {len(hot_keys) + len(restart_keys)} keys "
        f"applied immediately. **No restart needed for those.**",
        f"Applied now: {hot_str}",
        f"Need restart to apply: {restart_str}",
    ]
    if restart_reasons:
        lines.append(f"Still needs a restart: {_reasons_str(restart_reasons)}")
    lines.append(
        f"To apply those, {hint} when ready (or revert the "
        f"restart-only keys to a hot-reloadable equivalent)."
    )
    return "\n".join(lines)


def format_reload_notification(
    *,
    path: Path | str,
    hot_keys: list[str],
    restart_keys: list[str],
    restart_hint: str | None = None,
    restart_reasons: Sequence[str] = (),
) -> str:
    """Dispatch to the right per-case helper based on what changed.

    Public entry point used by ``telegram/loop.py:handle_reload``.
    """
    has_hot = bool(hot_keys)
    has_restart = bool(restart_keys) or bool(restart_reasons)
    if has_restart and has_hot:
        return format_partial_reload_notice(
            path=path,
            hot_keys=hot_keys,
            restart_keys=restart_keys,
            restart_hint=restart_hint,
            restart_reasons=restart_reasons,
        )
    if has_restart:
        return format_restart_required_notice(
            path=path,
            restart_keys=restart_keys,
            restart_hint=restart_hint,
            restart_reasons=restart_reasons,
        )
    return format_hot_reload_only_notice(path=path, changed_keys=hot_keys)


def _clip(text: str, limit: int = _TRIGGER_ERROR_MAX_CHARS) -> str:
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _invalid_lines(invalid: Sequence[InvalidTrigger]) -> list[str]:
    return [f"• {item.kind} {item.trigger_id}: {_clip(item.error)}" for item in invalid]


def _refusal_where(exposed: bool) -> str:
    return "on an exposed server" if exposed else "on a non-loopback host"


def _refusal_fix(exposed: bool) -> str:
    if exposed:
        return "remove exposed = true from [triggers.server]"
    return "set [triggers] allow_unauthenticated_webhooks = true (local demos only)"


def format_invalid_triggers_notice(
    invalid: Sequence[InvalidTrigger],
    *,
    phase: Literal["startup", "reload"],
    refused_webhook_ids: Sequence[str] = (),
    exposed: bool = False,
    running: bool = True,
    webhook_server_running: bool = True,
    restart_hint: str | None = None,
) -> str:
    """#1117: some ``[[triggers.*]]`` entries failed validation and were
    skipped; the rest are running. Plain text, secret-free (``error`` comes
    from ``format_trigger_error``).

    #1146: ``refused_webhook_ids`` are ``auth = "none"`` webhooks a reload
    dropped (``exposed``, or a non-loopback host). "…normally" is only said
    when the trigger runtime (``running``) and the webhook server
    (``webhook_server_running``) are really up; otherwise the notice says a
    restart is still needed.
    """
    refused = sorted(refused_webhook_ids)
    count = len(invalid) + len(refused)
    noun = "trigger" if count == 1 else "triggers"
    header = f"⚠️ Trigger config: {count} {noun} skipped"
    lines = [header if refused else f"{header} (invalid)", *_invalid_lines(invalid)]
    lines.extend(
        f'• webhook {webhook_id}: refused, auth = "none" isn\'t allowed '
        f"{_refusal_where(exposed)}"
        for webhook_id in refused
    )
    paragraphs = ["\n".join(lines)]
    others = "started" if phase == "startup" else "reloaded"
    if not running:
        paragraphs.append(
            "No triggers are running: they only start when Untether starts."
        )
    elif not webhook_server_running:
        paragraphs.append(
            f"Crons {others} normally, but the webhook server isn't running, "
            "so no webhooks are being served."
        )
    else:
        paragraphs.append(f"The other triggers {others} normally.")
    if running and (refused or any(item.kind == "webhook" for item in invalid)):
        paragraphs.append(
            "A skipped webhook is switched off: it refuses requests, and no "
            "earlier secret for it stays active."
        )
    fixes: list[str] = []
    if invalid:
        fixes.append("Fix the entry in untether.toml.")
    if refused:
        fixes.append(
            f"To serve a refused webhook, add auth to it or {_refusal_fix(exposed)}."
        )
    if running and webhook_server_running:
        fixes.append("With watch_config on, the saved fix applies without a restart.")
    elif running:
        # A reload can't start the webhook server that start-up refused.
        fixes.append(
            "A cron fix applies on save; to start the webhook server, "
            f"{_resolve_hint(restart_hint)}."
        )
    else:
        # Nothing starts on a reload, so the fix needs a restart too.
        fixes[-1] = (
            f"{fixes[-1].removesuffix('.')}, then {_resolve_hint(restart_hint)}."
        )
    paragraphs.append(" ".join(fixes))
    return "\n\n".join(paragraphs)


def format_webhook_server_refused_notice(
    refused_webhook_ids: Sequence[str],
    *,
    exposed: bool,
    invalid: Sequence[InvalidTrigger] = (),
    restart_hint: str | None = None,
) -> str:
    """#1146: at start-up an ``auth = "none"`` webhook (with ``exposed``, or
    on a non-loopback host) keeps the whole webhook server down, so even the
    webhooks with auth aren't served; crons still run. Skipped invalid
    entries are folded in so start-up sends one notice."""
    ids = ", ".join(sorted(refused_webhook_ids))
    paragraphs = [
        "⚠️ Webhook server not started: no webhooks are being served, not "
        "even ones with auth.\n"
        f'Refused: {ids} (auth = "none" isn\'t allowed {_refusal_where(exposed)})'
    ]
    if invalid:
        paragraphs.append(
            "\n".join(["Also skipped (invalid):", *_invalid_lines(invalid)])
        )
    has_invalid_cron = any(item.kind == "cron" for item in invalid)
    paragraphs.append(
        f"{'The other crons' if has_invalid_cron else 'Crons'} started normally."
    )
    fix = (
        f"To fix, add auth to those webhooks or {_refusal_fix(exposed)}, "
        f"then {_resolve_hint(restart_hint)}."
    )
    if invalid:
        fix += " Fix the skipped entries too."
    paragraphs.append(fix)
    return "\n\n".join(paragraphs)


def format_trigger_block_failure_notice(
    error: str,
    *,
    phase: Literal["startup", "reload"],
    running: bool,
    active_secret_webhook_ids: Sequence[str] = (),
    restart_hint: str | None = None,
) -> str:
    """#1117: the whole ``[triggers]`` block failed (server settings,
    duplicate ids, unknown keys).

    At startup nothing runs. On reload the previous triggers keep running,
    and the notice says so explicitly when that leaves old webhook secrets
    active (a rotated, leaked secret would otherwise keep working silently).
    """
    if phase == "startup":
        return (
            "⚠️ Triggers failed to start: no crons or webhooks are running.\n"
            f"Error: {_clip(error)}\n"
            f"Fix [triggers] in untether.toml, then {_resolve_hint(restart_hint)}."
        )
    if not running:
        # #1146: nothing started, so even a fixed config needs a restart.
        return (
            "⚠️ Trigger config is invalid: no crons or webhooks are running.\n"
            f"Error: {_clip(error)}\n"
            f"Fix [triggers] in untether.toml, then {_resolve_hint(restart_hint)}."
        )
    paragraphs = [
        "⚠️ Trigger config reload failed: the previous crons and webhooks "
        f"are still running unchanged.\nError: {_clip(error)}",
    ]
    if active_secret_webhook_ids:
        ids = ", ".join(sorted(active_secret_webhook_ids))
        paragraphs.append(
            f"The old webhook secret is still active for: {ids}. If you were "
            "rotating a leaked secret, the old one keeps working until "
            "[triggers] is fixed."
        )
    paragraphs.append("Fix [triggers] in untether.toml; the fix applies on save.")
    return "\n\n".join(paragraphs)
