"""Tests for persisted session quarantine store (#631, #632)."""

import json
import time
from pathlib import Path

from untether.session_quarantine import (
    QuarantineStore,
    get_quarantine_store,
    resolve_quarantine_path,
    set_quarantine_store,
)


def test_quarantine_roundtrip(tmp_path: Path):
    p = tmp_path / "quarantine.json"
    store = QuarantineStore.load(p)
    assert store.is_quarantined("claude", "sid-1") is False
    store.quarantine("claude", "sid-1", reason="forced_teardown_after_result")
    assert store.is_quarantined("claude", "sid-1") is True
    store.flush()
    # reload from disk → survives restart
    store2 = QuarantineStore.load(p)
    assert store2.is_quarantined("claude", "sid-1") is True
    store2.clear("claude", "sid-1")
    assert store2.is_quarantined("claude", "sid-1") is False


def test_quarantine_isolated_by_engine(tmp_path: Path):
    store = QuarantineStore.load(tmp_path / "q.json")
    store.quarantine("claude", "sid", reason="x")
    assert store.is_quarantined("pi", "sid") is False


def test_resolve_quarantine_path_sibling_to_config():
    assert resolve_quarantine_path(Path("/x/untether.toml")) == Path(
        "/x/session_quarantine.json"
    )


def test_get_quarantine_store_singleton_and_injection(tmp_path: Path, monkeypatch):
    # Reset to None to start fresh
    set_quarantine_store(None)

    try:
        # Setup: point to temp dir
        config_path = tmp_path / "untether.toml"
        monkeypatch.setenv("UNTETHER_CONFIG_PATH", str(config_path))

        # First call creates store
        store1 = get_quarantine_store()
        assert store1.path == tmp_path / "session_quarantine.json"

        # Second call returns same object
        store2 = get_quarantine_store()
        assert store1 is store2

        # Custom injection replaces singleton
        custom_store = QuarantineStore.load(tmp_path / "custom.json")
        set_quarantine_store(custom_store)
        assert get_quarantine_store() is custom_store

    finally:
        # Cleanup: reset for other tests
        set_quarantine_store(None)


def test_load_with_malformed_ts_drops_invalid_entry(tmp_path: Path):
    """Fix #1: load() must not crash on malformed ts, should drop the entry."""
    p = tmp_path / "quarantine.json"
    # Write one entry with invalid ts and one valid entry
    data = {
        "claude:bad-ts": {"reason": "test", "ts": "not-a-number"},
        "claude:valid": {"reason": "test", "ts": time.time()},
    }
    p.write_text(json.dumps(data))

    # load() must not raise, should drop malformed, keep valid
    store = QuarantineStore.load(p)
    assert store.is_quarantined("claude", "bad-ts") is False
    assert store.is_quarantined("claude", "valid") is True


def test_load_with_non_dict_json_logs_and_continues(tmp_path: Path, caplog):
    """Fix #6: load() should log when JSON is not a dict and continue."""
    p = tmp_path / "quarantine.json"
    # Write JSON that is valid but not a dict (e.g., a list or string)
    p.write_text(json.dumps(["not", "a", "dict"]))

    # load() must not raise, should log the issue
    store = QuarantineStore.load(p)
    assert store._entries == {}


def test_prune_persists_stale_entries_removed_from_disk(tmp_path: Path):
    """Fix #5: load() should call flush() after prune, so stale entries are removed from disk."""
    p = tmp_path / "quarantine.json"
    # Write one entry older than 7 days and one fresh
    now = time.time()
    old_ts = now - (8 * 24 * 3600)  # 8 days ago
    data = {
        "claude:old": {"reason": "test", "ts": old_ts},
        "claude:fresh": {"reason": "test", "ts": now},
    }
    p.write_text(json.dumps(data))

    # load() should prune and flush, so on-disk JSON no longer has the old key
    store = QuarantineStore.load(p)
    assert store.is_quarantined("claude", "old") is False
    assert store.is_quarantined("claude", "fresh") is True

    # Verify the disk file was updated (stale entry removed)
    reloaded_data = json.loads(p.read_text())
    assert "claude:old" not in reloaded_data
    assert "claude:fresh" in reloaded_data
