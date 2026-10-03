"""Tests for SessionStatsStore: record, aggregate, persist, prune."""

from __future__ import annotations

import json

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


# ── #897: day buckets older than 90 days roll up into an archive bucket ─────


def _bucket(runs: int, ts: float, *, triggered: int = 0) -> dict:
    return DayBucket(
        run_count=runs,
        action_count=runs * 3,
        duration_ms=runs * 1000,
        last_run_ts=ts,
        triggered_count=triggered,
        manual_count=runs - triggered,
    ).to_dict()


def _days_ago(n: int) -> str:
    from datetime import date, timedelta

    return (date.today() - timedelta(days=n)).isoformat()


def _write_stats(path, engines: dict) -> None:
    path.write_text(json.dumps({"version": 1, "engines": engines}), encoding="utf-8")


def _old_and_recent() -> dict:
    return {
        "claude": {
            "2020-01-01": _bucket(1, 100.0, triggered=1),
            _days_ago(120): _bucket(2, 300.0),
            _days_ago(91): _bucket(4, 200.0, triggered=2),
            _days_ago(90): _bucket(8, 400.0),  # exactly 90 days: kept
            _days_ago(3): _bucket(16, 500.0),
            _days_ago(0): _bucket(32, 600.0, triggered=5),
        },
        "codex": {"2021-06-01": _bucket(64, 50.0)},
    }


def _totals(stats) -> dict:
    return {
        s.engine: (
            s.run_count,
            s.action_count,
            s.duration_ms,
            s.last_run_ts,
            s.triggered_count,
            s.manual_count,
        )
        for s in stats
    }


def test_897_init_folds_old_buckets_into_archive(tmp_path) -> None:
    from structlog.testing import capture_logs

    from untether.session_stats import ARCHIVE_KEY

    path = tmp_path / "stats.json"
    _write_stats(path, _old_and_recent())

    with capture_logs() as logs:
        store = SessionStatsStore(path)

    claude = store._data["engines"]["claude"]
    assert set(claude) == {ARCHIVE_KEY, _days_ago(90), _days_ago(3), _days_ago(0)}
    archive = DayBucket.from_dict(claude[ARCHIVE_KEY])
    assert (archive.run_count, archive.action_count, archive.duration_ms) == (
        7,
        21,
        7000,
    )
    assert archive.last_run_ts == 300.0
    assert (archive.triggered_count, archive.manual_count) == (3, 4)
    assert set(store._data["engines"]["codex"]) == {ARCHIVE_KEY}
    rolled = [e for e in logs if e.get("event") == "session_stats.rolled_up"]
    assert len(rolled) == 1 and rolled[0]["days"] == 4
    # Persisted: the archive survives a save/load round trip.
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["engines"]["claude"][ARCHIVE_KEY] == claude[ARCHIVE_KEY]
    assert _days_ago(120) not in on_disk["engines"]["claude"]


def test_897_all_time_totals_identical_before_and_after(tmp_path) -> None:
    path = tmp_path / "stats.json"
    store = SessionStatsStore(path)
    store._data = {"version": 1, "engines": _old_and_recent()}
    before = {p: _totals(store.aggregate(period=p)) for p in ("today", "week", "all")}
    assert store.roll_up() == 4
    after = {p: _totals(store.aggregate(period=p)) for p in ("today", "week", "all")}
    assert after == before
    # Engine filter includes that engine's archive too.
    assert _totals(store.aggregate(engine="codex", period="all")) == {
        "codex": before["all"]["codex"]
    }


def test_897_archive_ignored_by_today_and_week(tmp_path) -> None:
    path = tmp_path / "stats.json"
    _write_stats(path, _old_and_recent())
    store = SessionStatsStore(path)

    assert _totals(store.aggregate(period="today")) == {
        "claude": (32, 96, 32000, 600.0, 5, 27)
    }
    week = _totals(store.aggregate(period="week"))
    assert week == {"claude": (48, 144, 48000, 600.0, 5, 43)}
    # An engine with only archived history is All Time only.
    assert "codex" not in week
    assert _totals(store.aggregate(engine="codex", period="today")) == {}
    assert _totals(store.aggregate(period="all"))["codex"][0] == 64


