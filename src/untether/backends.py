from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .runner import Runner

EngineConfig = dict[str, Any]


@dataclass(frozen=True, slots=True)
class SetupIssue:
    title: str
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EngineBackend:
    id: str
    build_runner: Callable[[EngineConfig, Path], Runner]
    cli_cmd: str | None = None
    install_cmd: str | None = None
    # #1089: for an engine whose binary can be configured or found off PATH,
    # returns the command ``build_runner`` spawns for this engine config.
    resolve_cli_cmd: Callable[[EngineConfig], str] | None = None

    def cli_command(self, config: EngineConfig | None = None) -> str:
        """The command the runner spawns for ``config`` — what availability
        checks look up, so they agree with the runner."""
        if self.resolve_cli_cmd is not None:
            return self.resolve_cli_cmd(config or {})
        return self.cli_cmd or self.id
