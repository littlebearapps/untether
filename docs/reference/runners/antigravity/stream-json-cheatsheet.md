# Antigravity CLI stream-json cheatsheet

`agy --output-format stream-json` writes one JSON object per line to stdout. Every line has an `event` field:
`init`, `step_update`, `result` or `command_result`. The examples below are real lines from agy 1.3.1 and 1.3.2,
with paths shortened and the tool list cut.

!!! note "Things that differ from what you might expect"
    - There is **no `error` event**. Failures arrive as a `result` with `status: "ERROR"`, or as a non-zero exit
      with text on stderr.
    - **`init` is not always first, or present at all.** An early failure (signed out, bad model) sends only a
      `result` with an empty `conversation_id`.
    - `result.usage`, `num_turns` and `duration_seconds` are **totals for the whole conversation**, including
      earlier resumed runs.

## Input

Untether starts agy with `--input-format stream-json` and writes one line to stdin, then closes it:

```json
{"event":"user","message":{"content":"your prompt"}}
```

## `init`

```json
{"event":"init","conversation_id":"90ca6744-e8d4-46d4-b5fc-64a30ae2fa6d","init":{"cwd":"/work/proj","tools":["ask_question","run_command","view_file","…"],"permission_mode":"request-review"}}
```

- `conversation_id` is the resume id (a UUID).
- `init.permission_mode` is the policy agy is running under: `request-review` (default), `strict`,
  `proceed-in-sandbox` or `always-proceed` (what `--dangerously-skip-permissions` reports).
