# Untether — Cursor Rules

Telegram bridge for AI coding agents (Claude Code, Codex, OpenCode, Pi; Gemini CLI and Amp are deprecated and no longer supported — still included, may be removed in a future release). Lets users control agents from their phone or any device — agents run on the user's machine while they're away from the terminal.

## Stack & conventions

- Python 3.12+, anyio for async, msgspec for JSONL parsing, structlog for logging
- Ruff for linting/formatting, pytest + anyio for testing (80% coverage threshold)
- Australian English in user-facing text (realise, colour, behaviour, licence)
- Conventional commits: feat:, fix:, docs:, refactor:, test:, chore:

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

- Runner 3-event contract: StartedEvent → ActionEvent(s) → CompletedEvent (always); Claude live sessions (#776) may follow it with TurnEvent(started) → ActionEvent(s) → TurnEvent(completed) segments, never a second CompletedEvent
- Runner instances are shared across chats: bridge code reads the per-run RunStreamHandle, never runner.current_stream / runner.last_pid (#510)
- Use EventFactory for event construction, never construct dataclasses directly
- ALL Telegram writes go through TelegramOutbox (never call Bot API directly)
- Callback data max 64 bytes (Telegram-enforced)
- Use anyio (not raw asyncio), msgspec.Struct (not dataclasses for schemas)

## Testing

- Stub subprocess runners with fake CLI scripts
- FakeTransport protocol doubles (not real Telegram clients)
- Verify 3-event contract in runner tests
- Run: uv run pytest, uv run ruff format --check src/ tests/, uv run ruff check src/ tests/

## Key files

- runners/claude.py — Claude Code runner with interactive features
- runners/gemini.py, runners/amp.py — deprecated, unsupported runners (no removal scheduled; xfail/skip on sweeps, don't fix)
- runner_bridge.py — Runner-to-transport bridge
- cost_tracker.py — Per-run/daily cost tracking
- telegram/bridge.py — Telegram message rendering
- telegram/commands/claude_control.py — Approve/Deny/Discuss callback handler
- telegram/commands/stats.py — /stats session statistics
- telegram/commands/auth.py — /auth Codex device re-authentication
- markdown.py — Progress/final message formatting
