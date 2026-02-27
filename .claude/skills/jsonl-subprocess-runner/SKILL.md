---
name: jsonl-subprocess-runner
description: >
  JSONL subprocess management patterns for Untether engine runners.
  Covers JsonlSubprocessRunner base class, template methods, event translation,
  session locking, resume tokens, process lifecycle, PTY usage, and concurrent
  session registries.
triggers:
  - working on runner code
  - adding a new engine backend
  - modifying event translation logic
  - changing subprocess management
  - working on session locking or resume tokens
  - debugging runner event streams
---

# JSONL Subprocess Runner Framework

All Untether engine runners (Claude, Codex, OpenCode, Pi) extend `JsonlSubprocessRunner`, which manages subprocess lifecycle, JSONL parsing, session locking, and error handling.

## Key files

| File | Purpose |
|------|---------|
| `src/untether/runner.py` | `JsonlSubprocessRunner` base class, `Runner` protocol, `SessionLockMixin`, `ResumeTokenMixin` |
| `src/untether/model.py` | `StartedEvent`, `ActionEvent`, `CompletedEvent`, `ResumeToken`, `Action`, `ActionKind` |
| `src/untether/events.py` | `EventFactory` — consistent event creation per engine |
| `src/untether/backends.py` | `EngineBackend` dataclass, `EngineConfig` |
| `src/untether/progress.py` | `ProgressTracker` — aggregates actions for UI rendering |
| `src/untether/runner_bridge.py` | `ProgressEdits`, `handle_message` — connects runners to transport |
| `src/untether/runners/` | Engine-specific runner implementations |

## Class hierarchy

```
Runner (Protocol)
  BaseRunner (SessionLockMixin)
    JsonlSubprocessRunner
      CodexRunner
      OpenCodeRunner
      PiRunner
      ClaudeRunner (overrides run_impl for PTY support)
```

## Template methods to override

When creating a new runner, override these methods on `JsonlSubprocessRunner`:

```python
class MyRunner(JsonlSubprocessRunner):
    engine: EngineId = "myengine"

    def command(self) -> str:
        """CLI binary name (e.g. 'codex', 'opencode', 'pi')."""

    def build_args(self, prompt, resume, *, state) -> list[str]:
        """Build CLI arguments. Called once per run."""

    def translate(self, data, *, state, resume, found_session) -> list[UntetherEvent]:
        """Translate a decoded JSON dict to Untether events. Core logic."""

    def new_state(self, prompt, resume) -> Any:
        """Create per-run state (e.g. EventFactory, accumulators)."""

    # Optional overrides:
    def stdin_payload(self, prompt, resume, *, state) -> bytes | None:
        """Data to send on stdin. Default: prompt.encode()."""

    def env(self, *, state) -> dict[str, str] | None:
        """Extra environment variables for the subprocess."""

    def decode_jsonl(self, *, line: bytes) -> Any | None:
        """Custom JSON decoder. Default: json.loads."""
```

## Event model (3 events)

Every run emits exactly this sequence:

```
StartedEvent       — emitted once when session ID is known
ActionEvent(s)     — zero or more, with phase: started → completed
CompletedEvent     — emitted exactly once, always last
```

### StartedEvent
```python
StartedEvent(engine="codex", resume=ResumeToken(engine="codex", value="thread_123"))
```

### ActionEvent
```python
ActionEvent(
    engine="codex",
    action=Action(id="item_5", kind="command", title="ls -la", detail={...}),
    phase="started",  # or "completed"
    ok=True,          # only on completed phase
)
```

### CompletedEvent
```python
CompletedEvent(engine="codex", ok=True, answer="Done.", resume=token, usage={...})
```

## ActionKind values

| Kind | When |
|------|------|
| `command` | Shell execution (Bash, shell) |
| `tool` | Generic tool call (Read, Grep, Glob, MCP tools) |
| `file_change` | File edits (Edit, Write, MultiEdit, NotebookEdit) |
| `web_search` | Web search/fetch (WebSearch, WebFetch) |
| `note` | Commentary, reasoning, todos (TodoWrite, AskUserQuestion) |
| `warning` | Non-fatal errors, permission denials |
| `turn` | Turn markers (metadata-only, ignored by ProgressTracker) |
| `telemetry` | Usage/cost data (metadata-only) |
| `subagent` | Subagent/Task invocations |

## Session locking

