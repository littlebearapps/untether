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
type UntetherEvent = StartedEvent | ActionEvent | CompletedEvent | TurnEvent
```

Every run emits: `StartedEvent` (once) -> `ActionEvent`s (zero+) -> `CompletedEvent` (once). A Claude live session (#776) may follow it with `TurnEvent(started) -> ActionEvent* -> TurnEvent(completed)` segments, delivered by `FollowupTurnRouter` as separate Telegram messages.

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
- Stall monitor reads the run's **own** `JsonlStreamState`/PID from the per-run `RunStreamHandle` (ContextVar bound in `run_runner_with_cancel`), never the shared `runner.current_stream`/`last_pid` (#510)
- Expected waits (rate-limit `rejected` latch #790, `api_retry` back-off #792, approvals) demote stall warnings; live-idle holds between turns raise no stall WARN and are reported as `peak_live_idle_seconds` in `session.summary`, not `peak_idle_seconds` (#787)

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

Per-chat persistent preferences: `default_engine`, listen mode, context project/branch,
`followup_mode` (#775) and a per-engine `engine_overrides: dict[str, EngineOverrides]`:

```python
class EngineOverrides(msgspec.Struct):  # telegram/engine_overrides.py
    model: str | None = None
    reasoning: str | None = None
    permission_mode: str | None = None
    ask_questions: bool | None = None
    diff_preview: bool | None = None
    # ... footer / budget / loop toggles
```

- Stored in `telegram_chat_prefs_state.json` (topic-level overrides in the topic state)
- Set via `/agent`, `/model`, `/reasoning`, `/planmode`, `/config` commands
- Applied at run time to override global config

## Engine backend registration

### Entry points (`pyproject.toml`)

```toml
[project.entry-points."untether.engine_backends"]
codex = "untether.runners.codex:BACKEND"
claude = "untether.runners.claude:BACKEND"
opencode = "untether.runners.opencode:BACKEND"
pi = "untether.runners.pi:BACKEND"
gemini = "untether.runners.gemini:BACKEND"  # deprecated, removed in 0.36.0
amp = "untether.runners.amp:BACKEND"        # deprecated, removed in 0.36.0
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
| `claude_control.py` | Approve/Deny/Discuss handlers, outline-gate wiring |
| `planmode.py` | `/planmode` toggle |
| `usage.py` | `/usage` — Claude subscription quota; token totals for other engines (#417) |
| `model.py` | `/model` override |
| `reasoning.py` | `/reasoning` override |
| `listen.py` | `/listen` (formerly `/trigger`, still accepted) — all-messages vs mentions-only |
| `agent.py` | `/agent` — engine selection |

### CommandResult

```python
@dataclass
class CommandResult:
    text: str
    notify: bool = True
    reply_to: MessageRef | None = None
    parse_mode: str | None = None  # "HTML" for bold formatting
    skip_reply: bool = False
    attachment: CommandAttachment | None = None  # #418: sent as a document
```

Commands return `CommandResult`; dispatch sends it as a Telegram message.

### Callback dispatch

Callback data format: `<prefix>:<action>:<id>` (max 64 bytes).
- `claude_control:approve:<request_id>` — approve control request
- `claude_control:deny:<request_id>` — deny control request
- `claude_control:discuss:<request_id>` — pause & outline plan
- `claude_control:chat:<request_id>` — let's discuss (holds the request open)
- synthetic post-outline buttons carry a `da:<session_id>` request id; AskUserQuestion options use `aq:opt:<i>` / `aq:other`

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
    edits: ProgressEdits | None      # #690: drain self-restart evidence scan
    thread_id: ThreadId | None       # #826: scopes /new and /cancel to a topic
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

## Trigger system

Triggers let external events or schedules start agent runs automatically. Opt-in via `[triggers] enabled = true`.

### Cron

`run_cron_scheduler()` ticks every minute, checking each `[[triggers.crons]]` entry against the current time via `cron_matches()` (5-field standard syntax). Per-cron `timezone` or global `default_timezone` converts UTC to local wall-clock time via `_resolve_now()` + `zoneinfo.ZoneInfo`. DST transitions handled automatically. `last_fired` dict prevents double-firing within the same minute.

### Webhooks

`run_webhook_server()` runs an aiohttp server. Each `[[triggers.webhooks]]` maps a URL path to auth (bearer/HMAC-SHA256/SHA1) + prompt template with `{{field.path}}` substitutions. Rate-limited per-webhook and globally.

### Dispatch

Both crons and webhooks feed into `TriggerDispatcher.dispatch_cron()`/`dispatch_webhook()` → sends a notification message to Telegram (`⏰`/`⚡`) → calls `run_job()` with the prompt, threading under the notification.

### Key files

- `triggers/cron.py` — cron parser, timezone-aware scheduler
- `triggers/settings.py` — `CronConfig`, `WebhookConfig`, `TriggersSettings` (pydantic)
- `triggers/dispatcher.py` — notification + `run_job()` bridge
- `triggers/server.py` — aiohttp webhook server
- `triggers/auth.py` — bearer/HMAC verification
- `triggers/templating.py` — `{{field.path}}` prompt substitution

## Key conventions

- Python 3.12+, anyio for async, msgspec for JSONL parsing, structlog for logging
- pydantic + pydantic-settings for config validation
- Ruff for linting, pytest with coverage for tests
- Runner backends registered via entry points
- All Telegram writes go through the outbox
- Exactly one CompletedEvent per run (enforced by JsonlStreamState)
