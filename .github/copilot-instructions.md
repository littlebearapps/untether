# Untether — Copilot Instructions

Untether is a Telegram bridge for AI coding agents (Claude Code, Codex, OpenCode, Pi). It runs on the user's machine and streams agent progress to Telegram.

## Stack

- Python 3.12+, anyio (async), msgspec (JSONL parsing), structlog (logging)
- Ruff (linting/formatting), pytest + anyio (testing), 80% coverage threshold
- pydantic-settings for config, httpx for HTTP, typer for CLI

## Code style

- Australian English in user-facing strings (realise, colour, behaviour, licence)
- Type hints on all public functions
- Use `match/case`, `type | None` unions, and other Python 3.12+ features
- Use `anyio` for async (not raw asyncio)
- Use `msgspec.Struct` for data classes (not dataclasses or pydantic models for schemas)
- Use `structlog` for logging (not stdlib logging)

## Architecture

Runners (`src/untether/runners/`) manage engine subprocesses, emit events via the 3-event contract (StartedEvent → ActionEvent(s) → CompletedEvent). RunnerBridge connects runners to the TelegramPresenter which handles message rendering and inline keyboards.

All Telegram writes go through `TelegramOutbox` — never call Bot API directly.

## Testing patterns

- Stub subprocess runners with fake CLI scripts for engine tests
- `FakeTransport` protocol doubles instead of real Telegram clients
- Verify the 3-event contract: `StartedEvent` → `ActionEvent(s)` → `CompletedEvent`
- Use `pytest-anyio` for async tests

## Commit conventions

- Conventional commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`
- Feature branches: `feature/*`, `fix/*`, `docs/*`
- Every bug fix references a GitHub issue
- Update CHANGELOG.md for user-facing changes