```python
class SessionLockMixin:
    session_locks: WeakValueDictionary[str, anyio.Semaphore]

    def lock_for(self, token: ResumeToken) -> anyio.Semaphore:
        # Key: "engine:session_id"
        # WeakValueDictionary auto-cleans when semaphore has no references
```

Locking rules:
- **Resume runs**: acquire lock immediately before spawning subprocess
- **New runs**: don't know session_id until `StartedEvent`; acquire lock when first `system.init` / `thread.started` / `step_start` arrives, before yielding the event
- Lock held until run completes (released in `finally`)
- Serialises concurrent runs on the same session

## Process lifecycle (`run_impl`)

```
1. new_state(prompt, resume)           → create per-run state
2. build_args(prompt, resume, state)   → construct CLI command
3. stdin_payload(prompt, resume, state) → optional stdin data
4. manage_subprocess(cmd, ...)         → spawn with PIPE for stdin/stdout/stderr
5. _send_payload(proc, payload)        → send stdin, close stdin
6. drain_stderr(proc.stderr)           → log stderr concurrently (task group)
7. _iter_jsonl_events(proc.stdout)     → parse JSONL, call translate()
8. proc.wait()                         → wait for exit code
9. stream_end_events() or              → emit CompletedEvent if not already emitted
   process_error_events()
```

## JSONL stream state

```python
@dataclass
class JsonlStreamState:
    expected_session: ResumeToken | None    # from resume arg
    found_session: ResumeToken | None       # from stream
    did_emit_completed: bool                # guard: exactly one CompletedEvent
    ignored_after_completed: bool           # drop lines after CompletedEvent
    jsonl_seq: int                          # line counter for logging
```

Key invariants:
- **Exactly one CompletedEvent per run** — after emitting, all subsequent lines are dropped
- **Session verification** — if expected_session is set and stream yields a different session_id, raise RuntimeError
- **Duplicate StartedEvent suppression** — only the first StartedEvent is yielded

## Error handling

| Scenario | Behaviour |
|----------|-----------|
| Invalid JSON line | `invalid_json_events()` → warning ActionEvent, continue |
| Decode error (msgspec) | `decode_error_events()` → warning ActionEvent, continue |
| Translation error | `translate_error_events()` → warning ActionEvent, continue |
| Non-zero exit code | `process_error_events()` → CompletedEvent(ok=False) |
| Stream ends without result | `stream_end_events()` → CompletedEvent(ok=False) |

## Resume token mixin

```python
class ResumeTokenMixin:
    engine: EngineId
    resume_re: re.Pattern[str]  # engine-specific regex

    def extract_resume(text) -> ResumeToken | None  # parse last match
    def format_resume(token) -> str                  # canonical resume line
    def is_resume_line(line) -> bool                 # for stripping from prompts
```

Resume line formats per engine:
- Claude: `` `claude --resume <session_id>` ``
- Codex: `` `codex resume <thread_id>` ``
- OpenCode: `` `opencode --session <ses_XXX>` ``
- Pi: `` `pi --session <token>` ``

## Engine backend registration

Each runner module exports a `BACKEND`:

```python
# src/untether/runners/myengine.py
BACKEND = EngineBackend(
    id="myengine",
    build_runner=_build_runner,
    cli_cmd="myengine",
    install_cmd="pip install myengine",
)
```

Registered in `pyproject.toml`:
```toml
[project.entry-points."untether.engine_backends"]
codex = "untether.runners.codex:BACKEND"
claude = "untether.runners.claude:BACKEND"
opencode = "untether.runners.opencode:BACKEND"
pi = "untether.runners.pi:BACKEND"
```

Discovery: `importlib.metadata.entry_points(group="untether.engine_backends")`

## EventFactory

Helper for consistent event creation:

```python
factory = EventFactory(engine="codex")
factory.started(token, title="Codex")
factory.action_started(action_id="item_1", kind="command", title="ls")
factory.action_completed(action_id="item_1", kind="command", title="ls", ok=True)
factory.completed_ok(answer="Done.", resume=token, usage={...})
factory.completed_error(error="timeout", resume=token)
```

## ClaudeRunner PTY exception

`ClaudeRunner` overrides `run_impl` entirely because it uses a PTY for the bidirectional control channel instead of standard PIPE stdin. It manages:
- `pty.openpty()` for stdin (prevents deadlock with persistent stdin)
- Session registries (`_SESSION_STDIN`, `_REQUEST_TO_SESSION`) for concurrent sessions
- Control request/response draining after every JSONL line

See `.claude/skills/claude-stream-json/SKILL.md` for Claude-specific details.
