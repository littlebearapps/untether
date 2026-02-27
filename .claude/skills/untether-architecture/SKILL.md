---
name: untether-architecture
description: >
  Untether overall architecture, event model, data flow, config system,
  engine backend registration, progress tracking, approval notifications,
  ephemeral cleanup, and key abstractions. Use when working on core
  infrastructure or understanding how the pieces fit together.
triggers:
  - working on core untether infrastructure
  - understanding the data flow from runner to Telegram
  - modifying the config system
  - working on progress tracking or rendering
  - adding new commands or callbacks
  - modifying the bridge layer
  - understanding engine registration
---

# Untether Architecture

Telegram bridge for agent CLIs (Claude Code, Codex, OpenCode, Pi). Control coding agents from anywhere.

## Data flow

```
Telegram Bot API
    |
    v
TelegramClient (httpx, long polling)
    |
    v
telegram/loop.py (parse updates, dispatch commands/callbacks)
    |
    v
handle_message() in runner_bridge.py
    |
    v
Runner.run(prompt, resume) -> AsyncIterator[UntetherEvent]
    |                              |
    v                              v
ProgressEdits <-- on_event() -- StartedEvent / ActionEvent / CompletedEvent
    |
    v
TelegramPresenter.render_progress() / render_final()
    |
    v
TelegramOutbox (coalesced edits, rate-limited sends)
    |
    v
Telegram Bot API
```

## Core abstractions

### Runner (Protocol)

```python
class Runner(Protocol):
    engine: str
    def run(self, prompt: str, resume: ResumeToken | None) -> AsyncIterator[UntetherEvent]
    def is_resume_line(self, line: str) -> bool
    def format_resume(self, token: ResumeToken) -> str
    def extract_resume(self, text: str | None) -> ResumeToken | None
```

### UntetherEvent (discriminated union)

```python
type UntetherEvent = StartedEvent | ActionEvent | CompletedEvent
```

Every run emits: `StartedEvent` (once) -> `ActionEvent`s (zero+) -> `CompletedEvent` (once, always last).

### RunnerBridge (`runner_bridge.py`)

Connects runners to the transport layer:

1. `handle_message()` — entry point for incoming Telegram messages
2. Creates `ProgressTracker` and `ProgressEdits`
3. Spawns runner in a task group with cancel support
4. Manages progress message lifecycle (create -> edit -> replace with final)
5. Handles errors, cancellation, ephemeral cleanup

### ProgressTracker (`progress.py`)

Aggregates events into renderable state:

```python
tracker = ProgressTracker(engine="claude")
tracker.note_event(evt)           # returns True if state changed
state = tracker.snapshot(
    resume_formatter=runner.format_resume,
    context_line="myproject@main",
)
```

Snapshot includes: resume line, action list, action count, context line.

### ProgressEdits

Live-updates the Telegram progress message:
- Signal-based: only renders when new events arrive
- Detects approval button transitions for push notifications
- Manages `_approval_notified` flag and `_approval_notify_ref`
- `delete_ephemeral()` cleans up notification messages on run completion

### TelegramPresenter (`telegram/bridge.py`)

Renders progress and final messages:
- `render_progress(state, elapsed_s, label)` -> `RenderedMessage`
- `render_final(state, elapsed_s, status, answer)` -> `RenderedMessage`
- Inline keyboard buttons in `extra["reply_markup"]`

### RenderedMessage

```python
@dataclass
class RenderedMessage:
    text: str
    extra: dict[str, Any]  # reply_markup, parse_mode, followups, etc.
```

## Config system

### untether.toml

```toml
# ~/.untether/untether.toml
default_engine = "claude"
default_project = "untether"

[transports.telegram]
bot_token = "..."
chat_id = -1001234567890
voice_transcription = true
session_mode = "chat"
topics.enabled = true

[claude]
model = "sonnet"
permission_mode = "plan"

[codex]
profile = "Codex"

[projects.untether]
path = "/home/nathan/untether"
```

