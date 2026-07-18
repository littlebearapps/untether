# Claude Code `stream-json` event cheatsheet

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

Optional fields (may appear in upstream Claude Code CLI output but are **not** captured
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

### `tool_progress`

Heartbeat emitted periodically while a long-running tool is in flight (verified on CLI
2.1.214 by running a Bash command longer than ~30 s). Purely informational — it carries no
result data and the tool's real `tool_result` still arrives separately.

Fields (all optional in Untether's schema):
- `type`: `"tool_progress"`
- `tool_use_id`: the heartbeat's own id, of the form `<parent_tool_use_id>-heartbeat-<n>`
- `tool_name`: the in-flight tool, e.g. `"Bash"`
- `parent_tool_use_id`: the `tool_use` this heartbeat belongs to
- `elapsed_time_seconds`: seconds since the tool started
- `heartbeat`: `true`
- `session_id`, `uuid`

Example:
```json
{"type":"tool_progress","tool_use_id":"toolu_011cbTyUrSBE4D28tMCVqRSt-heartbeat-0","tool_name":"Bash","parent_tool_use_id":"toolu_011cbTyUrSBE4D28tMCVqRSt","elapsed_time_seconds":30,"heartbeat":true,"session_id":"8e8245e8-952c-4b70-9c6f-4c1cb4d4a687","uuid":"a9786562-4e78-418e-b48a-b14e57a1076d"}
```

**Untether handling**: Decoded by `StreamToolProgressMessage` schema, silently skipped in
`translate_claude_event` (no Untether events emitted). Untether renders its own elapsed-time
tail on long-running actions from its own clock (#481), so the upstream heartbeat is
redundant for progress rendering — the schema entry exists so the line decodes instead of
being dropped with a `jsonl.msgspec.invalid` warning ([#637](https://github.com/littlebearapps/untether/issues/637)).

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

#### `ScheduleWakeup` (session-scoped scheduling)

Claude Code 2.1.x emits `ScheduleWakeup` `tool_use` blocks when the model parks itself for a delay (`/loop`'s no-interval dynamic mode and similar). Untether observes these for [Loop mode](../../../how-to/inline-settings.md#loop-mode) and the long-running tool tail.

```json
{"type":"tool_use","id":"toolu_3","name":"ScheduleWakeup","input":{"delaySeconds":300,"reason":"build check","prompt":"check if the build finished"}}
```

* `delaySeconds` (int, 60–3600) — wall-clock delay before wakeup. Untether reads this field for countdown rendering ([#481](https://github.com/littlebearapps/untether/issues/481) fixed; pre-rc7 reads `delay_ms`/`timeout_ms` were always 0.0 in production).
* `reason` (string) — short human label rendered in verbose mode (`→ fires in 4m 12s · "build check"`).
* `prompt` (string) — the prompt the model wants to fire on wakeup (loop iteration body).

`CronCreate` follows the same shape with a `cron` field (5-field expression) instead of `delaySeconds`. The Loop observer parses `cron` (not `cron_expression`) and `id` (not `taskId`/`cronId`); upstream 8-character cron IDs bind via `\bjob ([0-9a-f]{8})\b` from the assistant text.

### Tool result
String content:
```json
{"type":"tool_result","tool_use_id":"toolu_1","content":"ok"}
```

Array content (Task tool format):
```json
{"type":"tool_result","tool_use_id":"toolu_2","content":[{"type":"text","text":"Task done"}]}
```

### Image / document blocks (binary media echoes)

`Read` on an image or PDF echoes the content back inside the user-role
message as `image` / `document` blocks alongside the `tool_result`
([#597](https://github.com/littlebearapps/untether/issues/597) — the schema
must accept them or msgspec drops the whole line):

```json
{"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":"…"}}
```

```json
{"type":"document","source":{"type":"base64","media_type":"application/pdf","data":"…"},"title":"report.pdf"}
```

* `source` — `{"type":"base64"|"url", "media_type": …, "data"|"url": …}`.
  Untether never renders the payload; the blocks exist so the surrounding
  message parses. Translation treats them as non-`tool_result` content.
