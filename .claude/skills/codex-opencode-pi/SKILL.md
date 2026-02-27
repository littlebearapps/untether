---
name: codex-opencode-pi
description: >
  Codex CLI, OpenCode CLI, and Pi CLI runner protocols for Untether.
  Covers JSONL event types, event translation, resume mechanisms,
  and key differences between engines. All three are non-interactive
  (no control channel).
triggers:
  - working on Codex runner code
  - working on OpenCode runner code
  - working on Pi runner code
  - adding support for a new engine
  - comparing engine event models
  - debugging non-Claude runner streams
---

# Codex, OpenCode, and Pi Runner Protocols

These three engines are **non-interactive only** — no control channel, no permission prompts. They extend `JsonlSubprocessRunner` directly (unlike ClaudeRunner which overrides `run_impl`).

## Quick comparison

| Aspect | Codex | OpenCode | Pi |
|--------|-------|----------|-----|
| **CLI** | `codex exec --json` | `opencode run --format json` | `pi --print --mode json` |
| **Event model** | Turn-based (items in turns) | Step-based (tools in steps) | Agent-based (messages + tools) |
| **Resume line** | `` `codex resume <thread_id>` `` | `` `opencode --session ses_XXX` `` | `` `pi --session <token>` `` |
| **Resume token** | thread_id (UUID) | ses_XXXX (26+ chars) | UUID short (8 chars) or path |
| **Final answer** | `agent_message` item | Accumulated `text` events | `message_end` assistant content |
| **Error signal** | `turn.failed` | `error` event / missing `step_finish` | `stopReason` in `message_end` |

---

## Codex

### Key files

| File | Purpose |
|------|---------|
| `src/untether/runners/codex.py` | `CodexRunner` implementation |
| `src/untether/schemas/codex.py` | msgspec structs for Codex events |
| `docs/reference/runners/codex/exec-json-cheatsheet.md` | JSONL event shapes |
| `docs/reference/runners/codex/untether-events.md` | Event mapping spec |

### CLI invocation

```bash
codex exec --json --skip-git-repo-check --color=never \
  [--model MODEL] [--session THREAD_ID -] [-]
```

- Prompt on stdin (trailing `-` means read stdin)
- Resume: `--session <thread_id> -`
- `--skip-git-repo-check --color=never` for clean output

### JSONL events

| Event | Untether mapping |
|-------|-----------------|
| `thread.started` | `StartedEvent(resume=thread_id)` |
| `item.started` | `ActionEvent(phase="started")` |
| `item.updated` | `ActionEvent(phase="updated")` |
| `item.completed` | `ActionEvent(phase="completed")` |
| `turn.completed` | `CompletedEvent(ok=True, answer=final_answer)` |
| `turn.failed` | `CompletedEvent(ok=False, error=message)` |
| `error` (transient) | Progress note (reconnect handling) |

### Item types

| Item type | ActionKind | Notes |
|-----------|-----------|-------|
| `command_execution` | `command` | ok = (status=="completed" && exit_code==0) |
| `mcp_tool_call` | `tool` | Title: `server.tool` |
| `file_change` | `file_change` | `detail.changes = [{path, kind}]` |
| `web_search` | `web_search` | Title: query |
| `todo_list` | `note` | `detail.done`, `detail.total` |
| `reasoning` | `note` | Reasoning text |
| `agent_message` | (not emitted) | Stored as final answer candidate |
| `error` (item) | `warning` | Non-fatal error |

### Final answer selection

Multiple `agent_message` items may appear. Selection:
1. Prefer item with `phase == "final_answer"`
2. Fall back to last unnamed `agent_message`
3. Used in `CompletedEvent.answer`

### Config keys

```toml
[codex]
profile = "Codex"        # Codex profile name
extra_args = []           # additional CLI flags
```

---

## OpenCode

### Key files

| File | Purpose |
|------|---------|
| `src/untether/runners/opencode.py` | `OpenCodeRunner` implementation |
| `src/untether/schemas/opencode.py` | msgspec structs for OpenCode events |
| `docs/reference/runners/opencode/runner.md` | Runner spec |
| `docs/reference/runners/opencode/stream-json-cheatsheet.md` | JSONL event shapes |
| `docs/reference/runners/opencode/untether-events.md` | Event mapping spec |

### CLI invocation

```bash
opencode run --format json [--session SESSION_ID] [--model MODEL] -- <prompt>
```

