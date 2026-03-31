"""Stable public API for Untether plugins."""

from __future__ import annotations

from .backends import EngineBackend, EngineConfig, SetupIssue
from .backends_helpers import install_issue
from .commands import (
    CommandBackend,
    CommandContext,
    CommandExecutor,
    CommandResult,
    RunMode,
    RunRequest,
    RunResult,
    get_command,
    list_command_ids,
)
from .config import HOME_CONFIG_PATH, ConfigError, read_config, write_config
from .context import RunContext
from .directives import DirectiveError
from .engines import list_backends
from .events import EventFactory
from .ids import RESERVED_COMMAND_IDS
from .logging import bind_run_context, clear_context, get_logger, suppress_logs
from .model import (
    Action,
    ActionEvent,
    CompletedEvent,
    EngineId,
    ResumeToken,
    StartedEvent,
)
from .presenter import Presenter
from .progress import ActionState, ProgressState, ProgressTracker
from .router import RunnerUnavailableError
from .runner import BaseRunner, JsonlSubprocessRunner, Runner
from .runner_bridge import (
    ExecBridgeConfig,
    IncomingMessage,
    RunningTask,
    RunningTasks,
    handle_message,
)
from .scheduler import ThreadJob, ThreadScheduler
from .settings import load_settings
from .transport import MessageRef, RenderedMessage, SendOptions, Transport
from .transport_runtime import ResolvedMessage, ResolvedRunner, TransportRuntime
from .transports import SetupResult, TransportBackend
from .utils.paths import reset_run_base_dir, set_run_base_dir

TAKOPI_PLUGIN_API_VERSION = 1

__all__ = [
    "HOME_CONFIG_PATH",
    "RESERVED_COMMAND_IDS",
    "TAKOPI_PLUGIN_API_VERSION",
    "Action",
    "ActionEvent",
    "ActionState",
    "BaseRunner",
    "CommandBackend",
    "CommandContext",
    "CommandExecutor",
    "CommandResult",
    "CompletedEvent",
    "ConfigError",
    "DirectiveError",
    "EngineBackend",
    "EngineConfig",
    "EngineId",
    "EventFactory",
    "ExecBridgeConfig",
    "IncomingMessage",
    "JsonlSubprocessRunner",
    "MessageRef",
    "Presenter",
    "ProgressState",
    "ProgressTracker",
    "RenderedMessage",
    "ResolvedMessage",
    "ResolvedRunner",
    "ResumeToken",
    "RunContext",
    "RunMode",
    "RunRequest",
    "RunResult",
    "Runner",
    "RunnerUnavailableError",
    "RunningTask",
    "RunningTasks",
    "SendOptions",
    "SetupIssue",
    "SetupResult",
    "StartedEvent",
    "ThreadJob",
    "ThreadScheduler",
    "Transport",
    "TransportBackend",
    "TransportRuntime",
    "bind_run_context",
    "clear_context",
    "get_command",
    "get_logger",
    "handle_message",
    "install_issue",
    "list_backends",
    "list_command_ids",
    "load_settings",
    "read_config",
    "reset_run_base_dir",
    "set_run_base_dir",
    "suppress_logs",
    "write_config",
]
