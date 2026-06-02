# Pi -> Untether event mapping (spec)

This document describes how the Pi runner translates Pi CLI `--mode json` JSONL events into Untether events.

> **Authoritative source:** The schema definitions are in `src/untether/schemas/pi.py` and the translation logic is in `src/untether/runners/pi.py`. When in doubt, refer to the code.

The goal is to make Pi feel identical to the Codex/Claude Code runners from the bridge/renderer point of view while preserving Untether invariants (stable action ids, per-session serialization, single completed event).

---

## 1. Input stream contract (Pi CLI)

Pi CLI emits **one JSON object per line** (JSONL) when invoked with:

```
pi --print --mode json <prompt>
```

Notes:
- `--print` is required for non-interactive runs.
- `--mode json` outputs all agent events (no TUI banners).
- Pi does not support `-- <prompt>`; prompts starting with `-` must be
  prefixed (Untether does this automatically).

---

## 2. Resume tokens and resume lines

- Engine id: `pi`
- Canonical resume line (embedded in chat):

```
`pi --session <id>`
```

The token is the **short session id**, derived from the session header line
(`{"type":"session", ...}`) emitted on stdout when running in `--mode json`.
This requires **pi-coding-agent >= 0.45.1**.

Why not `--resume`?
- `--resume/-r` opens an interactive session picker; it does not accept a
  session token. Untether must use `--session <token>` instead.

---

## 3. Session lifecycle + serialization

Untether requires **serialization per session token**:

- For new runs (`resume=None`), do **not** acquire a lock until a `started`
  event is emitted (Untether emits this as soon as the session header or first
  JSON event arrives).
- Once the session is known, acquire a lock for `pi:<session_token>` and hold it
  until the run completes.
- For resumed runs, acquire the lock immediately on entry.

---

## 4. Event translation (Pi JSONL -> Untether)

Pi emits `AgentSessionEvent` objects. Only a subset is required for Untether.

**StartedEvent meta:** The Pi runner populates `meta` with `cwd`, and optionally `model` (from `--model` config) and `provider` (from `--provider` config). The `meta.model` field is used for the `🏷` footer line on final messages.

