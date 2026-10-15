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


def format_restart_required_notice(
    *,
    path: Path | str,
    restart_keys: list[str],
    restart_hint: str | None = None,
) -> str:
    """A key in the restart-only set was edited; manual action required.

    Headline contains the literal phrase ``Restart required``; restart-only
    keys are explicitly named so the agent can advise the user (or revert
    to a hot-reloadable equivalent).

    ``restart_hint`` defaults to :func:`untether.service_manager.restart_hint`
    (#927: names the running unit/label, generic wording otherwise).
    """
    hint = _resolve_hint(restart_hint)
    keys_str = ", ".join(f"`{k}`" for k in sorted(set(restart_keys))) or "(no keys)"
    return (
        f"⚠️ **Restart required for `{_short_path(path)}` change** — "
        f"the edited key is in the restart-only set.\n"
        f"To apply, {hint} — or revert and use the hot-reloadable equivalent.\n"
        f"Restart-only keys touched: {keys_str}"
    )


def format_partial_reload_notice(
    *,
    path: Path | str,
    hot_keys: list[str],
    restart_keys: list[str],
    restart_hint: str | None = None,
) -> str:
    """Mixed reload — some keys applied immediately, others need restart."""
    hint = _resolve_hint(restart_hint)
    hot_str = ", ".join(f"`{k}`" for k in sorted(set(hot_keys))) or "(none)"
    restart_str = ", ".join(f"`{k}`" for k in sorted(set(restart_keys))) or "(none)"
    return (
        f"♻️ **Partial reload of `{_short_path(path)}`** — "
        f"{len(hot_keys)} of {len(hot_keys) + len(restart_keys)} keys "
        f"applied immediately. **No restart needed for those.**\n"
        f"Applied now: {hot_str}\n"
        f"Need restart to apply: {restart_str}\n"
        f"To apply those, {hint} when ready (or revert the "
        f"restart-only keys to a hot-reloadable equivalent)."
    )


def format_reload_notification(
    *,
    path: Path | str,
    hot_keys: list[str],
    restart_keys: list[str],
    restart_hint: str | None = None,
) -> str:
    """Dispatch to the right per-case helper based on what changed.

    Public entry point used by ``telegram/loop.py:handle_reload``.
    """
    has_hot = bool(hot_keys)
    has_restart = bool(restart_keys)
    if has_restart and has_hot:
        return format_partial_reload_notice(
            path=path,
            hot_keys=hot_keys,
            restart_keys=restart_keys,
            restart_hint=restart_hint,
        )
    if has_restart:
        return format_restart_required_notice(
            path=path, restart_keys=restart_keys, restart_hint=restart_hint
        )
    return format_hot_reload_only_notice(path=path, changed_keys=hot_keys)


def _clip(text: str, limit: int = _TRIGGER_ERROR_MAX_CHARS) -> str:
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def format_invalid_triggers_notice(
    invalid: Sequence[InvalidTrigger],
    *,
    phase: Literal["startup", "reload"],
) -> str:
    """#1117: some ``[[triggers.*]]`` entries failed validation and were
    skipped; the rest are running. Plain text, secret-free (``error`` comes
    from ``format_trigger_error``)."""
    count = len(invalid)
    noun = "trigger" if count == 1 else "triggers"
    lines = [f"⚠️ Trigger config: {count} {noun} skipped (invalid)"]
    lines.extend(
        f"• {item.kind} {item.trigger_id}: {_clip(item.error)}" for item in invalid
    )
    others = "started" if phase == "startup" else "reloaded"
    paragraphs = ["\n".join(lines), f"The other triggers {others} normally."]
    if any(item.kind == "webhook" for item in invalid):
        paragraphs.append(
            "A skipped webhook is switched off: it refuses requests, and no "
            "earlier secret for it stays active."
        )
    paragraphs.append(
        "Fix the entry in untether.toml. With watch_config on, the saved fix "
        "applies without a restart."
    )
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
        return (
            "⚠️ Trigger config is invalid: no crons or webhooks are running.\n"
            f"Error: {_clip(error)}\n"
            "Fix [triggers] in untether.toml."
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