- Prompt as positional arg after `--`
- Resume: `--session ses_XXX`
- Session IDs: `ses_` prefix + 20+ chars

### JSONL events

| Event | Untether mapping |
|-------|-----------------|
| `step_start` (first, with `sessionID`) | `StartedEvent(resume=sessionID)` |
| `tool_use` (status="completed") | `ActionEvent(phase="completed", ok=exit==0)` |
| `tool_use` (status="error") | `ActionEvent(phase="completed", ok=False)` |
| `text` | Accumulated as final answer (no action) |
| `step_finish` (reason="stop") | `CompletedEvent(ok=True, answer=text)` |
| `error` | `CompletedEvent(ok=False, error=message)` |

### Tool mapping

| Tool | ActionKind |
|------|-----------|
| `bash`, `shell` | `command` |
| `edit`, `write`, `multiedit` | `file_change` |
| `read`, `glob`, `grep` | `tool` |
| `websearch`, `web_search`, `webfetch`, `web_fetch` | `web_search` |
| `todowrite`, `todoread` | `note` |
| `task` | `tool` |
| (other) | `tool` |

### Not yet implemented

Usage accumulation: OpenCode's `step_finish` may include token/cost data but the runner does not currently extract it. `CompletedEvent.usage` is not populated.

### Config keys

```toml
[opencode]
model = "claude-sonnet-4-5-20250929"
```

---

## Pi

### Key files

| File | Purpose |
|------|---------|
| `src/untether/runners/pi.py` | `PiRunner` implementation |
| `src/untether/schemas/pi.py` | msgspec structs for Pi events |
| `docs/reference/runners/pi/runner.md` | Runner spec |
| `docs/reference/runners/pi/stream-json-cheatsheet.md` | JSONL event shapes |
| `docs/reference/runners/pi/untether-events.md` | Event mapping spec |

### CLI invocation

```bash
pi --print --mode json [--session SESSION_PATH] \
  [--provider PROVIDER] [--model MODEL] <prompt>
```

- Prompt as positional arg (prefixed with space if starts with `-`)
- Resume: `--session <token>` (short ID or full path)
- Minimum version: 0.45.1
- Environment: `NO_COLOR=1`, `CI=1` (set by runner)

### JSONL events

| Event | Untether mapping |
|-------|-----------------|
| `session` | Session ID extraction, possible ID promotion |
| `agent_start` | `StartedEvent(resume=session_token)` |
| `tool_execution_start` | `ActionEvent(phase="started")` |
| `tool_execution_end` | `ActionEvent(phase="completed", ok=!isError)` |
| `message_end` (assistant) | Final answer + usage stored |
| `agent_end` | `CompletedEvent(ok=..., answer=last_text)` |

### Session ID promotion

Pi has a unique resume mechanism:
1. For new runs, Untether generates a session `.jsonl` file path
2. If a `session` header arrives with a UUID, the resume token is promoted to the 8-char short ID
3. `allow_id_promotion` flag ensures this only happens once
4. This gives a user-friendly resume token instead of a long path

Session path format: `~/.pi/agent/sessions/--<sanitized-cwd>--/<date>-<uuid>.jsonl`

### Tool mapping

| Tool | ActionKind | Title source |
|------|-----------|-------------|
| `bash` | `command` | `args.command` |
| `edit`, `write` | `file_change` | `args.path` |
| `read`, `grep`, `find`, `ls` | `tool` | `tool: <path>` or `tool: <pattern>` |
| (other) | `tool` | tool name |

### Error detection

- `stopReason` in `message_end`: `"error"` or `"aborted"` -> `ok=False`
- No `agent_end` received -> `CompletedEvent(ok=False, error="stream ended...")`

### Config keys

```toml
[pi]
provider = "anthropic"   # or "openai", "google", etc.
model = "claude-sonnet-4-5-20250929"
extra_args = []
```

---

## Adding a new engine

To add a new engine runner:

1. Create `src/untether/runners/myengine.py`
2. Define schemas in `src/untether/schemas/myengine.py`
3. Implement `MyEngineRunner(JsonlSubprocessRunner)` with required template methods
4. Export `BACKEND = EngineBackend(id="myengine", build_runner=..., cli_cmd="myengine")`
5. Register in `pyproject.toml` entry points:
   ```toml
   myengine = "untether.runners.myengine:BACKEND"
   ```
6. Add reference docs in `docs/reference/runners/myengine/`
7. Add tests mirroring existing `tests/test_*_runner.py` patterns