def test_897_roll_up_idempotent_and_merges_into_existing_archive(tmp_path) -> None:
    from untether.session_stats import ARCHIVE_KEY

    path = tmp_path / "stats.json"
    _write_stats(path, _old_and_recent())
    store = SessionStatsStore(path)
    snapshot = json.loads(json.dumps(store._data))
    assert store.roll_up() == 0
    assert store._data == snapshot

    # A later fold adds to the archive rather than replacing it.
    store._data["engines"]["claude"][_days_ago(200)] = _bucket(100, 999.0)
    assert store.roll_up() == 1
    archive = DayBucket.from_dict(store._data["engines"]["claude"][ARCHIVE_KEY])
    assert archive.run_count == 107
    assert archive.last_run_ts == 999.0
    # Reloading the saved file keeps the archive (save/load round trip).
    store._save()
    reloaded = SessionStatsStore(path)
    assert _totals(reloaded.aggregate(period="all")) == _totals(
        store.aggregate(period="all")
    )


def test_897_old_format_file_loads_unchanged(tmp_path, monkeypatch) -> None:
    """A pre-#897 file with only recent buckets (no archive) loads as-is and
    is not rewritten."""
    import untether.session_stats as session_stats

    writes: list = []
    monkeypatch.setattr(
        session_stats, "atomic_write_json", lambda *a, **k: writes.append(a)
    )
    path = tmp_path / "stats.json"
    engines = {
        "claude": {_days_ago(2): _bucket(3, 10.0), _days_ago(0): _bucket(1, 20.0)}
    }
    _write_stats(path, engines)

    store = SessionStatsStore(path)

    assert store._data == {"version": 1, "engines": engines}
    assert writes == []


def test_897_roll_up_uses_atomic_save(tmp_path, monkeypatch) -> None:
    import untether.session_stats as session_stats

    writes: list = []
    real = session_stats.atomic_write_json

    def _spy(path, data, **kwargs):
        writes.append(path)
        return real(path, data, **kwargs)

    monkeypatch.setattr(session_stats, "atomic_write_json", _spy)
    path = tmp_path / "stats.json"
    _write_stats(path, _old_and_recent())

    SessionStatsStore(path)

    assert writes == [path]


def test_897_record_run_rolls_up_once_per_new_day(tmp_path, monkeypatch) -> None:
    import untether.session_stats as session_stats
    from untether.session_stats import ARCHIVE_KEY

    today = {"value": "2026-10-03"}
    monkeypatch.setattr(session_stats, "_today", lambda: today["value"])
    path = tmp_path / "stats.json"
    _write_stats(path, {"claude": {"2026-07-05": _bucket(2, 10.0)}})
    store = SessionStatsStore(path)  # 2026-07-05 is exactly 90 days old: kept
    assert ARCHIVE_KEY not in store._data["engines"]["claude"]

    calls: list[str] = []
    real_roll_up = SessionStatsStore.roll_up

    def _counting(self) -> int:
        calls.append(today["value"])
        return real_roll_up(self)

    monkeypatch.setattr(SessionStatsStore, "roll_up", _counting)
    store.record_run("claude", actions=1, duration_ms=1)
    store.record_run("claude", actions=1, duration_ms=1)
    assert calls == []  # same day as the init roll-up

    today["value"] = "2026-10-04"
    store.record_run("claude", actions=1, duration_ms=1)
    store.record_run("claude", actions=1, duration_ms=1)
    assert calls == ["2026-10-04"]
    claude = store._data["engines"]["claude"]
    assert "2026-07-05" not in claude
    assert DayBucket.from_dict(claude[ARCHIVE_KEY]).run_count == 2
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["engines"]["claude"][ARCHIVE_KEY]["run_count"] == 2
    assert on_disk["engines"]["claude"]["2026-10-04"]["run_count"] == 2


