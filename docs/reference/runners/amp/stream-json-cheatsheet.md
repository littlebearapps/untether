# AMP `--stream-json` event cheatsheet

`amp -x --stream-json` writes **one JSON object per line** (JSONL) using a
Claude Code-compatible protocol with a `type` field discriminator.

## Event types

### `system` (subtype: `init`)

Session initialisation with thread ID, working directory, and available tools.

```json
{"type":"system","subtype":"init","session_id":"T-2775dc92-90ed-4f85-8b73-8f9766029e83","cwd":"/tmp","tools":["Bash","Read","Write"],"mcp_servers":[]}
```

### `assistant`

Assistant message containing text and/or tool use blocks. Includes per-message token usage.

Text block:

```json
{"type":"assistant","session_id":"T-2775dc92-90ed-4f85-8b73-8f9766029e83","message":{"role":"assistant","content":[{"type":"text","text":"hello"}],"stop_reason":"end_turn","usage":{"input_tokens":50,"output_tokens":10}}}
```

Tool use block:

```json
{"type":"assistant","session_id":"T-2775dc92-90ed-4f85-8b73-8f9766029e83","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_01","name":"Bash","input":{"command":"echo hello"}}],"stop_reason":"tool_use","usage":{"input_tokens":100,"output_tokens":20}}}
```

Mixed content (text + tool use):

```json
{"type":"assistant","session_id":"T-...","message":{"role":"assistant","content":[{"type":"text","text":"Let me check..."},{"type":"tool_use","id":"toolu_01","name":"Read","input":{"file_path":"src/main.py"}}],"stop_reason":"tool_use","usage":{"input_tokens":80,"output_tokens":30}}}
```

Subagent message (with `parent_tool_use_id`):

```json
{"type":"assistant","session_id":"T-...","message":{...},"parent_tool_use_id":"toolu_parent_01"}
```

### `user`

User message containing tool results.

```json
{"type":"user","session_id":"T-2775dc92-90ed-4f85-8b73-8f9766029e83","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_01","content":[{"type":"text","text":"hello"}]}]}}
```

Error result:

```json
{"type":"user","session_id":"T-...","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_01","is_error":true,"content":[{"type":"text","text":"command failed"}]}]}}
```

### `result`

Final result event. Always the last event in a run.

Success:

```json
{"type":"result","subtype":"success","is_error":false,"result":"hello","duration_ms":1500,"num_turns":1,"session_id":"T-2775dc92-90ed-4f85-8b73-8f9766029e83"}
```

Error:

```json
{"type":"result","subtype":"error","is_error":true,"result":"","error":"Authentication failed","session_id":"T-err-session-id","duration_ms":100,"num_turns":0}
```

## Notes

* AMP's protocol is Claude Code-compatible — `system(init)`, `assistant`, `user`, `result` follow the same shapes.
* Tool use and tool results are embedded in `message.content` blocks (not top-level events like Gemini).
* `usage` is per-message in `assistant` events — accumulate `input_tokens` and `output_tokens` across messages for session totals.
* `parent_tool_use_id` in `assistant`/`user` messages indicates subagent nesting.
* `stop_reason` can be `end_turn` (final text) or `tool_use` (pending tool call).
