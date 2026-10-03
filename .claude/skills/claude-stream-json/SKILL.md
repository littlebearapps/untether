---
name: claude-stream-json
description: >
  Claude Code CLI stream-json protocol as consumed by Untether.
  Covers JSONL event types, content blocks, control channel protocol,
  permission modes, auto-approve, the outline gate, and ExitPlanMode handling.
  This is the CONSUMER side — Untether spawns Claude Code as a subprocess.
triggers:
  - working on Claude runner code
  - modifying control channel handling
  - changing permission request logic
  - working on auto-approve or the outline gate
  - debugging Claude Code event streams
  - modifying ExitPlanMode or plan mode handling
---

# Claude Code stream-json Protocol (Consumer)

Untether spawns Claude Code CLI as a subprocess and consumes its JSONL output. This skill covers the protocol from Untether's perspective.

## Key files

| File | Purpose |
|------|---------|
| `src/untether/runners/claude.py` | `ClaudeRunner` — subprocess management, PTY, control channel, event translation |
| `src/untether/schemas/claude.py` | msgspec structs for Claude JSONL events |
| `src/untether/runners/tool_actions.py` | `tool_kind_and_title()` — tool name to ActionKind mapping |
| `docs/reference/runners/claude/runner.md` | Full runner specification |
| `docs/reference/runners/claude/stream-json-cheatsheet.md` | JSONL event shapes with examples |
| `docs/reference/runners/claude/untether-events.md` | Claude JSONL to Untether event mapping |
| `.claude/skills/claude-stream-json/control-channel-internals.md` | Control-channel mechanism detail: registries, claims, live-session stdin writers, async-hook hold, plan re-arm, parent-initiated requests |

## CLI invocation

### Non-interactive mode (`-p`)

```bash
claude -p --output-format stream-json --input-format stream-json --verbose -- <prompt>
```

- `-p` / `--print`: non-interactive, prompt as positional arg after `--`
- `--verbose`: required for full stream-json output
- `--input-format stream-json`: enables JSON input on stdin
- Prompt passed after `--` to protect prompts starting with `-`

### Interactive permission mode

```bash
claude --output-format stream-json --input-format stream-json --verbose \
  --permission-mode plan --permission-prompt-tool stdio
```

