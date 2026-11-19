"""Tests for run_once persistent state (#317).

Covers ``triggers/run_once_state.py`` helpers and the TriggerManager
integration that filters and persists fired one-shots across reloads.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from untether.triggers.manager import TriggerManager
from untether.triggers.run_once_state import (
    load_fired_state,
    resolve_state_path,
    save_fired_state,
)
from untether.triggers.settings import (
    parse_trigger_config,
    parse_trigger_config_isolated,
)


def test_resolve_state_path_is_sibling_of_config() -> None:
    config = Path("/home/user/.untether/untether.toml")
    state = resolve_state_path(config)
    assert state.name == "run_once_fired.json"
    assert state.parent == config.parent


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_fired_state(tmp_path / "missing.json") == {}


def test_load_corrupt_file_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("not json{", encoding="utf-8")
    assert load_fired_state(path) == {}


def test_load_wrong_shape_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text('{"not_fired_key": "x"}', encoding="utf-8")
    assert load_fired_state(path) == {}
    path.write_text('{"fired": "should be a dict"}', encoding="utf-8")
    assert load_fired_state(path) == {}


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_fired_state(
        path,
        {
            "daily-ping": "2026-04-17T09:00:00+00:00",
            "one-shot": "2026-04-17T10:00:00+00:00",
        },
    )
    assert load_fired_state(path) == {
        "daily-ping": "2026-04-17T09:00:00+00:00",
        "one-shot": "2026-04-17T10:00:00+00:00",
    }


def test_load_filters_non_string_values(tmp_path: Path) -> None:
    """Entries with non-string keys/values are silently dropped — the file
    must have been tampered with or written by an older/different process."""
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "fired": {
                    "valid": "2026-04-17T09:00:00",
                    "bad_value": 12345,
                }
            }
        ),
        encoding="utf-8",
    )
    assert load_fired_state(path) == {"valid": "2026-04-17T09:00:00"}


def test_893_pending_state_round_trip_and_separate_file(tmp_path: Path) -> None:
    """#893: the pending-retry map has its own sibling file, round-trips, and
    never touches (or reads) ``run_once_fired.json``."""
    from untether.triggers.run_once_state import (
        load_pending_state,
        resolve_pending_path,
        save_pending_state,
    )

    config = tmp_path / "untether.toml"
    path = resolve_pending_path(config)
    assert path.name == "run_once_pending.json"
    assert path.parent == config.parent
    assert load_pending_state(path) == {}  # missing → nothing pending
    save_pending_state(path, {"once": "2026-10-03T02:30:00+00:00"})
    assert load_pending_state(path) == {"once": "2026-10-03T02:30:00+00:00"}
    assert not resolve_state_path(config).exists()
    # A fired-state file is not a pending file (and vice versa).
    assert load_fired_state(path) == {}
    path.write_text("not json{", encoding="utf-8")
    assert load_pending_state(path) == {}


def test_893_manager_drops_unparseable_and_orphaned_pending(tmp_path: Path) -> None:
    """#893: on load, a pending entry with a bad timestamp is ignored and one
    whose cron is no longer in the TOML is pruned from disk."""
    from untether.triggers.run_once_state import (
        load_pending_state,
        resolve_pending_path,
        save_pending_state,
    )

    config = tmp_path / "untether.toml"
    save_pending_state(
        resolve_pending_path(config),
        {
            "kept": "2026-10-03T02:30:00+00:00",
            "naive": "2026-10-03T02:30:00",
            "garbled": "yesterday-ish",
            "gone": "2026-10-03T02:30:00+00:00",
        },
    )
    mgr = TriggerManager(
        _make_run_once_settings(["kept", "naive", "garbled"]), config_path=config
    )
    since = mgr.run_once_pending_since("kept")
    assert since is not None and since.utcoffset() is not None
    naive = mgr.run_once_pending_since("naive")
    assert naive is not None and naive.utcoffset() is not None
    assert mgr.run_once_pending_since("garbled") is None
    assert mgr.run_once_pending_since("gone") is None
    assert sorted(load_pending_state(resolve_pending_path(config))) == [
        "kept",
        "naive",
    ]


# ---------------------------------------------------------------------------
# TriggerManager integration
# ---------------------------------------------------------------------------


def _make_run_once_settings(ids: list[str]) -> object:
    return parse_trigger_config(
        {
            "enabled": True,
            "crons": [
                {
                    "id": cid,
                    "schedule": "0 9 * * *",
                    "prompt": f"fire {cid}",
                    "run_once": True,
                }
                for cid in ids
            ],
        }
    )


def test_manager_without_config_path_is_in_memory_only(tmp_path: Path) -> None:
    """Legacy behaviour preserved — no config_path means no state persistence."""
    mgr = TriggerManager(_make_run_once_settings(["one"]))
    assert mgr.remove_cron("one") is True
    # No file written, no fired state persisted.
    assert not (tmp_path / "run_once_fired.json").exists()
    assert mgr.fired_run_once_ids() == ["one"]  # still tracked in-memory


def test_manager_persists_fired_across_reload(tmp_path: Path) -> None:
    """#317 core case: a run_once cron that fired once must NOT re-activate
    when the config is reloaded."""
    config_path = tmp_path / "untether.toml"
    mgr = TriggerManager(_make_run_once_settings(["daily"]), config_path=config_path)
    assert mgr.crons[0].id == "daily"

    # Fire it — recorded to state file.
    assert mgr.remove_cron("daily") is True
    state_file = tmp_path / "run_once_fired.json"
    assert state_file.exists()
    assert "daily" in load_fired_state(state_file)

    # Reload same config — daily must NOT re-enter the active list.
    mgr.update(_make_run_once_settings(["daily"]))
    assert mgr.crons == []


def test_manager_restart_rehydrates_fired_set(tmp_path: Path) -> None:
    """A fresh TriggerManager constructed from a config path sees previously
    fired crons and excludes them from the active list."""
    config_path = tmp_path / "untether.toml"
    # Simulate a previous process having fired 'daily' earlier.
    save_fired_state(resolve_state_path(config_path), {"daily": "2026-04-17T09:00:00"})

    mgr = TriggerManager(_make_run_once_settings(["daily"]), config_path=config_path)
    assert mgr.crons == []  # filtered out on initial load
    assert "daily" in mgr.fired_run_once_ids()


def test_manager_drops_stale_fired_entries_on_reload(tmp_path: Path) -> None:
    """If a run_once cron is removed from untether.toml entirely, its fired
    entry is cleaned from state so the id is free to be reused with a fresh
    schedule."""
    config_path = tmp_path / "untether.toml"
    save_fired_state(
        resolve_state_path(config_path),
        {"removed": "2026-04-17", "still-here": "2026-04-17"},
    )
    mgr = TriggerManager(
        _make_run_once_settings(["still-here"]), config_path=config_path
    )

    # On the update() above, 'removed' should have been pruned.
    assert mgr.fired_run_once_ids() == ["still-here"]
    on_disk = load_fired_state(resolve_state_path(config_path))
    assert list(on_disk) == ["still-here"]


def _run_once_raw(schedule: str = "0 9 * * *") -> dict:
    return {
        "enabled": True,
        "crons": [
            {
                "id": "deploy-once",
                "schedule": schedule,
                "prompt": "deploy",
                "run_once": True,
            }
        ],
    }


def test_disable_then_enable_keeps_fired_one_shot(tmp_path: Path) -> None:
    """#1279: turning triggers off and on must not re-arm a fired one-shot."""
    config_path = tmp_path / "untether.toml"
    mgr = TriggerManager(
        _make_run_once_settings(["deploy-once"]), config_path=config_path
    )
    assert mgr.remove_cron("deploy-once") is True

    mgr.disable()
    assert mgr.crons == []
    assert mgr.fired_run_once_ids() == ["deploy-once"]

    mgr.update(_make_run_once_settings(["deploy-once"]))
    assert mgr.cron_ids() == []
    # A restart after the toggle still sees it as fired.
    assert "deploy-once" in load_fired_state(resolve_state_path(config_path))