### Settings hierarchy

```python
UntetherSettings (pydantic-settings, TOML source)
  ├── TransportsSettings
  │     └── TelegramTransportSettings
  │           ├── TelegramTopicsSettings
  │           └── TelegramFilesSettings
  ├── PluginsSettings
  ├── ProjectSettings (per project)
  └── engine_config(engine_id) -> dict  # [claude], [codex], etc.
```

- Config loaded from `~/.untether/untether.toml`
- Engine configs in `[engine_id]` sections (flat) or `[engines.engine_id]` (nested)
- Environment overrides: `UNTETHER__` prefix with `__` nesting

### ChatPrefsStore

Per-chat persistent preferences (engine, model, reasoning, permission_mode):

```python
class EngineOverrides:
    engine: str | None
    model: str | None
    reasoning: str | None
    permission_mode: str | None
```

- Stored in `telegram_chat_prefs_state.json`
- Set via `/agent`, `/model`, `/reasoning`, `/planmode` commands
- Applied at run time to override global config

## Engine backend registration

### Entry points (`pyproject.toml`)

```toml
[project.entry-points."untether.engine_backends"]
codex = "untether.runners.codex:BACKEND"
claude = "untether.runners.claude:BACKEND"
opencode = "untether.runners.opencode:BACKEND"
pi = "untether.runners.pi:BACKEND"
```

### EngineBackend

```python
@dataclass(frozen=True, slots=True)
class EngineBackend:
    id: str
    build_runner: Callable[[EngineConfig, Path], Runner]
    cli_cmd: str | None = None
    install_cmd: str | None = None
```

Discovery: `importlib.metadata.entry_points(group="untether.engine_backends")`

## Command system

### Command handlers (`telegram/commands/`)

| File | Commands |
|------|----------|
| `dispatch.py` | Callback dispatch, early answering, ephemeral registration |
| `claude_control.py` | Approve/Deny/Discuss handlers, cooldown wiring |
| `planmode.py` | `/planmode` toggle |
| `usage.py` | `/usage` — Claude Code API usage |
| `model.py` | `/model` override |
| `reasoning.py` | `/reasoning` override |
| `trigger.py` | `/trigger` — mentions-only mode |
| `agent.py` | `/agent` — engine selection |

### CommandResult

```python
@dataclass
class CommandResult:
    text: str
    parse_mode: str | None = None  # "HTML" for bold formatting
```

Commands return `CommandResult`; dispatch sends it as a Telegram message.

### Callback dispatch

Callback data format: `<prefix>:<action>:<id>` (max 64 bytes).
- `ctrl:approve:<request_id>` — approve control request
- `ctrl:deny:<request_id>` — deny control request
- `ctrl:discuss:<request_id>` — pause & outline plan

## Running tasks

```python
RunningTasks = dict[MessageRef, RunningTask]

@dataclass
class RunningTask:
    resume: ResumeToken | None
    resume_ready: anyio.Event
    cancel_requested: anyio.Event
    done: anyio.Event
    context: RunContext | None
```

- Keyed by progress message ref
- `/cancel` sets `cancel_requested` event
- `done` event signals run completion for cleanup

## Project system

Projects bind a directory + optional branch to a Telegram context:

```toml
[projects.untether]
path = "/home/nathan/untether"
default_engine = "claude"
chat_id = -1001234567890   # optional per-project chat
```

- `/topic <project> @branch` creates bound topics
- `/ctx set <project>` binds a chat context
- Project alias used as directive prefix: `/untether fix the bug`

## Key conventions

- Python 3.12+, anyio for async, msgspec for JSONL parsing, structlog for logging
- pydantic + pydantic-settings for config validation
- Ruff for linting, pytest with coverage for tests
- Runner backends registered via entry points
- All Telegram writes go through the outbox
- Exactly one CompletedEvent per run (enforced by JsonlStreamState)
