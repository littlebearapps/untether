# OpenCode to Untether Event Mapping

This document describes how OpenCode JSON events are translated to Untether's normalized event model.

> **Authoritative source:** The schema definitions are in `src/untether/schemas/opencode.py` and the translation logic is in `src/untether/runners/opencode.py`. When in doubt, refer to the code.

## Event Translation

### StartedEvent

Emitted on the first `step_start` event that contains a `sessionID`.

```
OpenCode: {"type":"step_start","sessionID":"ses_XXX",...}
Untether:   StartedEvent(engine="opencode", resume=ResumeToken(engine="opencode", value="ses_XXX"), meta={"model": "claude-sonnet"})
```

Note: OpenCode JSONL does not include model info in its event stream. The runner populates `meta.model` from the runner config or run options (`--model` flag) when available. This is used for the `🏷` footer line on final messages.

### ActionEvent

Tool usage is translated to action events. The code handles `status` values of `"completed"` and `"error"`. Pending/running tool states exist in the schema but are not commonly emitted by the CLI JSON stream.

**Started phase** (when tool is pending/running, if emitted by the JSON stream):
```
OpenCode: {"type":"tool_use","part":{"tool":"bash","state":{"status":"pending",...}}}
Untether:   ActionEvent(engine="opencode", action=Action(kind="command"), phase="started")
```

**Completed phase** (when tool finishes):
```
OpenCode: {"type":"tool_use","part":{"tool":"bash","state":{"status":"completed","metadata":{"exit":0}}}}
Untether:   ActionEvent(engine="opencode", action=Action(kind="command"), phase="completed", ok=True)
```

### CompletedEvent

Emitted on `step_finish` with `reason="stop"` or on `error` events.

**Success**:
```
OpenCode: {"type":"step_finish","part":{"reason":"stop","tokens":{...},"cost":0.001}}
Untether:   CompletedEvent(engine="opencode", ok=True, answer="<accumulated text>", usage={...})
```

If `step_finish` omits `reason`, Untether treats a clean process exit as successful completion and emits `CompletedEvent(ok=True)` with the accumulated usage.

**Error**:
```
OpenCode: {"type":"error","error":{"name":"APIError","data":{"message":"API rate limit exceeded"}}}
Untether:   CompletedEvent(engine="opencode", ok=False, error="API rate limit exceeded")
```

## Tool Kind Mapping

| OpenCode Tool | Untether ActionKind |
|---------------|-------------------|
| `bash`, `shell` | `command` |
| `edit`, `write`, `multiedit` | `file_change` |
| `read` | `tool` |
| `glob` | `tool` |
| `grep` | `tool` |
| `websearch`, `web_search` | `web_search` |
| `webfetch`, `web_fetch` | `web_search` |
| `todowrite`, `todoread` | `note` |
| `task` | `tool` |
| (other) | `tool` |

## Usage Accumulation

> **Not yet implemented.** OpenCode's `step_finish` events may include token
> usage and cost data, but the Untether runner does not currently extract or
> accumulate these fields. `CompletedEvent.usage` is not populated for
> OpenCode runs. This is a candidate for future work.
>
> Expected upstream shape (when available):
>
> ```json
> {
>   "total_cost_usd": 0.001,
>   "tokens": {
>     "input": 22443,
>     "output": 118,
>     "reasoning": 0,
>     "cache_read": 21415,
>     "cache_write": 0
>   }
> }
> ```
