# Plugin API

Untether’s **public plugin API** is exported from:

```
untether.api
```

Anything not imported from `untether.api` should be considered **internal** and
subject to change. The API version is tracked by `TAKOPI_PLUGIN_API_VERSION`.

---

## Versioning

- Current API version: `TAKOPI_PLUGIN_API_VERSION = 1`
- Plugins should pin to a compatible Untether range, e.g.:

```toml
dependencies = ["untether>=0.35,<0.36"]
```

---

## Exported symbols

### Engine backends and runners

| Symbol | Purpose |
|--------|---------|
| `EngineBackend` | Declares an engine backend (id + runner builder) |
| `EngineConfig` | Dict-based engine config table |
| `Runner` | Runner protocol |
| `BaseRunner` | Helper base class with resume locking |
| `JsonlSubprocessRunner` | Helper for JSONL-streaming CLIs |
| `EventFactory` | Helper for building untether events |

### Transport backends

| Symbol | Purpose |
|--------|---------|
| `TransportBackend` | Transport backend protocol |
| `SetupIssue` | Setup issue for onboarding / validation |
| `SetupResult` | Setup issues + config path |
| `Transport` | Transport protocol (send/edit/delete) |
| `Presenter` | Renders progress to `RenderedMessage` |
| `RenderedMessage` | Rendered text + transport metadata |
| `SendOptions` | Reply/notify/replace flags |
| `MessageRef` | Transport-specific message reference |
| `TransportRuntime` | Transport runtime facade (routers/projects hidden) |
| `ResolvedMessage` | Parsed prompt + resume/context resolution |
| `ResolvedRunner` | Runner selection result |

### Command backends

| Symbol | Purpose |
|--------|---------|
| `CommandBackend` | Slash command plugin protocol |
| `CommandContext` | Context passed to a command handler |
| `CommandExecutor` | Helper to send messages or run engines |
| `CommandResult` | Simple response payload for a command |
| `RunRequest` | Engine run request used by commands |
| `RunResult` | Engine run result (captured output) |
| `RunMode` | `"emit"` (send) or `"capture"` (collect) |

### Core types and helpers

| Symbol | Purpose |
|--------|---------|
| `EngineId` | Engine id type alias |
| `ResumeToken` | Resume token (engine + value) |
| `StartedEvent` / `ActionEvent` / `CompletedEvent` | Core event types (`TurnEvent`, emitted only by live Claude sessions, is internal: `untether.model`) |
| `Action` | Action metadata for `ActionEvent` |
| `ActionState` / `ProgressState` / `ProgressTracker` | Progress tracking helpers for presenters |
| `RunContext` | Project/branch context |
| `ConfigError` | Configuration error type |
| `DirectiveError` | Error raised when parsing directives |
| `RunnerUnavailableError` | Router error when a runner is unavailable |

### Bridge helpers (for transport plugins)

| Symbol | Purpose |
|--------|---------|
| `ExecBridgeConfig` | Transport + presenter config |
| `IncomingMessage` | Normalised incoming message |
| `RunningTask` / `RunningTasks` | Per-message run coordination |
| `handle_message()` | Core message handler used by transports |

### Plugin utilities

| Symbol | Purpose |
|--------|---------|
| `HOME_CONFIG_PATH` | Canonical config path (`~/.untether/untether.toml`) |
| `RESERVED_COMMAND_IDS` | Set of reserved command IDs |
| `read_config` | Read and parse TOML config file |
| `write_config` | Atomically write config to TOML file |
| `get_logger` | Get a structured logger for a module |
| `bind_run_context` | Bind contextual fields to all log entries |
| `clear_context` | Clear bound log context |
| `suppress_logs` | Context manager to suppress info-level logs |
| `set_run_base_dir` | Set working directory context for path relativization |
| `reset_run_base_dir` | Reset working directory context |
| `ThreadJob` | Job dataclass for ThreadScheduler |
| `ThreadScheduler` | Per-thread message serialisation |
| `get_command` | Get command backend by ID |
| `list_command_ids` | Get available command plugin IDs |
| `list_backends` | Discover available engine backends |
| `load_settings` | Load full UntetherSettings from config |
| `install_issue` | Create SetupIssue for missing dependency |

---

## Runner contract (engine plugins)

Runners emit events in a strict sequence (see `tests/test_runner_contract.py`):

- Exactly **one** `StartedEvent`
- Exactly **one** `CompletedEvent`
- `CompletedEvent` is **last**
- `CompletedEvent.resume == StartedEvent.resume`

Action events are optional. The minimal valid run is:

```
StartedEvent -> CompletedEvent
```

