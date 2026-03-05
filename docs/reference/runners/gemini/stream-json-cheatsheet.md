# Gemini `--output-format stream-json` event cheatsheet

`gemini -p --output-format stream-json` writes **one JSON object per line** (JSONL) with a
required `type` field.

## Event types

### `init`

Session initialisation with session ID and model.

```json
{"type":"init","session_id":"abc123def","model":"gemini-2.0-flash-exp","timestamp":"2026-01-15T10:00:00Z"}
```

### `message`

Text content from user or assistant. Delta messages have `delta: true`.

```json
{"type":"message","role":"assistant","content":"The command output `hello`.","timestamp":"2026-01-15T10:00:05Z"}
```

### `tool_use`

Tool invocation with name, ID, and parameters.

```json
{"type":"tool_use","tool_name":"Bash","tool_id":"tool_1","parameters":{"command":"echo hello"},"timestamp":"2026-01-15T10:00:01Z"}
```

File operation example:

```json
{"type":"tool_use","tool_name":"write_file","tool_id":"tool_2","parameters":{"file_path":"notes.md","content":"hello"},"timestamp":"2026-01-15T10:00:03Z"}
```

### `tool_result`

Tool completion with status and output.

```json
{"type":"tool_result","tool_id":"tool_1","status":"success","output":"hello","timestamp":"2026-01-15T10:00:02Z"}
```

### `result`

Final result with status and token usage stats.

```json
{"type":"result","status":"success","stats":{"input_tokens":100,"output_tokens":50},"timestamp":"2026-01-15T10:00:06Z"}
```

Stats may also include `total_cost_usd`:

```json
{"type":"result","status":"success","stats":{"input_tokens":100,"output_tokens":50,"total_cost_usd":0.0025},"timestamp":"2026-01-15T10:00:06Z"}
```

### `error`

Error with message. Terminates the session.

```json
{"type":"error","message":"API key invalid or expired","timestamp":"2026-01-15T10:00:01Z"}
```

## Notes

* `init` is always the first event and contains `session_id` for resume.
* `message` events with `role = "assistant"` accumulate to form the final answer text.
* `tool_use` and `tool_result` events are paired by `tool_id`.
* `result` is the terminal event for successful runs; `error` for failures.
* Tool names are snake_case (e.g., `read_file`, `edit_file`, `write_file`).
