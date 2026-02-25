"""Tests for the /restart command."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from untether.commands import CommandContext, CommandResult
from untether.shutdown import is_shutting_down, reset_shutdown
from untether.telegram.commands.restart import RestartCommand
from untether.transport import MessageRef


def _make_ctx() -> CommandContext:
    """Build a minimal CommandContext for testing."""
    return CommandContext(
        command="restart",
        text="/restart",
        args_text="",
        args=(),
        message=MessageRef(channel_id=123, message_id=1),
        reply_to=None,
        reply_text=None,
        config_path=Path("/tmp/untether.toml"),
        plugin_config={},
        runtime=MagicMock(),
        executor=MagicMock(),
    )


class TestRestartCommand:
    def setup_method(self) -> None:
        reset_shutdown()

    def teardown_method(self) -> None:
        reset_shutdown()

    @pytest.mark.anyio
    async def test_restart_triggers_shutdown(self) -> None:
        cmd = RestartCommand()
        result = await cmd.handle(_make_ctx())

        assert isinstance(result, CommandResult)
        assert "Draining" in result.text
        assert is_shutting_down() is True

    @pytest.mark.anyio
    async def test_restart_idempotent(self) -> None:
        cmd = RestartCommand()
        await cmd.handle(_make_ctx())
        result = await cmd.handle(_make_ctx())

        assert isinstance(result, CommandResult)
        assert "Already restarting" in result.text

    def test_command_id(self) -> None:
        cmd = RestartCommand()
        assert cmd.id == "restart"