Priority order for `meta["model"]` (#225):
1. `run_options.model` — per-run override set via `/model set`
2. `self.model` — `pi.model` in `untether.toml`
3. `message.model` from Pi's `message_end` event — extracted when 1 and 2 are both unset, via a supplementary `StartedEvent` emitted once per session. `ProgressTracker.note_event` merges this meta onto the initial tracker state.

`message.model` is populated by the `pi` CLI itself (e.g. `"model": "gpt-4o-mini"` alongside `"api"`, `"provider"`, `"usage"` in every `message_end` payload). Earlier versions of this doc stated "Pi JSONL does not include model info" — that was incorrect.

### 4.1 `tool_execution_start`

Example:
```json
{"type":"tool_execution_start","toolCallId":"tool_1","toolName":"bash","args":{"command":"ls"}}
```

Mapping:
- Emit `action` with `phase="started"`.
- `action.id = toolCallId`.
- `action.kind` from tool name (see section 5).
- `action.title` derived from tool + args.

### 4.2 `tool_execution_end`

Example:
```json
{"type":"tool_execution_end","toolCallId":"tool_1","toolName":"bash","result":{...},"isError":false}
```

Mapping:
- Emit `action` with `phase="completed"`.
- `ok = !isError`.
- Carry `result` and `isError` in `detail` for debugging.

### 4.3 `message_end` (assistant)

Pi emits message lifecycle events. For `message_end` where `message.role == "assistant"`:

- Store the latest assistant text as the **final answer fallback**.
- If `stopReason` is `error` or `aborted`, store `errorMessage`.
- Capture `usage` for `completed.usage`.

### 4.4 `agent_end`

Example:
```json
{"type":"agent_end","messages":[...]} 
```

Mapping:
- Emit a single `completed` event:
  - `ok = true` unless the last assistant message has `stopReason` `error` or `aborted`.
  - `answer = last assistant text` (from `message_end` or `agent_end.messages`).
  - `error = errorMessage` if present.
  - `resume = ResumeToken(engine="pi", value=session_token)`.
  - `usage = last assistant usage`.

### 4.5 `auto_compaction_start` / `auto_compaction_end`

When Pi compacts its context window to free tokens, it emits these events.

`auto_compaction_start` example:
```json
{"type":"auto_compaction_start","reason":"context_limit"}
```

Mapping:
- Emit `action` with `phase="started"`, `kind="note"`.
- `action.title = "compacting context… (reason)"`.
- Sequential action ids: `compaction_1`, `compaction_2`, etc.

`auto_compaction_end` example:
```json
{"type":"auto_compaction_end","result":{"newNumTokens":42000},"aborted":false}
```

Mapping:
- Emit `action` with `phase="completed"`.
- `action.title = "context compacted (42,000 tokens)"` (formatted with commas).
- If `aborted=true`, title is `"context compaction aborted"`.

### 4.6 `auto_retry_start` / `auto_retry_end`

When Pi transparently retries a transient provider failure (e.g. a 5xx), it
emits these events (#460). Untether translates them so the retry is visible in
Telegram and the liveness watchdog sees event activity at each retry boundary
rather than mistaking the backoff gap for a stall.

`auto_retry_start` example:
```json
{"type":"auto_retry_start","attempt":2,"maxAttempts":5,"delayMs":1500,"errorMessage":"503"}
```

Mapping:
- Emit `action` with `phase="started"`, `kind="note"`.
- `action.title = "retrying provider (attempt 2/5, ~1.5s delay)"` — attempt and
  delay parts are omitted gracefully when the fields are null.
- Sequential action ids: `retry_1`, `retry_2`, … (stable across the start/end pair).

`auto_retry_end` example:
```json
{"type":"auto_retry_end","success":true,"attempt":2}
```

Mapping:
- Emit `action` with `phase="completed"` reusing the start action id.
- `success=true` → `action.title = "retry succeeded"`, `ok=true`.
- `success=false` → `action.title = "retry exhausted: <finalError>"`, `ok=false`.

> Note: a dedicated stall-watchdog "retry-in-progress is CPU-active" suppression
> branch is deferred — provider retry delays sit well under the stall threshold,
> and the translated events already keep the liveness idle timer fresh.

### 4.7 Other events

Ignore unknown events. If a JSONL line is malformed, emit a warning action and
continue (default `JsonlSubprocessRunner` behavior).

### 4.8 Stream-end fallback diagnostics (no `agent_end`)

If the Pi subprocess exits `rc=0` without an `agent_end` event, the runner emits
a failing `completed` event whose `error` now distinguishes two cases (#565):

- **No translated events** (a startup/early-exit crash — e.g. MCP servers still
  cold while a resumed session rehydrates tool state): "pi exited cleanly (rc=0)
  but produced no events …"; on a resumed run it adds "the session may have
  failed to load on resume".
- **Some events but no `agent_end`** (a genuinely truncated stream): keeps the
  "pi finished without an agent_end event" wording.

In both cases a sanitised tail of Pi's captured stderr is appended when present,
and the `pi.stream.no_agent_end` WARNING log carries `had_events` and `resumed`
fields so the issue-watcher can promote resumed-run failures to error tier.

---

## 5. Tool name -> ActionKind mapping heuristics

Pi tool names are lower-case by default. Suggested mapping:

| Tool name | ActionKind | Title logic |
| --- | --- | --- |
| `bash` | `command` | `args.command` |
| `edit`, `write` | `file_change` | `args.path` |
| `read` | `tool` | `read: <path>` |
| `grep` | `tool` | `grep: <pattern>` |
| `find` | `tool` | `find: <pattern>` |
| `ls` | `tool` | `ls: <path>` |
| (default) | `tool` | tool name |

For `file_change`, include `detail.changes = [{"path": <path>, "kind": "update"}]`.

---

## 6. Usage mapping

Untether `completed.usage` should mirror Pi's assistant `usage` object without
transformation.

---

## 7. Suggested Untether config keys

A minimal TOML config for Pi:

=== "untether config"

    ```sh
    untether config set pi.model "..."
    untether config set pi.provider "..."
    untether config set pi.extra_args "[]"
    ```

=== "toml"

    ```toml
    [pi]
    model = "..."
    provider = "..."
    extra_args = []
    ```

Use `extra_args` for any Pi CLI flags not explicitly mapped.
