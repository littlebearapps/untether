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
they reach for ``systemctl restart`` after editing config.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "format_hot_reload_only_notice",
    "format_partial_reload_notice",
    "format_reload_notification",
    "format_restart_required_notice",
]


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


def format_restart_required_notice(
    *,
    path: Path | str,
    restart_keys: list[str],
) -> str:
    """A key in the restart-only set was edited; manual action required.

    Headline contains the literal phrase ``Restart required``; restart-only
    keys are explicitly named so the agent can advise the user (or revert
    to a hot-reloadable equivalent).
    """
    keys_str = ", ".join(f"`{k}`" for k in sorted(set(restart_keys))) or "(no keys)"
    return (
        f"⚠️ **Restart required for `{_short_path(path)}` change** — "
        f"the edited key is in the restart-only set.\n"
        f"Run `systemctl --user restart untether` to apply, or revert and "
        f"use the hot-reloadable equivalent.\n"
        f"Restart-only keys touched: {keys_str}"
    )


def format_partial_reload_notice(
    *,
    path: Path | str,
    hot_keys: list[str],
    restart_keys: list[str],
) -> str:
    """Mixed reload — some keys applied immediately, others need restart."""
    hot_str = ", ".join(f"`{k}`" for k in sorted(set(hot_keys))) or "(none)"
    restart_str = ", ".join(f"`{k}`" for k in sorted(set(restart_keys))) or "(none)"
    return (
        f"♻️ **Partial reload of `{_short_path(path)}`** — "
        f"{len(hot_keys)} of {len(hot_keys) + len(restart_keys)} keys "
        f"applied immediately. **No restart needed for those.**\n"
        f"Applied now: {hot_str}\n"
        f"Need restart to apply: {restart_str}\n"
        f"Run `systemctl --user restart untether` when ready (or revert the "
        f"restart-only keys to a hot-reloadable equivalent)."
    )


def format_reload_notification(
    *,
    path: Path | str,
    hot_keys: list[str],
    restart_keys: list[str],
) -> str:
    """Dispatch to the right per-case helper based on what changed.

    Public entry point used by ``telegram/loop.py:handle_reload``.
    """
    has_hot = bool(hot_keys)
    has_restart = bool(restart_keys)
    if has_restart and has_hot:
        return format_partial_reload_notice(
            path=path, hot_keys=hot_keys, restart_keys=restart_keys
        )
    if has_restart:
        return format_restart_required_notice(path=path, restart_keys=restart_keys)
    return format_hot_reload_only_notice(path=path, changed_keys=hot_keys)
