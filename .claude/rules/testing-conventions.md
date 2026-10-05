---
paths:
  - "tests/**"
---

# Testing Conventions

## Framework

- pytest + anyio for async tests
- structlog for log capture in tests
- msgspec for JSONL fixture generation

### Traps

- **Never `monkeypatch.setattr(module.logger, "warning", …)`** on a structlog lazy proxy. Undoing it pins a bound method with the default processors, silently hiding every later warning from `structlog.testing.capture_logs()` in full-suite runs only. Use `capture_logs()` to assert on logs.
- **`ClaudeRunner` is a slots dataclass**: overriding timing knobs (`_live_close_grace_s`, `_live_poll_s`, …) as subclass class attributes is inert — field defaults shadow them. Set them on the instance.

### Host isolation (#808)

`tests/conftest.py` isolates every test from the host automatically — **never
rely on the host config or the live network**:

- `_isolated_config` (autouse) patches `HOME_CONFIG_PATH` at every module
  binding (`HOME_CONFIG_PATH_MODULES`) to a per-test tmp path, deletes
  `UNTETHER_CONFIG_PATH`, and points `/usage`'s OAuth credentials path at tmp.
  A test that needs a config writes its own, or passes a path explicitly.
  It also calls `settings.clear_settings_cache()` before and after every test,
  so no test is served another test's parsed config (#506).
- `_no_live_network` (autouse) refuses non-loopback requests at
  `httpx.HTTPTransport` / `AsyncHTTPTransport` (`MockTransport` and loopback
  still work) and resets the usage cache. Opt out per test with
  `@pytest.mark.allow_network` (registered in `pyproject.toml`).
- `_host_config_untouched` (session) fails the run if the real
  `~/.untether/untether.toml` changes (sha256 + mtime).
- A new `HOME_CONFIG_PATH` import in `src/` must be added to
  `HOME_CONFIG_PATH_MODULES`; `tests/test_test_isolation.py` scans `src/` and
  fails otherwise.

## Patterns

### Stub subprocess runners

Use fake CLI scripts that emit known JSONL to test event translation:
```python
# Create a temporary script that outputs known events
fake_cli = tmp_path / "fake_claude"
fake_cli.write_text('#!/bin/bash\necho \'{"type":"system","subtype":"init",...}\'')
fake_cli.chmod(0o755)
```

### Mock transport

Use the `Transport` protocol for test doubles — don't instantiate `TelegramClient`:
```python
@dataclass
class FakeTransport:
    sent: list = field(default_factory=list)
    async def send(self, channel_id, message, options=None): ...
    async def edit(self, ref, message, wait=True): ...
    async def delete(self, ref): ...
```

### Event ordering assertions

Always verify the 3-event contract:
```python
events = [evt async for evt in runner.run(prompt, resume)]
assert isinstance(events[0], StartedEvent)
assert isinstance(events[-1], CompletedEvent)
assert all(isinstance(e, ActionEvent) for e in events[1:-1])
```

## Coverage

- Threshold: 80% (enforced by pytest config in `pyproject.toml`)
- Run all: `uv run pytest`
- Run specific: `uv run pytest tests/test_claude_control.py -x`

## Integration testing (MANDATORY before releases)

Unit tests don't cover live Telegram interaction. Before every version bump, drive `@untether_dev_bot` via the Telegram
MCP tools (`send_message`, `get_history`, `list_inline_buttons`, `press_inline_button`, `reply_to_message`, `send_voice`,
`send_file`) plus Bash (`journalctl`, `kill -TERM`). Chat IDs, tiers and the full pattern:
`docs/reference/integration-testing.md`; tier requirements per release type: `.claude/rules/release-discipline.md`.
The Gemini/AMP chats are deprecated and in no required tier. After a run, check dev logs and file issues for Untether
bugs (watch for phantom responses, cross-session contamination, wrong engine, disproportionate cost); note upstream
engine quirks separately.

## Test catalog

Per-file coverage lives in `docs/reference/test-catalog.md`. **When adding or substantially changing a test file,
update its entry there** — not this rule, and not `CLAUDE.md`.