def test_897_roll_up_skips_non_date_keys(tmp_path) -> None:
    path = tmp_path / "stats.json"
    _write_stats(
        path, {"claude": {"junk": _bucket(1, 1.0), "2020-01-01": _bucket(1, 2.0)}}
    )
    store = SessionStatsStore(path)
    assert "junk" in store._data["engines"]["claude"]


def test_897_no_log_when_nothing_to_roll_up(tmp_path) -> None:
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        store = SessionStatsStore(tmp_path / "stats.json")
        store.record_run("claude", actions=1, duration_ms=1)
    assert not [e for e in logs if e.get("event") == "session_stats.rolled_up"]


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


# ── #271 Tier 3: triggered/manual breakdown ────────────────────────────────


def test_day_bucket_record_manual_default() -> None:
    bucket = DayBucket()
    bucket.record(actions=1, duration_ms=100)
    assert bucket.manual_count == 1
    assert bucket.triggered_count == 0


def test_day_bucket_record_triggered() -> None:
    bucket = DayBucket()
    bucket.record(actions=1, duration_ms=100, triggered=True)
    assert bucket.triggered_count == 1
    assert bucket.manual_count == 0


def test_day_bucket_mixed_records_split() -> None:
    bucket = DayBucket()
    bucket.record(actions=1, duration_ms=100, triggered=True)
    bucket.record(actions=1, duration_ms=100)
    bucket.record(actions=1, duration_ms=100, triggered=True)
    assert bucket.run_count == 3
    assert bucket.triggered_count == 2
    assert bucket.manual_count == 1


def test_day_bucket_roundtrip_includes_breakdown() -> None:
    bucket = DayBucket(
        run_count=3,
        action_count=10,
        duration_ms=5000,
        last_run_ts=1000.0,
        triggered_count=2,
        manual_count=1,
    )
    restored = DayBucket.from_dict(bucket.to_dict())
    assert restored.triggered_count == 2
    assert restored.manual_count == 1


def test_day_bucket_from_dict_old_format_defaults_zero() -> None:
    """Old stats.json files (pre-#271) lack triggered_count/manual_count."""
    legacy = {
        "run_count": 5,
        "action_count": 25,
        "duration_ms": 10000,
        "last_run_ts": 1000.0,
    }
    restored = DayBucket.from_dict(legacy)
    assert restored.run_count == 5
    assert restored.triggered_count == 0
    assert restored.manual_count == 0


def test_store_record_run_triggered_kwarg(tmp_path) -> None:
    store = SessionStatsStore(tmp_path / "stats.json")
    store.record_run("claude", actions=1, duration_ms=100, triggered=True)
    store.record_run("claude", actions=1, duration_ms=100)
    stats = store.aggregate(period="today")
    assert len(stats) == 1
    assert stats[0].triggered_count == 1
    assert stats[0].manual_count == 1


def test_store_aggregate_sums_triggered_and_manual(tmp_path) -> None:
    store = SessionStatsStore(tmp_path / "stats.json")
    # Inject two days for the same engine.
    store._data = {
        "version": 1,
        "engines": {
            "claude": {
                "2026-03-01": DayBucket(
                    run_count=2,
                    action_count=4,
                    duration_ms=2000,
                    last_run_ts=1000.0,
                    triggered_count=1,
                    manual_count=1,
                ).to_dict(),
                "2026-03-04": DayBucket(
                    run_count=3,
                    action_count=6,
                    duration_ms=3000,
                    last_run_ts=2000.0,
                    triggered_count=2,
                    manual_count=1,
                ).to_dict(),
            }
        },
    }
    stats = store.aggregate(period="all")
    assert len(stats) == 1
    assert stats[0].triggered_count == 3
    assert stats[0].manual_count == 2
