# Antigravity `--output-format stream-json` event cheatsheet

`agy --output-format stream-json` writes **one JSON object per line** (JSONL / NDJSON) with a
required `event` field.

## Event types

### `init`

Session initialisation with conversation ID, working directory, model, and active tools.

```json
{"event":"init","conversation_id":"fe2e5cfc-e5e4-4b67-8575-61ca39ba3ecb","init":{"cwd":"/home/user/project","model":"gemini-3.8-flash-high","tools":["run_command","write_to_file"]}}
```

### `step_update`

Tool execution updates or streamed model response deltas.

Tool started:
```json
{"event":"step_update","step_update":{"step_index":1,"state":"ACTIVE","step_type":"tool","tool_name":"run_command","tool_info":{"CommandLine":"pytest"}}}
```

Tool completed:
```json
{"event":"step_update","step_update":{"step_index":1,"state":"DONE","step_type":"tool","tool_name":"run_command","tool_info":{"CommandLine":"pytest"}}}
```

Agent response delta:
```json
{"event":"step_update","step_update":{"step_index":2,"state":"ACTIVE","step_type":"agent_response","text_delta":"Tests all passed."}}
```

### `result`

Final result with conversation ID, status, final response, and usage metadata.

```json
{"event":"result","result":{"conversation_id":"fe2e5cfc-e5e4-4b67-8575-61ca39ba3ecb","status":"SUCCESS","response":"Done! All tests pass.","usage":{"input_tokens":1200,"output_tokens":350},"duration_seconds":12.5}}
```

### `error`

Error event terminating the run.

```json
{"event":"error","message":"Command execution timed out"}
```

## Notes

* `init` is always the first event and contains `conversation_id` for resume.
* `step_update` events with `step_type = "agent_response"` accumulate `text_delta` to build assistant responses.
* `result` is the terminal event for successful runs; `error` for fatal failures.
