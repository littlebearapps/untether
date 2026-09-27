"""Per-session cumulative-cost ledger (#778, #776 phase 05).

Claude Code's ``result.total_cost_usd`` is cumulative per *session* and is
carried across ``--resume`` (probe F12, 2026-09-27): a resumed run's result
reports the whole session's spend so far, and a live multi-turn process
reports a running total at every turn. Recording it raw over-counts every
resumed run. This ledger remembers the last cumulative value seen per
session so callers can record the delta.

Persisted to JSON beside ``untether.toml`` (like ``session_quarantine.json``)
so a restart doesn't turn the next resume into a raw-total recording.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .logging import get_logger
from .utils.json_state import atomic_write_json

logger = get_logger(__name__)

# Session ids stay resumable for a while; a month is a generous ceiling.
_MAX_AGE_SECONDS = 30 * 24 * 3600


def _key(engine: str, session_id: str) -> str:
    return f"{engine}:{session_id}"


@dataclass(frozen=True, slots=True)
class CostDelta:
    delta: float
    cumulative: float
    # "ledger" (previous value known), "baseline" (seeded by the runner, e.g.
    # the resume-guard's absorbed result), "new_session" (no prior spend),
    # "baseline_unknown" (resumed session never seen before — raw recorded).
    source: str


@dataclass
class SessionCostLedger:
    path: Path | None
    _entries: dict[str, dict[str, float]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> SessionCostLedger:
        entries: dict[str, dict[str, float]] = {}
        try:
            raw = json.loads(path.read_text())
            if isinstance(raw, dict):
                entries = {k: v for k, v in raw.items() if isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except (ValueError, OSError):
            logger.warning("session_costs.load_failed", path=str(path), exc_info=True)
        ledger = cls(path=path, _entries=entries)
        ledger._prune()
        return ledger

    def last(self, engine: str, session_id: str) -> float | None:
        entry = self._entries.get(_key(engine, session_id))
        if entry is None:
            return None
        try:
            return float(entry.get("cost", 0.0))
        except (TypeError, ValueError):
            return None

    def record(
        self,
        engine: str,
        session_id: str,
        cumulative: float,
        *,
        resumed: bool,
        baseline: float | None = None,
    ) -> CostDelta:
        """Store ``cumulative`` for the session and return the spend since
        the previous record."""
        previous = self.last(engine, session_id)
        if previous is not None:
            source = "ledger"
        elif baseline is not None:
            previous, source = baseline, "baseline"
        elif not resumed:
            previous, source = 0.0, "new_session"
        else:
            source = "baseline_unknown"
        delta = cumulative if previous is None else max(0.0, cumulative - previous)
        self._entries[_key(engine, session_id)] = {
            "cost": float(cumulative),
            "ts": time.time(),
        }
        self._flush()
        if source == "baseline_unknown":
            logger.info(
                "cost.baseline_unknown",
                engine=engine,
                session_id=session_id,
                recorded=cumulative,
            )
        return CostDelta(delta=delta, cumulative=cumulative, source=source)

    def _prune(self) -> None:
        cutoff = time.time() - _MAX_AGE_SECONDS
        for k in [
            k
            for k, v in self._entries.items()
            if not isinstance(v.get("ts"), (int, float)) or v["ts"] < cutoff
        ]:
            del self._entries[k]

    def _flush(self) -> None:
        if self.path is None:
            return
        try:
            atomic_write_json(self.path, self._entries)
        except OSError:
            logger.warning(
                "session_costs.flush_failed", path=str(self.path), exc_info=True
            )


_LEDGER: SessionCostLedger | None = None


def resolve_session_costs_path(config_path: Path) -> Path:
    return config_path.with_name("session_costs.json")


def get_session_cost_ledger() -> SessionCostLedger:
    global _LEDGER
    if _LEDGER is None:
        from .settings import _resolve_config_path

        _LEDGER = SessionCostLedger.load(
            resolve_session_costs_path(_resolve_config_path(None))
        )
    return _LEDGER


def set_session_cost_ledger(ledger: SessionCostLedger | None) -> None:
    global _LEDGER
    _LEDGER = ledger
