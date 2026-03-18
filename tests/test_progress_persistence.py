"""Tests for progress message persistence across restarts."""

from __future__ import annotations

from pathlib import Path

from untether.telegram.progress_persistence import (
    clear_all_progress,
    load_active_progress,
    register_progress,
    resolve_progress_path,
    unregister_progress,
)


def test_resolve_progress_path() -> None:
    p = resolve_progress_path(Path("/cfg/untether.toml"))
    assert p == Path("/cfg/active_progress.json")


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "active_progress.json"
    assert load_active_progress(path) == {}


def test_load_corrupt_file_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "active_progress.json"
    path.write_text("NOT JSON!", encoding="utf-8")
    assert load_active_progress(path) == {}


def test_load_non_dict_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "active_progress.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_active_progress(path) == {}


def test_register_and_load(tmp_path: Path) -> None:
    path = tmp_path / "active_progress.json"
    register_progress(path, "123:456", chat_id=123, message_id=456)
    entries = load_active_progress(path)
    assert entries == {"123:456": {"chat_id": 123, "message_id": 456}}


def test_unregister_removes_entry(tmp_path: Path) -> None:
    path = tmp_path / "active_progress.json"
    register_progress(path, "123:456", chat_id=123, message_id=456)
    unregister_progress(path, "123:456")
    assert load_active_progress(path) == {}


def test_unregister_nonexistent_is_noop(tmp_path: Path) -> None:
    path = tmp_path / "active_progress.json"
    register_progress(path, "123:456", chat_id=123, message_id=456)
    unregister_progress(path, "999:999")
    assert "123:456" in load_active_progress(path)


def test_multiple_entries(tmp_path: Path) -> None:
    path = tmp_path / "active_progress.json"
    register_progress(path, "1:10", chat_id=1, message_id=10)
    register_progress(path, "2:20", chat_id=2, message_id=20)
    entries = load_active_progress(path)
    assert len(entries) == 2
    assert entries["1:10"]["chat_id"] == 1
    assert entries["2:20"]["message_id"] == 20


def test_clear_all(tmp_path: Path) -> None:
    path = tmp_path / "active_progress.json"
    register_progress(path, "1:10", chat_id=1, message_id=10)
    register_progress(path, "2:20", chat_id=2, message_id=20)
    clear_all_progress(path)
    assert load_active_progress(path) == {}


def test_clear_nonexistent_is_noop(tmp_path: Path) -> None:
    path = tmp_path / "active_progress.json"
    clear_all_progress(path)  # should not raise
