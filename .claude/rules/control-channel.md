---
applies_to: "src/untether/runners/claude.py,src/untether/telegram/commands/claude_control.py"
---

# Control Channel Rules

## PTY lifecycle

ClaudeRunner uses `pty.openpty()` for stdin (not `subprocess.PIPE`):
1. `master_fd, slave_fd = pty.openpty()`
2. `tty.setraw(master_fd)` for raw byte passthrough
3. Slave FD passed to subprocess as stdin
4. Master FD wrapped in `anyio.AsyncFile` for async writes
5. **Always close master FD in `finally`** — FD leaks break subsequent runs

## Session registries

```python
_SESSION_STDIN: dict[str, anyio.abc.ByteSendStream]   # session_id -> stdin
_REQUEST_TO_SESSION: dict[str, str]                    # request_id -> session_id
_OUTLINE_PENDING: set[str]                             # sessions awaiting outline text
_DISCUSS_APPROVED: set[str]                            # sessions with post-outline approval
_PENDING_ASK_REQUESTS: dict[str, tuple[int, str]]       # request_id -> (channel_id, question)
```

- Register on first `system.init` event (when session_id is known)
- Clean up all registries in the `finally` block of `run_impl` (including outline and approval state)
- All control responses go through `write_control_response(session_id, request_id, approved, deny_message)`

## Auto-approve

Non-interactive requests are auto-approved without showing buttons:
- Request types in `_AUTO_APPROVE_TYPES` tuple: `ControlInitializeRequest`, `ControlHookCallbackRequest`, `ControlMcpMessageRequest`, `ControlRewindFilesRequest`, `ControlInterruptRequest`
- Tool requests: auto-approved UNLESS `tool_name in _TOOLS_REQUIRING_APPROVAL`
- `_TOOLS_REQUIRING_APPROVAL = {"ExitPlanMode", "AskUserQuestion"}`
- `ExitPlanMode`: NEVER auto-approved — always show Telegram buttons
- `AskUserQuestion`: NEVER auto-approved — shown in Telegram for user to reply with text

## Permission modes (#741)

Untether values map onto `--permission-mode` via
`runners/run_options.claude_cli_permission_mode()`. Only **`plan-auto`** is
translated (→ CLI `plan`); every genuine CLI mode — `default`, `manual`,
`plan`, `auto`, `acceptEdits`, `dontAsk`, `bypassPermissions` — passes through
verbatim.

- `plan-auto` is Untether's sugar: CLI plan mode **plus** the `ExitPlanMode`
  rubber stamp (`state.auto_approve_exit_plan_mode`, armed by
  `is_claude_plan_auto()`). It was spelled `auto` until 0.35.5rc8, shadowing
  the CLI's own mode.
- `auto` is Claude Code's classifier-gated mode. It must **never** arm
  `auto_approve_exit_plan_mode` — that would re-create the blanket downstream
  bypass tracked by #383 on a mode that has no plan gate.
- The control channel survives `auto`: `AskUserQuestion` still raises a
  `can_use_tool` control_request (probed on CLI 2.1.228), and classifier
  fallback after repeated blocks routes through `--permission-prompt-tool
  stdio` as a normal Telegram approval.
- Never re-introduce an inline `"plan" if mode == "auto"` remap. Add new modes
  to `CLAUDE_CLI_PERMISSION_MODES`; the drift test in
  `tests/test_claude_permission_modes.py` re-derives that set from the
  installed CLI and fails when it rots.

## AskUserQuestion flow

When Claude calls `AskUserQuestion`:
1. Control request intercepted → registered in `_PENDING_ASK_REQUESTS[request_id]`
2. Question extracted from `input.question` or `input.questions[0].question`
3. Progress message shows `❓ <question text>` with Approve/Deny buttons
4. User replies with text → `telegram/loop.py` intercepts via `get_pending_ask_request()`
5. `answer_ask_question()` sends deny response with user's text as `denial_message`
6. Claude reads the denial message as the answer and continues

## Diff preview

`_format_diff_preview(tool_name, tool_input)` generates compact diffs for approval messages:
- Only for tools going through `ControlRequest` (not auto-approved)
- Edit: `- old` / `+ new` lines (max 4 each, 60 char truncation)
- Write: `+ content` (max 8 lines)
- Bash: `$ command` (max 200 chars)

## Outline gate (Pause & Outline)

After a "Pause & Outline Plan" click, `mark_outline_pending(session_id)` arms a
purely TEXT-based gate on subsequent `ExitPlanMode` requests:
- Outline not yet written (`max_text_len_since_cooldown < _OUTLINE_MIN_CHARS`,
  200 chars) → auto-deny with the write-the-outline-first instruction
