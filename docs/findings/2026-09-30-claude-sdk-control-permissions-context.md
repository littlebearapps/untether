---
question: >-
  For the 0.35.5rc15 Claude issues (#209, #383, #684, #685, #747, #751, #819),
  what do the Claude Agent SDK and Claude Code CLI 2.1.285 natively provide for
  the control protocol (can_use_tool / control_cancel_request / timeouts),
  permission-mode transitions, bypass flags, compaction events and
  context-window accounting?
date: 2026-09-30
sources:
  - "npm @anthropic-ai/claude-agent-sdk@0.3.285 (published 2026-09-29, package.json claudeCodeVersion 2.1.285): sdk.d.ts, sdk.mjs"
  - "github anthropics/claude-agent-sdk-python@v0.2.162 (f2204bb, _cli_version.py = 2.1.285): types.py, _internal/query.py, _internal/transport/subprocess_cli.py"
  - https://code.claude.com/docs/en/permission-modes (retrieved 2026-09-30)
  - "binary: ~/.local/share/claude/versions/2.1.285 (strings / code context)"
  - "zero-token probes Z1-Z9 on lba-1, 2026-09-30 (appendix): blackholed API or a local fake Anthropic API on 127.0.0.1; no real model call was made"
confidence: high (Q1, Q2, Q3, Q4 shapes, Q5 formula); med (auto-compaction frame order, post_tokens semantics, get_context_usage mid-turn)
---

# Claude SDK control protocol, permission modes and context accounting (CLI 2.1.285)

**Date:** 2026-09-30 · **CLI:** Claude Code 2.1.285 (`claude --version`) · **Host:** lba-1 ·
**Binary:** `readlink -f $(which claude)` = `/home/nathan/.local/share/claude/versions/2.1.285` ·
**SDKs pinned to the matching CLI:** TypeScript `@anthropic-ai/claude-agent-sdk@0.3.285`
(`claudeCodeVersion: "2.1.285"`), Python `claude-agent-sdk` **v0.2.162** (bundles CLI `2.1.285`).

Builds on, and does not repeat: `2026-08-13-claude-permission-modes.md` (six-stage permission
order, allowlist gate, `set_permission_mode` round trip), `2026-09-27-claude-live-session-probes.md`
(live-session frames) and `2026-09-29-claude-rc14-cli-surface.md` (hooks, refusals,
`terminal_reason`, control_request subtype list).

## Method and labels

- **DOCS**: SDK type declarations/docstrings or official docs, version as above.
- **SDK-SRC**: SDK runtime source (`sdk.mjs` bundle for TS, `query.py` for Python).
- **BINARY**: code/strings in the 2.1.285 binary. Minified identifiers (`Tfr`, `RLr`, `qT`…) are
  unstable; quote literals only.
- **PROBE**: observed live on 2.1.285 at **zero token cost**. Two harnesses:
  (a) `ANTHROPIC_BASE_URL=http://127.0.0.1:9` (blackholed) with only control requests on stdin;
  (b) a ~60-line local **fake Anthropic Messages API** (Python `http.server`, SSE) on 127.0.0.1
  that returns a scripted `tool_use` (Bash or ExitPlanMode) or a text reply with fixed `usage`.
  Every `result.total_cost_usd` in (b) is the CLI pricing the fake usage; nothing reached Anthropic.
  Common argv: `claude -p --input-format stream-json --output-format stream-json --verbose
  --permission-prompt-tool stdio --setting-sources local --strict-mcp-config
  --no-session-persistence --model haiku`, `cwd=/tmp/sdkres/probe`. Scripts were scratch files
  (not committed); the appendix gives enough to rebuild them.
- **UNVERIFIED**: inferred, not confirmed.

## Summary

| Q | Question | Answer (short) | Confidence |
|---|---|---|---|
| Q1 | `control_cancel_request` | **The CLI sends it** to the stdio host when a pending `can_use_tool` becomes moot (interrupt/turn abort): `{"type":"control_cancel_request","request_id":"<id>"}`. A late answer is ignored. **No timeout** anywhere; stdin close rejects the pending request as a tool error. Re-sent `initialize` returns the authoritative `pending_permission_requests` list | high (PROBE + DOCS + BINARY) |
| Q2 | ExitPlanMode approval | Mode becomes `prePlanMode ?? "default"` (host can override with `updatedPermissions:[{type:"setMode",…}]`), and the CLI emits `{"type":"system","subtype":"status","status":null,"permissionMode":"<new>"}` on every mode change. `set_permission_mode` works mid-session and emits the same frame. **A live session stays out of plan mode for every later turn** | high (PROBE) |
| Q3 | Modes / bypass | SDK enum: `default acceptEdits bypassPermissions plan dontAsk auto`; CLI also accepts `manual` (= `default`). `--dangerously-skip-permissions` **overrides** `--permission-mode` (probe: `plan` + flag → `bypassPermissions`). `auto` on an unsupported model silently runs as `default` | high (PROBE + BINARY + DOCS) |
| Q4 | Compaction | `system/status{status:"compacting"}` (re-sent every 30 s while running) → `system/status{status:null, compact_result:"success"\|"failed", compact_error?}` → `system/init` → `system/compact_boundary{compact_metadata:{trigger, pre_tokens, post_tokens?, cumulative_dropped_tokens?, duration_ms?}, logical_parent_uuid?}`. `/compact` ends with a **0-turn result** | high for manual (PROBE); med for auto (BINARY + DOCS) |
| Q5 | Context % | CLI `/context` = `input + cache_creation + cache_read` of the **latest main-thread assistant usage** (+ a local estimate of later messages) over the autocompact window, which equals `modelUsage[<model>].contextWindow` unless a window is configured. Native, zero-API alternative: `get_context_usage{detail:"summary"}` → `percentage`. After `compact_boundary` the usage-based numerator is unknown until the next response | high (PROBE + BINARY) |
| Q6 | Other native signals | `result.permission_denials` (includes Telegram denials and interrupt-cancelled requests), `system/init.permissionMode` (effective mode), initialize `session_state:"requires_action"` / `current_permission_mode`, control-error `error_code`, plan-mode **model switch** (haiku session plans on `claude-sonnet-5-5`) | high (PROBE) |

---

## Q1. Control protocol: `can_use_tool`, `control_response`, `control_cancel_request` (#684, #685, #383)

### Finding

1. **`can_use_tool` request shape** (DOCS `SDKControlPermissionRequest`, TS 0.3.285; PROBE Z4):
   ```
   {"type":"control_request","request_id":"<uuid v4>","request":{
     "subtype":"can_use_tool","tool_name","input",
     "tool_use_id",                       // required; always present
     "permission_suggestions"?: PermissionUpdate[],   // e.g. addRules / addDirectories / setMode acceptEdits
     "blocked_path"?, "decision_reason"?, "decision_reason_type"?: rule|mode|subcommandResults|
        permissionPromptTool|hook|asyncAgent|sandboxOverride|workingDir|safetyCheck|classifier|other,
     "classifier_approvable"?, "suppress_always_allow_rule"?, "default_to_no"?,
     "matched_ask_rule"?: {source, tool_name, rule_content?},
     "title"?, "display_name"?, "description"?, "agent_id"?,   // agent_id = subagent id
     "mcp_server"?: {name, source}, "requires_user_interaction"? }}
   ```
   PROBE Z4 (Bash, default mode): `display_name:"Bash"`, `description:"probe"`,
   `permission_suggestions:[addRules Bash(touch …) → localSettings, addDirectories → session,
   setMode acceptEdits → session]`, `blocked_path`, `tool_use_id:"toolu_fake1"`.
   PROBE Z7 (ExitPlanMode): `input:{}` (empty, although the model's tool_use carried `plan`),
   `requires_user_interaction:true`, no suggestions.
   CLI-issued `request_id`s are UUID v4 (PROBE). TS SDK host-issued ids are 13 random base36
   chars; Python host-issued ids are `req_<n>_<8 hex>` (SDK-SRC). Untether's `ut_<feature>_…`
   namespace cannot collide with either.

2. **`control_response` shape** (DOCS `SDKControlResponse` / `ControlResponse` /
   `ControlErrorResponse`, `PermissionResult`):
   ```
   {"type":"control_response","response":{"subtype":"success","request_id","response":{
       "behavior":"allow","updatedInput"?,"updatedPermissions"?,"toolUseID"?,"decisionClassification"?
     | "behavior":"deny","message","interrupt"?,"toolUseID"?,"decisionClassification"? }}}
   {"type":"control_response","response":{"subtype":"error","request_id","error":"<text>"}}
   ```
   `decisionClassification` ∈ `user_temporary | user_permanent | user_reject` (telemetry only).
   The TS SDK always adds `toolUseID` from the request (SDK-SRC); the Python SDK echoes the
   original `input` as `updatedInput` when the callback returns none (SDK-SRC). CLI error
   responses can carry an extra `error_code` (PROBE Z2: `"error_code":"bypass_not_launched"`,
   not in the SDK type).

3. **`control_cancel_request`: the CLI sends it.** DOCS (`SDKControlCancelRequest`): *"Tells the
   other side that the sender no longer needs the answer to one of its own in-flight
   control_requests (for example a pending can_use_tool prompt after the turn was interrupted, or
   one that another client already answered). Either side may send it … The sender stops waiting
   at once and ignores any control_response that still arrives for that request_id … There is no
   reply to the cancel itself."* It is part of `StdoutMessage` (CLI → host).
   - **BINARY** (structuredIO `sendRequest`): the pending request's abort listener calls
     `this.enqueueCancelRequest(r)` → `{type:"control_cancel_request",request_id:s}` and rejects
     the pending promise. `injectControlResponse` (another client answered) also enqueues a cancel.
     A terminal shutdown (`eo()`) skips the cancel.
   - **PROBE Z4 (interrupt):** can_use_tool pending → host sends `interrupt` → within 1 ms the CLI
     writes `{"type":"control_cancel_request","request_id":"<the can_use_tool id>"}` (no
     `session_id`), then the interrupt response `{"still_queued":[]}`, a synthetic
     `tool_result` *"The user doesn't want to proceed with this tool use…"*, a user text
     `[Request interrupted by user for tool use]`, a result with
     `subtype:"error_during_execution"`, `terminal_reason:"aborted_tools"`,
     `permission_denials:[{tool_name:"Bash",…}]`, then `command_lifecycle{state:"cancelled"}`.
     A late `allow` for the cancelled id was **ignored** (the `touch` target never appeared). The
     process then exited **rc=1** on stdin close.
   - Stdin close does **not** produce a cancel (PROBE Z5, below).

4. **How each SDK handles the cancel** (SDK-SRC):
   - TS 0.3.285: `readMessages` routes `control_cancel_request` → `handleControlCancelRequest`
     → `cancelControllers.get(request_id).abort()`. `canUseTool` receives that `AbortSignal` as
     `options.signal`. If the callback still returns, the SDK still writes a response (the CLI
     ignores it). Duplicate delivery of an in-flight `request_id` is skipped.
   - Python v0.2.162 (`query.py`): pops the in-flight anyio task and `cancel()`s it; on
     cancellation **no response is written** (*"the CLI has already abandoned this request"*).
     `ToolPermissionContext.signal` is still `None` (`# TODO: Add abort signal support`).

5. **Timeouts: none on the permission path.**
   - DOCS (`CanUseTool`, TS): *"Fail-closed: an accidental null means no control_response is
     sent and the tool stays blocked indefinitely — permission prompts have no park deadline."*
   - BINARY: `sendRequest` for `can_use_tool` passes only the abort signal, no timer.
   - PROBE Z6: a can_use_tool left unanswered for **130 s** produced no cancel, no error and no
     timeout; the turn simply stayed open.
   - Host-initiated requests (host → CLI) do time out on the SDK side: Python `_send_control_request`
     60 s default (`initialize_timeout` 60 s); TS has no timer, only the caller's signal.

6. **If the host never answers:** the tool call blocks forever; `session_state` becomes
   `requires_action`. On **stdin close** the CLI rejects every pending request with
   `AbortError: Tool permission stream closed before response received`, which the model sees as an
   error `tool_result` (`"Tool permission request failed: AbortError: …"`), and **the model runs
   one more API call** before the turn ends (PROBE Z5, Z6). No cancel frame is written.

7. **Native "is it still answerable?" signals:**
   - `control_cancel_request` (edge signal, above).
   - A re-sent `initialize` (host → CLI) returns, on the **envelope** (sibling of `response`),
     `pending_permission_requests: SDKControlRequest[]` (and `pending_user_dialog_requests`),
     plus `response.session_state:"requires_action"` and `response.current_permission_mode`
     (PROBE Z6b, CLI ≥ 2.1.268 per DOCS). Absence from the list = not answerable. Caveat (DOCS):
     re-initialize re-registers the host's hooks and resolves any hook callback still waiting;
     Untether registers none (`"hooks": None`, `runners/claude.py:6349`), so this is benign.
   - A duplicate `control_response` for an already-answered id is silently ignored; the CLI
     emits nothing (PROBE Z3). There is no "already handled" echo.

### Implication for Untether

- **#684:** the event-driven expiry sweep can be replaced by the native edge.
  `StreamControlCancelRequest` is already decoded (`schemas/claude.py:380`) but `translate`
  drops it (no `case` in `runners/claude.py`). Handle it: resolve the pending request as
  `cancelled`, strip its Telegram buttons ("⏹️ no longer needed — the turn was interrupted"),
  clear `_REQUEST_TO_*`/`pending_control_requests`, and do **not** write a response. Keep a
  detect-only `control_request.unanswerable` backstop, because a cancel is only sent on abort or
  when another client answers; a lone request with no turn activity still waits forever. The
  `initialize` re-send is a cheap reconciliation probe (for example on the watchdog tick or before
  expiring), not a replacement. **Never auto-deny on a timer while a turn is legitimately waiting**:
  the CLI has no deadline, so the 5-minute `CONTROL_REQUEST_TIMEOUT_SECONDS` is Untether's own
  policy.
- **#685:** a second tap can be classified locally: *sent* (first write), *already handled*
  (id in `_HANDLED_REQUESTS`), *cancelled by CLI* (id seen in a `control_cancel_request`), or
  *not found*. The CLI gives no feedback on a duplicate write, so the three-way result must come
  from Untether's own state plus the cancel set.
- **Closing stdin with an approval pending costs one more model call** and records the tool as
  denied. `/cancel` of a turn with a pending approval should `interrupt` (clean cancel,
  `aborted_tools`) rather than close stdin.
- Exit code after an interrupted last turn is **1** (PROBE Z4). Check it is not read as a crash
  by the rc-based paths (#791 clean-idle-close, auto-continue signal-death test).

## Q2. ExitPlanMode approval and permission-mode reporting (#383)

### Finding

- **BINARY** (ExitPlanModeV2 `call`): when the mode is `plan`, approval sets
  `mode = prePlanMode ?? "default"` (with an auto-mode gate fallback to `default`) and fires
  `trigger:"exit_plan_mode"` telemetry. A session **started** with `--permission-mode plan` has
  no `prePlanMode`, so it lands in **`default`**.
- **BINARY**: any `toolPermissionContext.mode` change calls
  `sessionState.notifyPermissionModeChanged` → in print (stream-json) mode the handler enqueues
  `{type:"system",subtype:"status",status:null,permissionMode:<mode>,uuid,session_id}`.
- **PROBE Z7** (plan mode, fake ExitPlanMode tool_use, host answers `allow`): immediately after the
  approval and **before** the tool_result the CLI emitted
  `{"type":"system","subtype":"status","status":null,"permissionMode":"default"}`. The tool_result
  was *"User has approved your plan. You can now start coding…"*.
- **PROBE Z8** (same, but `updatedPermissions:[{"type":"setMode","mode":"acceptEdits","destination":"session"}]`):
  one status frame with `"permissionMode":"acceptEdits"`. So **the host chooses the post-plan mode**
  (the terminal's "Yes, auto-accept edits" option is exactly this `setMode`).
  With `updatedInput:{}` the tool_result was *"User has approved exiting plan mode"* and
  `tool_use_result.plan` was `null` (#793 context: echo the plan if you want it recorded).
- **`set_permission_mode`** is typed in both SDKs (TS `SDKControlSetPermissionModeRequest`,
  Python `set_permission_mode()` → `{"subtype":"set_permission_mode","mode":…}`).
  PROBE Z2: `plan → default` answered `{"mode":"default"}` and emitted the same `system/status`
  frame; `bypassPermissions` was refused with
  `"Cannot set permission mode to bypassPermissions because the session was not launched with --dangerously-skip-permissions"`,
  `error_code:"bypass_not_launched"`; `manual` answered `{"mode":"default"}` and emitted nothing
  (no change).
- **Per-turn reporting:** `system/init.permissionMode` is emitted at the start of **every** turn
  (DOCS `SDKSystemMessage`: "at the start of each turn"; PROBE: a second `system/init` also
  appears after `/compact`). `system/status.permissionMode` is the edge signal between turns.
  Initialize responses carry `current_permission_mode`.
- DOCS (permission-modes, "Review and approve a plan"): *"Approving a plan exits plan mode and
  switches the session to the permission mode each approve option describes … To plan again,
  cycle back to plan mode with Shift+Tab, or prefix your next prompt with /plan."*

### Implication for Untether (#383)

- The bug is wider than `_PLAN_EXIT_APPROVED`. Under live sessions (#776) **the CLI itself stays in
  `default` for every later follow-up and wake turn** after one plan approval, for up to 4 h.
  Before #776 each message re-spawned with `--permission-mode plan`.
- Fix shape: at each follow-up/wake turn boundary, if the chat's configured mode is `plan` or
  `plan-auto` and the effective mode (last `system/status.permissionMode` or
  `system/init.permissionMode`) is not `plan`, send `set_permission_mode {"mode":"plan"}` (reuse
  the `ut_<feature>_<sid>_<seq>` namespace, e.g. `ut_rearm_plan_…`) **before** writing the
  follow-up. Clear `_PLAN_EXIT_APPROVED` on the same boundary, or better, when a status/init frame
  reports `plan`.
- Parse `system/status` with `permissionMode` (already a field on `StreamSystemMessage`) and track
  the effective mode per session. This also gives the footer's mode label a truthful source.
- Button wording can state the real consequence: "Approve → Claude edits with prompts" (`default`)
  vs an optional "Approve + auto-accept edits" (`setMode acceptEdits`).

## Q3. Permission modes and bypass flags (#209, #747, #751)

### Finding

- **Enums.** TS 0.3.285 and Python v0.2.162 `PermissionMode` =
  `default | acceptEdits | bypassPermissions | plan | dontAsk | auto` (DOCS). CLI 2.1.285
  `--help` choices: `acceptEdits, auto, bypassPermissions, manual, dontAsk, plan`; `default` is
  accepted but not advertised; `manual` is an alias of `default` (DOCS permission-modes: *"The CLI
  accepts `manual` as an alias"*; PROBE Z2: `set_permission_mode manual` → `{"mode":"default"}`).
- **Official descriptions** (DOCS permission-modes, "Available modes", 2026-09-30), for #747 wording:

  | Mode | What runs without asking | Best for |
  |---|---|---|
  | `default` (shown as **Manual**) | Reads only | Reviewing every action yourself, sensitive work |
  | `acceptEdits` | Reads, file edits, and common filesystem commands (`mkdir`, `touch`, `mv`, `cp`, etc.) | Iterating on code you're reviewing |
  | `plan` | Reads, plus classifier-approved commands when auto mode is available | Exploring a codebase before changing it |
  | `auto` | Everything, with background safety checks | Long tasks, reducing prompt fatigue |
  | `dontAsk` | Reads and pre-approved tools; anything that would prompt is denied | Locked-down CI and scripts |
  | `bypassPermissions` | Everything | Isolated containers and VMs only |

  SDK one-liners (TS `PermissionMode` docstring): *default – "prompts for dangerous operations";
  acceptEdits – "Auto-accept file edit operations"; bypassPermissions – "Bypass all permission
  checks (requires allowDangerouslySkipPermissions)"; plan – "no actual tool execution";
  dontAsk – "Don't prompt for permissions, deny if not pre-approved"; auto – "Use a model
  classifier to approve/deny permission prompts".* "acceptEdits = run freely" is therefore wrong.
- **`auto` mode** (DOCS): a separate classifier model reviews actions; explicit `ask` rules still
  prompt; requires Opus 4.6+/Sonnet 4.6+/Fable on the Anthropic API (**Haiku unsupported**);
  3 blocks in a row or 20 total pause auto mode and **fall back to prompting** (under
  `--permission-prompt-tool stdio` that is a normal Telegram approval); critical-path `rm` in
  `-p` is denied immediately. Default starting mode for interactive terminal/VS Code sessions on
  CLI ≥ 2.1.283.
- **The CLI does not warn in `-p` when `auto` can't be used.** PROBE Z9: `--permission-mode auto
  --model haiku` → `system/init.permissionMode:"default"`, nothing on stderr, no
  `system/notification`; with `--model opus` → `"auto"`. BINARY: the killswitch/unavailable
  paths log via debug only, and the plan-exit fallback posts a TUI-only notification.
- **Mode precedence** (BINARY `RLr`, the first candidate wins): `--dangerously-skip-permissions` →
  `--permission-mode` → agent frontmatter `permissionMode` → settings `permissions.defaultMode`
  (bypass/auto from project/local settings ignored) → built-in default. A settings
  `permissions.disableBypassPermissionsMode:"disable"` skips any `bypassPermissions` candidate.
  PROBE Z9: `--permission-mode plan --dangerously-skip-permissions` → **`bypassPermissions`**;
  `plan --allow-dangerously-skip-permissions` → `plan`;
  `plan --settings '{"permissions":{"defaultMode":"bypassPermissions"}}'` → `plan`;
  `default --permission-prompts none` → `default`.
- **Flags/options that weaken Untether's approval flow if passed through `extra_args`**
  (BINARY + `claude --help` + DOCS; TS SDK maps `allowDangerouslySkipPermissions` →
  `--allow-dangerously-skip-permissions`, `canUseTool` → `--permission-prompt-tool stdio`,
  `permissionPrompts` → `--permission-prompts`; the Python SDK has no bypass option and uses
  `extra_args`):

  | Flag | Effect | Severity for Untether |
  |---|---|---|
  | `--dangerously-skip-permissions` | Forces `bypassPermissions`, **overriding** Untether's `--permission-mode` | critical — reserve |
  | `--allow-dangerously-skip-permissions` | Makes `bypassPermissions` reachable later (`set_permission_mode`, plan-exit, settings `defaultMode` in some paths) | high — reserve |
  | `--permission-prompts none` | Every would-prompt is auto-denied; Telegram approvals never appear (fail-closed, breaks the flow) | medium — reserve |
  | `--permission-prompt-tool <mcp tool>` | Redirects stage 6 away from Untether | already reserved |
  | `--allowedTools` / `--allowed-tools` | Pre-approves at stage 5; undoes #749 in prompting modes | high — reserve or merge |
  | `--settings <json\|file>` | Can inject `permissions.allow` rules and `PreToolUse` hooks returning `allow` (stage 1); `defaultMode` loses to `--permission-mode` | high — can't fully denylist; document |
  | `--plugin-dir`, `--plugin-url` | Plugins bring hooks (stage-1 allow) | medium — document |
  | `--agents`, `--agent` | Agent frontmatter `permissionMode` (can't widen an inherited mode; BINARY warning *"would widen the agent view's inherited mode"*) | low |
  | `--add-dir` | Widens the directories that `acceptEdits` and reads cover | low–medium |
  | `--mcp-config` | New tools (still permission-checked) | low |
  | `--bare`, `--safe-mode` | Skip hooks, so the user's own security hooks stop running | low–medium |
  | `--disallowedTools`, `--restricted` | Strengthen, never weaken | none |
  | `--setting-sources` | Can drop the user settings that hold deny/ask rules | low–medium |

### Implication for Untether

- **#209:** add `--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`,
  `--permission-prompts` (and `=` forms) to `_RESERVED_FLAGS` / `_RESERVED_PREFIXES`; decide on
  `--allowedTools`/`--allowed-tools`. Document that `--settings`, `--plugin-dir` and
  `--setting-sources` can't be fully denylisted (same posture as Codex `-c`). Optional
  belt-and-braces for non-bypass modes: pass `--settings
  '{"permissions":{"disableBypassPermissionsMode":"disable"}}'` (BINARY: skips every bypass
  candidate; UNVERIFIED how a second `--settings` from `extra_args` merges).
- **#751:** the CLI will not warn, so Untether must. At startup, and at cron dispatch, warn for
  `auto` (and for unattended crons in prompting modes). At run time compare the requested mode with
  `system/init.permissionMode` and log/notify once when they differ (e.g. `auto` on Haiku → `default`).
- **#747:** use the DOCS table wording above; `off` → `acceptEdits` must say "file edits and
  common filesystem commands run; other commands still ask", not "run freely".

## Q4. Compaction events (#819)

### Finding

- **Types (DOCS, TS 0.3.285):**
  - `SDKCompactBoundaryMessage` = `{type:"system", subtype:"compact_boundary",
    compact_metadata:{trigger:"manual"|"auto", pre_tokens:number, post_tokens?:number,
    duration_ms?:number, preserved_segment?, preserved_messages?}, uuid, session_id}`.
  - `SDKStatusMessage` = `{type:"system", subtype:"status", status:"compacting"|"requesting"|null,
    permissionMode?, compact_result?:"success"|"failed", compact_error?:string, uuid, session_id}`.
  - Hooks: `PreCompact` and `PostCompact` (`PostCompactHookInput{trigger, compact_summary}`); per
    rc14 finding A2 they never emit `hook_started`.
- **BINARY** (compact metadata mapper): the wire object also carries
  `cumulative_dropped_tokens`, `user_context`, `messages_summarized`, `precomputed`,
  `pre_compact_discovered_tools`, `pre_compact_artifact_read_versions`, and the frame may carry
  `logical_parent_uuid`. `status:"compacting"` is emitted at compaction start and **re-emitted
  every 30 000 ms** by a heartbeat while compaction runs (`setInterval(…,30000)`); the end is
  `status:null` with `compact_result` (`compact_error` on failure; a PreCompact-hook skip ends
  with plain `status:null`). `status:"requesting"` is emitted per API request start on some
  surfaces (DOCS enum); **not observed** in these `-p` probes.
- **PROBE Z10** (`/compact` sent as a stream-json user line, fake API), exact order:
  ```
  command_lifecycle{queued}, command_lifecycle{started}
  system/status {"status":"compacting"}
  system/status {"status":null,"compact_result":"success"}
  system/init   (a fresh one, mid-command)
  system/compact_boundary {"compact_metadata":{"trigger":"manual","pre_tokens":6336,"post_tokens":277,
                           "cumulative_dropped_tokens":6059,"duration_ms":47},"logical_parent_uuid":"…"}
  user          (the "This session is being continued from a previous conversation…" summary, not isReplay)
  user          {"content":"<local-command-stdout>Compacted </local-command-stdout>","isReplay":true}
  result        {"subtype":"success","num_turns":0,"result":"","usage":{all zeros}, no terminal_reason}
  command_lifecycle{completed}
  ```
  `pre_tokens` 6336 = the last response's `input 1234 + cache_creation 100 + cache_read 5000 +
  output 2`. `post_tokens` 277 is an estimate of the **post-compaction message list only**; it
  excludes the system prompt and tools (the `get_context_usage` total straight after was 8753).
- **Auto-compaction** could not be forced through the fake API (a 186k-token usage on Haiku did not
  trigger it). Shape from DOCS/BINARY: `trigger:"auto"`, same status/boundary frames, fired
  **inside a turn** before the next API request (or reactively on prompt-too-long).

### Implication for Untether (#819)

- Rows: `status:"compacting"` → `🗜️ Compacting context…`; `compact_boundary` →
  `🗜️ Context compacted · <pre_tokens> → <post_tokens> tokens (<trigger>)` (omit the arrow when
  `post_tokens` is absent); `status:null` + `compact_result:"failed"` → failure row with
  `compact_error`.
- Liveness: `status:"compacting"` and its 30 s heartbeat are native liveness. Count every
  `system/status` frame as activity and suppress stall warnings while the last status is
  `compacting`.
- `/compact` from Telegram yields a **0-turn, empty-text result**. Make sure the #631 empty-resume
  heuristic, the live-session resume guard and the "empty answer" final don't misfire (cost is
  non-zero, `total_cost_usd` rises by the compaction call).
- Add `compact_metadata`, `compact_result`, `compact_error`, `logical_parent_uuid` to
  `StreamSystemMessage` (typed `Any`, like the #814 fields).

## Q5. Context-window math (#819 Track B)

### Finding

- **What the CLI counts (BINARY):**
  - `Dx(usage) = input_tokens + cache_creation_input_tokens + cache_read_input_tokens + output_tokens`,
    taken from the **latest assistant message that carries usage**, skipping `<synthetic>`-model
    and unmetered messages. Consecutive stream frames sharing a `message.id` are one response.
  - The usage lookup **returns 0 as soon as it walks back past a compact boundary**
    (`if(isCompactBoundary(r)) return 0`), so right after compaction the CLI has no usage number
    until the next response.
  - `/context` / `get_context_usage`: `totalTokens ≈ (input + cache_creation + cache_read of the
    last usage) + a local estimate of messages after it`; `percentage =
    Math.round(totalTokens / window * 100)`; `maxTokens = rawMaxTokens = window`, where `window`
    is the **autocompact window** (`env CLAUDE_CODE_AUTO_COMPACT_WINDOW` → settings
    `autoCompactWindow` / `--autocompact` → client data → experiment → model default). Normally
    this is the model's context window, but it can be clamped (the SDK doc cites "the 200K boundary
    on 1M-window models").
  - `autoCompactThreshold = window − min(maxOutputTokens, 20000) − 13000`.
  - The terminal status line only appears near the limit: `"<n>% until auto-compact"`, where
    `n = round((threshold − used)/threshold × 100)` and `used = Dx + trailing estimate`; it falls
    back to `"<n>% context used"` when auto-compact isn't enforced. It warns from `threshold − 20000`.
- **PROBE Z1/Z10/Z11** (`get_context_usage {"detail":"summary"}`):

  | Model | window (`rawMaxTokens`) | `autoCompactThreshold` | `autocompactSource` |
  |---|---|---|---|
  | haiku → `claude-haiku-4-5-20251001` | 200 000 | 167 000 | `auto` |
  | opus → `claude-opus-5-5` | 1 000 000 | 967 000 | `model-default` |
  | `sonnet[1m]` → `claude-sonnet-5-5[1m]` | 1 000 000 | 967 000 | `model-default` |

  With a fake usage of `1234 + 100 + 185000` (+2 output) on Haiku: `totalTokens 186334,
  percentage 93` (so input-side only, over 200 000). The summary call made **no API request** (the
  fake API logged none) and answered in < 100 ms; `apiUsage` echoes the last response's usage and
  is `null` right after a compaction.
- **`modelUsage.<model>.contextWindow`** (DOCS `ModelUsage`; BINARY `contextWindow:Wg(model)`)
  is the model's raw window, and `maxOutputTokens` its default output cap (Haiku 200 000 / 32 000;
  Sonnet 5.5 1 000 000 / 128 000, PROBE). It appears **only on `result`** (cumulative per model,
  keyed by the full model id, which equals `message.model` and `system/init.model`, PROBE).
  `system/init` and the initialize `models` list carry no window (DOCS `ModelInfo`).
- `result.usage` is **not** a context measure: DOCS *"MAIN AGENT LOOP ONLY — excludes Task
  subagent … and is per-turn in streaming-input sessions"* (a per-turn sum across API calls).
- **Plan-mode model switch (PROBE Z7):** a `--model haiku` session in `plan` mode ran its
  planning call on `claude-sonnet-5-5` (1M window), then returned to Haiku after approval, and
  `modelUsage` gained both entries. So the denominator must follow the **model of the assistant
  message** that supplied the usage, not `system/init.model`.
- Subagent frames carry non-null `parent_tool_use_id` (DOCS `SDKAssistantMessage`) and must be
  excluded. The CLI itself counts only the main thread.

### Recommended formula

```
on assistant frame where parent_tool_use_id is null and message.model != "<synthetic>":
    u = message.usage
    used  = u.input_tokens + u.cache_creation_input_tokens + u.cache_read_input_tokens
            (+ u.output_tokens if you want Dx; the difference is one response's output and
             matches compact_metadata.pre_tokens)
    model = message.model
on result:  window[model] = result.modelUsage[model].contextWindow   (remember per model)
on compact_boundary: used = None   # unknown until the next main-thread assistant usage
pct = round(used / window[model] * 100)  if used and window known  else omit
fallback window before the first result: omit (brief) — or 1 000 000 when model id ends in
    "[1m]" or is a known native-1M model (claude-opus-5-5, claude-sonnet-5-5), else 200 000
```

Exact-match option: after each `result` (and after a `compact_boundary`), send
`{"type":"control_request","request_id":"ut_ctx_<sid>_<seq>","request":{"subtype":"get_context_usage","detail":"summary"}}`
and render `response.percentage`. It is the CLI's own `/context` number, handles configured
windows and post-compaction estimates, and costs no API call. It needs a parsed response (unlike
the fire-and-forget #365 path) and a feature check (missing on older CLIs → fall back to the
formula).

### Implication for Untether (#819)

- Render `NN% ctx` from the formula; prefer `get_context_usage` when available. The number
  matches `/context` in the terminal. The terminal's status-line number is "% until
  auto-compact" (threshold-based), which is a different quantity; don't try to match that.
- Don't show `post_tokens / window` after compaction: `post_tokens` excludes the system prompt and
  tools, so it under-reports. Show nothing (or the `get_context_usage` number) until the next response.
- Add `modelUsage` and `permission_denials` to `StreamResultMessage`.

## Q6. Other native signals that bear on these issues

- `result.permission_denials: [{tool_name, tool_use_id, tool_input}]` is authoritative (DOCS) and
  includes Telegram denials, stdin-close rejections and interrupt-cancelled requests (PROBE
  Z3–Z5). Usable for #684/#685 reconciliation and a "N actions denied" footer.
- `system/permission_denied{tool_name, tool_use_id, agent_id?, decision_reason_type?,
  decision_reason?, message}` (DOCS): auto-denials that never reach `can_use_tool` (auto
  classifier, `dontAsk`, deny rules). Relevant to #751 (auto mode visibility). Not observed in these probes.
- `can_use_tool.decision_reason_type` / `classifier_approvable` / `matched_ask_rule` /
  `default_to_no` / `suppress_always_allow_rule`: auto-mode escalations can be labelled
  ("classifier fallback", "safety check") and "Always allow" suppressed where the CLI says so.
- `system/init.permissionMode` = the effective mode per turn; initialize response
  `current_permission_mode` and `session_state` (`requires_action` while a prompt is pending).
- `system/session_state_changed` (DOCS) was **not emitted** in these `-p` probes (rc14 finding:
  remote/CCR tee only). Don't depend on it.
- `autocompact_state` (a top-level `type`, `{enabled, effective_window, threshold, enforced,
  source}`) exists in the binary but is only wired when `CLAUDE_CODE_REMOTE` is set. Not
  available to Untether.
- The Python SDK marks its permission callback `signal=None` (TODO), so its behaviour is "task
  cancelled, no response written". The TS SDK is the better reference for cancel semantics.

## Drift probes to add (`tests/test_claude_cli_schema_drift.py`)

Pin literals, never minified names. Group by the plan that depends on them.

- **#684 / #685 (Q1):**
  - `type:"control_cancel_request",request_id:` (7 lines)
  - `Tool permission stream closed before response received` (2)
  - `pending_permission_requests` (10)
  - `enqueueCancelRequest(` (2; a class method name, stable across minification so far)
- **#383 (Q2):**
  - `subtype:"status",status:null,permissionMode:` (2)
  - `trigger:"exit_plan_mode"` (3)
  - `prePlanMode??"default"` (1)
  - `bypass_not_launched` (4)
- **#209 / #751 / #747 (Q3):** the cached `claude --help` probe must list
  `--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions` and
  `--permission-prompts`, and keep the #741 mode-enum re-derivation. Binary literals:
  - `Cannot set permission mode to bypassPermissions because the session was not launched with --dangerously-skip-permissions` (2)
  - `disableBypassPermissionsMode` (13)
- **#819 (Q4):**
  - `subtype:"compact_boundary"` (5)
  - `compact_metadata` (6)
  - `pre_tokens:` (3)
  - `post_tokens` (4)
  - `cumulative_dropped_tokens:` (2)
  - `type:"sdk_status",status:"compacting"` (5)
  - `compact_result` (6)
  - `compact_error:` (4)
- **#819 (Q5):**
  - `get_context_usage` (13)
  - `'summary' answers from the last response's usage` (2)
  - `rawMaxTokens` (8)
  - `autoCompactThreshold` (7)
  - `contextWindow` (10)
  - `% until auto-compact` (4)
  - `% context used` (2)

(Counts are `LC_ALL=C grep -acF '<needle>' $B`, i.e. matching "lines" of the binary.)

## Open unknowns / needs a live capture

None of these were run. Each needs a real model turn (paid). Use `@untether_dev_bot` or a
scratch dir with a cheap model.

1. **Auto-compaction frame order** (`trigger:"auto"`), including the 30 s `compacting` heartbeat
   and whether it lands mid-turn between tool calls. Capture:
   ```
   cd "$(mktemp -d)" && claude -p --input-format stream-json --output-format stream-json --verbose \
     --permission-mode bypassPermissions --model haiku --autocompact 100k \
     < prompts.jsonl > tests/fixtures/claude_autocompact_2.1.285.jsonl
   ```
   Here `prompts.jsonl` holds several `{"type":"user",…}` lines that each ask Claude to `Read`
   a ~60 KB file. `--autocompact 100k` is the documented minimum; that is ~5–6 turns on Haiku.
   Also capture a manual `/compact` with real usage, to confirm `post_tokens` against a real
   summary (the fake-API value was only an estimate).
2. **`get_context_usage` while a turn is running** (answered mid-turn, or queued until the turn
   ends?) and its latency on a real session with MCP servers. Probe: send it right after an
   `assistant` frame of a multi-tool turn.
3. **`control_cancel_request` for a subagent's pending `can_use_tool`** when the parent turn is
   interrupted, and whether one arrives when a sibling tool in the same batch fails.
4. **`system/status{status:"requesting"}`** in real `-p` sessions (not seen with the fake API).
   If it appears, Untether must ignore it or use it as liveness.
5. **Real-model plan-mode model switch.** Confirm on the subscription that `--model haiku` + `plan`
   plans on Sonnet 5.5 (PROBE used the fake API; the model id came from the CLI's own choice).
6. **`--settings` merge** when Untether and `extra_args` both pass one (for the optional
   `disableBypassPermissionsMode` guard). Zero-token probe with the fake API:
   `--settings A --settings B`, then read `system/init.permissionMode`.

## Appendix: probes (CLI 2.1.285, lba-1, 2026-09-30, zero token cost)

Fake API: `ThreadingHTTPServer` on 127.0.0.1. `POST /v1/messages` answers SSE
`message_start(usage{input 1234, cache_creation 100, cache_read 5000, output 1})` →
`content_block_*` (`tool_use` Bash `touch …/SHOULD_NOT_EXIST` / ExitPlanMode `{plan}`, or text
"ok" once a `tool_result` is in the request) → `message_delta(stop_reason, output_tokens)` →
`message_stop`. `count_tokens` returns `{"input_tokens":10}`. Driver env:
`ANTHROPIC_BASE_URL=http://127.0.0.1:<port> CLAUDE_CODE_MAX_RETRIES=0 DISABLE_TELEMETRY=1
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `--tools Bash` (or `Bash,ExitPlanMode` / `Read`).
The host first sends `{"type":"control_request","request_id":"ut_init","request":{"subtype":"initialize"}}`.

| # | Setup | Observation |
|---|---|---|
| Z1 | blackholed; `plan` / `default`; `get_context_usage summary`, no user message | Window and threshold table (Q5); the plan-mode context model was `claude-sonnet-5-5` for a `--model haiku` session |
| Z2 | blackholed; `set_permission_mode` default → bypassPermissions → manual | `{"mode":"default"}` + `system/status{status:null,permissionMode:"default"}`; bypass error `bypass_not_launched`; manual → `{"mode":"default"}`, no status |
| Z3 | fake; Bash can_use_tool; host sends `deny` twice, then `get_context_usage` | First deny → error tool_result, `tool_result_meta[].non_execution_kind:"permission-rule"`; second ignored silently; `permission_denials` lists Bash |
| Z4 | fake; Bash can_use_tool; host sends `interrupt` after 3 s, then a late `allow` | `control_cancel_request{request_id}`; interrupt `{"still_queued":[]}`; result `error_during_execution`/`aborted_tools`; late allow ignored (no file); rc=1 on close |
| Z5 | fake; can_use_tool pending; stdin closed | Error tool_result "Tool permission stream closed before response received"; model called again; result success; no cancel frame; rc=0 |
| Z6 | fake; can_use_tool unanswered 130 s | No cancel/timeout; stdin close → as Z5 |
| Z6b | fake; can_use_tool pending; host re-sends `initialize` | Envelope `pending_permission_requests:[<the can_use_tool>]`; `response.session_state:"requires_action"`, `current_permission_mode:"default"` |
| Z7 | fake; `plan`; ExitPlanMode can_use_tool (`input:{}`, `requires_user_interaction:true`); host `allow` with `updatedInput:{plan}` | `system/status{permissionMode:"default"}` before the tool_result; `modelUsage` has `claude-sonnet-5-5` (1M/128k) and haiku (200k/32k) |
| Z8 | as Z7 with `updatedPermissions:[setMode acceptEdits session]`, `updatedInput:{}` | One `system/status{permissionMode:"acceptEdits"}`; tool_result "approved exiting plan mode", `plan:null` |
| Z9 | fake; `--permission-mode` X + extra flag | auto+haiku → init `default`; auto+opus → `auto`; plan+`--dangerously-skip-permissions` → `bypassPermissions`; plan+`--allow-dangerously-skip-permissions` → `plan`; plan+settings defaultMode bypass → `plan`; default+`--permission-prompts none` → `default` |
| Z10 | fake; "hello there", `get_context_usage`, `/compact`, `get_context_usage` | Q4 frame order; context 8373 → 8753 (apiUsage null after compaction); 2 API POSTs only |
| Z11 | fake with cache_read 185 000 on the first call | `get_context_usage` 186334/200000 = 93 %; no auto-compaction on the next turn |
