---
applies_to: "tests/**"
---

# Testing Conventions

## Framework

- pytest + anyio for async tests
- structlog for log capture in tests
- msgspec for JSONL fixture generation

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

## Key test files

| File | Covers |
|------|--------|
| `test_claude_control.py` | Control channel, session registries, auto-approve, cooldown |
| `test_callback_dispatch.py` | Callback parsing, dispatch, early answering |
| `test_exec_bridge.py` | Ephemeral cleanup, approval notifications |
| `test_ask_user_question.py` | AskUserQuestion handling, question extraction, answer routing |
| `test_diff_preview.py` | Edit/Write/Bash diff preview formatting and truncation |
| `test_cost_tracker.py` | Per-run/daily cost tracking, budget alerts, daily reset |
| `test_export_command.py` | Session export (markdown/JSON), event recording, trimming |
| `test_browse_command.py` | File browser, path registry, inline keyboards, project root |
| `test_codex_runner.py` | Codex event translation, session locking |
| `test_opencode_runner.py` | OpenCode event translation |
| `test_pi_runner.py` | Pi event translation, session ID promotion |
| `test_settings.py` | Config validation, engine config parsing |
