# Antigravity -> Untether event mapping (spec)

This document describes how the Antigravity runner translates Antigravity CLI (`agy`) `--output-format stream-json` JSONL events into Untether events.

> **Authoritative source:** The schema definitions are in `src/untether/schemas/antigravity.py` and the translation logic is in `src/untether/runners/antigravity.py`. When in doubt, refer to the code.

---

## 1. Input stream contract (Antigravity CLI)

Antigravity CLI emits **one JSON object per line** (JSONL) when invoked with:

```
agy --output-format stream-json --prompt=<prompt>
```

Notes:
- `--output-format stream-json` enables NDJSON stream output.
- All events have an `event` field used as the discriminator (`init`, `step_update`, `result`, `error`).

---

## 2. Resume tokens and resume lines

- Engine id: `antigravity`
- CLI command: `agy`
- Canonical resume line (embedded in chat):

```
`agy --conversation fe2e5cfc-e5e4-4b67-8575-61ca39ba3ecb`
```

The token is the **conversation ID** (UUID string), captured from the `init` event's `conversation_id` field.

Resume regex: `(?im)^\s*`?agy\s+--conversation\s+(?P<token>[A-Za-z0-9_-]+)`?\s*$`

---

## 3. Session lifecycle + serialization

Untether requires **serialization per session token**:

- For new runs (`resume=None`), do **not** acquire a lock until a `StartedEvent`
  is emitted (when the `init` event arrives with a conversation ID).
- Once the conversation is known, acquire a lock for `antigravity:<conversation_id>` and hold it
  until the run completes.
- For resumed runs, acquire the lock immediately on entry.

---

## 4. Event translation (Antigravity JSONL -> Untether)

### 4.1 `init`

Example:
```json
{"event":"init","conversation_id":"abc-123","init":{"cwd":"/tmp","model":"gemini-3.8-flash-high"}}
```

Mapping:
- Emit `StartedEvent`.
- `resume = ResumeToken(engine="antigravity", value=conversation_id)`.
- `meta.model = model` (used for footer line).
- Store `conversation_id` and `model` in state.

### 4.2 `step_update` (step_type=tool)

Example:
```json
{"event":"step_update","step_update":{"step_index":1,"state":"ACTIVE","step_type":"tool","tool_name":"run_command","tool_info":{"CommandLine":"pytest"}}}
```

Mapping:
- If `state == "ACTIVE"`:
  - Emit `ActionEvent` with `phase="started"`.
  - `action.kind` and `title` derived from `tool_name` and `tool_info`.
  - Track in `pending_actions[str(step_index)]`.
- If `state == "DONE"`:
  - Emit `ActionEvent` with `phase="completed"`.
  - `ok = True`.
  - Pop from `pending_actions[str(step_index)]`.

### 4.3 `step_update` (step_type=agent_response)

Example:
```json
{"event":"step_update","step_update":{"step_index":2,"state":"ACTIVE","step_type":"agent_response","text_delta":"Processing..."}}
```

Mapping:
- Append `text_delta` to `state.last_text` for final answer text.
- No Untether event emitted.

### 4.4 `result`

Example:
```json
{"event":"result","result":{"conversation_id":"abc-123","status":"SUCCESS","response":"Done!","usage":{"input_tokens":100,"output_tokens":50}}}
```

Mapping:
- Emit `CompletedEvent`.
- `ok = (status == "SUCCESS")`.
- `answer = response or state.last_text`.
- `resume = ResumeToken(engine="antigravity", value=conversation_id)`.
- `usage` built from `usage` dict.

### 4.5 `error`

Example:
```json
{"event":"error","message":"Fatal error occurred"}
```

Mapping:
- Emit `CompletedEvent` with `ok=false`.
- `error = message`.
- `answer = state.last_text`.

---

## 5. Tool name mapping

Antigravity uses snake_case tool names. The runner maps them:

| Antigravity tool | ActionKind | Title logic |
|---|---|---|
| `run_command` | `command` | `tool_info.CommandLine` |
| `write_to_file` | `file_change` | `tool_info.TargetFile` |
| `replace_file_content` | `file_change` | `tool_info.TargetFile` |
| `view_file` | `tool` | `tool_info.AbsolutePath` |
| `search_web` | `tool` | `tool_info.query` |
| `read_url_content` | `tool` | `tool_info.Url` |
| `find_by_name` | `tool` | `tool_info.Pattern` |
| `grep_search` | `tool` | `tool_info.Query` |
| `list_dir` | `tool` | `tool_info.DirectoryPath` |
