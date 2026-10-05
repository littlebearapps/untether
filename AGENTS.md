# Untether — Agent Instructions

Telegram bridge for AI coding agents. Control Claude Code, Codex, OpenCode, and Pi from your phone (Gemini CLI and Amp are deprecated and unsupported — when a sweep breaks them, xfail/skip the test rather than fixing the runner) or any device — agents run on your machine in the background while you're away from the terminal. Features interactive permissions, voice input, cost tracking, and live progress streaming.

Engine parity roadmap: v0.36.1 Antigravity (#558), v0.36.2 Codex app-server (#960–#968), v0.36.3 OpenCode ACP (#969–#974).

## Architecture

```
Telegram <-> TelegramPresenter <-> RunnerBridge <-> Runner (claude/codex/opencode/pi/gemini/amp)
                                       |
                                  ProgressTracker
```

- **Runners** (`src/untether/runners/`) — engine-specific subprocess managers
- **RunnerBridge** (`src/untether/runner_bridge.py`) — connects runners to Telegram presenter
- **TelegramPresenter** (`src/untether/telegram/bridge.py`) — renders progress, inline keyboards, answers
- **Commands** (`src/untether/telegram/commands/`) — command/callback handlers
- **Schemas** (`src/untether/schemas/`) — msgspec structs for JSONL parsing

## Key conventions

- Python 3.12+, anyio for async, msgspec for JSONL, structlog for logging
- Ruff for linting (`uv run ruff check src/ tests/`), pytest with 80% coverage threshold
- Australian English in user-facing text (realise, colour, behaviour, licence)
- Conventional commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`
- Feature branches: `feature/*`, `fix/*`, `docs/*`

## Runner 3-event contract

Every run MUST emit exactly:
1. `StartedEvent` — once, when session ID is known
2. `ActionEvent(s)` — zero or more
3. `CompletedEvent` — exactly once, always final

Exception (Claude live sessions, #776): after `CompletedEvent` a runner that keeps its process live may emit later turns as `TurnEvent(started) → ActionEvent* → TurnEvent(completed)` segments. Never a second `CompletedEvent`. See `.claude/rules/runner-development.md`.

Use `EventFactory` for event construction. Never construct event dataclasses directly.

Runner instances are shared across chats: bridge code reads the per-run `RunStreamHandle` (filled by `publish_run_stream()`), never `runner.current_stream` / `runner.last_pid`, which are diagnostics only (#510).

## Telegram transport rules

- ALL writes go through `TelegramOutbox` (never call Bot API directly)
- Callback data max 64 bytes, format: `prefix:action:id`
- Call `answerCallbackQuery` promptly to clear button spinners
- Message limit 4096 chars; Untether splits long responses across multiple messages by default

## Testing

```sh
uv run pytest                    # all tests
uv run pytest tests/test_*.py -x # specific file
```

- Use stub subprocess runners with fake CLI scripts
- Use `FakeTransport` protocol doubles (not real Telegram clients)
- Verify 3-event contract in all runner tests

## Commands

| Command | Description |
|---------|-------------|
| `/cancel` | Stop the running agent (an idle live session is closed; pending `/at` runs and loops are dropped too) |
| `/agent` | Show or set engine for this chat |
| `/model` | Override the model for an engine |
| `/planmode` | Set Claude Code permission mode (on/plan-auto/auto/off) |
| `/usage` | Claude subscription quota; token totals for Codex/OpenCode |
| `/stats` | Per-engine session statistics (today/week/all-time) |
| `/auth` | Codex device re-authentication |
| `/export` | Export session transcript |
| `/browse` | Browse project files |
| `/config` | Interactive settings menu |
| `/steer` / `/queue` | Claude: steer a follow-up into the running session, or queue it (bare form sets the chat/topic default) |
| `/verbose` | Toggle verbose progress mode |
| `/restart` | Gracefully restart Untether |

## Before committing

```sh
uv run ruff format --check src/ tests/
uv run ruff check src/ tests/
uv run pytest
uv lock --check
```
