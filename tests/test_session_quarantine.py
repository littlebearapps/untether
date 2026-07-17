"""Tests for persisted session quarantine store (#631, #632)."""

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
