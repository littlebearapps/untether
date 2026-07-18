"""Persisted per-session quarantine markers (#631/W2).

A session id is quarantined when Untether had to forcibly terminate its
subprocess after a valid result (post-result limbo SIGTERM/SIGKILL), or when
a strict empty-0-turn resume anomaly is observed. A quarantined session is
never resumed again — the next message on it starts a FRESH session.

Persisted to JSON (sibling to untether.toml) so a service restart cannot
re-enable a poisoned token. Writes are flushed synchronously on each mutation
(rare events); markers are pruned by age.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .logging import get_logger
from .utils.json_state import atomic_write_json

logger = get_logger(__name__)

# Prune markers older than this (a session id is only resumable within
# Claude's ~24h transcript retention anyway; 7d is a safe generous ceiling).
_MAX_AGE_SECONDS = 7 * 24 * 3600


def _key(engine: str, session_id: str) -> str:
    return f"{engine}:{session_id}"


@dataclass
class QuarantineStore:
    path: Path
    _entries: dict[str, dict[str, object]] = field(default_factory=dict)
    _dirty: bool = False

    @classmethod
    def load(cls, path: Path) -> QuarantineStore:
        entries: dict[str, dict[str, object]] = {}
        try:
            raw = json.loads(path.read_text())
            if isinstance(raw, dict):
                entries = {k: v for k, v in raw.items() if isinstance(v, dict)}
            else:
                logger.warning(
                    "quarantine.load_failed",
                    path=str(path),
                    reason="not_a_dict",
                )
        except FileNotFoundError:
            pass
        except (ValueError, OSError):
            logger.warning("quarantine.load_failed", path=str(path), exc_info=True)
        store = cls(path=path, _entries=entries)
        store._prune()
        store.flush()  # persist pruned state to disk
        return store

    def is_quarantined(self, engine: str, session_id: str) -> bool:
        return _key(engine, session_id) in self._entries

    def quarantine(self, engine: str, session_id: str, reason: str) -> None:
        k = _key(engine, session_id)
        if k in self._entries:
            return
        self._entries[k] = {"reason": reason, "ts": time.time()}
        self._dirty = True
        logger.warning(
            "session.quarantined",
            engine=engine,
            session_id=session_id,
            reason=reason,
        )
        self.flush()

    def clear(self, engine: str, session_id: str) -> None:
        if self._entries.pop(_key(engine, session_id), None) is not None:
            self._dirty = True
            self.flush()

    def _prune(self) -> None:
        cutoff = time.time() - _MAX_AGE_SECONDS
        stale = []
        for k, v in self._entries.items():
            try:
                ts = float(v.get("ts", 0) or 0)
                if ts < cutoff:
                    stale.append(k)
            except (TypeError, ValueError):
                # malformed/invalid ts — treat as expired
                stale.append(k)
        for k in stale:
            del self._entries[k]
            self._dirty = True

    def flush(self) -> None:
        if not self._dirty:
            return
        try:
            atomic_write_json(self.path, self._entries)
            self._dirty = False
        except OSError:
            logger.warning(
                "quarantine.flush_failed", path=str(self.path), exc_info=True
            )


# Module-level singleton — the bridge and the Claude runner both need the
# store without threading it through every constructor (mirrors how
# _load_auto_continue_settings is reached from the runner).
_STORE: QuarantineStore | None = None


def resolve_quarantine_path(config_path: Path) -> Path:
    """Return the quarantine state file path (sibling to the config file)."""
    return config_path.with_name("session_quarantine.json")


def get_quarantine_store() -> QuarantineStore:
    """Return the process-wide QuarantineStore, loading it on first use.

    The path resolves sibling to the active config file (env override via
    UNTETHER_CONFIG_PATH respected, matching settings._resolve_config_path).
    Tests inject a store via set_quarantine_store().
    """
    global _STORE
    if _STORE is None:
        from .settings import _resolve_config_path

        _STORE = QuarantineStore.load(
            resolve_quarantine_path(_resolve_config_path(None))
        )
    return _STORE


def set_quarantine_store(store: QuarantineStore | None) -> None:
    """Replace (or reset with None) the process-wide store — used by startup
    wiring and tests."""
    global _STORE
    _STORE = store
