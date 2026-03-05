"""Tests for SessionStatsStore: record, aggregate, persist, prune."""

from __future__ import annotations

import json
import time

from untether.session_stats import (
    DayBucket,
    SessionStatsStore,
)


def test_day_bucket_record() -> None:
    bucket = DayBucket()
    bucket.record(actions=5, duration_ms=1000)
    assert bucket.run_count == 1
    assert bucket.action_count == 5
    assert bucket.duration_ms == 1000
    assert bucket.last_run_ts > 0


def test_day_bucket_accumulates() -> None:
    bucket = DayBucket()
    bucket.record(actions=3, duration_ms=500)
    bucket.record(actions=7, duration_ms=800)
    assert bucket.run_count == 2
    assert bucket.action_count == 10
    assert bucket.duration_ms == 1300


def test_day_bucket_roundtrip() -> None:
    bucket = DayBucket(
        run_count=2, action_count=10, duration_ms=5000, last_run_ts=1000.0
    )
    data = bucket.to_dict()
    restored = DayBucket.from_dict(data)
    assert restored.run_count == bucket.run_count
    assert restored.action_count == bucket.action_count
    assert restored.duration_ms == bucket.duration_ms
    assert restored.last_run_ts == bucket.last_run_ts


def test_store_record_and_aggregate(tmp_path) -> None:
    store = SessionStatsStore(tmp_path / "stats.json")
    store.record_run("claude", actions=5, duration_ms=2000)
    store.record_run("claude", actions=3, duration_ms=1000)
    store.record_run("codex", actions=2, duration_ms=500)

    stats = store.aggregate(period="today")
    assert len(stats) == 2

    claude = next(s for s in stats if s.engine == "claude")
    assert claude.run_count == 2
    assert claude.action_count == 8
    assert claude.duration_ms == 3000

    codex = next(s for s in stats if s.engine == "codex")
    assert codex.run_count == 1


def test_store_aggregate_by_engine(tmp_path) -> None:
    store = SessionStatsStore(tmp_path / "stats.json")
    store.record_run("claude", actions=5, duration_ms=2000)
    store.record_run("codex", actions=2, duration_ms=500)

    stats = store.aggregate(engine="claude", period="today")
    assert len(stats) == 1
    assert stats[0].engine == "claude"


def test_store_aggregate_empty(tmp_path) -> None:
    store = SessionStatsStore(tmp_path / "stats.json")
    stats = store.aggregate(period="today")
    assert stats == []


def test_store_persistence(tmp_path) -> None:
    path = tmp_path / "stats.json"
    store1 = SessionStatsStore(path)
    store1.record_run("claude", actions=5, duration_ms=2000)

    # Load from same file
    store2 = SessionStatsStore(path)
    stats = store2.aggregate(period="today")
    assert len(stats) == 1
    assert stats[0].run_count == 1


def test_store_corrupt_file(tmp_path) -> None:
    path = tmp_path / "stats.json"
    path.write_text("not json", encoding="utf-8")

    store = SessionStatsStore(path)
    # Should recover gracefully
    stats = store.aggregate(period="today")
    assert stats == []

    # Should still be able to record
    store.record_run("claude", actions=1, duration_ms=100)
    stats = store.aggregate(period="today")
    assert len(stats) == 1


def test_store_wrong_version(tmp_path) -> None:
    path = tmp_path / "stats.json"
    path.write_text(json.dumps({"version": 99}), encoding="utf-8")

    store = SessionStatsStore(path)
    stats = store.aggregate(period="today")
    assert stats == []


def test_store_prune(tmp_path) -> None:
    path = tmp_path / "stats.json"
    store = SessionStatsStore(path)

    # Manually inject old data
    store._data = {
        "version": 1,
        "engines": {
            "claude": {
                "2020-01-01": DayBucket(
                    run_count=1, action_count=5, duration_ms=1000
                ).to_dict(),
                time.strftime("%Y-%m-%d"): DayBucket(
                    run_count=2, action_count=10, duration_ms=5000
                ).to_dict(),
            }
        },
    }
    store._save()

    removed = store.prune()
    assert removed == 1

    # Today's data should survive
    stats = store.aggregate(period="all")
    assert len(stats) == 1
    assert stats[0].run_count == 2


def test_store_aggregate_all_period(tmp_path) -> None:
    store = SessionStatsStore(tmp_path / "stats.json")
    # Manually inject data for multiple days
    store._data = {
        "version": 1,
        "engines": {
            "claude": {
                "2026-03-01": DayBucket(
                    run_count=1, action_count=5, duration_ms=1000, last_run_ts=1000.0
                ).to_dict(),
                "2026-03-04": DayBucket(
                    run_count=2, action_count=10, duration_ms=5000, last_run_ts=2000.0
                ).to_dict(),
            }
        },
    }

    stats = store.aggregate(period="all")
    assert len(stats) == 1
    assert stats[0].run_count == 3
    assert stats[0].action_count == 15
