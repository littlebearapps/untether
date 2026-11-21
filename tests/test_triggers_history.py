"""Tests for the trigger ``last_fired_at`` history store (#271 Tier 3)."""

from __future__ import annotations

import json

import pytest

from untether.triggers import history


@pytest.fixture(autouse=True)
def _reset_singleton():
    history.reset_history()
    yield
    history.reset_history()


def test_record_and_get_round_trip(tmp_path):
    history.init_history(tmp_path / "untether.toml")
    history.record_fired("cron", "daily-review")
    ts = history.get_last_fired("cron", "daily-review")
    assert ts is not None
    assert ts > 0


def test_missing_trigger_returns_none(tmp_path):
    history.init_history(tmp_path / "untether.toml")
    assert history.get_last_fired("cron", "never-fired") is None


def test_record_no_op_when_uninitialised():
    # Singleton not initialised — should be a no-op, not raise.
    history.record_fired("cron", "orphan")
    assert history.get_last_fired("cron", "orphan") is None


def test_persistence_across_init(tmp_path):
    config_path = tmp_path / "untether.toml"
    history.init_history(config_path)
    history.record_fired("cron", "cron-a")
    first = history.get_last_fired("cron", "cron-a")
    assert first is not None

    # Reset singleton (simulates restart) and re-init.
    history.reset_history()
    history.init_history(config_path)
    second = history.get_last_fired("cron", "cron-a")
    assert second == first


def test_corrupt_json_resets_to_empty(tmp_path):
    state_path = tmp_path / history.STATE_FILENAME
    state_path.write_text("{not json", encoding="utf-8")
    config_path = tmp_path / "untether.toml"
    history.init_history(config_path)
    # Corrupt file → empty in-memory state → record/get still work.
    history.record_fired("cron", "cron-after-corrupt")
    assert history.get_last_fired("cron", "cron-after-corrupt") is not None


def test_version_mismatch_resets_to_empty(tmp_path):
    state_path = tmp_path / history.STATE_FILENAME
    state_path.write_text(
        json.dumps({"version": 999, "triggers": {"old": 1.0}}), encoding="utf-8"
    )
    config_path = tmp_path / "untether.toml"
    history.init_history(config_path)
    # Old data should be discarded; only fresh entries persist.
    assert history.get_last_fired("cron", "old") is None
    history.record_fired("cron", "fresh")
    assert history.get_last_fired("cron", "fresh") is not None


def test_state_file_lives_next_to_config(tmp_path):
    config_path = tmp_path / "untether.toml"
    expected = tmp_path / history.STATE_FILENAME
    history.init_history(config_path)
    history.record_fired("cron", "cron-x")
    assert expected.exists()


def test_resolve_history_path_uses_filename_constant(tmp_path):
    config_path = tmp_path / "untether.toml"
    assert history.resolve_history_path(config_path).name == history.STATE_FILENAME
    assert history.resolve_history_path(config_path).parent == config_path.parent


def test_record_overwrites_previous_timestamp(tmp_path):
    history.init_history(tmp_path / "untether.toml")
    history.record_fired("cron", "cron-a")
    first = history.get_last_fired("cron", "cron-a")
    # Force a small delay so the second timestamp differs.
    import time

    time.sleep(0.01)
    history.record_fired("cron", "cron-a")
    second = history.get_last_fired("cron", "cron-a")
    assert second is not None and first is not None
    assert second >= first


def test_corrupt_triggers_field_falls_back_to_empty(tmp_path):
    state_path = tmp_path / history.STATE_FILENAME
    # Valid version, but `triggers` is the wrong type.
    state_path.write_text(
        json.dumps({"version": 1, "triggers": ["not", "a", "dict"]}),
        encoding="utf-8",
    )
    config_path = tmp_path / "untether.toml"
    history.init_history(config_path)
    history.record_fired("cron", "fresh")
    assert history.get_last_fired("cron", "fresh") is not None


def test_1123_cron_and_webhook_with_one_id_do_not_collide(tmp_path):
    history.init_history(tmp_path / "untether.toml")
    history.record_fired("webhook", "shared")
    assert history.get_last_fired("cron", "shared") is None
    assert history.get_last_fired("webhook", "shared") is not None
    raw = json.loads((tmp_path / history.STATE_FILENAME).read_text())
    assert list(raw["triggers"]) == ["webhook:shared"]


def test_1123_reads_a_0361_bare_key(tmp_path):
    (tmp_path / history.STATE_FILENAME).write_text(
        json.dumps({"version": 1, "triggers": {"daily": 123.0}}), encoding="utf-8"
    )
    history.init_history(tmp_path / "untether.toml")
    assert history.get_last_fired("cron", "daily") == 123.0


def test_1123_prune_drops_removed_triggers(tmp_path):
    (tmp_path / history.STATE_FILENAME).write_text(
        json.dumps(
            {
                "version": 1,
                "triggers": {
                    "cron:kept": 1.0,
                    "cron:gone": 1.0,
                    "webhook:hook": 1.0,
                    "legacy-kept": 1.0,
                    "legacy-gone": 1.0,
                },
            }
        ),
        encoding="utf-8",
    )
    history.init_history(tmp_path / "untether.toml")
    history.prune_history(["kept", "legacy-kept"], ["hook"])
    raw = json.loads((tmp_path / history.STATE_FILENAME).read_text())
    assert sorted(raw["triggers"]) == ["cron:kept", "legacy-kept", "webhook:hook"]