The one exception to "`CompletedEvent` is last" is a runner that keeps its engine process live after the first result (Claude Code's live sessions): it may follow `CompletedEvent` with `TurnEvent(started) -> ActionEvent* -> TurnEvent(completed)` segments. Plugin runners don't need to emit them. See the [Specification](specification.md) §4.3.4 and §5.4.

`BaseRunner` takes the per-session lock for you (on the resume token, or on the token from the first `StartedEvent` for new and `/continue` runs) and closes `run_impl` with `contextlib.aclosing`. Runners are shared across chats, so don't keep per-run state on the runner instance.

### Resume tokens

Runners own the resume format:

- `format_resume(token)` returns a command line users can paste
- `extract_resume(text)` parses resume tokens from user text
- `is_resume_line(line)` lets Untether strip resume lines before running

---

## EngineBackend

```py
EngineBackend(
    id: str,
    build_runner: Callable[[EngineConfig, Path], Runner],
    cli_cmd: str | None = None,
    install_cmd: str | None = None,
)
```

- `id` must match the entrypoint name and the ID regex.
- `build_runner` should raise `ConfigError` for invalid config.
- `cli_cmd` is used to check whether the engine CLI is on `PATH`.
- `install_cmd` is surfaced in onboarding output.

---

## TransportBackend

```py
class TransportBackend(Protocol):
    id: str
    description: str

    def check_setup(
        self,
        engine_backend: EngineBackend,
        *,
        transport_override: str | None = None,
    ) -> SetupResult: ...
    async def interactive_setup(self, *, force: bool) -> bool: ...
    def lock_token(
        self, *, transport_config: object, _config_path: Path
    ) -> str | None: ...
    def build_and_run(
        self,
        *,
        transport_config: object,
        config_path: Path,
        runtime: TransportRuntime,
        final_notify: bool,
        default_engine_override: str | None,
    ) -> None: ...
```

Transport backends are responsible for:

- Validating config and onboarding users (`check_setup`, `interactive_setup`)
- Providing a lock token (e.g. the bot token) whose fingerprint is stamped into the config lock file. The lock itself is per config file, so two instances can't share one config
- Starting the transport loop in `build_and_run`

---

## CommandBackend

```py
class CommandBackend(Protocol):
    id: str
    description: str

    async def handle(self, ctx: CommandContext) -> CommandResult | None: ...
```

Command handlers receive a `CommandContext` with:

- the raw command text and parsed args
- the original message + reply metadata
- `config_path` for the active `untether.toml` (when known)
- `plugin_config` from `[plugins.<id>]` (dict, defaults to `{}`)
- `runtime` (engine/project resolution)
- `executor` (send messages or run engines)

The full field list:

| Field | Type | Notes |
|-------|------|-------|
| `command` | `str` | Command id, without the slash |
| `text` | `str` | Full message text |
| `args_text` | `str` | Text after the command |
| `args` | `tuple[str, ...]` | Parsed arguments |
| `message` | `MessageRef` | The command message |
| `reply_to` | `MessageRef \| None` | The message being replied to |
| `reply_text` | `str \| None` | Text of the replied-to message |
| `config_path` | `Path \| None` | Active `untether.toml` |
| `plugin_config` | `dict[str, Any]` | `[plugins.<id>]` table |
| `runtime` | `TransportRuntime` | Engine/project resolution |
| `executor` | `CommandExecutor` | Send messages or run engines |
| `trigger_manager` | `TriggerManager \| None` | Live trigger config; `None` for transports without triggers |
| `default_chat_id` | `int \| None` | Chat that unscoped triggers fall back to |
| `file_deny_globs` | `tuple[str, ...] \| None` | Live `[transports.telegram.files] deny_globs` ([#389](https://github.com/littlebearapps/untether/issues/389)); `None` means the defaults |
| `callback_query_id` | `str \| None` | Callback query id of a button tap ([#685](https://github.com/littlebearapps/untether/issues/685)); `None` for text commands |

The last four are optional (default `None`), so transports that don't set them keep working.

Use `ctx.executor.run_one(...)` or `ctx.executor.run_many(...)` to reuse Untether's
engine pipeline. Use `mode="capture"` to collect results and build a custom reply.

`ctx.message` and `ctx.reply_to` are `MessageRef` objects with:

- `channel_id` (`int | str`, chat/channel id)
- `message_id` (`int | str`, message id)
- `thread_id` (`int | str | None`; set when the transport supports threads, like Telegram topics)
- `sender_id` (`int | None`; the sending user, when known)
- `raw` (transport-specific payload, may be `None`)

Example: key per-thread state by `(ctx.message.channel_id, ctx.message.thread_id)`.

---

## TransportRuntime helpers

`TransportRuntime` keeps transports away from internal router/project types. Key helpers:

- `resolve_message(text=..., reply_text=..., ambient_context=None, chat_id=None)` → `ResolvedMessage` (prompt, resume token, engine override, context, `context_source`)
- `resolve_engine(engine_override, context)` → `EngineId`
- `resolve_runner(resume_token, engine_override)` → `ResolvedRunner` (runner + availability info)
- `resolve_run_cwd(context)` → `Path | None` (raises `ConfigError` for project/worktree issues)
- `format_context_line(context)` → `str | None`
- `available_engine_ids()` / `missing_engine_ids()` / `engine_ids` / `default_engine`
- `project_aliases()`
- `config_path` (active config path when available)
- `plugin_config(plugin_id)` → `dict` from `[plugins.<id>]`

---

## Bridge usage (transport plugins)

Most transports can delegate message handling to `handle_message()`. Use
`TransportRuntime` to resolve messages and select a runner:

```py
from untether.api import (
    ExecBridgeConfig,
    IncomingMessage,
    RunningTask,
    RunningTasks,
    TransportRuntime,
    handle_message,
)

async def on_message(...):
    resolved = runtime.resolve_message(text=text, reply_text=reply_text)
    entry = runtime.resolve_runner(
        resume_token=resolved.resume_token,
        engine_override=resolved.engine_override,
    )
    context_line = runtime.format_context_line(resolved.context)
    incoming = IncomingMessage(
        channel_id=...,
        message_id=...,
        text=...,
        reply_to=...,
        thread_id=...,
    )
    await handle_message(
        exec_cfg,
        runner=entry.runner,
        incoming=incoming,
        resume_token=resolved.resume_token,
        context=resolved.context,
        context_line=context_line,
        strip_resume_line=runtime.is_resume_line,
        running_tasks=running_tasks,
        on_thread_known=on_thread_known,
    )
```

`handle_message()` also takes optional `on_resume_failed`, `progress_ref` (reuse an existing progress message) and `quarantine_store` keyword arguments.

`handle_message()` implements:

- Progress updates and throttling
- Resume handling
- Cancellation propagation
- Final rendering

This keeps transport backends thin and consistent with core behaviour.
