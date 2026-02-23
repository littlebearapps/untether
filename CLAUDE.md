# Takopi

Telegram bridge for Claude Code, Codex, OpenCode, and other agent CLIs.

**Upstream**: [banteg/takopi](https://github.com/banteg/takopi)
**Fork**: [littlebearapps/takopi](https://github.com/littlebearapps/takopi) (this repo)
**Branch**: `interactive-features` - adds Claude Code interactive permission control via Telegram

## Architecture

```
Telegram <-> TelegramPresenter <-> RunnerBridge <-> Runner (claude/codex/opencode/pi)
                                       |
                                  ProgressTracker
```

- **Runners** (`src/takopi/runners/`) - Engine-specific subprocess managers. Each spawns a CLI process, reads JSONL stdout, and translates events into `TakopiEvent`s.
- **RunnerBridge** (`src/takopi/runner_bridge.py`) - Connects runners to the Telegram presenter. Manages `ProgressEdits` (the edit loop that updates Telegram messages).
- **TelegramPresenter** (`src/takopi/telegram/bridge.py`) - Renders progress, inline keyboards, and final answers to Telegram.
- **Schemas** (`src/takopi/schemas/`) - msgspec models for each engine's JSONL protocol.
- **Commands** (`src/takopi/telegram/commands/`) - Telegram command/callback handlers (cancel, claude_control, usage, planmode).

## Claude Code Interactive Control (LBA fork feature)

The `interactive-features` branch adds bidirectional control channel support for Claude Code's `--permission-mode plan --permission-prompt-tool stdio` protocol. This enables:

- **ExitPlanMode approval** - Claude enters plan mode, Telegram shows Approve/Deny buttons, user clicks to continue
- **Tool permission requests** - Any tool requiring approval shows inline keyboard buttons
- **Concurrent sessions** - Multiple chat routes can run simultaneously on the same runner

### Key concurrency design decisions

`ClaudeRunner` is a **singleton per engine**, shared across all Telegram chat routes. This creates challenges when multiple chats run Claude Code concurrently:

1. **`self._proc_stdin` is unreliable** - overwritten by the last subprocess to spawn. All stdin references must be captured locally at subprocess creation time and passed explicitly.
2. **`_SESSION_STDIN` registry** - Maps `session_id -> stdin pipe`. Registered inside `_iter_jsonl_events` (not `translate`) because `self._proc_stdin` may be stale by the time events arrive.
3. **`_REQUEST_TO_SESSION` registry** - Maps `request_id -> session_id` so Telegram callbacks can route to the correct runner/stdin.
4. **Stdout pipe FD inheritance** - Claude Code's MCP servers inherit the stdout pipe. After `CompletedEvent`, break immediately instead of waiting for EOF.
5. **Auto-approve drain** - `ControlInitializeRequest` and other auto-approved requests must be drained after every JSONL line, not just after yielded events.
6. **Tool auto-approve** - `ControlCanUseToolRequest` is auto-approved for all tools except those in `_TOOLS_REQUIRING_APPROVAL` (currently only `ExitPlanMode`). This prevents noisy approval prompts in Telegram for routine tools like Grep, Glob, WebFetch, Task, etc.

### Approval notifications and ephemeral message cleanup

Telegram's `edit_message_text` does not trigger push notifications. `ProgressEdits.run()` detects when `reply_markup.inline_keyboard` transitions from 1 row (cancel only) to 2+ rows (approve/deny buttons) and sends a separate `transport.send()` with `notify=True` so the user gets a push notification on their phone. The `_approval_notified` flag resets when approval buttons disappear, allowing subsequent approvals in the same run to also notify.

Approval-related messages are **ephemeral** -- they auto-delete to keep the chat clean:

- **"Action required — approval needed"** notification: deleted immediately when the user clicks Approve/Deny (tracked via `_approval_notify_ref` in `ProgressEdits`)
- **"Approved/Denied permission request"** feedback: deleted when the run finishes and the final summary is sent (tracked via `_EPHEMERAL_MSGS` registry in `runner_bridge.py`, registered by `_dispatch_callback` in `dispatch.py`)

Both are cleaned up by `ProgressEdits.delete_ephemeral()`, called in the `handle_message` finally block.

### Files modified from upstream

- `src/takopi/runners/claude.py` - Control channel, `_iter_jsonl_events` override, `_SESSION_STDIN`/`_REQUEST_TO_SESSION` registries, `write_control_response`, `send_claude_control_response`
- `src/takopi/runner_bridge.py` - `ProgressEdits` approval push notifications, ephemeral message tracking/cleanup, `_EPHEMERAL_MSGS` registry
- `src/takopi/telegram/commands/claude_control.py` - Approve/Deny callback handler
- `src/takopi/telegram/commands/dispatch.py` - Callback dispatch with `callback_query_id` passthrough and ephemeral message registration
- `src/takopi/telegram/commands/planmode.py` - `/planmode` toggle command
- `src/takopi/telegram/commands/usage.py` - `/usage` command
- `src/takopi/telegram/bridge.py` - Inline keyboard rendering for control requests
- `src/takopi/telegram/loop.py` - Passes `callback_query_id` to dispatch for deferred callback answering
- `tests/test_claude_control.py` - 30 tests covering control request translation, response routing, registry lifecycle, auto-approve drain, tool auto-approve, and full tool-use lifecycle
- `tests/test_exec_bridge.py` - 4 tests for ephemeral notification cleanup
- `tests/test_callback_dispatch.py` - 4 tests for callback dispatch toast/ephemeral behaviour

## Development

```bash
# Install (editable)
pipx install -e /home/nathan/takopi-fork

# Run as systemd service
systemctl --user restart takopi
journalctl --user -u takopi -f

# Config
~/.takopi/takopi.toml

# Tests
cd /home/nathan/takopi-fork && uv run pytest

# Lint
cd /home/nathan/takopi-fork && uv run ruff check src/
```

## Key types

- `TakopiEvent` - Union of `StartedEvent`, `ActionEvent`, `CompletedEvent`
- `JsonlStreamState` - Tracks stdout parsing state, `did_emit_completed` flag
- `ClaudeStreamState` - Claude-specific state: `auto_approve_queue`, `control_action_for_tool`, `factory`
- `StreamControlRequest` / `ControlCanUseToolRequest` - Permission requests from Claude Code
- `ResumeToken` - Session ID wrapper for session persistence

## Conventions

- Python 3.12+, anyio for async, msgspec for JSONL parsing, structlog for logging
- Ruff for linting (`ruff check`), pytest with coverage for tests
- Runner backends registered via entry points in `pyproject.toml`
