# Untether — Gemini Instructions

Telegram bridge for AI coding agents. Control Claude Code, Codex, OpenCode, Pi, Gemini CLI, and Amp from your phone or any device — agents run on your machine in the background while you're away from the terminal. Features interactive permissions, voice input, cost tracking, and live progress streaming.

## Stack & conventions

- Python 3.12+, anyio for async, msgspec for JSONL parsing, structlog for logging
- Ruff for linting/formatting, pytest + anyio for testing (80% coverage threshold)
- Australian English in user-facing text (realise, colour, behaviour, licence)
- Conventional commits: feat:, fix:, docs:, refactor:, test:

## Architecture

```
Telegram <-> TelegramPresenter <-> RunnerBridge <-> Runner
                                       |
                                  ProgressTracker
```

- Runners (src/untether/runners/) — engine subprocess managers
- RunnerBridge (src/untether/runner_bridge.py) — connects runners to transport
- TelegramPresenter (src/untether/telegram/bridge.py) — message rendering
- Commands (src/untether/telegram/commands/) — Telegram command handlers
- Schemas (src/untether/schemas/) — msgspec structs for JSONL decoding

## Key rules

- Runner 3-event contract: StartedEvent -> ActionEvent(s) -> CompletedEvent (always)
- Use EventFactory for event construction, never construct dataclasses directly
- ALL Telegram writes go through TelegramOutbox (never call Bot API directly)
- Callback data max 64 bytes (Telegram-enforced)
- Use anyio (not raw asyncio), msgspec.Struct (not dataclasses for schemas)

## Testing

- Stub subprocess runners with fake CLI scripts
- FakeTransport protocol doubles (not real Telegram clients)
- Verify 3-event contract in runner tests
- Run: uv run pytest, uv run ruff check src/

## Key files

- runners/claude.py — Claude Code runner with interactive features
- runners/gemini.py — Gemini CLI runner
- runners/amp.py — AMP CLI runner (Sourcegraph)
- runner_bridge.py — Runner-to-transport bridge
- cost_tracker.py — Per-run/daily cost tracking
- telegram/bridge.py — Telegram message rendering
- commands/claude_control.py — Approve/Deny/Discuss callback handler
- markdown.py — Progress/final message formatting
