# Claude Code -> Untether event mapping (spec)

This document describes how the Claude Code runner translates Claude Code CLI JSONL events into Untether events.

> **Authoritative source:** The schema definitions are in `src/untether/schemas/claude.py` and the translation logic is in `src/untether/runners/claude.py`. When in doubt, refer to the code.

The goal is to make a Claude Code runner feel identical to the Codex runner from the bridge/renderer point of view while preserving Untether invariants (stable action ids, per-session serialization, single completed event).

---

## 1. Input stream contract (Claude Code CLI)

The Claude Code CLI emits **one JSON object per line** (JSONL) when invoked with
`--output-format stream-json`.

Non-interactive invocation:

```
claude -p --output-format stream-json --input-format stream-json --verbose -- <query>
```

Permission mode invocation (bidirectional control channel):

```
claude --output-format stream-json --input-format stream-json --verbose --permission-mode plan --permission-prompt-tool stdio
```

Notes:
- `--verbose` is required for `stream-json` output (CLI may otherwise drop events).
- `--input-format stream-json` enables JSON input on stdin.
- In `-p` mode, the prompt is passed as a positional argument after `--`.
- In permission mode, the prompt is sent via stdin as a JSON user message (no `-p`).
- Resuming uses `--resume <session_id>`.
- `-- <query>` safely passes prompts that start with `-`.

---

## 2. Resume tokens and resume lines

- Engine id: `claude`
- Canonical resume line (embedded in chat):

```
`claude --resume <session_id>`
```

Runner must implement its own regex because the resume format is
`claude --resume <session_id>`. Suggested regex:

