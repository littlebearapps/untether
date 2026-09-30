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

Example (assistant frame stopped by Anthropic's safeguards, #814 — `message.id`,
`stop_reason` and `stop_details` are decoded since 0.35.5rc14):
```json
{"type":"assistant","session_id":"session_01","parent_tool_use_id":null,"message":{"id":"msg_5","type":"message","role":"assistant","model":"claude-opus-5-5","content":[],"stop_reason":"refusal","stop_details":{"type":"refusal","category":"cyber","explanation":"…"}}}
```
`stop_details.category` ∈ `cyber` | `bio` | `frontier_llm` | `reasoning_extraction` |
`general_harms` | `null`. The CLI forwards the API message verbatim; one API response can
span several frames, so Untether dedupes on `message.id`. A frame truncated by an interrupt
carries `aborted: true` and no `stop_reason`.

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

Turn-ending fields (decoded since 0.35.5rc14, all optional):
- `terminal_reason`: why the turn ended. `aborted_streaming` / `aborted_tools` mean it was
  interrupted; the subtype is then usually `success`, sometimes `error_during_execution`
  with an `[ede_diagnostic]` error, so classify a cancel on `terminal_reason`, never on
  `subtype` / `is_error` ([#806](https://github.com/littlebearapps/untether/issues/806);
  `CLAUDE_ABORTED_TERMINAL_REASONS` in `schemas/claude.py`).
- `origin`: what started the turn, e.g. `{"kind":"task-notification","producer":"session-task"}`
  for a turn the CLI started itself (a background-task finish or an `asyncRewake` hook, #812).
  Typed `Any`: readers check it is an object first.
- `stop_reason`: passed through, typed `Any`.

Optional fields (may appear in upstream Claude Code CLI output but are **not** captured
by Untether's `StreamResultMessage` schema):
- `error`: error description string
- `permission_denials`: array of `{tool_name, tool_use_id, tool_input}`
- `structured_output`: arbitrary structured output (captured by schema but unused)

### `rate_limit_event`

A **quota-status snapshot**, not a throttle notice ([#790](https://github.com/littlebearapps/untether/issues/790)).
The CLI emits one whenever an API response moves the rounded utilization or a reset
time of the account's subscription windows — so a healthy session sees a steady trickle
of `status: "allowed"` events. The event never terminates the session.

Real payload (captured on CLI 2.1.283, one-turn Haiku probe):
```json
{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1790578200,"rateLimitType":"five_hour","overageStatus":"rejected","overageDisabledReason":"out_of_credits","isUsingOverage":false,"unifiedWindows":{"five_hour":{"utilization":0.09,"resetsAt":1790578200},"seven_day":{"utilization":0.15,"resetsAt":1791036000}}},"uuid":"…","session_id":"…"}
```

`rate_limit_info` fields (upstream zod, CLI 2.1.283):

| Field | Type | Notes |
|---|---|---|
| `status` | `"allowed"` \| `"allowed_warning"` \| `"rejected"` | always present |
| `resetsAt` | int (epoch s) | reset time of the window named by `rateLimitType` |
| `rateLimitType` | `five_hour` \| `seven_day` \| `seven_day_opus` \| `seven_day_sonnet` \| `seven_day_overage_included` \| `overage` | optional |
| `utilization` | number (0–1) | optional; the warning window's utilization |
| `unifiedWindows` | `{five_hour?, seven_day?, seven_day_overage_included?}: {utilization, resetsAt}` | absent for API-key / Bedrock / Vertex sessions |
| `overageStatus` | same 3 values as `status` | paid extra-usage state |
| `overageResetsAt` | int | optional |
| `overageDisabledReason` | enum (13 values, e.g. `out_of_credits`, `org_level_disabled`) | optional |
| `isUsingOverage` | bool | `rejected` + `isUsingOverage` is **not** a throttle — extra usage covers it |
| `errorCode` | `"credits_required"` | optional |
| `limitScope`, `surpassedThreshold`, `overageInUse`, … | | ignored by Untether |

The enum lists are mirrored as `CLAUDE_RATE_LIMIT_STATUSES` / `CLAUDE_RATE_LIMIT_TYPES` /
`CLAUDE_OVERAGE_STATUSES` in `schemas/claude.py` and pinned by the zero-token drift test
`tests/test_claude_cli_schema_drift.py`, which reads them out of the installed CLI.

**Legacy shape.** Earlier docs described `requests_limit` / `requests_remaining` /
`requests_reset` / `tokens_limit` / `tokens_remaining` / `tokens_reset` / `retry_after_ms`.
No real CLI has been observed sending these; the schema keeps them optional so such an
emitter still gets a precise countdown ([#518](https://github.com/littlebearapps/untether/issues/518)).

**Untether handling** (`_translate_rate_limit_event` in `runners/claude.py`):

| Snapshot | Untether |
|---|---|
| `allowed` | Snapshot only — `unifiedWindows` stashed on `ClaudeStreamState.rate_limit_windows`, DEBUG `claude.rate_limit_snapshot`. No note, no latch, no `cumulative_s`. |
| `allowed_warning` | One `⚠️ 5h limit 85% used — resets 17:30 AEST` note per (window, reset) when utilization ≥ 0.7 (or absent) and extra usage isn't covering it. No latch. |
| `rejected`, not `isUsingOverage`, `resetsAt` in the future | Throttle: note `⏳ Rate limited until 17:30 AEST (~30 min)`, `rate_limit_wait_until` latched to `resetsAt` (clamped to 24 h), extension-only accounting so repeats don't double-count, and repeats update the same note. Beats the [#692](https://github.com/littlebearapps/untether/issues/692) result-text parse. |
| `rejected` without `resetsAt` | Legacy timing if present → `errorCode: credits_required` / overage → [#701](https://github.com/littlebearapps/untether/issues/701) remedy title (`⛔ Model limit reached — …`) → #692 harvested reset → #701 latch → `⏳ Rate limited — waiting to retry (~60s)`. |
| `rejected` with `isUsingOverage`, or `resetsAt` already past | Not a throttle (INFO log `retry_after_source=covered_by_overage` / `stale`). |
| unknown `status` | WARN `claude.rate_limit_event.unknown_status` once per value; no latch. |
| no `status`, legacy timing | #518 path: `⏳ Rate limited — retrying in Ns`. |
| truly bare (`{"type":"rate_limit_event"}`) | Nothing — INFO `retry_after_source=bare`. The [#657](https://github.com/littlebearapps/untether/issues/657) "bare = 60 s throttle" guess is retired: bare was an artefact of the old schema decoding every real snapshot to all-`None`. |

### `system` / `api_retry` (#792)

Emitted when an API request fails with a retryable error and the CLI will retry after a
delay — the wire twin of the REPL's retry banner (`SDKAPIRetryMessage`, CLI 2.1.283).
This, not `rate_limit_event`, is the real "backing off, retrying in N s" signal.

```json
{"type":"system","subtype":"api_retry","attempt":2,"max_retries":10,"retry_delay_ms":8000,"error_status":529,"error":"overloaded","uuid":"…","session_id":"…"}
```

| Field | Type | Notes |
|---|---|---|
| `attempt` / `max_retries` | int | retry counters |
| `retry_delay_ms` | int | back-off before the next attempt |
| `error_status` | int \| null | HTTP status; `null` for connection errors (timeouts) with no response |
| `error` | string | category: `rate_limit`, `overloaded`, `server_error`, `authentication_failed`, `billing_error`, `invalid_request`, `unknown`, … |
| `no_response` | `{waited_ms, retry_wait_ms}` (optional) | only when no response headers arrived within `CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS`; `max_retries` is then this cause's own cap (normally 1) |

A sibling `system/control_request_progress` with `status: "api_retry"` carries the same
counters for client-originated control requests (side questions only) — not handled.

**Untether handling** (`_translate_api_retry` in `runners/claude.py`): decoded into the
flat `StreamSystemMessage` (all fields optional, `error` typed `Any`). Renders one note per
retry sequence, updated in place as attempts climb — `🔁 API error 529 (overloaded) —
retrying in 8s (attempt 2/10)`, `🔁 API unreachable — retrying in 5s (attempt 1/10)`
(no status), or `🔁 No response from API after 45s — retrying in 2s (attempt 1/1)`
(`no_response`). Latches `ClaudeStreamState.api_retry_wait_until` = now + delay (+ the
retry's `retry_wait_ms` header window); the bridge's `awaiting_api_retry()` probe treats that
window as an expected wait (`reason=api_retry_waiting`), like a rate-limit window. Logs
`claude.api_retry` at INFO (WARN, and a `warning`-level note, on the final attempt). Retry
time accrues in `api_retry_total_s`, kept separate from rate-limit time.

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

### Background-task lifecycle (`system` subtypes, CLI ≥ 2.1.28x) — #776

Verified on 2.1.283 (see `docs/findings/2026-09-27-claude-live-session-probes.md`).

```json
{"type":"system","subtype":"background_tasks_changed","tasks":[{"task_id":"b1","task_type":"local_bash","description":"…"}]}
{"type":"system","subtype":"task_started","task_id":"b1","tool_use_id":"toolu_…","description":"…","is_backgrounded":true,"task_type":"local_bash"}
{"type":"system","subtype":"task_started","task_id":"a1","tool_use_id":"toolu_…","subagent_type":"general-purpose","is_backgrounded":true,"spawn_depth":1,"task_type":"local_agent","prompt":"…"}
{"type":"system","subtype":"task_started","task_id":"f1","owned_by_subagent":true,"is_backgrounded":false,"task_type":"local_bash"}
{"type":"system","subtype":"task_progress","task_id":"a1","usage":{"total_tokens":52470,"tool_uses":2,"duration_ms":4314},"last_tool_name":"Bash"}
{"type":"system","subtype":"task_updated","task_id":"b1","patch":{"status":"completed","end_time":1790500249636}}
{"type":"system","subtype":"task_notification","task_id":"b1","tool_use_id":"toolu_…","status":"completed","output_file":"…","summary":"…"}
```

- `background_tasks_changed` is a full snapshot of live background tasks (`[]` when none) and arrives just *before* `task_started` / `task_updated`.
- `owned_by_subagent: true` with `is_backgrounded: false` is a subagent's own foreground tool — not background work.
- `task_updated.patch.status`: `completed` | `killed`; `task_notification.status`: `completed` | `stopped`.
- `Monitor` is `task_type: local_bash`; each streamed line starts a new turn with **no** per-line task event; the stream end emits `task_updated` + `task_notification`.
- `ScheduleWakeup` / `RemoteTrigger` emit **no** task events.
- Resuming a finished background agent (e.g. `SendMessage` to it) **reuses its `task_id`**: a fresh snapshot lists it again and a new `task_started` follows. Untether revives the ended task instead of leaving it terminal ([#801](https://github.com/littlebearapps/untether/issues/801)).
- Closing stdin stops live background tasks (`killed` / `stopped`) and the CLI exits rc=0 a few seconds later.
- On `--resume` after such a stop, the CLI first emits `task_notification{status:"stopped", output_file:""}`, then `system/init`, then a **0-turn result**, then the real turn.

### `command_lifecycle` (#776)

One line per input command (user line or scheduled wake-up):

```json
{"type":"command_lifecycle","command_uuid":"<uuid>","state":"queued"}
{"type":"command_lifecycle","command_uuid":"<uuid>","state":"started"}
{"type":"command_lifecycle","command_uuid":"<uuid>","state":"completed"}
```

`command_uuid` echoes the `uuid` field of the stream-json `user` line that was written to stdin, so a turn can be attributed to the message that caused it. A ScheduleWakeup firing appears as `started` with a uuid Untether never wrote. `completed` for command N can arrive lazily (when command N+1 is queued).

### Multi-result streams (#776)

In control-channel mode the process does not exit after `result`: background-task completions, Monitor lines, ScheduleWakeup firings and user lines written while idle each produce another `system/init` → … → `result`. `total_cost_usd` is cumulative per session (including across `--resume`); `num_turns` is per result.

### `system` / `status` — permission-mode edges (#383)

Emitted on **every** change of the CLI's permission mode — an approved `ExitPlanMode` (mode becomes `prePlanMode ?? "default"`, so a session started in plan lands in `default`; emitted *before* the tool_result) and a successful `set_permission_mode`. A no-op change emits nothing. (The same subtype also carries `status:"compacting"` during compaction.)

```json
{"type":"system","subtype":"status","status":null,"permissionMode":"default","uuid":"…","session_id":"…"}
```

`system/init.permissionMode` reports the mode each turn starts in (live follow-up turns included on 2.1.285). Untether tracks the effective mode from both (`claude.permission_mode.changed`); neither produces an Untether event.

Untether's plan re-arm (host → CLI, handled inline by the CLI's stdin reader, not queued behind a turn) and its answers:

```json
{"type":"control_request","request_id":"ut_plan_rearm_<sid>_1","request":{"subtype":"set_permission_mode","mode":"plan"}}
{"type":"control_response","response":{"subtype":"success","request_id":"ut_plan_rearm_<sid>_1","response":{"mode":"plan"}}}
{"type":"control_response","response":{"subtype":"error","request_id":"…","error":"…","error_code":"invalid_mode"}}
```

A real change is followed by the `system/status` frame above; `plan` while already `plan` is acked with no status frame. Refusal codes (2.1.285): `invalid_mode`, `bypass_*` (target `bypassPermissions`) and `auto_mode_*` (target `auto`) — `plan` is never refused.

### Hook lifecycle (`system` subtypes, `--include-hook-events`) — #812

Emitted only when Untether passes `--include-hook-events` (control-channel mode, CLI lists the
flag, `[watchdog] hold_for_async_hooks` on). `SessionStart` / `Setup` hook frames are emitted
even without the flag. Source: `docs/findings/2026-09-29-claude-rc14-cli-surface.md` §A2/§A3.

```json
{"type":"system","subtype":"hook_started","hook_id":"<uuid>","hook_name":"Stop","hook_event":"Stop","uuid":"…","session_id":"…"}
{"type":"system","subtype":"hook_progress","hook_id":"<uuid>","hook_name":"Stop","hook_event":"Stop","stdout":"…","stderr":"…","output":"…","uuid":"…","session_id":"…"}
{"type":"system","subtype":"hook_response","hook_id":"<uuid>","hook_name":"Stop","hook_event":"Stop","outcome":"error","exit_code":2,"stdout":"","stderr":"<findings>","output":"…","uuid":"…","session_id":"…"}
```

- `hook_id` pairs `hook_started` with `hook_response`. `hook_name` can carry a matcher suffix
  (`SessionStart:startup`).
- `outcome`: `success` | `error` | `cancelled`; `exit_code` is omitted when undefined. Exit 2
  (`outcome: "error"`) is a blocking error, which for an `asyncRewake` hook is the wake signal.
- **No field marks a hook as async.** A background hook shows up only as a `hook_started` whose
  `hook_response` arrives after the turn's `result`.
- A plain `async` hook's `hook_response` is **withheld while the session is idle** and
  delivered at the next turn or at stdin close, even though its process exited long ago
  (probed on 2.1.285). `asyncRewake` and synchronous hooks report as soon as they exit.
- An `asyncRewake` hook that exits 2 while idle starts a new turn with **no**
  `command_lifecycle` frames: `hook_response{exit_code:2}` → `system/init` →
  `system/informational` ("Original prompt: `<task-notification>` … Stop hook feedback") → …
  → `result` with `origin: {"kind":"task-notification"}`.
- After stdin closes the CLI kills plain `async` hooks (`outcome: "cancelled"`), waits up to
  30 s for pending `asyncRewake` hooks, and **drops** any rewake they produce.

**Untether handling**: decoded into the flat `StreamSystemMessage` (`hook_id`, `hook_name`,
`hook_event`, `outcome`, `exit_code`, all typed `Any`; `stdout` / `stderr` / `output` are
deliberately not declared, so hook output is never held in memory). No Untether events; the
frames feed the live-session hook hold (see the [runner spec](runner.md), "Async hooks") and
don't overwrite `JsonlStreamState.last_event_type`.

### Safeguard stops and model fallback (#814)

```json
{"type":"system","subtype":"informational","content":"Opus 5.5's safeguards stopped the response above · continuing once with that noted","level":"notice","uuid":"…","session_id":"…"}
{"type":"system","subtype":"model_refusal_fallback","trigger":"refusal","direction":"retry","scope":"session","original_model":"claude-opus-5-5","fallback_model":"claude-opus-4-8","api_refusal_category":"cyber","api_refusal_explanation":"…","content":"…","session_id":"…","uuid":"…"}
{"type":"system","subtype":"model_refusal_no_fallback","original_model":"claude-opus-5-5","api_refusal_category":"bio","api_refusal_explanation":"…","content":"…","session_id":"…","uuid":"…"}
{"type":"system","subtype":"model_fallback","trigger":"overloaded","original_model":"claude-opus-5-5","fallback_model":"claude-sonnet-5-5","content":"…","session_id":"…","uuid":"…"}
```

- `informational` (`SDKInformationalMessage`): `content`, `level` (`info` | `notice` |
  `suggestion` | `warning`), optional `tool_use_id` / `prevent_continuation`. It is a general
  banner (hook blocks, notices, …); the safeguard notice is one of these at level `notice`.
  Its position relative to the turn's `result` is unverified, so Untether parses it on either
  side (and between turns in a live session).
- `model_refusal_fallback` / `model_refusal_no_fallback` / `model_fallback` are undocumented
  in the SDK reference; shapes come from the CLI binary (2.1.284/2.1.285). `scope: "local"` on
  a refusal fallback means a subagent or side question fell back and the session model is
  unchanged. Other optional keys seen: `request_id`, `refused_user_message_uuid`,
  `saw_cyber_refusal`, `retracted_message_uuids`.
- The drift test (`tests/test_claude_cli_schema_drift.py`) probes the installed binary for
  these subtypes and the safeguard notice text at zero token cost.

**Untether handling**: all fields decode into `StreamSystemMessage` typed `Any` (about 40
system subtypes share the struct, so a type clash must never drop the line). Dispatch is
`_SYSTEM_SUBTYPE_HANDLERS` in `runners/claude.py`; the mapping is in
[untether-events.md](untether-events.md) §4.1 and the runner spec's "Safeguard stops".
Source: `docs/findings/2026-09-29-claude-rc14-cli-surface.md` §B.

### Context usage and compaction (#819)

The context-window numbers ride on frames already listed above (CLI 2.1.285; zero-token
probes in `tests/test_claude_cli_schema_drift.py`, research in
`docs/findings/2026-09-30-claude-sdk-control-permissions-context.md` Q4/Q5):

```json
{"type":"assistant","message":{"model":"claude-haiku-4-5","usage":{"input_tokens":1234,"cache_creation_input_tokens":100,"cache_read_input_tokens":185000,"output_tokens":5},"content":[…]},"parent_tool_use_id":null,"session_id":"…"}
{"type":"result","subtype":"success",…,"modelUsage":{"claude-haiku-4-5":{"inputTokens":…,"contextWindow":200000,"maxOutputTokens":64000,…}}}
{"type":"system","subtype":"compact_boundary","compact_metadata":{"trigger":"manual","pre_tokens":6336,"post_tokens":277,"cumulative_dropped_tokens":6059,"duration_ms":47},"logical_parent_uuid":"…","session_id":"…"}
```

- `modelUsage` is keyed by the model id the CLI used; `contextWindow` is the only place the
  window appears. Decoded as `StreamResultMessage.modelUsage` (`Any`).
- Assistant `message.usage` decodes as `StreamAssistantMessageBody.usage` (`Any`).
- Compaction also emits `system/status {"status":"compacting"}` (re-sent every 30 s),
  then `status:null` with `compact_result` (`success` / `failed`) and optional
  `compact_error`, a fresh mid-command `system/init`, the boundary above and a summary
  `user` frame (`isCompactSummary`). All these keys decode as `Any` on
  `StreamSystemMessage` / `StreamUserMessage`. rc15 (C2) uses only the boundary: it clears
  `% ctx` until the next main-thread response. Compaction rows are the next #819 step.

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
