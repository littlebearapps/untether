"""Is the agy gate loaded for a run? (#980, 13 §6): the install check, agy's
own ``-p /hooks`` listing, the cache and self-protection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.antigravity_gate_support import hooks_listing, install_plugin
from untether.antigravity import gate_plugin, gate_precheck
from untether.runners.antigravity import AntigravityRunner
from untether.utils import antigravity_quota, antigravity_scan


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    gate_precheck.clear_cache()


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub ``agy -p /hooks``: returns ``state["data"]`` or raises it."""
    state: dict[str, Any] = {"data": hooks_listing(), "calls": []}

    async def fake(runner: Any, cwd: Path) -> dict[str, Any]:
        state["calls"].append(cwd)
        data = state["data"]
        if isinstance(data, Exception):
            raise data
        return data

    monkeypatch.setattr(gate_precheck, "_probe_hooks", fake)
    return state


def _groups(data: dict[str, Any]) -> list[gate_precheck.HookGroup]:
    return gate_precheck.parse_hooks(data)


def test_precheck_parses_hooks_command_result() -> None:
    result = gate_precheck.evaluate_hooks(_groups(hooks_listing()), wait_s=900)
    assert result.ok and result.foreign_sources == ()


def test_precheck_rejects_wrong_command_string() -> None:
    data = hooks_listing()
    data["hooks"][0]["actions"][1]["command"] = "exit 0"
    result = gate_precheck.evaluate_hooks(_groups(data), wait_s=900)
    assert not result.ok and result.reason == gate_precheck.MISMATCH
    assert result.detail == "PreToolUse: command"


def test_precheck_rejects_short_timeout() -> None:
    # Installed for a 900 s wait, but agy only allows 300 s.
    data = hooks_listing(wait_s=240)
    result = gate_precheck.evaluate_hooks(_groups(data), wait_s=900)
    assert not result.ok and result.detail == "PreToolUse: timeout"


def test_precheck_default_timeout_is_too_short() -> None:
    data = hooks_listing()
    del data["hooks"][0]["actions"][1]["timeout_seconds"]  # agy's 30 s default
    assert not gate_precheck.evaluate_hooks(_groups(data), wait_s=900).ok


def test_precheck_rejects_disabled_group() -> None:
    result = gate_precheck.evaluate_hooks(
        _groups(hooks_listing(enabled=False)), wait_s=900
    )
    assert not result.ok and result.reason == gate_precheck.DISABLED


def test_precheck_rejects_missing_or_duplicate_group() -> None:
    assert (
        gate_precheck.evaluate_hooks([], wait_s=900).reason == gate_precheck.NOT_LISTED
    )
    data = hooks_listing()
    data["hooks"].append(dict(data["hooks"][0]))
    assert (
        gate_precheck.evaluate_hooks(_groups(data), wait_s=900).reason
        == gate_precheck.NOT_LISTED
    )


def test_precheck_rejects_wrong_source_and_extra_event() -> None:
    data = hooks_listing()
    data["hooks"][0]["source"] = "<proj>/.agents/hooks.json"
    assert gate_precheck.evaluate_hooks(_groups(data), wait_s=900).detail == "source"
    # Security review: a project plugin of the same name isn't ours.
    data["hooks"][0]["source"] = "/proj/.agents/plugins/untether-gate/hooks.json"
    assert gate_precheck.evaluate_hooks(_groups(data), wait_s=900).detail == "source"
    data["hooks"][0]["source"] = str(gate_plugin.plugin_dir() / "hooks.json")
    assert gate_precheck.evaluate_hooks(_groups(data), wait_s=900).ok
    data = hooks_listing()
    data["hooks"][0]["actions"].append(
        {"event": "PostToolUse", "type": "command", "command": "x"}
    )
    result = gate_precheck.evaluate_hooks(_groups(data), wait_s=900)
    assert not result.ok and "PostToolUse" in (result.detail or "")


def test_foreign_hook_groups_recorded() -> None:
    """REVIEW-2 B2: phase 14 refuses gated modes while any is present."""
    extra = [
        {
            "name": "theirs",
            "enabled": True,
            "source": "<proj>/.agents/hooks.json",
            "actions": [{"event": "Stop", "type": "command", "command": "x"}],
        }
    ]
    result = gate_precheck.evaluate_hooks(
        _groups(hooks_listing(extra_groups=extra)), wait_s=900
    )
    assert result.ok
    assert result.foreign_sources == ("<proj>/.agents/hooks.json",)


@pytest.mark.anyio
async def test_not_installed_never_runs_agy(
    tmp_path: Path, probe: dict[str, Any]
) -> None:
    result = await gate_precheck.check(AntigravityRunner(), tmp_path)
    assert not result.ok and result.reason == gate_precheck.MISSING
    assert probe["calls"] == []


