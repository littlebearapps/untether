# Control channel internals (full reference)

Verbatim detail moved out of `.claude/rules/control-channel.md` (2026-10-01). The rule keeps the
invariants; this file keeps the mechanism. Read it before changing the control channel, live-session
stdin writers, the async-hook hold, plan re-arm, or parent-initiated control requests.

## (former rule body)

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
_DISCUSS_CARRY: set[str]                               # #383: approvals already carried one boundary
_PLAN_EXIT_APPROVED: set[str]                          # #283 diff-preview skip — turn-scoped (#383)
_PENDING_ASK_REQUESTS: dict[str, tuple[int, str]]       # request_id -> (channel_id, question)
_HANDLED_REQUESTS: dict[str, HandledControl | None]    # #685: answered/cancelled/expired record (action, outcome, channel)
_INFLIGHT_CONTROL_RESPONSES: dict[str, str]            # #685: request_id -> claim owner while a tap is being written
_REQUEST_TO_CHANNEL: dict[str, int]                    # #388: request_id -> chat its buttons were posted in (bind after every _REQUEST_TO_SESSION[...] =)
_CANCELLED_DURING_WRITE: set[str]                      # #684: CLI withdrew the request while a tap was mid-write
```

- Register on first `system.init` event (when session_id is known)
- Every `can_use_tool` request logs INFO `control_request.received` (`request_id`, `tool_name`, `session_id`, `permission_mode`) before any branch decides it (#822); housekeeping subtypes don't. Keyboard / write / tap logs carry `tool_name` too — never `tool_input`
- Clean up all registries in the `finally` block of `run_impl` (including outline and approval state)
- All control responses go through `write_control_response(session_id, request_id, approved, deny_message)`
- Taps go through `respond_to_control_request()` (#685): `claim_control_request()` reserves the id before the dispatcher's first `await` (early-toast hook), and the result is three-way — sent / already handled (`Already answered`, silent `ℹ️` line) / not found or expired. `classify_control_request()` is channel-scoped. `send_claude_control_response()` is the bool wrapper. Never write a response without a claim
- The CLI can withdraw a pending request with `control_cancel_request` (#684): `_handle_control_cancel` retires it from every registry, strips its keyboard and records `cancelled` (a late tap toasts `No longer needed`); nothing is written to the CLI. A cancel racing an in-flight tap defers via `_CANCELLED_DURING_WRITE`

## Auto-approve

Non-interactive requests are auto-approved without showing buttons:
- Request types in `_AUTO_APPROVE_TYPES` tuple: `ControlInitializeRequest`, `ControlHookCallbackRequest`, `ControlMcpMessageRequest`, `ControlRewindFilesRequest`, `ControlInterruptRequest`
- Exception (#925): a `ControlHookCallbackRequest` whose `callback_id` is in `_LOOP_HOOK_IDS` is intercepted before that tuple and decided from its `input` (see Scheduling hooks below). Every other hook callback stays payload-blind (`TestAutoApproveSafetyInvariant`)
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
- **Plan re-arm (#383).** An approved `ExitPlanMode` moves the CLI to
  `prePlanMode ?? "default"`; a live session used to stay there. The runner
  tracks the effective mode (`_note_permission_mode` ← `system/init`,
  `system/status` via `_SYSTEM_SUBTYPE_HANDLERS["status"]`, and our ack) and
  re-arms with `set_permission_mode plan` **only** when the configured mode
  maps to CLI `plan` (`state.configured_plan_mode`), the session is live, the
  CLI reported `plan` at least once (`plan_mode_observed` — never fight
  `--dangerously-skip-permissions`), it has left plan, and nothing is in
  flight. Never for prompting modes or the other autonomous modes (`auto`,
  `dontAsk`, `bypassPermissions`); `plan-auto` only before follow-ups/idle
  steers (`_PLAN_AUTO_REARM_AT_IDLE = False`, Decision 6). Kill switch
  `[watchdog] rearm_plan_mode` (read per spawn). Approval flags are
  turn-scoped regardless: every `_open_followup_turn` clears
  `_PLAN_EXIT_APPROVED`; an unconsumed `_DISCUSS_APPROVED` survives one
  boundary (`_DISCUSS_CARRY`). A mid-turn steer fold is never a boundary.
- **Agent deferral (#383 C4).** Background subagents inherit the parent's
  live mode (probe P-3), so `_claim_plan_rearm` returns None while a live
  `local_agent` launched in the plan-exit turn (`origin_turn ==
  plan_exit_turn`) still runs. Key on `origin_turn`, never start time (no
  chaining). The bound is agent *inactivity* — `latest_background_progress`
  restricted to those tasks, `post_result_bg_max_hold` — with
  `live_session_max_s` as the ceiling (plan 21 D7), never a fixed time since
  the exit. Re-checked at every turn close, on the agent's end frame while
  idle (post-line drain, reason `agents_done`), by the lifecycle while idle,
  and before a follow-up / idle steer; turns that run meanwhile carry
  `TurnEvent.detail["plan_deferred"]`.

## AskUserQuestion flow

When Claude calls `AskUserQuestion`:
1. Control request intercepted → registered in `_PENDING_ASK_REQUESTS[request_id]`
2. Question extracted from `input.question` or `input.questions[0].question`
3. Progress message shows `❓ <question text>` with Approve/Deny buttons
4. User replies with text → `telegram/loop.py` intercepts via `get_pending_ask_request()`
5. `answer_ask_question()` sends a deny response with the user's text as its `message` (`deny_message=` on `write_control_response`)
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
- User clicks "Let's discuss" → control request held open (never responded to) so Claude stays alive; the 5-minute sweep (`CONTROL_REQUEST_TIMEOUT_SECONDS = 300.0`) is event-driven (it runs when the next control event arrives, not on a timer) and records the request `expired`; a request nothing can answer is logged `control_request.unanswerable` by the bridge (#684, detect-only, `[watchdog] detect_unanswerable_control_requests`)
- Next `ExitPlanMode` checks `_DISCUSS_APPROVED` → auto-approves if present
- #383: an approval not consumed when its turn ends survives exactly ONE live turn boundary (`_DISCUSS_CARRY`), then is cleared; consuming it discards both sets
- Synthetic callback_data prefix: `da:` (fits 64-byte Telegram limit)
- Handled in `claude_control.py` before the normal approve/deny flow
- Outlines rendered as formatted text via `render_markdown()` + `split_markdown_body()` — approval buttons on last message
- Outline/notification cleanup via module-level `_OUTLINE_REGISTRY` on approve/deny

## Control request/response format

Request (from Claude on stdout):
```json
{"type":"control_request","request_id":"req_1","request":{"subtype":"can_use_tool","tool_name":"Bash","input":{...}}}
```

Response (to Claude on stdin, built by `write_control_response`):
```json
{"type":"control_response","response":{"subtype":"success","request_id":"req_1","response":{"behavior":"allow","updatedInput":{...}}}}
```

Denial with message:
```json
{"type":"control_response","response":{"subtype":"success","request_id":"req_1","response":{"behavior":"deny","message":"..."}}}
```

## Stdin writers and live sessions (#776)

Stdin is written from several tasks (control responses, the auto-approve/deny/catalog drains, follow-up injection, the live-session close), so every write goes through `_locked_send` (a per-pipe `anyio.Lock`). `write_user_message(session_id, text, command_uuid=…)` writes a stream-json `user` line with `uuid` — the CLI echoes it as `command_lifecycle.command_uuid`, which attributes the turn. Never write a follow-up mid-turn outside steer mode: `inject_when_idle` waits until the session is idle (a mid-turn write is folded into the running turn). `LiveSession.lock` serialises injection against `close_live_session` (the race guard). Closing a live session closes stdin; if the CLI hasn't exited `_live_close_grace_s` (15 s) later, `_await_live_exit_or_force` logs `claude.live_session.close_grace_expired` with a proc snapshot, sends SIGINT, then SIGTERM/SIGKILL after `_live_close_sigint_grace_s` (5 s). `forced_teardown_after_result` quarantine is skipped when `LiveSession.closed_idle_clean` still holds (#791). #829 B2: background **agents** ignore stdin EOF (they run to completion), so a close over one always reaches SIGINT; an Untether-initiated close (`_STOPPED_CLEAN_REASONS`: `max_hold`/`cancel`/`new`/`drain`/`options_changed`) whose turn was closed at close time (`LiveSession.closed_turn_idle`) decides its quarantine **after** the SIGINT wait (shielded) — rc 0 → `exited_after_sigint stopped_clean=True`, not quarantined; `abs_cap`, `error`, rc ≠ 0 and the SIGTERM path keep it. The live-session background hold counts quiet time (`latest_background_progress`; `[watchdog] bg_hold_rearm_on_progress`). `_SESSION_STDIN` still means "a process owns this session"; use `is_session_accepting()` to ask "can I write a follow-up into it".

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

## Scheduling hooks (#925, #926)

Untether owns Claude's schedules. `_loop_hooks_config()` puts exact-name `PreToolUse` matchers for `CronCreate` and
`CronDelete` (callback ids `ut_loop_cron_create` / `ut_loop_cron_delete`, 30 s timeout) in the `initialize` request of
**every** control-channel spawn; `[loop] own_schedule = false` sends no hooks (rc19 behaviour). `-p` mode has no
control channel, so no hooks.

- `ut_loop_cron_create`: Loop mode is read when the callback arrives (per-chat override, else live `[loop] enabled`).
  Off → deny with `_LOOP_OFF_DENY_REASON` (Loop mode / `/at` guidance; must never match `_LOOP_CRON_ID_RE`). On →
  `_register_cron_from_input()` registers an Untether loop with the `[loop]` caps, then deny with the loop's
  `ut_loop_…` token. Logged as `loop.cli_job_denied` (`loop_mode=on|off`).
- `ut_loop_cron_delete`: a `ut_loop_` id stops that loop **only if this session owns it** (foreign and unknown tokens
  get the same answer) and denies; any other id passes through so a real CLI job is deleted natively. CLI 2.1.289
  validates CronDelete ids **before** running hooks, so the `tool_use` observer (`_observe_loop_tool_use`) does the
  stop in practice; the hook branch is the fallback for a CLI that runs hooks first. Keep both.
- Never leave a registered callback unanswered (the tool blocks for the hook timeout): `_loop_hook_decision_safe()`
  fails closed for CronCreate (deny) and open for CronDelete (passthrough). Answers queue on
  `state.hook_callback_queue`.
- Self-paced wake chains (`ScheduleWakeup`) are capped per process at `[loop] max_iterations` (`state.wake_cap`); the
  close reason is `wake_cap`.
- Residual native jobs (pre-rc20 sessions, `-p` chats, `own_schedule = false`) are tracked as cron-suppression records
  in `loop_scheduler.py`; a later control-channel spawn resuming that session gets `CLAUDE_CODE_DISABLE_CRON=1` until
  the CLI's 7-day resurrect window has passed. `/cancel` writes a per-loop cancel sentinel so the cancelled task
  doesn't come back on the next resume (#926).

## Parent-initiated control_requests (Untether → Claude)

Untether can also *initiate* control_requests on stdin, following the wire format documented in Anthropic's [`claude-agent-sdk-python`](https://github.com/anthropics/claude-agent-sdk-python). Subtypes accepted by Claude Code include: `mcp_status`, `mcp_reconnect` (`serverName`), `mcp_toggle` (`serverName` + `enabled`), `set_permission_mode`, `interrupt`, `set_model`, `stop_task` (`task_id`).

Untether uses this direction in [#365](https://github.com/littlebearapps/untether/issues/365):
```json
{"type":"control_request","request_id":"ut_catalog_refresh_<sid>_<seq>","request":{"subtype":"mcp_status"}}
```

Drained via `ClaudeRunner._drain_catalog_refresh` alongside `_drain_auto_approve` / `_drain_auto_deny`. **Fire-and-forget** — Untether does not register a pending response entry or parse the eventual `control_response`. Request IDs use the `ut_<feature>_<session_id>_<seq>` namespace so they can't collide with Claude Code's own `req_*` IDs. If you add another parent-initiated subtype, reuse this namespace convention and extend this section.

The second user is the **#383 plan re-arm**:
```json
{"type":"control_request","request_id":"ut_plan_rearm_<sid>_<seq>","request":{"subtype":"set_permission_mode","mode":"plan"}}
```
- **Not** fire-and-forget: the ack is parsed (`StreamControlResponse` arm in `_translate_claude_event_base`, `ut_plan_rearm_` ids only, matched against `state.plan_rearm_inflight`; stale ids are ignored). Success `{"mode":"plan"}` counts as observing plan; an error (`error_code`) logs `claude.permission_mode.rearm_failed` WARN and sets `plan_rearm_failed` → `inject_live_followup` closes the session (`plan_rearm_failed`, `only_if_idle=True`) and the lifecycle does the same once idle.
- Claimed synchronously (`_claim_plan_rearm`, single flight) and written via `_locked_send`.
- **Idle boundary: written BEFORE the turn-closing `CompletedEvent` / `TurnEvent(completed)` is yielded** (`_drain_plan_rearm_pre_yield` in `_iter_jsonl_events`), at every live turn close whatever the outcome. Never move it to the post-yield drains: the bridge's `on_completed` / turn router run inside the yield (up to 60 s), and a wake turn the CLI starts from a queued notification reads no stdin line first. A post-yield `_drain_plan_rearm` is only a backstop.
- Follow-up (`inject_when_idle`) and idle steer (`steer_into_session`) write it again under `live.lock` immediately before the user line if still needed — FIFO on stdin; no ack wait (a `plan` request can't be refused on 2.1.285; drift test `test_set_permission_mode_refusal_codes`).
- Its `system/status` / `control_response` frames arrive while idle; that is only safe because re-arms are live-only (the #470 `last_event_type` check).

## After changes

```bash
uv run pytest tests/test_claude_control.py tests/test_ask_user_question.py tests/test_diff_preview.py -x
```

If this change will be released, also run integration tests C1-C6 (Claude interactive), T8 (stale buttons), S9 (concurrent clicks) via `@untether_dev_bot`. See `docs/reference/integration-testing.md`.
