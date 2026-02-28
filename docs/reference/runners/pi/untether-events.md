# Pi -> Untether event mapping (spec)

This document describes how the Pi runner translates Pi CLI `--mode json` JSONL events into Untether events.

> **Authoritative source:** The schema definitions are in `src/untether/schemas/pi.py` and the translation logic is in `src/untether/runners/pi.py`. When in doubt, refer to the code.

The goal is to make Pi feel identical to the Codex/Claude runners from the bridge/renderer point of view while preserving Untether invariants (stable action ids, per-session serialization, single completed event).

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

**StartedEvent meta:** The Pi runner populates `meta` with `cwd`, and optionally `model` (from `--model` config) and `provider` (from `--provider` config). The `meta.model` field is used for the `🏷` footer line on final messages. Pi JSONL does not include model info in its event stream, so this comes from runner config.

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

### 4.6 Other events

Ignore unknown events. If a JSONL line is malformed, emit a warning action and
continue (default `JsonlSubprocessRunner` behavior).

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
