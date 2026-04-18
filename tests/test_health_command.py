"""Tests for the `/health` command (#348)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from untether.commands import CommandContext, CommandResult
from untether.telegram.commands.health import (
    HealthCommand,
    _format_mb,
    _read_meminfo_fields,
    render_health_snapshot,
)
from untether.transport import MessageRef


def _make_ctx(
    *, trigger_manager=None, config_path: Path | None = None
) -> CommandContext:
    return CommandContext(
        command="health",
        text="/health",
        args_text="",
        args=(),
        message=MessageRef(channel_id=123, message_id=1),
        reply_to=None,
        reply_text=None,
        config_path=config_path,
        plugin_config={},
        runtime=MagicMock(),
        executor=MagicMock(),
        trigger_manager=trigger_manager,
    )


def test_command_id() -> None:
    assert HealthCommand().id == "health"


@pytest.mark.anyio
async def test_handle_returns_html_command_result(tmp_path) -> None:
    cmd = HealthCommand()
    result = await cmd.handle(_make_ctx(config_path=tmp_path / "untether.toml"))
    assert isinstance(result, CommandResult)
    assert result.parse_mode == "HTML"
    assert result.notify is False
    assert "Untether health" in result.text


def test_render_includes_system_and_triggers(tmp_path) -> None:
    snapshot = render_health_snapshot(_make_ctx(config_path=tmp_path / "untether.toml"))
    assert "Untether health" in snapshot
    # Trigger line always present (even if "none configured" or "disabled")
    assert "triggers" in snapshot


def test_render_handles_no_trigger_manager(tmp_path) -> None:
    snapshot = render_health_snapshot(_make_ctx(trigger_manager=None))
    assert "triggers: disabled" in snapshot


def test_render_handles_empty_trigger_manager(tmp_path) -> None:
    mgr = MagicMock()
    mgr.cron_ids.return_value = []
    mgr.webhook_ids.return_value = []
    snapshot = render_health_snapshot(_make_ctx(trigger_manager=mgr))
    assert "triggers: none configured" in snapshot


def test_render_counts_triggers(tmp_path) -> None:
    mgr = MagicMock()
    mgr.cron_ids.return_value = ["daily-review", "weekly-summary"]
    mgr.webhook_ids.return_value = ["deploy"]
    snapshot = render_health_snapshot(_make_ctx(trigger_manager=mgr))
    assert "2 crons" in snapshot
    assert "1 webhook" in snapshot


def test_render_pluralisation_single_cron(tmp_path) -> None:
    mgr = MagicMock()
    mgr.cron_ids.return_value = ["only-one"]
    mgr.webhook_ids.return_value = []
    snapshot = render_health_snapshot(_make_ctx(trigger_manager=mgr))
    assert "1 cron" in snapshot
    assert "1 crons" not in snapshot  # no incorrect pluralisation


def test_format_mb_gb_boundary() -> None:
    assert _format_mb(1024 * 1024) == "1.0 GB"
    assert _format_mb(512 * 1024) == "512 MB"
    assert _format_mb(512) == "512 KB"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_read_meminfo_fields_live() -> None:
    """Live /proc/meminfo read — expect MemTotal + MemAvailable present."""
    mem = _read_meminfo_fields(("MemTotal", "MemAvailable"))
    assert "MemTotal" in mem
    assert "MemAvailable" in mem
    assert mem["MemTotal"] > 0
    assert mem["MemAvailable"] > 0


def test_read_meminfo_fields_returns_empty_on_non_linux(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert _read_meminfo_fields(("MemTotal",)) == {}


def test_read_meminfo_fields_handles_missing_file(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    import builtins

    real_open = builtins.open

    def fake_open(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(path, str) and path == "/proc/meminfo":
            raise FileNotFoundError(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    assert _read_meminfo_fields(("MemTotal",)) == {}


def test_render_shows_ram_line_on_linux(tmp_path) -> None:
    """On Linux with a healthy host, the RAM line appears with percent used."""
    if not sys.platform.startswith("linux"):
        pytest.skip("Linux only")
    snapshot = render_health_snapshot(_make_ctx(config_path=tmp_path / "untether.toml"))
    assert "RAM:" in snapshot
    # The line should include a percentage figure
    assert "%" in snapshot
