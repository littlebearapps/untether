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
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..logging import get_logger
from ..utils.json_state import atomic_write_json

logger = get_logger(__name__)

STATE_FILENAME = "triggers_history.json"
_STATE_VERSION = 1

# #1123: entries are keyed ``kind:id`` so a cron and a webhook sharing an id
# don't collide; a bare ``id`` key (0.36.1 and older) is read as a fallback.
type TriggerKind = Literal["cron", "webhook"]


def history_key(kind: TriggerKind, trigger_id: str) -> str:
    return f"{kind}:{trigger_id}"


@dataclass
class TriggerHistoryStore:
    """JSON-backed last-fired-at timestamps keyed by ``kind:id``."""

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

    def record_fired(self, kind: TriggerKind, trigger_id: str) -> None:
        triggers = self._data.setdefault("triggers", {})
        triggers[history_key(kind, trigger_id)] = time.time()
        self._save()

    def get_last_fired(self, kind: TriggerKind, trigger_id: str) -> float | None:
        triggers = self._data.get("triggers", {})
        value = triggers.get(history_key(kind, trigger_id), triggers.get(trigger_id))
        if isinstance(value, int | float):
            return float(value)
        return None

    def prune(self, valid_keys: Collection[str]) -> list[str]:
        """Drop entries for triggers no longer in the config (#1123). A bare
        legacy key survives while either kind still uses its id."""
        valid = set(valid_keys)
        valid_ids = {key.partition(":")[2] for key in valid}
        triggers = self._data.get("triggers", {})
        dropped = [
            key
            for key in triggers
            if key not in valid and (":" in key or key not in valid_ids)
        ]
        if dropped:
            for key in dropped:
                del triggers[key]
            self._save()
        return dropped


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


def record_fired(kind: TriggerKind, trigger_id: str) -> None:
    """Record a trigger firing. No-op if the store isn't initialised.

    Wraps the underlying write in a best-effort try/except so a transient
    disk failure can't break the cron loop or webhook dispatch path.
    """
    if _store is None:
        return
    try:
        _store.record_fired(kind, trigger_id)
    except OSError as exc:
        logger.warning(
            "triggers.history.write_failed",
            trigger_id=trigger_id,
            error=str(exc),
        )


def get_last_fired(kind: TriggerKind, trigger_id: str) -> float | None:
    """Return the unix timestamp of the trigger's last firing, or None."""
    if _store is None:
        return None
    return _store.get_last_fired(kind, trigger_id)


def prune_history(cron_ids: Collection[str], webhook_ids: Collection[str]) -> None:
    """#1123: forget triggers that left the config. Best-effort like
    :func:`record_fired`."""
    if _store is None:
        return
    keys = [history_key("cron", i) for i in cron_ids]
    keys += [history_key("webhook", i) for i in webhook_ids]
    try:
        dropped = _store.prune(keys)
    except OSError as exc:
        logger.warning("triggers.history.write_failed", error=str(exc))
        return
    if dropped:
        logger.info("triggers.history.pruned", dropped=sorted(dropped))


def resolve_history_path(config_path: Path) -> Path:
    return config_path.with_name(STATE_FILENAME)