def test_briefly_invalid_entry_keeps_fired_one_shot(tmp_path: Path) -> None:
    """#1279: a skipped (invalid) entry is still in the TOML, so its fired
    state survives; fixing the typo doesn't re-arm the one-shot."""
    config_path = tmp_path / "untether.toml"
    first = parse_trigger_config_isolated(_run_once_raw())
    mgr = TriggerManager(
        first.settings, config_path=config_path, keep_fired=first.cron_ids
    )
    assert mgr.remove_cron("deploy-once") is True

    typo = parse_trigger_config_isolated(_run_once_raw("0 0 9 * * *"))
    assert typo.settings.crons == [] and typo.invalid
    assert typo.cron_ids == {"deploy-once"}
    mgr.update(typo.settings, keep_fired=typo.cron_ids)
    assert mgr.fired_run_once_ids() == ["deploy-once"]

    fixed = parse_trigger_config_isolated(_run_once_raw())
    mgr.update(fixed.settings, keep_fired=fixed.cron_ids)
    assert mgr.cron_ids() == []

    # Removing it from the TOML still frees the id (#317 behaviour kept).
    gone = parse_trigger_config_isolated({"enabled": True, "crons": []})
    mgr.update(gone.settings, keep_fired=gone.cron_ids)
    assert mgr.fired_run_once_ids() == []


@pytest.mark.anyio
async def test_save_failure_does_not_raise(tmp_path: Path, monkeypatch) -> None:
    """Disk-full / permission errors must not propagate — the run_once
    persistence is best-effort."""
    from untether.triggers import run_once_state

    def explode(_path, _fired):
        raise OSError("disk full")

    # save_fired_state catches OSError internally already; verify that
    # the higher-level atomic_write_json path also degrades gracefully.
    monkeypatch.setattr(run_once_state, "atomic_write_json", explode)
    run_once_state.save_fired_state(tmp_path / "state.json", {"x": "y"})  # no raise
