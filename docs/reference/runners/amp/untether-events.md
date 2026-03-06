# AMP -> Untether event mapping (spec)

This document describes how the AMP runner translates AMP CLI `--stream-json` JSONL events into Untether events.

> **Authoritative source:** The schema definitions are in `src/untether/schemas/amp.py` and the translation logic is in `src/untether/runners/amp.py`. When in doubt, refer to the code.

AMP uses a Claude Code-compatible JSONL protocol, so the event shapes are similar to the Claude runner but with some differences in usage reporting and session management.

---

## 1. Input stream contract (AMP CLI)

AMP CLI emits **one JSON object per line** (JSONL) when invoked with:

```
amp -x --stream-json <prompt>
```

Notes:
- `-x` is required for execute mode (non-interactive).
- `--stream-json` enables JSONL output.
- `--dangerously-allow-all` auto-approves all tool calls.
- For resumed sessions: `amp threads continue <thread-id> -x --stream-json`.

---

## 2. Resume tokens and resume lines

- Engine id: `amp`
- Canonical resume line (embedded in chat):

```
`amp threads continue T-2775dc92-90ed-4f85-8b73-8f9766029e83`
```

The token is the **thread ID** (format: `T-<uuid>`), captured from the `system(init)` event's `session_id` field.

Resume regex: `(?im)^\s*`?amp\s+threads\s+continue\s+(?P<token>T-[A-Za-z0-9-]+)`?\s*$`

---

## 3. Session lifecycle + serialization

Untether requires **serialization per session token**:

- For new runs (`resume=None`), do **not** acquire a lock until a `StartedEvent`
  is emitted (when the `system(init)` event arrives with a session ID).
- Once the session is known, acquire a lock for `amp:<thread_id>` and hold it
  until the run completes.
- For resumed runs, acquire the lock immediately on entry.

---

## 4. Event translation (AMP JSONL -> Untether)

### 4.1 `system` (subtype=`init`)

Example:
```json
{"type":"system","subtype":"init","session_id":"T-2775dc92-90ed-4f85-8b73-8f9766029e83","cwd":"/tmp","tools":["Bash","Read","Write"]}
```

Mapping:
- Only process if `subtype == "init"`.
- Emit `StartedEvent`.
- `resume = ResumeToken(engine="amp", value=session_id)`.
- `meta` includes model if configured.
- Store `session_id` in state.

### 4.2 `assistant` (tool_use blocks)

Example:
```json
{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_01","name":"Bash","input":{"command":"echo hello"}}],"usage":{"input_tokens":100,"output_tokens":20}}}
```

Mapping:
- Accumulate `usage` from `message.usage` (see section 6).
- For each `tool_use` block in `message.content`:
  - Emit `ActionEvent` with `phase="started"`.
  - `action.id = id`.
  - `action.kind` from tool name (see section 5).
  - `action.title` derived from tool + input.
  - Store in `pending_actions[id]`.

### 4.3 `assistant` (text blocks)

Example:
```json
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Done."}],"usage":{"input_tokens":50,"output_tokens":10}}}
```

Mapping:
- Accumulate `usage` from `message.usage`.
- For each `text` block, append to `state.last_text`.
- No Untether event emitted for text.

### 4.4 `user` (tool_result blocks)

Example:
```json
{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_01","content":[{"type":"text","text":"hello"}]}]}}
```

Mapping:
- For each `tool_result` block:
  - Pop from `pending_actions[tool_use_id]`.
  - Emit `ActionEvent` with `phase="completed"`.
  - `ok = !is_error`.
  - Extract text from `content` blocks for `output_preview` (truncated to 500 chars).

### 4.5 `result`

Example:
```json
{"type":"result","subtype":"success","is_error":false,"result":"hello","duration_ms":1500,"num_turns":1,"session_id":"T-..."}
```

Mapping:
- Emit `CompletedEvent`.
- `ok = !is_error`.
- `answer = state.last_text` (accumulated assistant text).
- `error = error` field if `is_error` is true.
- `resume = ResumeToken(engine="amp", value=session_id)`.
- `usage` from accumulated token data (see section 6).

### 4.6 Other events

Ignore unknown event types. If a JSONL line is malformed, log a warning and continue.

---

## 5. Tool name -> ActionKind mapping

AMP uses the same tool names as Claude Code (PascalCase). The runner delegates
to the shared `tool_kind_and_title()` helper with `task_kind="subagent"`.

| Tool name | ActionKind | Title logic |
|---|---|---|
| `Bash` | `command` | `input.command` |
| `Edit`, `Write` | `file_change` | `input.file_path` or `input.path` |
| `Read` | `tool` | `read: <path>` |
| `Grep` | `tool` | `grep: <pattern>` |
| `Glob` | `tool` | `glob: <pattern>` |
| `Task` | `subagent` | `task: <description>` |
| (default) | `tool` | tool name |

For `file_change`, include `detail.changes = [{"path": <path>, "kind": "update"}]`.

Path keys checked: `file_path`, `path`.

---

## 6. Usage accumulation

Unlike Claude Code (which reports usage in the `result` event) or Gemini (which reports in `result.stats`),
AMP reports per-message `usage` in each `assistant` event.

The runner accumulates tokens across all assistant messages:

```python
state.accumulated_usage["input_tokens"] += message.usage.input_tokens
state.accumulated_usage["output_tokens"] += message.usage.output_tokens
```

At completion, `_build_usage()` returns:

```python
{
    "usage": {
        "input_tokens": <total>,
        "output_tokens": <total>,
    }
}
```

Returns `None` if no usage data was accumulated.

---

## 7. Config keys

=== "untether config"

    ```sh
    untether config set amp.model "claude-sonnet-4-6"
    untether config set amp.mode "smart"
    untether config set amp.dangerously_allow_all true
    ```

=== "toml"

    ```toml
    [amp]
    model = "claude-sonnet-4-6"       # optional
    mode = "smart"                     # optional: deep|free|rush|smart
    dangerously_allow_all = true       # default: true
    ```