- `init.model` is present from agy 1.3.2.
- `init.tools` is a long list of tool names; don't rely on its contents.
- `init.json_schema` echoes a `--json-schema` argument (Untether doesn't pass one).

## `step_update`

One step is a numbered unit of work. `state` is `ACTIVE`, `DONE` or `ERROR`. `step_type` is `user_input`,
`agent_response`, `tool`, `subagent`, `system_message`, `checkpoint`, `finish` or `unknown`.

Your message:

```json
{"event":"step_update","step_update":{"conversation_id":"90ca…","step_index":0,"state":"DONE","step_type":"user_input"}}
```

Answer text, streamed as deltas and then closed with per-step usage:

```json
{"event":"step_update","step_update":{"conversation_id":"90ca…","step_index":1,"state":"ACTIVE","step_type":"agent_response","text_delta":"OK"}}
{"event":"step_update","step_update":{"conversation_id":"90ca…","step_index":1,"state":"DONE","step_type":"agent_response","text_delta":"\n","duration_seconds":2.23,"usage":{"input_tokens":13051,"output_tokens":1,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":13052}}}
```

A tool call, started and finished:

```json
{"event":"step_update","step_update":{"conversation_id":"a46e…","step_index":2,"state":"ACTIVE","step_type":"tool","tool_name":"view_file","tool_info":{"name":"view_file","parameters":{"AbsolutePath":"/work/proj/notes.txt"}}}}
{"event":"step_update","step_update":{"conversation_id":"a46e…","step_index":2,"state":"DONE","step_type":"tool","tool_name":"view_file","duration_seconds":0.97,"tool_info":{"name":"view_file","parameters":{"AbsolutePath":"/work/proj/notes.txt"},"output":"2 lines, 20 bytes"}}}
```

`tool_info` carries `name`, `parameters`, and on completion `output` or `error` (`{"type": …, "message": …}`).

Tool names seen: `run_command`, `view_file`, `write_to_file`, `replace_file_content`,
`multi_replace_file_content`, `sed_file`, `list_dir`, `find_by_name`, `grep_search`, `search_web`,
`read_url_content`, `call_mcp_tool`, `browser_*`, `invoke_subagent`, `ask_question`, `schedule`, `manage_task`,
`finish`.

### A blocked tool looks like a finished one

When agy's permission policy blocks a tool (the headless default blocks shell, web and MCP calls), the step still
ends `DONE`, with no `output` and no `error`:

```json
{"event":"step_update","step_update":{"conversation_id":"a46e…","step_index":4,"state":"ACTIVE","step_type":"tool","tool_name":"run_command","tool_info":{"name":"run_command","parameters":{"CommandLine":"echo probe-shell"}}}}
{"event":"step_update","step_update":{"conversation_id":"a46e…","step_index":4,"state":"DONE","step_type":"tool","tool_name":"run_command","duration_seconds":0.05,"tool_info":{"name":"run_command","parameters":{"CommandLine":"echo probe-shell"}}}}
```

Only the `result` says it was blocked, in `denied_actions`. agy 1.3.x ends the turn at the first blocked tool, and
writes a `jetski: no output produced — a tool required the "command" permission …` line to stderr.

A tool refused by a **hook** is different: its step ends `ERROR` with `tool_info.error`:

```json
{"event":"step_update","step_update":{"conversation_id":"cb98…","step_index":11,"state":"ERROR","step_type":"tool","tool_name":"run_command","duration_seconds":0.38,"tool_info":{"name":"run_command","parameters":{"CommandLine":"echo denyme"},"error":{"type":"TOOL_ERROR","message":"tool call denied by pre-tool hook: …"}}}}
```

### Subagents

```json
{"event":"step_update","step_update":{"conversation_id":"4054…","step_index":2,"state":"ACTIVE","step_type":"subagent","tool_name":"invoke_subagent","subagent_info":{"subagents":[{"type_name":"research","role":"Secret Reader","initial_prompt":"Read notes.txt and reply with the secret word only."}]}}}
```

The `DONE` line adds each subagent's own `conversation_id` and a `log_uri`. The subagent's steps are not in the
parent stream.

### `finish`

With a JSON schema, the `finish` tool goes `ACTIVE` as `step_type: "tool"` and `DONE` as `step_type: "finish"`.

## `result`

Success:

```json
{"event":"result","result":{"conversation_id":"90ca…","status":"SUCCESS","response":"OK\n","duration_seconds":2.59,"num_turns":1,"usage":{"input_tokens":13051,"output_tokens":1,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":13052}}}
```

A turn that stopped at a blocked tool is still `SUCCESS`, with an empty `response`:

```json
{"event":"result","result":{"conversation_id":"a46e…","status":"SUCCESS","response":"","duration_seconds":6.9,"num_turns":1,"usage":{"input_tokens":26430,"output_tokens":143,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":26573},"denied_actions":[{"action":"command","display_name":"RunCommand"}]}}
```

`denied_actions[].action` values seen or documented: `command`, `unsandboxed`, `mcp`, `read_url`, `execute_url`,
`read_file`, `write_file`.

Cancelled (SIGINT or SIGTERM): the open step never closes, and the result is

```json
{"event":"result","result":{"conversation_id":"f40d…","status":"ERROR","response":"","error":"interrupted","duration_seconds":11.9,"num_turns":1,"usage":{"input_tokens":13082,"output_tokens":273,"thinking_tokens":179,"cache_read_tokens":0,"total_tokens":13355}}}
```

Signed out (no `init`, exit 1):

```json
{"event":"result","result":{"conversation_id":"","status":"ERROR","response":"","error":"authentication failed or timed out","duration_seconds":0,"num_turns":0,"usage":{"input_tokens":0,"output_tokens":0,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":0}}}
```

Other fields: `structured_output` and `json_schema` (with `--json-schema`). `error` is usually a string, but
Untether accepts any JSON value there so a structured error never loses the result. Statuses other than `SUCCESS`
and `ERROR` (`CANCELED`, `WAITING`, …) can appear.

## Unknown conversation id

`agy --conversation <unknown id>` prints a warning on stderr, then carries on in a **new** conversation:

```text
warning: conversation "00000000-1111-2222-3333-444444444444" not found
```

```json
{"event":"init","conversation_id":"65f94e86-51cc-46d0-8aee-97c679364d8c","init":{"cwd":"/work/proj","tools":["…"],"permission_mode":"request-review"}}
```

The only signals are the stderr line and an `init` id that differs from the one asked for.

## `command_result`

Slash commands run with `agy -p /<command>` answer with a `command_result` line and a zero-token `result`. Untether
uses three of them, none of which calls a model:

```json
{"event":"command_result","command":{"name":"effort","data":{"adjustable":true,"current":"high","available":["low","medium","high"]}}}
```

- `/effort [--model=<model>]` (Untether always joins the value to the flag) — the effort levels a model accepts (`{"adjustable": false}` for a fixed-effort model)
- `/usage` — quota `groups`, each with `buckets` (`window` such as `5h` or `weekly`, `remaining_fraction`,
  `reset_time`)
- `/config` — agy's effective settings (`toolPermission`, `permissions.allow`, `allowNonWorkspaceAccess`,
  `modelProvider`)

## stderr

stderr is not JSON. The lines Untether reacts to are listed in the [runner reference](runner.md#errors). agy's
changelog also describes `AGY_ERROR: {json}` with exit 3 for a turn-level failure; its keys aren't documented and
it hasn't been captured live.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Finished, including a turn that stopped at a blocked tool |
| 1 | Error result (signed out, invalid model or effort, interrupted) |
| 2 | Bad command line (`flags provided but not defined: …`) |
| 3 | Turn-level model or agent failure (`AGY_ERROR`; from agy's changelog, not captured live) |
