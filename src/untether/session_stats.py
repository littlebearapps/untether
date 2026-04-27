"""Per-engine session statistics with persistent JSON storage."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .logging import get_logger
from .utils.json_state import atomic_write_json

logger = get_logger(__name__)

STATE_FILENAME = "stats.json"
_PRUNE_DAYS = 90


@dataclass(slots=True)
class DayBucket:
    run_count: int = 0
    action_count: int = 0
    duration_ms: int = 0
    last_run_ts: float = 0.0
    # #271 Tier 3: split runs by provenance for the /stats breakdown.
    triggered_count: int = 0
    manual_count: int = 0

    def record(
        self, actions: int, duration_ms: int, *, triggered: bool = False
    ) -> None:
        self.run_count += 1
        self.action_count += actions
        self.duration_ms += duration_ms
        self.last_run_ts = time.time()
        if triggered:
            self.triggered_count += 1
        else:
            self.manual_count += 1

    def to_dict(self) -> dict:
        return {
            "run_count": self.run_count,
            "action_count": self.action_count,
            "duration_ms": self.duration_ms,
            "last_run_ts": self.last_run_ts,
            "triggered_count": self.triggered_count,
            "manual_count": self.manual_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> DayBucket:
        return cls(
            run_count=data.get("run_count", 0),
            action_count=data.get("action_count", 0),
            duration_ms=data.get("duration_ms", 0),
            last_run_ts=data.get("last_run_ts", 0.0),
            triggered_count=data.get("triggered_count", 0),
            manual_count=data.get("manual_count", 0),
        )


@dataclass(frozen=True, slots=True)
class AggregatedStats:
    engine: str
    run_count: int = 0
    action_count: int = 0
    duration_ms: int = 0
    last_run_ts: float = 0.0
    triggered_count: int = 0
    manual_count: int = 0


@dataclass
class SessionStatsStore:
    path: Path
    _data: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw.get("version") == 1:
                    self._data = raw
                else:
                    logger.warning(
                        "session_stats.version_mismatch", path=str(self.path)
                    )
                    self._data = {"version": 1, "engines": {}}
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "session_stats.load_failed", path=str(self.path), error=str(exc)
                )
                self._data = {"version": 1, "engines": {}}
        else:
            self._data = {"version": 1, "engines": {}}

    def _save(self) -> None:
        atomic_write_json(self.path, self._data)

    def record_run(
        self,
        engine: str,
        actions: int,
        duration_ms: int,
        *,
        triggered: bool = False,
    ) -> None:
        today = time.strftime("%Y-%m-%d")
        engines = self._data.setdefault("engines", {})
        engine_days = engines.setdefault(engine, {})
        bucket = DayBucket.from_dict(engine_days.get(today, {}))
        bucket.record(actions, duration_ms, triggered=triggered)
        engine_days[today] = bucket.to_dict()
        self._save()

    def aggregate(
        self,
        *,
        engine: str | None = None,
        period: str = "today",
    ) -> list[AggregatedStats]:
        today = time.strftime("%Y-%m-%d")
        engines_data = self._data.get("engines", {})

        target_engines = [engine] if engine else list(engines_data.keys())
        results: list[AggregatedStats] = []

        for eng in target_engines:
            days = engines_data.get(eng, {})
            if not days:
                continue

            total_runs = 0
            total_actions = 0
            total_duration = 0
            last_ts = 0.0
            total_triggered = 0
            total_manual = 0

            for date_str, bucket_data in days.items():
                if period == "today" and date_str != today:
                    continue
                if period == "week":
                    # Simple: include last 7 days
                    from datetime import datetime, timedelta

                    try:
                        dt = datetime.strptime(date_str, "%Y-%m-%d")
                        cutoff = datetime.strptime(today, "%Y-%m-%d") - timedelta(
                            days=6
                        )
                        if dt < cutoff:
                            continue
                    except ValueError:
                        continue

                bucket = DayBucket.from_dict(bucket_data)
                total_runs += bucket.run_count
                total_actions += bucket.action_count
                total_duration += bucket.duration_ms
                last_ts = max(last_ts, bucket.last_run_ts)
                total_triggered += bucket.triggered_count
                total_manual += bucket.manual_count

            if total_runs > 0:
                results.append(
                    AggregatedStats(
                        engine=eng,
                        run_count=total_runs,
                        action_count=total_actions,
                        duration_ms=total_duration,
                        last_run_ts=last_ts,
                        triggered_count=total_triggered,
                        manual_count=total_manual,
                    )
                )

        return results

    def prune(self) -> int:
        """Remove day buckets older than _PRUNE_DAYS. Returns count removed."""
        from datetime import datetime, timedelta

        cutoff = datetime.now() - timedelta(days=_PRUNE_DAYS)
        cutoff_str = cutoff.strftime("%Y-%m-%d")
        removed = 0
        for days in self._data.get("engines", {}).values():
            expired = [d for d in days if d < cutoff_str]
            for d in expired:
                del days[d]
                removed += 1
        if removed:
            self._save()
        return removed


# ── Module-level convenience ───────────────────────────────────────────────

_store: SessionStatsStore | None = None


def init_stats(config_path: Path) -> None:
    """Initialise the module-level stats store."""
    global _store
    stats_path = config_path.with_name(STATE_FILENAME)
    _store = SessionStatsStore(stats_path)


def record_run(
    engine: str,
    actions: int,
    duration_ms: int,
    *,
    triggered: bool = False,
) -> None:
    """Record a completed run. No-op if store not initialised."""
    if _store is not None:
        _store.record_run(engine, actions, duration_ms, triggered=triggered)


def get_stats(
    *,
    engine: str | None = None,
    period: str = "today",
) -> list[AggregatedStats]:
    """Get aggregated stats. Returns empty list if store not initialised."""
    if _store is None:
        return []
    return _store.aggregate(engine=engine, period=period)


def resolve_stats_path(config_path: Path) -> Path:
    return config_path.with_name(STATE_FILENAME)
