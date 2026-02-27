After you finish work, commit with a conventional message. Only commit the files you edited.

Always run checks before committing:
```sh
uv run ruff check src/
uv run pytest
```

If you fix anything from the checks, rerun and confirm they pass before committing.

When using gh to edit or create PR descriptions, prefer `--body-file` to preserve newlines.
Always include a "Manual testing" checklist section in PRs.

## Project conventions

- Python 3.12+, anyio for async, msgspec for JSONL parsing, structlog for logging
- Australian English in user-facing text (realise, colour, behaviour, licence)
- Conventional commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`
- 80% test coverage threshold enforced
- Runner 3-event contract: StartedEvent -> ActionEvent(s) -> CompletedEvent
- All Telegram writes go through TelegramOutbox (never call Bot API directly)
- Use EventFactory for event construction, never construct dataclasses directly
