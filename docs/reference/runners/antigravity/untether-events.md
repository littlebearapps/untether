# Antigravity CLI → Untether events

How `AntigravityRunner.translate()` (`src/untether/runners/antigravity.py`) turns agy's stream into Untether's
three-event contract: one `StartedEvent`, any number of `ActionEvent`s, exactly one `CompletedEvent`. Line shapes
are in the [stream-json cheatsheet](stream-json-cheatsheet.md). Checked against agy 1.3.2;
the subagent, timer and background rows against 1.3.3.

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
| `step_update` `tool`, `DONE` with `tool_info.error` | The same as `ERROR` (a step agy stopped itself ends this way: "context canceled", [#1062](https://github.com/littlebearapps/untether/issues/1062)) |
| `step_update` `tool` `invoke_subagent`, `ACTIVE` (agy 1.3.3) | `ActionEvent` of kind `subagent` started, titled `<type>: <role>` (`+N` for more) from `parameters.Subagents[]` |
| `step_update` `subagent`, `ACTIVE` / `DONE` / `ERROR` | `ActionEvent` of kind `subagent`, started (1.3.1 sends this type at `ACTIVE` too) / completed |
| `step_update` `agent_response` | No event. `text_delta` is collected per step as the fallback answer |
| `step_update` `agent_response`, `DONE`, with `usage`, when `init.model` named a model with a known window | `ActionEvent` updated, kind `telemetry`, id `antigravity.context`, only when the percentage changed. `detail`: `context_pct`, `context_used`, `context_window`, `model`, `context_estimated: true`. It feeds the header's `~N% ctx` and is never a step ([#979](https://github.com/littlebearapps/untether/issues/979)) |
| `step_update` `finish` (or any type) closing a step that opened as a tool | `ActionEvent` completed |
| `step_update` `user_input`, `system_message`, `checkpoint`, `unknown`, anything newer | No event. A `user_input` injected mid-run is not a new run; with the gate, one after a steer injection adds a completed `note` action `↪️ steer received: …` (`detail.absorbed_command_uuid`, [#981](https://github.com/littlebearapps/untether/issues/981)). A `system_message` updates the background task map (below). An `ask_question` arrives as `unknown`; its card comes from the gate, not the stream (below) |
| Gate: `PreToolUse` for `ask_question` (socket, not stdout) | `ActionEvent(started, kind="warning")` titled `❓ <question>` with `detail.request_id`, `ask_flow`, `inline_keyboard` (option buttons + Other); completed as `❓ answered: …`, `⏱️ No answer — agy carried on` or `❓ question withdrawn …` ([#980](https://github.com/littlebearapps/untether/issues/980), [runner reference](runner.md#untethers-agy-gate)) |
| a `run_command` that counts as background, or a `schedule` / `manage_task` step `ACTIVE` for more than 5 s | `ActionEvent` updated, title `⏳ background: <command>` (once, on the next line agy prints) |
| `result` `SUCCESS` | Open rows are closed, then `CompletedEvent(ok=True)`. Answer is `result.response`, or the collected text when that is empty |
| `result` with `denied_actions` | A `⚠️ Blocked: <what> (<agy name>) — <title>` row per denial, and a closing paragraph on the answer |
| `result` `SUCCESS` of a Plan first run, after a `write_to_file` `DONE` into `brain/<conversation id>/` ([#981](https://github.com/littlebearapps/untether/issues/981)) | `CompletedEvent(ok=True)`: the answer is `📋 Plan — <file>`, the plan read from that file, then agy's reply. `usage["review_card"]` = `{"text", "buttons"}` (Approve Plan / Deny / Let's discuss, callback `agy_plan:<action>:<id>`), sent by the bridge as the final's last message. A cron or webhook run gets `usage["unattended"]` (`denied: {"plan approval": 1}`, `hint`) instead of the card. No plan file: the ordinary answer plus a one-line note |
| `result` `SUCCESS` of the run an Approve started | The answer starts `📋 Plan (approved) — <file>`; the footer label is `workspace · plan approved` |
| Any `CompletedEvent` after ⚠️ rows | The answer gets those rows appended after it, once each, one per line (the post-run config re-check's rows included), because the final replaces the progress message that showed them ([#1045](https://github.com/littlebearapps/untether/issues/1045)). `⚠️ Blocked` rows are left out: the closing paragraph already explains them |
| `result` `ERROR` + `interrupted` | `CompletedEvent(ok=False)`, resumable, "Antigravity was interrupted …" |
| `result` `ERROR` + other text | `CompletedEvent(ok=False)` with agy's text, redacted and trimmed to three lines |
| `result` with any other status | `CompletedEvent(ok=False)` `antigravity ended with status <status>[: <error>]` |
| `command_result` | Ignored during a run (used only by the `/usage`, `/credits`, `/effort` and `/config` probes) |
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
| `no_project` | No project directory, or it is `/`, the home directory or the bot's own directory, or its path can't be resolved |
| `invalid_argument` | The model id or the conversation id being resumed isn't a valid value (it could be read by agy as a flag). The bad id is not kept as the resume token |
| `gate_missing` | Ask me, and Untether's agy plugin isn't installed on the host ([#980](https://github.com/littlebearapps/untether/issues/980)) |
| `gate_not_ready` | Ask me, and the plugin is edited, outdated or disabled, agy doesn't list its hooks for the project, the planted-config scan didn't finish, or the run's socket couldn't open |
| `gate_self_protect` | Ask me in a project that contains Untether's own package, venv or plugin folder |
| `gate_foreign_config` | Ask me in a project with agy hooks, plugins, agents or MCP servers Untether doesn't manage (or user-level ones) |
| `unsupported_version` | agy is older than 1.3.1 |
| `config_changed` | Unattended run, and the project's agy config differs from what an attended run last showed |
| `config_unchecked` | Unattended run, and that config couldn't be checked |
| `agy_always_proceed` | Workspace run, and agy's own settings report a `toolPermission` other than `request-review` / `strict` |
| `tool_permission_unknown` | Unattended Workspace run, and the `agy -p /config` check failed or returned no `toolPermission` ([#1044](https://github.com/littlebearapps/untether/issues/1044)) |

## Background task map

Besides events, `translate()` keeps `AntigravityStreamState.tasks`, the map the bridge renders as the
`⏳ background (N)` block ([#978](https://github.com/littlebearapps/untether/issues/978)). It emits no event of its
own: the block is re-read whenever the progress message is drawn. Each entry is an `EngineBackgroundTask`
(`src/untether/background_tasks.py`).

| agy line | Task map |
|---|---|
| `tool` `run_command`, `ACTIVE` | `cmd:<step>` added, listed from the moment it counts as background: `IsDaemon` / `WaitMsBeforeAsync` from the parameters if present, a later `tool` / `subagent` / `agent_response` step while it is still open, else 5 s |
| `tool` `schedule`, `ACTIVE` | `timer:<step>` added and listed at once (`timer (<DurationSeconds>s)`) |
| `tool` `invoke_subagent` `ACTIVE`, or `subagent` `ACTIVE` / `DONE` | `sub:<step>:<n>` per subagent, listed at once as `<role> (<type>)`. `DONE` only adds the child's conversation id: the children are still working |
| the `run_command` / `schedule` step `DONE` / `ERROR` | Removed if it never counted as background; otherwise `completed` / `failed`, and one `system_message` is expected for it. A `DONE` with a cancellation error is `stopped` (⏹️), with no notice expected ([#1062](https://github.com/littlebearapps/untether/issues/1062)) |
| `tool` `manage_task` `ACTIVE` with `Action: kill` | The `cmd:<step>` / `timer:<step>` its `TaskId` (`<cid>/task-<step>`) names → `stopped`, if still running |
| `invoke_subagent` step `ERROR` | Its subagents → `failed` |
| `system_message` `DONE` | Spends an expected notice if there is one; otherwise the oldest running subagent → `completed` |
| any `tool` step `ACTIVE` | Expected notices are dropped |
| `result`, or the run ending without one | Everything still running → `ended` (never `failed`: a daemon command is still running by design) |

The state also offers two lines the bridge duck-types ([#1061](https://github.com/littlebearapps/untether/issues/1061)): `background_hold_note()`, the last line
of the `⏳ background` block, and `cancelled_hold_note()`, added to the cancelled progress message when a listed
task was still running at `/cancel` (agy drops an answer it is holding when it is stopped).

Only `run_command`, `schedule` and `manage_task` steps count for the background wait (`awaiting_background`); a
subagent row never does.

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
| `invoke_subagent` | a subagent row titled `<type>: <role>` |
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

When a context percentage was shown, `usage["context"]` holds `{pct, used, window, model, estimated: true}` for
the `runner.completed` log (`context_pct`). `used` is the latest model call's `input_tokens` +
`cache_read_tokens`, not the summed `input_tokens` beside it. See
[Context use](runner.md#context-use-n-ctx).

## Session handling

- Resume line: `` `agy --conversation <id>` ``; ids shorter than 8 characters are never parsed as one.
- A failed resume clears the chat's saved session only for the "conversation no longer exists" reply. Sign-in,
  quota and other errors keep it. So does a `/continue` that finds a deleted conversation: the reply is "The
  conversation Antigravity had recorded for this project no longer exists, so nothing was resumed", with no resume
  token.
- Antigravity is not a turn-counting engine, and its child processes don't earn the long stall threshold except
  through the background wait described in the [runner reference](runner.md#background-commands).
