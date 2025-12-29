# takopi — Developer Guide

This document describes the internal architecture and module responsibilities.

## Module Responsibilities

### `exec_bridge.py` — Main Entry Point

The orchestrator module containing:

| Component | Purpose |
|-----------|---------|
| `main()` / `run()` | CLI entry point via Typer |
| `BridgeConfig` | Frozen dataclass holding runtime config |
| `CodexExecRunner` | Spawns `codex exec`, streams JSONL, handles cancellation |
| `poll_updates()` | Async generator that drains backlog, long-polls updates, filters messages |
| `_run_main_loop()` | TaskGroup-based main loop that spawns per-message handlers |
| `_handle_message()` | Per-message handler with progress updates |
| `extract_session_id()` | Parses `resume: <uuid>` from message text |
| `truncate_for_telegram()` | Smart truncation preserving resume lines |

**Key patterns:**
- Per-session locks prevent concurrent resumes to the same `session_id`
- `asyncio.Semaphore` limits overall concurrency (default: 16)
- `asyncio.TaskGroup` manages per-message tasks
- Progress edits are throttled to ~2s intervals
- Subprocess stderr is drained to a bounded deque for error reporting

### `telegram_client.py` — Telegram Bot API

Minimal async client wrapping the Bot API:

```python
class TelegramClient:
    async def get_updates(...)   # Long-polling
    async def send_message(...)  # With entities support
    async def edit_message_text(...)
    async def delete_message(...)
```

**Features:**
- Automatic retry on 429 (rate limit) with `retry_after`
- Raises `TelegramAPIError` with payload details on failure

### `exec_render.py` — JSONL Event Rendering

Transforms Codex JSONL events into human-readable text:

| Function/Class | Purpose |
|----------------|---------|
| `format_event()` | Core dispatcher returning `(item_num, cli_lines, progress_line, prefix)` |
| `render_event_cli()` | Simplified wrapper for console logging |
| `ExecProgressRenderer` | Stateful renderer tracking recent actions for progress display |
| `format_elapsed()` | Formats seconds as `Xh Ym`, `Xm Ys`, or `Xs` |

**Supported event types:**
- `thread.started`, `turn.started/completed/failed`
- `item.started/completed` for: `agent_message`, `reasoning`, `command_execution`, `mcp_tool_call`, `web_search`, `file_change`, `error`

### `rendering.py` — Markdown to Telegram

Converts Markdown to Telegram-compatible text with entities:

```python
def render_markdown(md: str) -> tuple[str, list[dict[str, Any]]]:
    # Uses markdown-it-py + sulguk for entity extraction
    # Fixes: replaces bullets, removes invalid language fields
```

### `config.py` — Configuration Loading

```python
def load_telegram_config(path=None) -> dict:
    # Loads ~/.codex/telegram.toml (or custom path)
```

### `constants.py` — Shared Constants

```python
TELEGRAM_HARD_LIMIT = 4096  # Max message length
TELEGRAM_CONFIG_PATH = ~/.codex/telegram.toml
```

### `logging.py` — Secure Logging Setup

```python
class RedactTokenFilter:
    # Redacts bot tokens from log output

def setup_logging(*, debug: bool):
    # Configures root logger with redaction filter
```

## Data Flow

### New Message Flow

```
Telegram Update
    ↓
poll_updates() drains backlog, long-polls, filters chat_id == from_id == cfg.chat_id
    ↓
_run_main_loop() spawns tasks in TaskGroup
    ↓
_handle_message() spawned as task
    ↓
Send initial progress message (silent)
    ↓
CodexExecRunner.run_serialized()
    ├── Spawns: codex exec --json ... -
    ├── Streams JSONL from stdout
    ├── Calls on_event() for each event
    │       ↓
    │   ExecProgressRenderer.note_event()
    │       ↓
    │   Throttled edit_message_text()
    └── Returns (session_id, answer, saw_agent_message)
    ↓
render_final() with resume line
    ↓
Send/edit final message
```

### Resume Flow

Same as above, but:
- `extract_session_id()` finds UUID in message or reply
- Command becomes: `codex exec --json resume <session_id> -`
- Per-session lock serializes concurrent resumes

## Error Handling

| Scenario | Behavior |
|----------|----------|
| `codex exec` fails (rc≠0) | Shows stderr tail in error message |
| Telegram API error | Logged, edit skipped (progress continues) |
| Cancellation | Subprocess terminated, CancelledError re-raised |
| No agent_message | Final shows "error" status |
