# Antigravity CLI → Untether events

How `AntigravityRunner.translate()` (`src/untether/runners/antigravity.py`) turns agy's stream into Untether's
three-event contract: one `StartedEvent`, any number of `ActionEvent`s, exactly one `CompletedEvent`. Line shapes
are in the [stream-json cheatsheet](stream-json-cheatsheet.md). Checked against agy 1.3.2.

## Mapping

| agy line | Untether event |
|---|---|
| `init` with a `conversation_id` | `StartedEvent` (once), resume token `agy --conversation <id>`, meta `model` · `effort` · `permissionMode`. Followed by any ⚠️ rows learnt before the run started |
| `init` without an id | Nothing. The id is taken from the first `step_update` or the `result` instead; an empty id never becomes a session |
| `init` reporting a permission policy other than `request-review` / `strict` when Untether didn't pass the bypass flag | agy is stopped; `CompletedEvent(ok=False)` with the "agy's own settings change its permission checks" text |
| `init` (or first id) that differs from the `--conversation` id asked for | agy is stopped; `CompletedEvent(ok=False)` "conversation no longer exists"; every later line is ignored |
| `step_update` `tool`, `ACTIVE` | `ActionEvent` started. Kind and title come from the shared tool vocabulary (see below) |
| `step_update` `tool`, `DONE`, with output | `ActionEvent` completed, `ok=True`, with a 500-character output preview |
| `step_update` `tool`, `DONE`, no output, for a tool agy can block | Held back. Closed `ok=True` when a later step arrives; turned into a ⚠️ Blocked row if `result.denied_actions` names its family |
| `step_update` `tool`, `ERROR` | `ActionEvent` completed, `ok=False`, with `tool_info.error.message` (a hook refusal lands here) |
| `step_update` `subagent`, `ACTIVE` / `DONE` / `ERROR` | `ActionEvent` of kind `subagent`, started / completed |
| `step_update` `agent_response` | No event. `text_delta` is collected per step as the fallback answer |
| `step_update` `finish` (or any type) closing a step that opened as a tool | `ActionEvent` completed |
| `step_update` `user_input`, `system_message`, `checkpoint`, `unknown`, anything newer | Ignored. A `user_input` injected mid-run is not a new run |
| a background-capable tool `ACTIVE` for more than 5 s | `ActionEvent` updated, title `⏳ background: <command>` (once, on the next line agy prints) |
| `result` `SUCCESS` | Open rows are closed, then `CompletedEvent(ok=True)`. Answer is `result.response`, or the collected text when that is empty |
| `result` with `denied_actions` | A `⚠️ Blocked: <what> (<agy name>) — <title>` row per denial, and a closing paragraph on the answer |
| `result` `ERROR` + `interrupted` | `CompletedEvent(ok=False)`, resumable, "Antigravity was interrupted …" |
| `result` `ERROR` + other text | `CompletedEvent(ok=False)` with agy's text, redacted and trimmed to three lines |
| `result` with any other status | `CompletedEvent(ok=False)` `antigravity ended with status <status>[: <error>]` |
| `command_result` | Ignored during a run (used only by the `/usage`, `/effort` and `/config` probes) |
| a line that isn't valid JSON | A note row (`invalid JSON from antigravity; ignoring line`); the run continues. Leading non-JSON text before a `{` is skipped first |
| a JSON line with an event name Untether doesn't know | Dropped and logged (`jsonl.msgspec.invalid`), not shown |

## Ends without a `result`

| What happened | Untether event |
|---|---|
| A stderr line of a stop class (signed out, account block, quota, credits, unknown conversation) | agy is stopped; `CompletedEvent(ok=False)` with that class's text. It also wins over whatever `result` agy then sends |
| Exit code 3, or an `AGY_ERROR:` line | `CompletedEvent(ok=False)` `agy reported a model/agent error (<status>, retryable=…; …)` |
| Any other non-zero exit | `CompletedEvent(ok=False)` `antigravity failed (<exit>)` plus the session and a redacted stderr excerpt (300 characters) |
| Stream ended, exit 0, no result | `CompletedEvent(ok=False)` `antigravity finished without a result event` (or `… but no session_id was captured`) |

In every case rows still open are closed first, so nothing is left "running". A `result` that arrived before a
late stderr error is kept.

## Refused before agy starts

These yield a single `CompletedEvent(ok=False)` and keep the chat's saved session:

| Reason (`prespawn_blocked`) | When |
|---|---|
| RAM / concurrency guard | Same pre-spawn guard as every engine |
| `no_project` | No project directory, or it is `/`, the home directory or the bot's own directory |
| `gate_missing` | The mode is Ask me or Plan first |
| `unsupported_version` | agy is older than 1.3.1 |
| `config_changed` | Unattended run, and the project's agy config differs from what an attended run last showed |
| `config_unchecked` | Unattended run, and that config couldn't be checked |
| `agy_always_proceed` | Workspace run, and agy's own settings report a `toolPermission` other than `request-review` / `strict` |

## Tool vocabulary

| agy tool | Shown as |
|---|---|
| `run_command` | `bash` (the command line is the title) |
| `view_file` | `read` |
| `write_to_file` | `write` |
| `replace_file_content`, `multi_replace_file_content`, `sed_file` | `edit` |
| `list_dir` | `ls` |
| `find_by_name` | `glob` |
| `grep_search` | `grep` |
| `search_web` | `websearch` |
| `read_url_content` | `webfetch` |
| `invoke_subagent` | `agent` (a subagent row) |
| `ask_question` | `askuserquestion` |
| `call_mcp_tool` | `mcp: <server>/<tool>` |
| `browser_*` | `browser: <action>` |
| anything else | its own name, lower-cased |

File paths are read from `TargetFile`, `AbsolutePath`, `DirectoryPath`, `SearchPath`, `SearchDirectory`,
`file_path`, `path` or `filePath`.

## Usage

`CompletedEvent.usage` carries `input_tokens`, `output_tokens`, `cache_read_tokens`, `reasoning_tokens` (agy's
`thinking_tokens`) and `duration_ms` (measured by Untether). agy's totals are per conversation, so the bridge's
token ledger turns them into a per-run figure. `num_turns` and agy's `duration_seconds` are conversation totals
too and are not passed on.

## Session handling

- Resume line: `` `agy --conversation <id>` ``; ids shorter than 8 characters are never parsed as one.
- A failed resume clears the chat's saved session only for the "conversation no longer exists" reply. Sign-in,
  quota and other errors keep it.
- Antigravity is not a turn-counting engine, and its child processes don't earn the long stall threshold except
  through the background wait described in the [runner reference](runner.md#background-commands).
