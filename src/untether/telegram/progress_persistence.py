"""Persist active progress message refs across restarts.

On startup, orphan progress messages from a prior instance are edited to show
"interrupted by restart" and their inline keyboards are removed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..logging import get_logger
from ..utils.json_state import atomic_write_json

logger = get_logger(__name__)

STATE_FILENAME = "active_progress.json"


def resolve_progress_path(config_path: Path) -> Path:
    """Return the progress state file path (sibling to config file)."""
    return config_path.with_name(STATE_FILENAME)


def load_active_progress(path: Path) -> dict[str, dict[str, Any]]:
    """Load active progress entries.

    Returns ``{session_key: {"chat_id": int, "message_id": int}}``.
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


def unregister_progress(path: Path, session_key: str) -> None:
    """Remove a completed progress message."""
    entries = load_active_progress(path)
    if session_key in entries:
        del entries[session_key]
        save_active_progress(path, entries)


def clear_all_progress(path: Path) -> None:
    """Clear all entries (after startup cleanup)."""
    if path.exists():
        save_active_progress(path, {})
