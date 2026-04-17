"""Persist the set of fired ``run_once`` cron ids across config reloads and
restarts.

``run_once = true`` crons are removed from the active list in-memory after
firing (#288), but the entry stays in ``untether.toml`` so the user can see
the historical schedule. Without persistence, every config hot-reload (#269)
and every process restart would re-activate them and fire the cron again —
which the user explicitly asked it not to. See #317 for the incident.

The state lives in ``run_once_fired.json`` next to ``untether.toml``. Schema:

::

    {
      "fired": {
        "<cron_id>": "<ISO-8601 fire timestamp>",
        ...
      }
    }

Entries are pruned lazily on config reload — if a cron id no longer appears
in the TOML, its fired-state entry is dropped so re-adding the same id under
a new schedule starts fresh.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

from ..logging import get_logger
from ..utils.json_state import atomic_write_json

logger = get_logger(__name__)

STATE_FILENAME = "run_once_fired.json"

__all__ = [
    "STATE_FILENAME",
    "load_fired_state",
    "resolve_state_path",
    "save_fired_state",
]


def resolve_state_path(config_path: Path) -> Path:
    """Return the fired-state file path (sibling of ``untether.toml``)."""
    return config_path.with_name(STATE_FILENAME)


def load_fired_state(path: Path) -> dict[str, str]:
    """Return ``{cron_id: iso_timestamp}`` from *path*, or ``{}`` on any error.

    A missing or corrupt file is treated as "nothing fired yet" — worst case
    a run_once cron re-fires, which matches the legacy behaviour and won't
    make things worse.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(
            "run_once_state.load_failed",
            path=str(path),
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        return {}
    if not isinstance(data, dict):
        return {}
    fired = data.get("fired")
    if not isinstance(fired, dict):
        return {}
    # Defensive copy with key/value type checks.
    return {
        str(k): str(v)
        for k, v in fired.items()
        if isinstance(k, str) and isinstance(v, str)
    }


def save_fired_state(path: Path, fired: dict[str, str]) -> None:
    """Atomically write the fired set to disk. Swallows errors (logs warning)."""
    try:
        atomic_write_json(path, {"fired": dict(fired)})
    except (OSError, ValueError, TypeError) as exc:
        logger.warning(
            "run_once_state.save_failed",
            path=str(path),
            error=str(exc),
            error_type=exc.__class__.__name__,
        )


def iso_now() -> str:
    """ISO-8601 UTC timestamp (seconds precision) for the fired entry."""
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
