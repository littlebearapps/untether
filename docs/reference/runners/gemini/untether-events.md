# Gemini -> Untether event mapping (spec)

This document describes how the Gemini runner translates Gemini CLI `--output-format stream-json` JSONL events into Untether events.

> **Authoritative source:** The schema definitions are in `src/untether/schemas/gemini.py` and the translation logic is in `src/untether/runners/gemini.py`. When in doubt, refer to the code.

---

## 1. Input stream contract (Gemini CLI)

Gemini CLI emits **one JSON object per line** (JSONL) when invoked with:

```
gemini -p --output-format stream-json <prompt>
```

Notes:
- `-p` is required for non-interactive (print) mode.
- `--output-format stream-json` enables JSONL output.
- All events have a `type` field used as the discriminator.

---

## 2. Resume tokens and resume lines

- Engine id: `gemini`
- Canonical resume line (embedded in chat):

```
`gemini --resume abc123def`
```

The token is the **session id** (short alphanumeric string), captured from the `init` event's `session_id` field.

Resume regex: `(?im)^\s*`?gemini\s+--resume\s+(?P<token>[A-Za-z0-9_-]+)`?\s*$`

---

## 3. Session lifecycle + serialization

Untether requires **serialization per session token**:

- For new runs (`resume=None`), do **not** acquire a lock until a `StartedEvent`
  is emitted (when the `init` event arrives with a session ID).
- Once the session is known, acquire a lock for `gemini:<session_id>` and hold it
  until the run completes.
- For resumed runs, acquire the lock immediately on entry.

---

## 4. Event translation (Gemini JSONL -> Untether)

### 4.1 `init`

Example:
```json
{"type":"init","session_id":"abc123def","model":"gemini-2.0-flash-exp"}
```

Mapping:
- Emit `StartedEvent`.
- `resume = ResumeToken(engine="gemini", value=session_id)`.
- `meta.model = model` (used for the footer line).
- Store `session_id` and `model` in state.

### 4.2 `tool_use`

Example:
```json
{"type":"tool_use","tool_name":"Bash","tool_id":"tool_1","parameters":{"command":"echo hello"}}
```

Mapping:
- Emit `ActionEvent` with `phase="started"`.
- `action.id = tool_id`.
- `action.kind` from tool name (see section 5).
- `action.title` derived from tool + parameters.
- Store in `pending_actions[tool_id]`.

### 4.3 `tool_result`

Example:
```json
{"type":"tool_result","tool_id":"tool_1","status":"success","output":"hello"}
```

Mapping:
- Emit `ActionEvent` with `phase="completed"`.
- `ok = (status == "success")`.
- Pop from `pending_actions[tool_id]`.
- Include `output_preview` in detail (truncated to 500 chars).

### 4.4 `message` (role=assistant)

Example:
```json
{"type":"message","role":"assistant","content":"Done."}
```

Mapping:
- Accumulate assistant text in `state.last_text` for the final answer.
- No Untether event emitted.

### 4.5 `result`

Example:
```json
{"type":"result","status":"success","stats":{"input_tokens":100,"output_tokens":50,"total_cost_usd":0.0025}}
```

Mapping:
- Emit `CompletedEvent`.
- `ok = (status == "success")`.
- `answer = state.last_text` (accumulated assistant text).
- `resume = ResumeToken(engine="gemini", value=session_id)`.
- `usage` built from `stats` (see section 6).

### 4.6 `error`

Example:
```json
{"type":"error","message":"API key invalid or expired"}
```

Mapping:
- Emit `CompletedEvent` with `ok=false`.
- `error = message`.
- `answer = state.last_text` (any text accumulated before the error).

### 4.7 Other events

Ignore unknown event types. If a JSONL line is malformed, log a warning and continue.

---

## 5. Tool name -> ActionKind mapping

Gemini uses snake_case tool names. The runner normalises them via `_TOOL_NAME_MAP` before
delegating to the shared `tool_kind_and_title()` helper.

| Gemini tool | Normalised | ActionKind | Title logic |
|---|---|---|---|
| `Bash` | `bash` | `command` | `parameters.command` |
| `read_file` | `read` | `tool` | `read: <path>` |
| `edit_file` | `edit` | `file_change` | `edit: <path>` |
| `write_file` | `write` | `file_change` | `write: <path>` |
| `web_search` | `websearch` | `tool` | `websearch: <query>` |
| `web_fetch` | `webfetch` | `tool` | `webfetch: <url>` |
| `list_dir` | `ls` | `tool` | `ls: <path>` |
| `find_files` | `glob` | `tool` | `glob: <pattern>` |
| `search_files` | `grep` | `tool` | `grep: <pattern>` |
| (default) | lowercased | `tool` | tool name |

For `file_change`, include `detail.changes = [{"path": <path>, "kind": "update"}]`.

Path keys checked: `file_path`, `path`, `filePath`.

---

## 6. Usage mapping

`_build_usage(stats)` extracts from the `result.stats` dict:

```python
{
    "total_cost_usd": stats.get("total_cost_usd"),  # float, optional
    "usage": {
        "input_tokens": stats.get("input_tokens"),   # int
        "output_tokens": stats.get("output_tokens"),  # int
    }
}
```

Returns `None` if stats is empty or missing.

---

## 7. Config keys

=== "untether config"

    ```sh
    untether config set gemini.model "gemini-2.5-pro"
    ```

=== "toml"

    ```toml
    [gemini]
    model = "gemini-2.5-pro"   # optional; passed as --model
    ```
