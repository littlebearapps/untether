# Untether

Telegram bridge for Claude Code, Codex, OpenCode, and other agent CLIs. Control your coding agents from anywhere — walking the dog, watching footy, at a friend's place.

**Repo**: [littlebearapps/untether](https://github.com/littlebearapps/untether)
**Based on**: [banteg/takopi](https://github.com/banteg/takopi) (upstream)

Untether adds interactive permission control, plan mode support, and several UX improvements on top of upstream takopi. All interactive features are Claude Code-specific; Codex, OpenCode, and other engines use standard non-interactive mode.

## Features (vs upstream takopi)

### Interactive permission control

Bidirectional control channel for Claude Code's `--permission-mode plan --permission-prompt-tool stdio` protocol:

- **ExitPlanMode approval** — Claude enters plan mode, Telegram shows **Approve / Deny / Pause & Outline Plan** buttons
- **Tool permission requests** — any tool requiring approval shows inline keyboard buttons
- **Tool auto-approve** — routine tools (Grep, Glob, Read, Bash, etc.) are auto-approved silently; only `ExitPlanMode` prompts the user
- **Concurrent sessions** — multiple chats can run Claude Code simultaneously via `_SESSION_STDIN` and `_REQUEST_TO_SESSION` registries

### "Pause & Outline Plan" button

When Claude requests ExitPlanMode, a third button lets the user ask Claude to write a step-by-step plan outline in the chat before proceeding:

- Sends a detailed deny message instructing Claude to list every file, change, and phase as visible text
- **Progressive cooldown** — auto-denies rapid ExitPlanMode retries: 30s → 60s → 90s → 120s (capped), escalating with clear BLOCKED messages
- Cooldown count is preserved across expiry so repeated discuss clicks keep escalating
- Deny count resets on explicit Approve or Deny

### `/planmode` command

Toggle Claude Code's `--permission-mode` per chat:

- `/planmode` — toggle on/off
- `/planmode on` / `/planmode off` — explicit set
- `/planmode show` — show current state
- `/planmode clear` — remove override, use engine config default

Persisted via `ChatPrefsStore` as an `EngineOverrides.permission_mode` field.

### Early callback answering

Telegram inline buttons show a spinner until `answerCallbackQuery` is called. Upstream defers this to the `finally` block (~150-300ms delay). Backends can set `answer_early = True` and provide `early_answer_toast()` to clear the spinner immediately with a toast ("Approved", "Denied", "Outlining plan...").

### Approval push notifications

`edit_message_text` doesn't trigger phone push notifications. Untether detects when approval buttons appear and sends a separate `notify=True` message ("Action required — approval needed"). The `_approval_notified` flag resets when buttons disappear, so subsequent approvals in the same run also notify.

### Ephemeral message cleanup

Approval-related messages auto-delete to keep the chat clean:

- "Action required" notification — deleted when the user clicks a button
- "Approved/Denied" feedback — deleted when the run finishes

Tracked via `_approval_notify_ref` (in `ProgressEdits`) and `_EPHEMERAL_MSGS` (in `runner_bridge.py`).

### Bold formatting in command responses

`/planmode`, `/agent`, `/model`, `/reasoning`, and `/trigger` commands return responses with key state values bolded. `CommandResult` supports a `parse_mode` field for HTML formatting through the command dispatch path.

### `/usage` command

Shows Claude Code API usage and cost for the current session.

## Architecture

```
Telegram <-> TelegramPresenter <-> RunnerBridge <-> Runner (claude/codex/opencode/pi)
                                       |
                                  ProgressTracker
```

- **Runners** (`src/untether/runners/`) — engine-specific subprocess managers
- **RunnerBridge** (`src/untether/runner_bridge.py`) — connects runners to Telegram presenter, manages `ProgressEdits`
- **TelegramPresenter** (`src/untether/telegram/bridge.py`) — renders progress, inline keyboards, and answers
- **Commands** (`src/untether/telegram/commands/`) — command/callback handlers

### Concurrency design

`ClaudeRunner` is a singleton per engine, shared across all chats:

1. `self._proc_stdin` is unreliable (overwritten by last subprocess) — all stdin refs captured locally at spawn time
2. `_SESSION_STDIN` maps `session_id -> stdin pipe` (registered in `_iter_jsonl_events`)
3. `_REQUEST_TO_SESSION` maps `request_id -> session_id` for callback routing
4. Stdout pipe breaks immediately after `CompletedEvent` (MCP servers inherit the FD)
5. `ControlInitializeRequest` and auto-approved requests drained after every JSONL line

## Key files

| File | Purpose |
|------|---------|
| `runners/claude.py` | Control channel, stdin/session registries, `write_control_response` (with `deny_message`), Outline Plan button, progressive discuss cooldown |
| `runner_bridge.py` | Approval push notifications, ephemeral message tracking/cleanup |
| `commands/claude_control.py` | Approve/Deny/Discuss handler, early answer toast, cooldown wiring |
| `commands/dispatch.py` | Callback dispatch, `callback_query_id` passthrough, ephemeral registration, early answering, `parse_mode` support |
| `commands/planmode.py` | `/planmode` toggle command |
| `commands/usage.py` | `/usage` command |
| `commands/model.py` | Bold formatting for model override responses |
| `commands/reasoning.py` | Bold formatting for reasoning override responses |
| `commands/trigger.py` | Bold formatting for trigger mode responses |
| `commands/agent.py` | Bold formatting for engine selection responses |
| `telegram/bridge.py` | Inline keyboard rendering for control requests |
| `telegram/loop.py` | `callback_query_id` passthrough |
| `commands.py` | `parse_mode` field on `CommandResult` |

## Tests

- `test_claude_control.py` — 50 tests: control requests, response routing, registry lifecycle, auto-approve/auto-deny, tool auto-approve, custom deny messages, discuss action, early toast, progressive cooldown
- `test_callback_dispatch.py` — 25 tests: callback parsing, dispatch toast/ephemeral behaviour, early answering
- `test_exec_bridge.py` — 4 tests: ephemeral notification cleanup

## Development

```bash
# Install (editable)
pipx install -e /home/nathan/untether

# Run as systemd service
systemctl --user restart untether
journalctl --user -u untether -f

# Config
~/.untether/untether.toml

# Tests
cd /home/nathan/untether && uv run pytest

# Lint
cd /home/nathan/untether && uv run ruff check src/
```

## Conventions

- Python 3.12+, anyio for async, msgspec for JSONL parsing, structlog for logging
- Ruff for linting, pytest with coverage for tests
- Runner backends registered via entry points in `pyproject.toml`
