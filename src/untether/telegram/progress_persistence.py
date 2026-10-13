"""Persist active progress message refs across restarts.

On startup, orphan progress messages from a prior instance are edited to show
"interrupted by restart" and their inline keyboards are removed. An entry may
also carry the "Action required" push sent for that message
(``notify_message_id``, #1084), which the cleanup deletes: its buttons are
gone, so the push would point at nothing to tap. Entries without it (files
written before #1084) load as before.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..logging import get_logger
from ..utils.json_state import atomic_write_json

logger = get_logger(__name__)

STATE_FILENAME = "active_progress.json"
# #1084: the approval push ("Action required — approval needed") sent as a
# reply to an entry's progress message, while it is visible.
NOTIFY_KEY = "notify_message_id"


def resolve_progress_path(config_path: Path) -> Path:
    """Return the progress state file path (sibling to config file)."""
    return config_path.with_name(STATE_FILENAME)


def load_active_progress(path: Path) -> dict[str, dict[str, Any]]:
    """Load active progress entries.

    Returns ``{session_key: {"chat_id": int, "message_id": int}}``, plus
    ``"notify_message_id": int`` while an approval push is visible (#1084).
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:  # noqa: BLE001
        logger.warning(
            "progress_persistence.load_failed", path=str(path), exc_info=True
        )
        return {}


def save_active_progress(path: Path, entries: dict[str, dict[str, Any]]) -> None:
    """Save active progress entries atomically."""
    try:
        atomic_write_json(path, entries)
    except Exception:  # noqa: BLE001
        logger.warning(
            "progress_persistence.save_failed", path=str(path), exc_info=True
        )


def register_progress(
    path: Path, session_key: str, chat_id: int, message_id: int
) -> None:
    """Record a new active progress message."""
    entries = load_active_progress(path)
    entries[session_key] = {"chat_id": chat_id, "message_id": message_id}
    save_active_progress(path, entries)


def set_progress_notify(path: Path, session_key: str, message_id: int | None) -> bool:
    """Record the approval push for an active progress message (#1084), or
    forget it with ``None``.

    Returns True when the file changed. An unregistered key is a no-op: the
    run has already been released (or was never tracked), so there is no
    orphan for the push to outlive.
    """
    entries = load_active_progress(path)
    entry = entries.get(session_key)
    if not isinstance(entry, dict):
        return False
    if message_id is None:
        if NOTIFY_KEY not in entry:
            return False
        del entry[NOTIFY_KEY]
    else:
        if entry.get(NOTIFY_KEY) == message_id:
            return False
        entry[NOTIFY_KEY] = message_id
    save_active_progress(path, entries)
    return True


def unregister_progress(path: Path, session_key: str) -> bool:
    """Remove a completed progress message.

    Returns True when an entry was removed; a missing key is a no-op
    (False), so callers can release the same message more than once (#810).
    """
    entries = load_active_progress(path)
    if session_key not in entries:
        return False
    del entries[session_key]
    save_active_progress(path, entries)
    return True


def clear_all_progress(path: Path) -> None:
    """Clear all entries (after startup cleanup)."""
    if path.exists():
        save_active_progress(path, {})
