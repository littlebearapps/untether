# OpenCode to Untether Event Mapping

This document describes how OpenCode JSON events are translated to Untether's normalized event model.

> **Authoritative source:** The schema definitions are in `src/untether/schemas/opencode.py` and the translation logic is in `src/untether/runners/opencode.py`. When in doubt, refer to the code.

## Event Translation

### StartedEvent

Emitted on the first `step_start` event that contains a `sessionID`.

```
OpenCode: {"type":"step_start","sessionID":"ses_XXX",...}
Untether:   StartedEvent(engine="opencode", resume=ResumeToken(engine="opencode", value="ses_XXX"), meta={"model": "anthropic/claude-sonnet-5-5"})
```

Note: OpenCode JSONL does not include model info in its event stream. The runner populates `meta.model` from the `/config` model override, else `[opencode] model`, else the `"model"` in `~/.config/opencode/opencode.json` (the same value it passes as `--model`). This is used for the `🏷` footer line on final messages.

### ActionEvent

Tool usage is translated to action events, keyed by `part.callID` (falling back to `part.id`). `status` `"completed"` and `"error"` complete the action; any other status (pending/running, rarely emitted by the CLI JSON stream) starts it. When `part.state.title` is set it replaces the derived title.

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

`ok` is false only when `metadata.exit` is a non-zero int. `detail` carries `name`, `input`, `callID`, `exit_code` and `output_preview` (first 500 characters of `state.output`); file-change tools also get `changes=[{path, kind: "update"}]`.

**Error status** (`state.status == "error"`): completed with `ok=False`, `detail.error` and `message` set to `state.error`. The error text is kept as a fallback answer if the run then finishes with no text. A tool call that an OpenCode `ask` rule would have prompted for lands here too: `opencode run` auto-rejects `ask` permissions (it never auto-approves them), so the rejection shows as a failed tool. See [Permissions in `opencode run`](runner.md#permissions-in-opencode-run).

Lines that fail to decode: an unknown `type` becomes a warning action (`opencode emitted unsupported event: <type>`) and logs `opencode.event.unsupported`; a line with no readable `type` (e.g. malformed JSON) is only logged (`jsonl.msgspec.invalid`).

### CompletedEvent

Emitted on `step_finish` with `reason="stop"` or on `error` events.

**Success**:
```
OpenCode: {"type":"step_finish","part":{"reason":"stop","tokens":{...},"cost":0.001}}
Untether:   CompletedEvent(engine="opencode", ok=True, answer="<accumulated text>", usage={...})
```

The answer is all `text` parts joined with a blank line (`opencode run` emits each part once, complete; a repeated `part.id` replaces its earlier text), or the last tool error when there was no text.

If `step_finish` omits `reason`, Untether treats a clean process exit as successful completion and emits `CompletedEvent(ok=True)` with the accumulated usage. A clean exit with no `step_finish` at all completes with `ok=False` (`opencode finished without a result event`), as does one with no `sessionID`; a non-zero exit code emits a warning plus `CompletedEvent(ok=False)` with a stderr excerpt.

**Error**:
```
OpenCode: {"type":"error","error":{"name":"APIError","data":{"message":"API rate limit exceeded"}}}
Untether:   CompletedEvent(engine="opencode", ok=False, error="API rate limit exceeded")
```

## Tool Kind Mapping

| OpenCode Tool | Untether ActionKind |
|---------------|-------------------|
| `bash`, `shell`, `killshell` | `command` |
| `edit`, `write`, `multiedit`, `notebookedit` | `file_change` |
| `read`, `glob`, `grep`, `find`, `ls` | `tool` |
| `websearch`, `web_search` | `web_search` |
| `webfetch`, `web_fetch` | `web_search` |
| `todowrite`, `todoread` | `note` |
| `task`, `agent` | `tool` |
| (other) | `tool` |

Tool names match case-insensitively.

## Usage Accumulation

Every `step_finish` in the run adds its `part.cost` and `part.tokens` to a running sum (OpenCode reports per step, so a multi-step run has several). `CompletedEvent.usage` is then:

```json
{
  "total_cost_usd": 0.001,
  "usage": {
    "input_tokens": 22443,
    "output_tokens": 118,
    "reasoning_tokens": 0,
    "cache_read_tokens": 21415,
    "cache_write_tokens": 0
  }
}
```

`total_cost_usd` is omitted when the cost is 0, `reasoning_tokens` when it is 0, and the two cache fields when both are 0; `usage` is `None` when no step reported anything. Cache read/write tokens are **separate from** `input_tokens`, not a subset.

These figures are this run's only. The bridge keeps a per-session total in `session_costs.json` and adds it as `session_total_usage`; `/usage` in an OpenCode chat shows the chat's last OpenCode session total and last run ([#417](https://github.com/littlebearapps/untether/issues/417)). OpenCode has no subscription-quota data.
