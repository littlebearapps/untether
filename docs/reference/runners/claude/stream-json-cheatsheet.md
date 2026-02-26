# Claude `stream-json` event cheatsheet

`claude -p --output-format stream-json --input-format stream-json --verbose` writes
**one JSON object per line** (JSONL) with a required `type` field.

In permission mode (without `-p`), the same flags apply but the prompt is sent via
stdin as JSON rather than as a CLI argument.

This cheatsheet is derived from `humanlayer/claudecode-go/types.go` and
`client_test.go`.

## Top-level event lines

### `system` (init)

Fields:
- `type`: `"system"`
- `subtype`: `"init"`
- `session_id`
- `tools`: array of tool names
- `mcp_servers`: array of `{name, status}`
- `cwd`, `model`, `permissionMode`, `apiKeySource` (optional)

Example:
```json
{"type":"system","subtype":"init","session_id":"session_01","cwd":"/repo","model":"sonnet","permissionMode":"auto","apiKeySource":"env","tools":["Bash","Read","Write","WebSearch"],"mcp_servers":[{"name":"approvals","status":"connected"}]}
```

### `assistant` / `user`

Fields:
- `type`: `"assistant"` or `"user"`
- `session_id`
- `message` (see below)

Example (assistant text):
```json
{"type":"assistant","session_id":"session_01","message":{"id":"msg_1","type":"message","role":"assistant","content":[{"type":"text","text":"Planning next steps."}],"usage":{"input_tokens":120,"output_tokens":45}}}
```

Example (assistant tool use):
```json
{"type":"assistant","session_id":"session_01","message":{"id":"msg_2","type":"message","role":"assistant","content":[{"type":"tool_use","id":"toolu_1","name":"Bash","input":{"command":"ls -la"}}]}}
```

Example (user tool result, string content):
```json
{"type":"user","session_id":"session_01","message":{"id":"msg_3","type":"message","role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_1","content":"total 2\nREADME.md\nsrc\n"}]}}
```

Example (user tool result, array content):
```json
{"type":"user","session_id":"session_01","message":{"id":"msg_4","type":"message","role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_2","content":[{"type":"text","text":"Task completed"}]}]}}
```

Optional parent field (for nested tool usage):
```json
{"type":"assistant","parent_tool_use_id":"toolu_parent","session_id":"session_01", ...}
```

### `result`

Fields (success path):
- `type`: `"result"`
- `subtype`: `"success"` (or `"completion"`)
- `session_id`
- `total_cost_usd`, `is_error`, `duration_ms`, `duration_api_ms`, `num_turns`
- `result`: final answer string
- `usage`: usage object
- `modelUsage`: optional per-model usage

Example (success):
```json
{"type":"result","subtype":"success","session_id":"session_01","total_cost_usd":0.0123,"is_error":false,"duration_ms":12345,"duration_api_ms":12000,"num_turns":2,"result":"Done.","usage":{"input_tokens":150,"output_tokens":70,"service_tier":"standard","server_tool_use":{"web_search_requests":0}}}
```

Fields (error path):
- Same as success, but `is_error`: `true`, `subtype`: `"error"`
- `result` may be empty or contain an error description

Example (error):
```json
{"type":"result","subtype":"error","session_id":"session_02","total_cost_usd":0.001,"is_error":true,"duration_ms":2000,"duration_api_ms":1800,"num_turns":1,"result":""}
```

Optional fields (may appear in upstream Claude CLI output but are **not** captured
by Untether's `StreamResultMessage` schema):
- `error`: error description string
- `permission_denials`: array of `{tool_name, tool_use_id, tool_input}`
- `structured_output`: arbitrary structured output (captured by schema but unused)

### `rate_limit_event`

Informational event emitted when Claude Code hits or approaches a rate limit (CLI v2.1.45+).
Purely informational — the run continues, it does not terminate the session.

Fields:
- `type`: `"rate_limit_event"`
- `rate_limit_info` (optional): object with rate limit details

`rate_limit_info` fields (all optional):
- `requests_limit`, `requests_remaining`, `requests_reset` (ISO 8601)
- `tokens_limit`, `tokens_remaining`, `tokens_reset` (ISO 8601)
- `retry_after_ms`

Example (full):
```json
{"type":"rate_limit_event","rate_limit_info":{"requests_limit":1000,"requests_remaining":0,"requests_reset":"2026-01-01T00:01:00Z","tokens_limit":50000,"tokens_remaining":0,"tokens_reset":"2026-01-01T00:01:00Z","retry_after_ms":60000}}
```

Example (bare):
```json
{"type":"rate_limit_event"}
```

**Untether handling**: Decoded by `StreamRateLimitMessage` schema, silently skipped in
`translate_claude_event` (no Untether events emitted).

## Message object (`message` field)

Fields:
- `id`, `type`, `role`
- `model` (optional)
- `content`: array of content blocks
- `usage` (assistant messages)

## Content block shapes (in `message.content[]`)

### Text
```json
{"type":"text","text":"Hello"}
```

### Tool use
```json
{"type":"tool_use","id":"toolu_1","name":"Bash","input":{"command":"ls -la"}}
```

### Tool result
String content:
```json
{"type":"tool_result","tool_use_id":"toolu_1","content":"ok"}
```

Array content (Task tool format):
```json
{"type":"tool_result","tool_use_id":"toolu_2","content":[{"type":"text","text":"Task done"}]}
```
