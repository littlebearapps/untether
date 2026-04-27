"""Persistent ``last_fired_at`` history for cron + webhook triggers (#271 Tier 3).

Single-writer JSON file at ``<config_path>.with_name("triggers_history.json")``.
Mirrors the ``session_stats`` pattern: simple JSON, ``atomic_write_json``, a
module-level singleton initialised once at startup. Recording is best-effort —
a write failure is logged and swallowed so a corrupted state file can't break
the cron loop or webhook server.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..logging import get_logger
from ..utils.json_state import atomic_write_json

logger = get_logger(__name__)

STATE_FILENAME = "triggers_history.json"
_STATE_VERSION = 1


@dataclass
class TriggerHistoryStore:
    """JSON-backed last-fired-at timestamps keyed by trigger id."""

    path: Path
    _data: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._data = {"version": _STATE_VERSION, "triggers": {}}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "triggers.history.load_failed",
                path=str(self.path),
                error=str(exc),
            )
            self._data = {"version": _STATE_VERSION, "triggers": {}}
            return
        if not isinstance(raw, dict) or raw.get("version") != _STATE_VERSION:
            logger.warning("triggers.history.version_mismatch", path=str(self.path))
            self._data = {"version": _STATE_VERSION, "triggers": {}}
            return
        triggers = raw.get("triggers")
        if not isinstance(triggers, dict):
            triggers = {}
        self._data = {"version": _STATE_VERSION, "triggers": triggers}

    def _save(self) -> None:
        atomic_write_json(self.path, self._data)

    def record_fired(self, trigger_id: str) -> None:
        triggers = self._data.setdefault("triggers", {})
        triggers[trigger_id] = time.time()
        self._save()

    def get_last_fired(self, trigger_id: str) -> float | None:
        triggers = self._data.get("triggers", {})
        value = triggers.get(trigger_id)
        if isinstance(value, int | float):
            return float(value)
        return None


# ── Module-level convenience ───────────────────────────────────────────────

_store: TriggerHistoryStore | None = None


def init_history(config_path: Path) -> None:
    """Initialise the module-level history store. Idempotent."""
    global _store
    history_path = config_path.with_name(STATE_FILENAME)
    _store = TriggerHistoryStore(history_path)


def reset_history() -> None:
    """Reset the module singleton. Intended for tests."""
    global _store
    _store = None


def record_fired(trigger_id: str) -> None:
    """Record a trigger firing. No-op if the store isn't initialised.

    Wraps the underlying write in a best-effort try/except so a transient
    disk failure can't break the cron loop or webhook dispatch path.
    """
    if _store is None:
        return
    try:
        _store.record_fired(trigger_id)
    except OSError as exc:
        logger.warning(
            "triggers.history.write_failed",
            trigger_id=trigger_id,
            error=str(exc),
        )


def get_last_fired(trigger_id: str) -> float | None:
    """Return the unix timestamp of the trigger's last firing, or None."""
    if _store is None:
        return None
    return _store.get_last_fired(trigger_id)


def resolve_history_path(config_path: Path) -> Path:
    return config_path.with_name(STATE_FILENAME)
