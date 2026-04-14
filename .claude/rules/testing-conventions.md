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

## Integration testing (MANDATORY before releases)

Unit tests cover code paths but NOT live Telegram interaction. Before every version bump, run integration tests against `@untether_dev_bot`. See `docs/reference/integration-testing.md` for the full playbook and `.claude/rules/release-discipline.md` for tier requirements per release type.

## Integration testing via Telegram MCP

Integration tests are automated via Telegram MCP tools by Claude Code during the release process. See `docs/reference/integration-testing.md` for the full playbook.

### Test chats

| Chat | Chat ID | Bot API chat_id |
|------|---------|-----------------|
| Claude Code | `5284581592` | `-5284581592` |
| Codex CLI | `4929463515` | `-4929463515` |
| OpenCode | `5200822877` | `-5200822877` |
| Pi | `5156256333` | `-5156256333` |
| Gemini CLI | `5207762142` | `-5207762142` |
| AMP CLI | `5230875989` | `-5230875989` |

### Pattern

1. `send_message` — send test prompt or command to engine chat
2. Wait for bot response (sleep or poll)
3. `get_history`/`get_messages` — read back response, verify content
4. `list_inline_buttons` → `press_inline_button` for interactive tests
5. `reply_to_message` for resume/session continuation tests

### Log inspection and issue creation

After integration tests, use Bash tool to check dev bot logs for warnings/errors and create GitHub issues for any Untether bugs found. Distinguish Untether bugs from upstream engine API errors.

### Detecting unexpected engine behaviour

Watch for phantom responses (substantive output from empty input), session cross-contamination, wrong engine running, or disproportionate cost. Note the engine, chat ID, message IDs, and exact behaviour. Create a GitHub issue if the root cause is in Untether; note as an engine quirk if upstream.

### Additional MCP tools

- `send_voice` — OGG/Opus voice files for T1 (voice message test)
- `send_file` — file upload/media group tests (T2, T5)
- Bash tool — `kill -TERM` for B4 (SIGTERM), `journalctl` for B5 (log inspection)

All integration test tiers are fully automatable by Claude Code.

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
| `test_build_args.py` | CLI argument construction for all 6 engines |
| `test_loop_coverage.py` | Update loop edge cases, message routing, shutdown |
| `test_exec_runner.py` | Event tracking, ring buffer, PID in StartedEvent meta |
| `test_runner_utils.py` | Error formatting, drain_stderr, stderr sanitisation |
| `test_trigger_server.py` | Webhook HTTP server, multipart, rate limit burst, fire-and-forget dispatch |
| `test_trigger_actions.py` | file_write (multipart short-circuit), http_forward (SSRF), notify_only |
| `test_trigger_cron.py` | Cron expression matching, timezone conversion, step validation |
| `test_trigger_settings.py` | CronConfig/WebhookConfig/TriggersSettings validation, timezone |
| `test_trigger_ssrf.py` | SSRF blocking (IPv4/IPv6, DNS rebinding, allowlist) |
| `test_trigger_fetch.py` | Cron data-fetch (HTTP, file read, parse modes, failure) |
