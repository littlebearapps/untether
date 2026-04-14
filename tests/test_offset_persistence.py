"""Tests for Telegram update_id offset persistence (#287)."""

from __future__ import annotations

import json
from pathlib import Path

from untether.telegram.offset_persistence import (
    STATE_FILENAME,
    DebouncedOffsetWriter,
    load_last_update_id,
    resolve_offset_path,
    save_last_update_id,
)


class TestResolveAndLoad:
    def test_resolve_offset_path_uses_config_sibling(self, tmp_path: Path):
        config_path = tmp_path / "untether.toml"
        assert resolve_offset_path(config_path) == tmp_path / STATE_FILENAME

    def test_load_missing_file_returns_none(self, tmp_path: Path):
        path = tmp_path / STATE_FILENAME
        assert load_last_update_id(path) is None

    def test_load_valid_payload(self, tmp_path: Path):
        path = tmp_path / STATE_FILENAME
        path.write_text(json.dumps({"last_update_id": 12345}), encoding="utf-8")
        assert load_last_update_id(path) == 12345

    def test_load_corrupt_json_returns_none(self, tmp_path: Path):
        path = tmp_path / STATE_FILENAME
        path.write_text("{not valid", encoding="utf-8")
        assert load_last_update_id(path) is None

    def test_load_wrong_type_returns_none(self, tmp_path: Path):
        path = tmp_path / STATE_FILENAME
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        assert load_last_update_id(path) is None

    def test_load_negative_value_returns_none(self, tmp_path: Path):
        path = tmp_path / STATE_FILENAME
        path.write_text(json.dumps({"last_update_id": -5}), encoding="utf-8")
        assert load_last_update_id(path) is None

    def test_load_string_value_returns_none(self, tmp_path: Path):
        path = tmp_path / STATE_FILENAME
        path.write_text(json.dumps({"last_update_id": "42"}), encoding="utf-8")
        assert load_last_update_id(path) is None


class TestSave:
    def test_save_then_load_round_trip(self, tmp_path: Path):
        path = tmp_path / STATE_FILENAME
        save_last_update_id(path, 999999)
        assert load_last_update_id(path) == 999999

    def test_save_no_leftover_tmp_file(self, tmp_path: Path):
        path = tmp_path / STATE_FILENAME
        save_last_update_id(path, 42)
        tmp_files = list(tmp_path.glob(f"{STATE_FILENAME}.tmp"))
        assert tmp_files == []

    def test_save_creates_parent_dir(self, tmp_path: Path):
        path = tmp_path / "nested" / "subdir" / STATE_FILENAME
        save_last_update_id(path, 7)
        assert load_last_update_id(path) == 7


class TestDebouncedWriter:
    def test_note_below_interval_does_not_flush(self, tmp_path: Path):
        path = tmp_path / STATE_FILENAME
        writer = DebouncedOffsetWriter(path, min_interval_s=1000.0, max_pending=1000)
        writer.note(1)
        writer.note(2)
        assert load_last_update_id(path) is None

    def test_note_after_interval_triggers_flush(self, tmp_path: Path, monkeypatch):
        path = tmp_path / STATE_FILENAME
        t = [100.0]
        monkeypatch.setattr(
            "untether.telegram.offset_persistence.time.monotonic", lambda: t[0]
        )
        writer = DebouncedOffsetWriter(path, min_interval_s=5.0, max_pending=1000)
        # First note within interval does not flush.
        t[0] = 101.0
        writer.note(10)
        assert load_last_update_id(path) is None

        # Subsequent notes within 5s still do not flush.
        t[0] = 102.0
        writer.note(11)
        writer.note(12)
        assert load_last_update_id(path) is None

        # After 5s since last_flush (was init time 100), next note flushes.
        t[0] = 106.0
        writer.note(13)
        assert load_last_update_id(path) == 13

    def test_max_pending_forces_flush_before_interval(self, tmp_path: Path):
        path = tmp_path / STATE_FILENAME
        writer = DebouncedOffsetWriter(path, min_interval_s=1_000_000.0, max_pending=3)
        # No flush until 3rd note (max_pending threshold).
        writer.note(1)
        writer.note(2)
        assert load_last_update_id(path) is None
        writer.note(3)
        assert load_last_update_id(path) == 3

    def test_flush_writes_latest_pending(self, tmp_path: Path):
        path = tmp_path / STATE_FILENAME
        writer = DebouncedOffsetWriter(path, min_interval_s=1_000_000.0)
        writer.note(7)
        writer.note(8)
        writer.note(9)
        # No automatic flush yet.
        assert load_last_update_id(path) is None

        # Explicit flush commits the latest pending.
        writer.flush()
        assert load_last_update_id(path) == 9

    def test_flush_no_pending_is_noop(self, tmp_path: Path):
        path = tmp_path / STATE_FILENAME
        writer = DebouncedOffsetWriter(path)
        writer.flush()
        assert load_last_update_id(path) is None
