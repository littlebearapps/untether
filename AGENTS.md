# Untether — Agent Instructions

Telegram bridge for AI coding agents. Connects Claude Code, Codex, OpenCode, and Pi to Telegram with interactive permissions, voice notes, and live progress streaming.

## Architecture

```
Telegram <-> TelegramPresenter <-> RunnerBridge <-> Runner (claude/codex/opencode/pi)
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
- Ruff for linting (`uv run ruff check src/`), pytest with 80% coverage threshold
- Australian English in user-facing text (realise, colour, behaviour, licence)
- Conventional commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`
- Feature branches: `feature/*`, `fix/*`, `docs/*`

## Runner 3-event contract

Every run MUST emit exactly:
1. `StartedEvent` — once, when session ID is known
2. `ActionEvent(s)` — zero or more
3. `CompletedEvent` — exactly once, always final

Use `EventFactory` for event construction. Never construct event dataclasses directly.

## Telegram transport rules

- ALL writes go through `TelegramOutbox` (never call Bot API directly)
- Callback data max 64 bytes, format: `prefix:action:id`
- Call `answerCallbackQuery` promptly to clear button spinners
- Message limit 4096 chars; Untether trims to ~3500 by default

## Testing

```sh
uv run pytest                    # all tests
uv run pytest tests/test_*.py -x # specific file
```

- Use stub subprocess runners with fake CLI scripts
- Use `FakeTransport` protocol doubles (not real Telegram clients)
- Verify 3-event contract in all runner tests

## Before committing

```sh
uv run ruff check src/
uv run pytest
uv lock --check
```