- Outline written → hold the request open + synthetic Approve/Deny buttons
- Outline and approval state cleaned up on session end

**#570 (retired workaround):** a time-based progressive cooldown
(30/60/90/120s escalation, `_DISCUSS_COOLDOWN`) used to also gate this path —
it worked around Claude Code v2.1.72–2.1.74 re-issuing `ExitPlanMode`
immediately after a denial (#126 lineage). Verified fixed on CLI 2.1.215
(2026-07-20: denied ExitPlanMode → clean text turn, no re-issue) and removed.
If the upstream loop ever regresses, the repro is: deny an ExitPlanMode
control_request via the Telegram buttons and watch for an immediate re-issue.

## Post-outline approval

After the outline-gate auto-deny, synthetic Approve/Deny/Let's discuss buttons (✅/❌/📋 emoji prefixes) appear in Telegram:
- User clicks "Approve Plan" → session added to `_DISCUSS_APPROVED`, outline-pending cleared
- User clicks "Deny" → outline-pending cleared, no auto-approve flag set
- User clicks "Let's discuss" → control request held open (never responded to) so Claude stays alive; 5-minute safety timeout (`CONTROL_REQUEST_TIMEOUT_SECONDS = 300.0`) cleans up stale held requests
- Next `ExitPlanMode` checks `_DISCUSS_APPROVED` → auto-approves if present
- Synthetic callback_data prefix: `da:` (fits 64-byte Telegram limit)
- Handled in `claude_control.py` before the normal approve/deny flow
- Outlines rendered as formatted text via `render_markdown()` + `split_markdown_body()` — approval buttons on last message
- Outline/notification cleanup via module-level `_OUTLINE_REGISTRY` on approve/deny

## Control request/response format

Request (from Claude on stdout):
```json
{"type":"control_request","request_id":"req_1","tool_name":"Bash","tool_input":{...}}
```

Response (to Claude on stdin):
```json
{"type":"control_response","request_id":"req_1","approved":true}
```

Denial with message:
```json
{"type":"control_response","request_id":"req_1","approved":false,"denial_message":"..."}
```

## Stdin writers and live sessions (#776)

Stdin is written from several tasks (control responses, the auto-approve/deny/catalog drains, follow-up injection, the live-session close), so every write goes through `_locked_send` (a per-pipe `anyio.Lock`). `write_user_message(session_id, text, command_uuid=…)` writes a stream-json `user` line with `uuid` — the CLI echoes it as `command_lifecycle.command_uuid`, which attributes the turn. Never write a follow-up mid-turn outside steer mode: `inject_when_idle` waits until the session is idle (a mid-turn write is folded into the running turn). `LiveSession.lock` serialises injection against `close_live_session` (the race guard). Closing a live session closes stdin; if the CLI hasn't exited `_live_close_grace_s` (15 s) later, `_await_live_exit_or_force` logs `claude.live_session.close_grace_expired` with a proc snapshot, sends SIGINT, then SIGTERM/SIGKILL after `_live_close_sigint_grace_s` (5 s). `forced_teardown_after_result` quarantine is skipped when `LiveSession.closed_idle_clean` still holds (#791). `_SESSION_STDIN` still means "a process owns this session"; use `is_session_accepting()` to ask "can I write a follow-up into it".

## Async-hook hold (#812)

Live sessions also stay open while a background command hook (`async` /
`asyncRewake`) is still running, because the CLI **drops** an `asyncRewake`
hook's rewake once stdin has closed (`docs/findings/2026-09-29-claude-rc14-cli-surface.md` §A1).

- `--include-hook-events` is added in control-channel mode when a cached
  `claude --help` probe lists it (`claude.hook_events.probe`) and
  `[watchdog] hold_for_async_hooks` is on; skipped if `extra_args` already has
  it. It is **not** in `_RESERVED_FLAGS`.
- `system/hook_*` frames pair by `hook_id` into `state.pending_hooks` (cap 256,
  `SessionStart`/`Setup` never hold). No UntetherEvents; `runner.py` keeps them
  out of `last_event_type` (#470 / auto-continue) but counts them as liveness.
- `has_pending_async_hooks()` is a **sibling predicate** OR'd into
  `_live_session_lifecycle`, never part of `has_live_background_work()` — hooks
  must not reach footers, the #777 panel, the #592 cap or `_is_clean_idle`.
- Bound `[watchdog] async_hook_max_hold` (630 s, 0–3600) from the **newest**
  unpaired `hook_started` → every unpaired hook expires together, ONE
  `claude.hook.hold_expired` WARN per hold (`live_hook_processes`,
  `pending_hooks`, `hook_events`, `held_s`, `max_hold_s`); the hold then ends
  even if a hook process lives.
- The CLI withholds a plain `async` hook's `hook_response` until the next turn
  or stdin close. Each idle tick counts **hook processes**
  (`proc_diag.cli_children()` → `hook_evidence_children()`): any direct CLI
  child except (a) Bash-tool shells, (b) children that started more than
  `HOOK_START_SLACK_S` (5 s) before the oldest unpaired hook's
  `hook_started` (the CLI emits the frame, *then* spawns the hook; compared
  on `hook_clock()`, which counts through sleep), and (c) children **in the
  CLI's own process group** that are in the `system/init` baseline
  (`capture_cli_baseline()`, (pid, start time) — MCP servers) or have
  MCP/LSP-looking argv. The CLI spawns every command hook `detached` (own
  process group; drift-tested), so a hook alive at init, a baselined PID
  reused later, or a hook named like `mcp-scan` is never exempt, while a
  non-detached `sh -c`-wrapped MCP server is. A `/hooks/` token counts
  unless (b). Never rely on `sh -c` alone: bash (macOS `/bin/sh`) and zsh
  exec a single hook command (security-guidance's `bash …/sg-python.sh …`).
  Children come from `/proc/<pid>/task/*/children`, falling back to a
  /proc ppid scan when that file doesn't exist (no `CONFIG_PROC_CHILDREN`) —
  never read "no children file" as "no hook"; a forking shell/`env` wrapper
  around the CLI is resolved to the CLI (Untether's own `env -i` execs).
  **All-or-nothing — never bind a process to a hook** (frames carry no pid;
  a turn's hooks start in the same ms; the per-hook binding in 271bb96
  released a live `asyncRewake` hook and lost its rewake): any hook process
  alive → every unpaired hook holds; none for 1 s →
  `release_settled_async_hooks()` releases them all
  (`claude.hook.hold_released reason=no_hook_process`). Unreadable process
  table → keep the bounded hold. `close_live_session` re-checks.
- Labels never claim more hooks than live hook processes: N = live ones
  (capped by the unpaired candidates; the candidate count only when the table
  is unreadable), events = the distinct candidate events. N = 0 at a close →
  no `async_hook_killed`, no notice.
- Idle exit-2 `hook_response` → next turn `reason="hook_rewake"` (always
  pushed, never folded); a turn already open is confirmed at its result by
  `origin.kind == "task-notification"`.
- A close with a hook still evident (unpaired hooks with a live shell, a
  `/hooks/` argv child, or any live hook process — process evidence) uses
  `_live_close_grace_hooks_s` (35 s = the CLI's 30 s rewake wait + 5 s)
  instead of 15 s, logs `claude.live_session.async_hook_killed` (`hook_count`,
  `live_hook_processes`, `hook_events`, candidate `hook_names`/`hook_ids` +
  `note`; INFO for `cancel`/`new`/`drain`/`options_changed`, WARN otherwise)
  and, on automatic closes only, sends `⏳ Closing session — a background hook
  (Stop or UserPromptSubmit) was still running; its feedback wasn't
  delivered.` (`N background hooks (…)` when N ≥ 2).
- Timing knobs are slots-dataclass fields: set them on the instance in tests.

## Parent-initiated control_requests (Untether → Claude)

Untether can also *initiate* control_requests on stdin, following the wire format documented in Anthropic's [`claude-agent-sdk-python`](https://github.com/anthropics/claude-agent-sdk-python). Subtypes accepted by Claude Code include: `mcp_status`, `mcp_reconnect` (`serverName`), `mcp_toggle` (`serverName` + `enabled`), `set_permission_mode`, `interrupt`, `set_model`, `stop_task` (`task_id`).

Untether uses this direction in [#365](https://github.com/littlebearapps/untether/issues/365):
```json
{"type":"control_request","request_id":"ut_catalog_refresh_<sid>_<seq>","request":{"subtype":"mcp_status"}}
```

Drained via `ClaudeRunner._drain_catalog_refresh` alongside `_drain_auto_approve` / `_drain_auto_deny`. **Fire-and-forget** — Untether does not register a pending response entry or parse the eventual `control_response` today. Request IDs use the `ut_<feature>_<session_id>_<seq>` namespace so they can't collide with Claude Code's own `req_*` IDs. If you add another parent-initiated subtype, reuse this namespace convention and extend this section.

## After changes

```bash
uv run pytest tests/test_claude_control.py tests/test_ask_user_question.py tests/test_diff_preview.py -x
```

If this change will be released, also run integration tests C1-C6 (Claude interactive), T8 (stale buttons), S9 (concurrent clicks) via `@untether_dev_bot`. See `docs/reference/integration-testing.md`.