```
(?im)^\s*`?claude\s+(?:--resume|-r)\s+(?P<token>[^`\s]+)`?\s*$
```

**Note:** Claude Code session IDs should be treated as opaque strings.

Resume rules:
- If a resume token is provided to `run()` (other than a `/continue` token, which
  names no session), the runner MUST verify that any `session_id` observed in
  the stream matches it.
- If the stream yields a different `session_id`, emit a fatal error and end the run.

---

## 3. Session lifecycle + serialization

Untether requires **serialization per session id**:

- For new runs (`resume=None`), do **not** acquire a lock until a `session_id`
  is observed (usually the first `system.init` event).
- Once the session id is known, acquire a lock for `claude:<session_id>` and hold
  it until the run completes.
- For resumed runs, acquire the lock immediately on entry.
- A `/continue` token carries no session id, so it is handled like a new run:
  the lock is taken on the id named by its first `started` event (#817).

This matches the Codex runner behavior in `untether/runners/codex.py`.

---

## 4. Event translation (Claude Code JSONL -> Untether)

### 4.1 Top-level `system` events

Claude Code emits a system init event early in the stream:

```
{"type":"system","subtype":"init","session_id":"...", ...}
```

**Mapping:**
- Emit a Untether `started` event as soon as `session_id` is known.
- Populate `meta` from `system.init` fields: `cwd`, `model`, `tools`, `permissionMode`, `output_style`, `apiKeySource`, `mcp_servers` (whichever are present), plus `effort` when a reasoning override is set. The `model` and `permissionMode` fields are used by the bridge to render the `🏷` footer line on final messages.
- The first `system.init` per run produces the `started` event. A later
  `system.init` never re-emits `started`; in a live session (#776) one that
  arrives while no turn is open opens a follow-up turn (see 4.5). A manual
  `/compact` also re-emits `init` mid-command.
- #751: on the first `init` only, a `permissionMode` that differs from the
  requested mode adds a warning `note` after `started` (`⚠️ Asked for <mode>
  mode — Claude Code is running <mode>`) and, when the effective mode is a
  prompting one, re-arms the stage-6 approval gate.
- Background-task subtypes (`task_started`, `task_progress`, `task_updated`,
  `task_notification`, `background_tasks_changed`) emit no Untether events;
  they maintain the native task map (`ClaudeStreamState.tasks`).
- `system/api_retry` (#792) emits one `note` action per retry sequence,
  updated in place (`🔁 API error 529 (overloaded) — retrying in 8s (attempt
  2/10)`; level `warning` on the final attempt) and latches an expected wait
  so the stall monitor stays quiet during the back-off.
- Hook lifecycle subtypes (`hook_started`, `hook_progress`, `hook_response`;
  only with `--include-hook-events`, #812) emit no Untether events. They pair
  by `hook_id` into `ClaudeStreamState.pending_hooks`, which drives the
  live-session async-hook hold, and an idle `hook_response{outcome:"error",
  exit_code:2}` arms the `hook_rewake` attribution for the next turn (4.5).
  The base runner does not let them overwrite `last_event_type`.
- `system/informational` (#814): the safeguard notice ("…'s safeguards
  stopped the response above · continuing once …") feeds the per-turn
  safeguard tally (outcome `retried`, see 4.2). Any other banner at level
  `warning` / `notice` emits one `note` action (`⚠️` / `ℹ️` + the first line
  of `content`; one row per `tool_use_id`); `info` / `suggestion` are logged
  only (`claude.informational`).
- `system/model_refusal_fallback` / `model_refusal_no_fallback` (#814) feed
  the safeguard tally with outcome `switched` / `not_retried`;
  `model_refusal_fallback` also updates the session model unless
  `scope == "local"`. `system/model_fallback` (not a safeguard stop) emits a
  `note` action `↪️ Switched model <a> → <b> (<trigger>)` and logs
  `claude.model_fallback`.
- `system/status` with a string `permissionMode` (#383) emits no Untether
  event; it updates `ClaudeStreamState.effective_permission_mode` (as does
  every `system.init.permissionMode` and the ack of Untether's own
  `set_permission_mode`, request id `ut_plan_rearm_…`). Seeing `plan`
  clears the session's `_PLAN_EXIT_APPROVED`; a plan chat seeing any other
  mode records `plan_exited_at` / `plan_exit_turn`.
- `TurnEvent(started).detail["plan_deferred"] = {"agents": N}` (#383 C4): the
  turn runs unplanned because the plan re-arm is deferred while background
  agents launched in the plan-exit turn still work — set on a follow-up /
  idle steer written during the deferral and, in `plan` chats, on a wake
  turn opened during it. The bridge adds `⚠️ Not re-planned: the approved
  plan's background agents are still running. Plan mode resumes when they
  finish.` under the turn header (the whole header for a follow-up).
- Optional: emit a `note` action summarizing tools/MCP servers (debug-only).

A top-level `control_request` (control-channel mode) is answered on stdin.
Housekeeping subtypes and the tool requests the mode doesn't route to the
user (#749) are approved silently and emit nothing; a request that needs the
user emits a started `warning` action (`claude.control.<n>`) whose
`detail.inline_keyboard` carries the Telegram buttons (Approve / Deny, plan
buttons for `ExitPlanMode`, option buttons for `AskUserQuestion`) and
`detail.request_id`. See the runner spec, "Permissions" and "Permission
modes". A `control_response` from the CLI emits nothing; the ack of
Untether's own `set_permission_mode` updates the effective mode (see the `system/status` bullet above).

The top-level `control_cancel_request` line (#684) — the CLI withdrawing a
pending permission request — writes no reply and retires the request: one
`action.completed` (kind `warning`, `⏹️ Permission request withdrawn — Claude
Code no longer needs an answer`) for the request's action, which drops its
keyboard; nothing for an unknown or already-answered id. The base runner keeps
it out of `last_event_type` (control traffic) and treats it as resolving an
approval wait in the ring-buffer fallback.

The top-level `rate_limit_event` line (#790) is a quota snapshot, not a
throttle notice: `allowed` emits nothing; `allowed_warning` emits at most one
`note` per window (`⚠️ 5h limit N% used — resets HH:MM`); only `rejected` not
covered by overage emits a `⏳ Rate limited until …` note and latches the wait
until `resetsAt`. Bare events emit nothing. Decision table:
[stream-json cheatsheet](stream-json-cheatsheet.md#rate_limit_event).

### 4.2 `assistant` / `user` message events

Claude Code messages include a `message` object with a `content[]` array. Each content
block can represent text, tool usage, or tool results.

For each content block:

#### A) `type = "tool_use"`
**Mapping:** emit `action` with `phase="started"`.

- `action.id` = `content.id`
- `action.kind` = map from tool name (see section 5)
- `title`:
  - if kind=`command`: use `input.command` if present
  - else: tool name or derived label
- `detail` includes `name` (tool name), `input` (tool input),
  `parent_tool_use_id` (if provided) and, for `file_change`, `changes`.
- `server_tool_use` blocks (#489) are translated the same way.

#### B) `type = "tool_result"`
**Mapping:** emit `action` with `phase="completed"`.

- `action.id` = `content.tool_use_id`
- `ok`:
  - if `content.is_error` exists and is true -> `ok=False`
  - else `ok=True`
- `detail` is the started action's detail plus `tool_use_id`,
  `result_preview` (the normalised content), `result_len` and `is_error`.
- `advisor_tool_result` blocks (#489) are translated the same way.

The runner SHOULD keep a small in-memory map from `tool_use_id -> tool_name`
(learned from `tool_use`) so the completed action title can match the started
action title.

#### C) `type = "text"`
**Mapping:**
- Default: do **not** emit an action (avoid duplicate rendering).
- Store the latest assistant text as a fallback final answer if `result.result`
  is empty or missing.

#### D) `type = "thinking"` or other block types
**Mapping:** a non-empty `thinking` block emits a completed `note` action
(`claude.thinking.<n>`, title = the thinking text, `detail.signature` when
present). `image` / `document` blocks (#597) and other types emit nothing.

#### E) Safeguard stops (`message.stop_reason == "refusal"`, #814)
A main-thread assistant frame (no `parent_tool_use_id`) whose
`message.stop_reason` is `"refusal"` counts one safeguard stop, deduped on
`message.id`. With the `informational` notice and the refusal-fallback
subtypes (4.1) it feeds one per-turn tally, so a paired stop counts once.
**Mapping:** one `note` action per turn, id `claude.safeguard.<turn>`,
updated in place: `🛡️ <model> safeguards stopped a response · <outcome>
(×N)`, `ok=True` (level `warning` only when not retried). If no CLI reaction
arrives by the turn's result, the outcome is `retried` when more output
followed the refusal, else `not_retried`. The result then carries
`usage["safeguard"] = {stops, outcome, outcome_label, category, model,
fallback_model?}`, and `claude.safeguard_stop` is logged once per resolved
stop. A stop never sets `ok=False`.

### 4.3 `result` events

The terminal event looks like:

```
{"type":"result","subtype":"success", ...}
```

**Mapping:** emit a single Untether `completed` event:

- `ok = !event.is_error`
- `answer = event.result` (fallback to last assistant text if empty); on
  success an approved `ExitPlanMode` plan body is prepended as
  `📋 Plan (approved)` when the answer is brief (#508/#793)
- `error` (only when `is_error`): built by `_extract_error` from the result
  text (or subtype), a diagnostic line and the #438 stream-idle classification
- `resume = ResumeToken(engine="claude", value=event.session_id)`
- `usage`: see section 6
- Emit exactly one `completed` event per run. With live sessions off (or in
  legacy `-p` mode) trailing lines are ignored; with live sessions on (#776)
  reading continues and later results close follow-up turns (4.5).
- `total_cost_usd` is cumulative per session, across `--resume` too; the bridge
  derives per-run/per-turn deltas (#778).
- `terminal_reason` (#806): `aborted_streaming` / `aborted_tools` mean the
  turn was interrupted. The runner sets `usage["terminal_reason"]`, and the
  bridge renders that turn as `cancelled` (never on `subtype` / `is_error`).
- `origin.kind == "task-notification"` marks a turn the CLI started itself;
  it confirms a `hook_rewake` turn at its result (4.5).
- `usage["safeguard"]` is added when the turn had a safeguard stop (4.2 E).
- **Resume guard:** on a resumed run, a 0-turn result (`num_turns == 0`,
  `duration_api_ms == 0`) that follows a replayed `task_notification{stopped}`
  before any assistant output is absorbed — no `completed`; the next result is
  the answer.

#### Supplementary `started` event after `result` (`✓ turn complete`)

Every successful `result` (i.e. `is_error=false`) MAY also emit a supplementary `started` event carrying late-arriving meta — `meta={"complete": "✓ turn complete"}` ([#333](https://github.com/littlebearapps/untether/issues/333)). This is the supported pattern for late-arriving meta documented in `runner-development.md`: `ProgressTracker.note_event` merges meta idempotently so the marker shows up in the footer (`format_meta_line`) alongside model / effort / permission / trigger without duplicating the StartedEvent. Errored results do **not** emit the marker — no false "complete" tag on a failure.

The runner emits this supplementary event only for the result that closes the run. Later turns of a live session ([#776](https://github.com/littlebearapps/untether/issues/776)) end in a `TurnEvent(phase="completed")` instead, so the bridge adds the marker itself: `handle_message`'s `_deliver_final` adds `meta["complete"]` (the shared `untether.model.TURN_COMPLETE_MARKER`) to the final's snapshot for every `ok=True` turn, whatever started it (follow-up, queued follow-up, background-task wake, Monitor tick, scheduled wake-up). The turn's tracker is left alone, so the turn's in-flight progress message never shows the marker; failed and interrupted turns don't get it ([#798](https://github.com/littlebearapps/untether/issues/798)).

#### Permission denials

> **Not yet implemented.** The upstream Claude Code CLI may include
> `result.permission_denials` with blocked tool calls, but Untether's
> `StreamResultMessage` schema does not capture this field and the runner does
> not emit warning actions for denials. This is a candidate for future work.

### 4.5 Follow-up turns in a live session (#776)

After the run's `completed`, each later turn in the same process is emitted as

```
TurnEvent(phase="started", turn=N, reason=…) → ActionEvent* → TurnEvent(phase="completed", turn=N, ok, answer, usage)
```

built with `EventFactory.turn_started` / `turn_completed`. The turn opens on the
first post-result `system.init`, `system/status{"status":"compacting"}` (a
`/compact` follow-up, #819), assistant message, or non-tool-result user
message; `reason` comes from what preceded it: an injected line's
`command_lifecycle.command_uuid` (`followup`), a `task_notification`
(`task_finished`), a fresh (≤ 10 s) idle `hook_response` with exit code 2
from a background hook (`hook_rewake`, #812; `detail.hook` / `hook_event`),
a `command_lifecycle(started)` with an unknown uuid (`scheduled_wakeup`), or a
live Monitor task (`monitor_event`). A turn that opened `unknown` becomes
`hook_rewake` at its result when an earlier turn's hook exited 2 during it
and the result's `origin.kind` is `task-notification`
(`detail.retro_attributed`). The bridge always pushes a `hook_rewake` final
(`🪝 Hook feedback — <event>`) and never folds it. Assistant/user
events tagged `parent_tool_use_id` (a background subagent) never open a turn.
`command_lifecycle` lines emit nothing themselves, with one exception (#775):
a `started` for a line Untether injected (a steer or a follow-up) while a
turn is still open means the CLI folded it into that turn, and emits a
`↪️ steer received: …` (or `↪️ follow-up received`) note
(`detail.absorbed_command_uuid`).

Attribution (#785): a `task_notification` only labels the next turn when the
task is top-level background work (`is_backgrounded` and not
`owned_by_subagent`); others are logged `claude.turn.notification_ignored`.
A turn that opened `unknown` (the CLI often starts it on a background agent's
result before any task event) completes as `task_finished` if a top-level task
ends during it (`detail.retro_attributed`), or is paired with a task ending
within 30 s after it. The task's own notification turn that follows carries
`detail.already_announced` and the bridge delivers it without a push.
`TurnEvent(completed)` carries the turn's `detail`; a task's own
notification turn also carries `detail.announced_turns`, the wake turn(s) its
end was paired with, so the bridge files an unattributed ack under the right
task (#813).

A turn ended by an interrupt (result `terminal_reason` aborted, or the bridge
closing the run with reason `cancel` mid-turn) is rendered `cancelled`, not as
an error; its cost delta is still accounted first (#806).

### 4.4 Error handling / malformed lines

- If a JSONL line is invalid JSON, or valid JSON that doesn't match the schema:
  emit nothing and continue. The base runner logs invalid JSON
  (`jsonl.parse.invalid`); a schema mismatch logs WARN `jsonl.msgspec.invalid`.
- If the subprocess exits non-zero or the stream ends without a `result` event:
  emit `completed` with `ok=False` and `error` explaining the failure.
- Emit **exactly one** `completed` event per run.

---

## 5. Tool name -> ActionKind mapping heuristics

Claude Code tool names can evolve. The runner SHOULD map based on tool name and input
shape. Suggested rules:

The implemented mapping is `tool_kind_and_title` in `runners/tool_actions.py`
(paths from `input.file_path`, else `input.path`):

| Tool name pattern | ActionKind | Title logic |
| --- | --- | --- |
| `Bash`, `Shell`, `KillShell` | `command` | `input.command` |
| `Write`, `Edit`, `MultiEdit`, `NotebookEdit` | `file_change` | the path |
| `Read` | `tool` | ``read: `<path>` `` |
| `Glob` / `Grep` | `tool` | ``glob: `<pattern>` `` / `grep: <pattern>` |
| `WebSearch` / `WebFetch` | `web_search` | `input.query` / `input.url` |
| `TodoWrite` / `TodoRead` | `note` | `update todos` / `read todos` |
| `AskUserQuestion` | `note` | `ask user` |
| `Task`, `Agent` | `subagent` | `input.description`, else `input.prompt` |
| (default) | `tool` | tool name |

For `file_change`, emit `detail.changes = [{"path": <path>, "kind": "update"}]`
(always `update`; creation is not distinguished).

If a tool name is unknown, map to `tool` and include the full input in `detail`.

---

## 6. Usage mapping

Untether `completed.usage` (and a live turn's `TurnEvent(completed).usage`) is
a dict built from the result: `total_cost_usd`, `duration_ms`,
`duration_api_ms`, `num_turns`, `subtype` (when present) and the raw
`result.usage` object under `usage`. Extra keys are added when they apply:
`safeguard` (4.2 E), `terminal_reason` (4.3), `context` and `compaction`
(6.1). `modelUsage` is not copied; it is read only to learn each model's
context window.

### 6.1 Context-window use (#819)

| Claude frame | Untether event |
|---|---|
| main-thread `assistant` with int `message.usage` input fields | `ActionEvent(kind="telemetry", id="claude.context", phase="updated", detail={context_pct, context_used, context_window, model})` — only when the integer % changes and the model's window is known |
| `result.modelUsage.<model>.contextWindow` | learned into the per-process window cache; a first-known value is emitted as the same telemetry event **before** the supplementary `StartedEvent{complete}` / `CompletedEvent`; `usage["context"] = {pct, used, window, model}` (log field) |
| `system/status {"status":"compacting"}` | `ActionEvent(kind="note", id="claude.compaction.<n>", phase="started", title="🗜️ Compacting context…")`; a repeat (the 30 s heartbeat) is `phase="updated"` on the same id. Live session with no turn open (a `/compact` follow-up): a `TurnEvent(started)` first |
| `system/status {"status":null,"compact_result":"success"}` | `phase="completed"`, `🗜️ Context compacted`, `ok=True` |
| `system/status {"status":null,"compact_result":"failed"}` | `phase="completed"`, `🗜️ Compaction failed · <compact_error>`, `ok=False`, `level="warning"` |
| `system/status {"status":null}` with a row open / none open | `🗜️ Compaction skipped` / nothing (#383's `permissionMode` frame) |
| `system/compact_boundary` | the same row re-completed as `🗜️ Context compacted · <pre> → <post> tokens (<trigger>)` (one step), then telemetry with `context_pct: None` (segment hidden until the next response) |
| `result` after a compaction | `usage["compaction"] = {count, trigger, pre_tokens, post_tokens, result, manual_success}` on the `CompletedEvent` / live `TurnEvent(completed)` |
| live `TurnEvent(started)` | followed by the current value (the turn's tracker starts empty) |

Subagent (`parent_tool_use_id`) and `<synthetic>` frames never change the value.
`ProgressTracker` keeps telemetry out of its actions (no step, no running tool, never
exported). Full rules: runner spec → "Context usage" and "Compaction".

---

## 7. Implementation checklist (v0.3.0)

Claude Code runner implementation summary (no Untether domain model changes):

1. [x] Create `untether/runners/claude.py` implementing `Runner` and (custom)
   resume parsing.
2. [x] Define `BACKEND` in `untether/runners/claude.py`:
   - `install_cmd`: install command for the `claude` binary
   - `build_runner`: read `[claude]` config + construct runner
3. [x] Add new docs (this file + `stream-json-cheatsheet.md`).
4. [x] Add fixtures in `tests/fixtures/` (see below).
5. [x] Add unit tests mirroring `tests/test_codex_*` but for Claude Code translation
   and resume parsing (recommended, not required for initial handoff).

---

## 8. Suggested Untether config keys

A minimal TOML config for Claude Code:

=== "untether config"

    ```sh
    untether config set claude.model "sonnet"
    untether config set claude.allowed_tools '["Bash", "Read", "Edit", "Write", "WebSearch"]'
    untether config set claude.dangerously_skip_permissions false
    untether config set claude.use_api_billing false
    ```

=== "toml"

    ```toml
    [claude]
    # model: opus | sonnet | haiku
    model = "sonnet"

    allowed_tools = ["Bash", "Read", "Edit", "Write", "WebSearch"]
    dangerously_skip_permissions = false
    use_api_billing = false
    ```

Untether only maps these keys to Claude Code CLI flags; other options should be configured in Claude Code settings.
If `allowed_tools` is omitted, Untether defaults to `["Bash", "Read", "Edit", "Write"]`.
When `use_api_billing` is false (default), Untether strips `ANTHROPIC_API_KEY` from the Claude Code subprocess environment to prefer subscription billing.