@pytest.mark.anyio
async def test_edited_plugin_fails_without_running_agy(
    tmp_path: Path, probe: dict[str, Any]
) -> None:
    folder = install_plugin()
    (folder / "hooks.json").write_text("{}")
    result = await gate_precheck.check(AntigravityRunner(), tmp_path)
    assert not result.ok and result.reason == gate_precheck.EDITED
    assert probe["calls"] == []
    assert gate_precheck.latest(tmp_path) is result


@pytest.mark.anyio
async def test_ready_and_cached(tmp_path: Path, probe: dict[str, Any]) -> None:
    install_plugin()
    runner = AntigravityRunner()
    first = await gate_precheck.check(runner, tmp_path, scan_digest="d1")
    second = await gate_precheck.check(runner, tmp_path, scan_digest="d1")
    assert first.ok and second is first
    assert probe["calls"] == [tmp_path]
    # A different planted-config digest for the cwd is a cache miss.
    await gate_precheck.check(runner, tmp_path, scan_digest="d2")
    assert len(probe["calls"]) == 2


@pytest.mark.anyio
async def test_cache_key_changes_with_cwd_and_agy_state_files(
    tmp_path: Path, probe: dict[str, Any]
) -> None:
    install_plugin()
    runner = AntigravityRunner()
    other = tmp_path / "other"
    other.mkdir()
    await gate_precheck.check(runner, tmp_path)
    await gate_precheck.check(runner, other)
    assert len(probe["calls"]) == 2
    # `agy plugin disable` writes config.json: stat changes → re-check.
    (antigravity_scan.user_config_dir() / "config.json").write_text('{"plugins": {}}')
    await gate_precheck.check(runner, tmp_path)
    assert len(probe["calls"]) == 3


@pytest.mark.anyio
async def test_failure_cached_and_reasoned(
    tmp_path: Path, probe: dict[str, Any]
) -> None:
    install_plugin()
    probe["data"] = antigravity_quota.AntigravityNotSignedIn()
    runner = AntigravityRunner()
    result = await gate_precheck.check(runner, tmp_path)
    assert not result.ok and result.reason == gate_precheck.NOT_SIGNED_IN
    again = await gate_precheck.check(runner, tmp_path)
    assert again is result and len(probe["calls"]) == 1
    probe["data"] = antigravity_quota.AgySlashError("timeout")
    fresh = await gate_precheck.check(runner, tmp_path, use_cache=False)
    assert fresh.reason == gate_precheck.CHECK_FAILED and fresh.detail == "timeout"


@pytest.mark.anyio
async def test_disabled_plugin_refused_before_listing(
    tmp_path: Path, probe: dict[str, Any]
) -> None:
    install_plugin()
    gate_plugin.agy_plugin_state_path().write_text(
        '{"plugins": {"untether-gate": {"enabled": false}}}'
    )
    result = await gate_precheck.check(AntigravityRunner(), tmp_path)
    assert result.reason == gate_precheck.DISABLED and probe["calls"] == []


@pytest.mark.anyio
async def test_clear_cache_for_one_project(
    tmp_path: Path, probe: dict[str, Any]
) -> None:
    install_plugin()
    runner = AntigravityRunner()
    await gate_precheck.check(runner, tmp_path)
    gate_precheck.clear_cache(tmp_path)
    assert gate_precheck.latest(tmp_path) is None
    await gate_precheck.check(runner, tmp_path)
    assert len(probe["calls"]) == 2


@pytest.mark.anyio
async def test_precheck_records_project_containing_untether_package(
    probe: dict[str, Any],
) -> None:
    """REVIEW-2 B3: recorded on the result (phase 14's gated modes refuse)."""
    import untether

    install_plugin()
    package = Path(untether.__file__).resolve().parent
    result = await gate_precheck.check(AntigravityRunner(), package.parent)
    assert result.ok and result.self_protect == str(package)
    assert gate_precheck.self_protect_path(Path("/nonexistent/elsewhere")) is None


def test_self_protect_covers_config_folder_and_base_python(tmp_path: Path) -> None:
    """Security review 3 M5: a project holding this instance's config
    folder, or the Python Untether runs on, can't have Ask me."""
    import sys

    config = tmp_path / "inst" / "untether.toml"
    config.parent.mkdir()
    assert gate_precheck.self_protect_path(tmp_path, config_path=config) == (
        config.parent.resolve()
    )
    assert (
        gate_precheck.self_protect_path(tmp_path / "elsewhere", config_path=config)
        is None
    )
    base = Path(sys.base_prefix).resolve()
    assert gate_precheck.self_protect_path(base) is not None


def test_reason_text_names_the_fix() -> None:
    assert "untether agy-gate install" in gate_precheck.reason_text(
        gate_precheck.MISSING
    )
    assert gate_precheck.reason_text("weird") == "weird"