- **No `-p` flag** — prompt sent via stdin as JSON user message
- `--permission-prompt-tool stdio`: enables bidirectional control channel
- `--permission-mode <mode>`: the configured mode verbatim (`plan`, `auto`, `default`, `acceptEdits`, …); only Untether's `plan-auto` is translated (to `plan`, #741)
- `--include-hook-events` when the cached `claude --help` probe lists it (#812)

### Common flags

- `--resume <session_id>` / `--continue` (`/continue`): resume a previous session
- `--model <name>`: model override (sonnet, opus, haiku)
- `--effort <level>`: reasoning override
- `--allowedTools "<rules>"`: pre-approve tools at stage 5 — not sent in prompting modes unless set explicitly (#749)
- `[engines.claude] extra_args` go after the I/O prelude; approval/sandbox-bypass flags are refused (#209)

## JSONL event types

One JSON object per line on stdout. Required field: `type`.

### `system` (init)

```json
{"type":"system","subtype":"init","session_id":"...","cwd":"/repo","model":"sonnet",
 "permissionMode":"auto","tools":["Bash","Read","Write"],"mcp_servers":[...]}
```

- Emitted once at stream start
- `session_id`: opaque string (do NOT assume UUID format)
- Untether emits `StartedEvent` here

### `assistant` / `user` messages

```json
{"type":"assistant","session_id":"...","message":{"id":"msg_1","role":"assistant",
 "content":[...],"usage":{...}}}
```

Content blocks in `message.content[]`:

| Block type | Fields | Untether mapping |
|-----------|--------|-----------------|
| `text` | `text` | Stored as fallback answer; no action emitted |
| `tool_use` | `id`, `name`, `input` | `ActionEvent(phase="started")` |
| `tool_result` | `tool_use_id`, `content`, `is_error` | `ActionEvent(phase="completed")` |
| `thinking` | `thinking` | Optional note action or ignored |

### `result`

```json
{"type":"result","subtype":"success","session_id":"...","is_error":false,
 "result":"Done.","total_cost_usd":0.01,"usage":{...},
 "duration_ms":12345,"duration_api_ms":12000,"num_turns":2}
```

- `is_error`: authoritative error indicator
- `result`: final answer string
- Untether emits exactly one `CompletedEvent` here (the run's first result)
- In control-channel mode with live sessions (#776, default on) the process keeps
  running after `result`; each later turn is a `TurnEvent(started) → ActionEvent* →
  TurnEvent(completed)` segment. With `[watchdog] live_sessions = false` (or legacy
  `-p` mode) lines after `result` are dropped

Fields NOT in Untether's `StreamResultMessage` schema (silently ignored by msgspec):
- `error`, `permission_denials`, `modelUsage`

### `rate_limit_event` (#790)

A **quota-status snapshot**, not a throttle notice: `rate_limit_info.status` is
`allowed` / `allowed_warning` / `rejected`, plus `resetsAt`, `rateLimitType`,
`unifiedWindows` and overage fields. `allowed` → stashed, nothing rendered;
`allowed_warning` → one `⚠️ 5h limit N% used — resets HH:MM` note per window, no
latch; only `rejected` not covered by overage latches an expected wait until
`resetsAt` (`⏳ Rate limited until …`). Bare events latch nothing — the #657
"bare = 60 s throttle" guess is retired. `tests/test_claude_cli_schema_drift.py`
re-reads the enums from the installed CLI.

### `system` / `api_retry` (#792)

Emitted before Claude Code backs off a retryable API error (`attempt`,
`max_retries`, `retry_delay_ms`, `error_status`, `error`). Rendered as one
updating `🔁 API error 529 (overloaded) — retrying in 8s (attempt 2/10)` note and
latched as an expected wait (bridge threshold reason `api_retry_waiting`), kept
apart from rate-limit time.

### `system` / `status` + `compact_boundary` (#819)

`status: "compacting"` (re-sent every 30 s) → `status: null` + `compact_result`
→ (manual `/compact` only: a fresh `init`) → `compact_boundary` with
`compact_metadata{trigger, pre_tokens, post_tokens}` → a synthetic summary
`user` frame. One `🗜️` row per compaction; liveness-only in `runner.py`;
bounded expected wait (`compacting`, `awaiting_compaction()`). A manual
`/compact`'s 0-turn empty result is exempt from #596/#631 only when
`usage["compaction"].manual_success`. `status: null` + `permissionMode` is
#383's mode edge in the same handler. Main-thread `message.usage` over
`result.modelUsage.<model>.contextWindow` is the header's `% ctx`.

Full shapes and decision tables: `docs/reference/runners/claude/stream-json-cheatsheet.md`.

## Tool name to ActionKind mapping

| Tool name | ActionKind | Title source |
|-----------|-----------|-------------|
| `Bash` | `command` | `input.command` |
| `Edit`, `Write`, `MultiEdit`, `NotebookEdit` | `file_change` | `input.file_path` or `input.path` |
| `Read` | `tool` | `Read <path>` |
| `Glob`, `Grep` | `tool` | pattern from input |
| `WebSearch` | `web_search` | `input.query` |
| `WebFetch` | `web_search` | URL from input |
| `TodoWrite`, `TodoRead` | `note` | "update todos" |
| `AskUserQuestion` | `note` | "ask user" |
| `Task`, `Agent` | `tool` | tool name |
| `KillShell` | `command` | tool name |
| (other) | `tool` | tool name |

Mapping implemented in `src/untether/runners/tool_actions.py`.

## Control channel protocol

When using `--permission-prompt-tool stdio`, Claude Code sends control requests as JSONL on stdout and expects responses on stdin.

### Control request (stdout)

```json
{"type":"control_request","request_id":"<id>","request":{"subtype":"can_use_tool",
 "tool_name":"Bash","input":{"command":"rm -rf /"},"permission_suggestions":[...]}}
```

Other `request.subtype`s (`initialize`, `hook_callback`, `mcp_message`, `rewind_files`, `interrupt`) are
housekeeping. Decoded by `StreamControlRequest` / `Control*Request` in `schemas/claude.py`. The CLI can withdraw a
pending request with `control_cancel_request` (#684).

### Control response (stdin)

```json
{"type":"control_response","response":{"subtype":"success","request_id":"<id>",
 "response":{"behavior":"allow","updatedInput":{...}}}}
```

Or with denial (the message is what Claude reads — also how a typed AskUserQuestion answer is delivered):
```json
{"type":"control_response","response":{"subtype":"success","request_id":"<id>",
 "response":{"behavior":"deny","message":"Not allowed — explain your plan first."}}}
```

Exact shapes: `docs/reference/runners/claude/stream-json-cheatsheet.md`.

## PTY for stdin

ClaudeRunner uses `pty.openpty()` instead of `subprocess.PIPE` for stdin:
- Prevents deadlock when keeping stdin open for control responses
- Master FD held by the runner; slave FD passed to subprocess
- `tty.setraw(master_fd)` for raw byte passthrough
- Stdin refs captured locally at spawn time (not on `self`)

## Session registries (concurrent sessions)

```python
_SESSION_STDIN: dict[str, Any]                        # session_id -> stdin writer (PTY/pipe)
_REQUEST_TO_SESSION: dict[str, str]                    # request_id -> session_id
_PLAN_EXIT_APPROVED: set[str]                          # #283 diff-preview skip — cleared at every live turn open (#383)
_DISCUSS_APPROVED / _DISCUSS_CARRY: set[str]           # post-outline approval; carried ONE boundary (#383)
```

- Registered in `_iter_jsonl_events` when session_id is first seen
- Control responses routed via `_REQUEST_TO_SESSION` lookup
- Cleaned up when run completes

## Auto-approve logic

Decided per control request in `ClaudeRunner` (mechanism: `control-channel-internals.md`):

- Housekeeping request types (`_AUTO_APPROVE_TYPES`: initialize, hook_callback, mcp_message, rewind_files,
  interrupt) are auto-approved without looking at the payload
- `can_use_tool` in an autonomous mode (`plan`, `plan-auto`, `auto`, `dontAsk`, `bypassPermissions`): auto-approved
  unless the tool is in `_TOOLS_REQUIRING_APPROVAL = {"ExitPlanMode", "AskUserQuestion"}` or diff preview routes it
- `can_use_tool` in a prompting mode (`default`/`manual`/`acceptEdits`, `state.prompting_mode`): every tool goes to
  Telegram (#749)
- Unattended cron/webhook runs never wait for a tap: would-wait requests are denied (`_unattended_deny`, #835)
- `ExitPlanMode`: never auto-approved by the generic path (only `plan-auto`'s rubber stamp or a prior approval)

## ExitPlanMode handling

When Claude requests `ExitPlanMode`:
1. Inline keyboard shown: **Approve Plan** / **Deny** / **Pause & Outline Plan** (#383: plus a caption saying what approving does; "Plan mode resumes when this reply ends, or after the background agents it starts have finished." only when true)
2. "Pause & Outline Plan" sends a deny with a detailed message asking Claude to write a step-by-step plan
3. After outline is written, post-outline buttons appear: **Approve Plan** / **Deny** / **Let's discuss**
4. "Let's discuss" sends a deny asking Claude to discuss the plan (action: `chat`)
5. Text-based outline gate: retries without written outline text are auto-denied

### After approval: the plan re-arm (#383)

- Approval moves the CLI to `prePlanMode ?? "default"` and emits
  `system/status{status:null,permissionMode:"default"}` before the tool_result.
- In a live session of a `plan` / `plan-auto` chat the runner sends
  `{"type":"control_request","request_id":"ut_plan_rearm_<sid>_<n>","request":{"subtype":"set_permission_mode","mode":"plan"}}`
  at every turn close, **before yielding the turn-closing event** (never after —
  see `.claude/rules/control-channel.md`), and again before a follow-up / idle
  steer if still needed. Ack `{"mode":"plan"}` + `system/status plan`; no status
  frame when already plan. `plan-auto`: follow-ups/idle steers only.
- Kill switch `[watchdog] rearm_plan_mode`. Residual: a wake turn the CLI starts
  ~20 ms after the result (notification already queued) has an unplanned first
  model call (probe P-6). Running background agents inherit the mode (P-3), so
  the re-arm is deferred while agents launched in the plan-exit turn
  (`origin_turn == plan_exit_turn`) run — until they end, go quiet for
  `post_result_bg_max_hold` (`latest_background_progress`) or hit
  `live_session_max_s`; turns meanwhile carry `detail.plan_deferred`
  (`⚠️ Not re-planned …` header line).

### Outline gate (#570 retired the progressive cooldown)

- After "Pause & Outline Plan", `mark_outline_pending(session_id)` arms a
  purely TEXT-based gate: ExitPlanMode is auto-denied until
  `max_text_len_since_cooldown >= _OUTLINE_MIN_CHARS` (200), then held open
  behind synthetic Approve/Deny buttons
- The former time-based progressive cooldown (30/60/90/120s escalation) was a
  workaround for the v2.1.72–74 immediate-retry-after-denial loop — verified
  fixed on CLI 2.1.215 and removed (#570)

## Early callback answering

Telegram buttons show a spinner until `answerCallbackQuery`. The Claude control callback handler sets `answer_early = True` to clear the spinner immediately with a toast ("Approved", "Denied", "Outlining plan...").

## `write_control_response` helper

```python
async def write_control_response(          # ClaudeRunner method
    self,
    request_id: str,
    approved: bool,
    *,
    deny_message: str | None = None,
    rejects_plan: bool = True,
) -> bool:
```

Resolves the session via `_REQUEST_TO_SESSION` → `_SESSION_STDIN`, writes the response through `_locked_send`, and
cleans up. Telegram taps go through `respond_to_control_request()` after `claim_control_request()` (#685), never
straight to the writer.

## Config keys (`[claude]` section in untether.toml)

```toml
[claude]
model = "sonnet"
allowed_tools = ["Bash", "Read", "Edit", "Write"]
dangerously_skip_permissions = false
use_api_billing = false
permission_mode = "plan"  # set via /planmode command or ChatPrefsStore
```

- `use_api_billing = false` (default): strips `ANTHROPIC_API_KEY` from subprocess env
- `permission_mode`: overridable per-chat via `/planmode` command
