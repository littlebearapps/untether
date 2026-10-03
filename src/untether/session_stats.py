"""Per-engine session statistics with persistent JSON storage."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .logging import get_logger
from .utils.json_state import atomic_write_json

logger = get_logger(__name__)

STATE_FILENAME = "stats.json"
# #897: day buckets older than this are folded into the engine's ARCHIVE_KEY
# bucket, so stats.json stops growing while /stats "All Time" stays true.
_ROLL_UP_AFTER_DAYS = 90
# Lives beside the engine's "YYYY-MM-DD" keys: an older build reading the
# file skips it for today/week (not a date) and still counts it in "all".
ARCHIVE_KEY = "archive"


def _today() -> str:
    """Host-local date key for day buckets."""
    return time.strftime("%Y-%m-%d")


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

    def merge(self, other: DayBucket) -> None:
        """Add ``other``'s totals into this bucket (#897 roll-up)."""
        self.run_count += other.run_count
        self.action_count += other.action_count
        self.duration_ms += other.duration_ms
        self.last_run_ts = max(self.last_run_ts, other.last_run_ts)
        self.triggered_count += other.triggered_count
        self.manual_count += other.manual_count

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
    # #897: local date of the last roll-up — at most one per day.
    _rolled_up_on: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._load()
        if self.roll_up():
            self._save()

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
        today = _today()
        if today != self._rolled_up_on:
            self.roll_up()  # first run of a new day; saved with the run below
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
        today = _today()
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
                if date_str == ARCHIVE_KEY and period in ("today", "week"):
                    continue  # #897: rolled-up history counts in "all" only
                if period == "today" and date_str != today:
                    continue
                if period == "week":
                    # Simple: include last 7 days
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

    def roll_up(self) -> int:
        """#897: fold day buckets older than ``_ROLL_UP_AFTER_DAYS`` into each
        engine's ``ARCHIVE_KEY`` bucket, so "all" totals are unchanged.

        Marks today as rolled up and returns how many day buckets were
        folded. The caller saves (``record_run`` always does; init when > 0).
        """
        today = _today()
        self._rolled_up_on = today
        cutoff = datetime.strptime(today, "%Y-%m-%d") - timedelta(
            days=_ROLL_UP_AFTER_DAYS
        )
        folded = 0
        for engine_days in self._data.get("engines", {}).values():
            if not isinstance(engine_days, dict):
                continue
            expired: list[str] = []
            for key in engine_days:
                if key == ARCHIVE_KEY:
                    continue
                try:
                    if datetime.strptime(key, "%Y-%m-%d") < cutoff:
                        expired.append(key)
                except ValueError:
                    continue  # not a day bucket — leave it alone
            if not expired:
                continue
            archive = DayBucket.from_dict(engine_days.get(ARCHIVE_KEY, {}))
            for key in expired:
                archive.merge(DayBucket.from_dict(engine_days.pop(key)))
            engine_days[ARCHIVE_KEY] = archive.to_dict()
            folded += len(expired)
        if folded:
            logger.info("session_stats.rolled_up", days=folded, path=str(self.path))
        return folded


# ── Module-level convenience ───────────────────────────────────────────────

_store: SessionStatsStore | None = None


def init_stats(config_path: Path) -> None:
    """Initialise the module-level stats store (rolls up old days, #897)."""
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
