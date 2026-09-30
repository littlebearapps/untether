---
question: >-
  For the 0.35.5rc14 fixes (#812 async hooks, #814 safeguard stops, #806/#813
  SDK facts, #808 config isolation), what does Claude Code CLI 2.1.284 actually
  emit and do in `-p --input-format stream-json --output-format stream-json`?
date: 2026-09-29
sources:
  - https://code.claude.com/docs/en/hooks
  - https://code.claude.com/docs/en/cli-reference.md
  - https://code.claude.com/docs/en/headless.md
  - https://code.claude.com/docs/en/agent-sdk/typescript
  - https://code.claude.com/docs/en/model-config.md
  - https://code.claude.com/docs/en/settings-reference.md
  - https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback
  - https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/handle-streaming-refusals
  - https://support.claude.com/en/articles/14604842-real-time-cyber-safeguards-on-claude
  - https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md
  - https://github.com/anthropics/claude-agent-sdk-python (main @ 37422c2, 2026-09-28)
  - binary: ~/.local/share/claude/versions/2.1.284 (strings)
  - zero-token live probes P1-P5 (appendix)
confidence: high (hooks, interrupt, TaskOutput, config); med (refusal stream shape — not live-probed)
---

# Claude Code 2.1.284 CLI surface for rc14 (#812, #814, #806, #813, #808)

**Date:** 2026-09-29 · **CLI:** Claude Code 2.1.284 (`claude --version`) · **Host:** lba-1 ·
**Binary:** `readlink -f $(which claude)` = `/home/nathan/.local/share/claude/versions/2.1.284`

## Method and labels

- **VERIFIED-DOCS**: official docs / SDK source, URL + retrieval date 2026-09-29.
- **VERIFIED-BINARY**: string/code found in the 2.1.284 binary. Extracted with a small mmap
  context dumper (`python3 ctx.py '<needle>' <before> <after>`; the system `grep` is ugrep and
  rejects long `.{0,N}` contexts). Offsets are byte offsets into the binary. Minified identifiers
  are unstable across versions. Quote the literal strings, not the names.
- **VERIFIED-PROBE**: observed live on 2.1.284 with **zero model tokens**. Every probe ran with
  `ANTHROPIC_BASE_URL=http://127.0.0.1:9` (blackholed) and `CLAUDE_CODE_MAX_RETRIES=0`. The
  user prompt was blocked by a `UserPromptSubmit` hook (`exit 2`), so no API request was ever
  made. Every result reported `total_cost_usd: 0` and `duration_api_ms: 0`. Commands are in the
  appendix.
- **UNVERIFIED**: inferred, not confirmed.

Scope of reading: of the ~276k-character hooks page I read only the async/timeout/common-fields/
Setup/JSON-output sections, located by grep. I read the TypeScript SDK reference's message-types,
hook-message, interrupt, TaskOutput/TaskStop and origin sections, not the whole page.

## Summary table

| # | Question | Answer | Label |
|---|---|---|---|
| A1 | `async` / `asyncRewake` | Command-hook fields. `async` runs the hook in the background, and its output is delivered on the **next** turn. `asyncRewake` implies async and, on **exit 2**, **wakes the model immediately** with stderr (or stdout) as a system reminder | DOCS + BINARY + PROBE |
| A1 | Timeouts | Async command hook registered with the hook's `timeout`, default **600 000 ms**. Docs say `timeout` is **not enforced** on `async`, but **is** enforced on `asyncRewake` | DOCS + PROBE |
| A1 | stdin close in `-p` stream-json | Plain `async` hooks are **killed at once**, and `hook_response{outcome:"cancelled",exit_code:1}` is emitted. For pending `asyncRewake` hooks the CLI **waits up to 30 s** (`Rxo=30000`), then exits. A rewake that fires after stdin closed is **dropped**: no turn runs and no `hook_response` reaches stdout | BINARY + PROBE |
| A2 | `--include-hook-events` | Exists. Emits `system/hook_started`, `system/hook_progress` and `system/hook_response`. There is **no async marker**. `SessionStart`/`Setup` events are emitted even without the flag | DOCS + BINARY + PROBE |
| A3 | Rewake in the stream | A self-started turn with no `command_lifecycle` and a fresh `system/init`. The result has `origin:{"kind":"task-notification","producer":"session-task"}`. The model sees a `<task-notification><summary>Stop hook feedback</summary>…` prompt | PROBE |
| A4 | SDK | `includeHookEvents` (TS) / `include_hook_events` (Py) pass `--include-hook-events`. `SDKHook{Started,Progress,Response}Message` are typed. The Python SDK maps them to `HookEventMessage` | DOCS + BINARY |
| A5 | "Pending async hooks?" query | **No such control_request.** The closest is `get_hooks_listing`, a config snapshot where each hook carries `runsInBackground:true` if it is `async` **or** `asyncRewake` | BINARY |
| B1 | API `stop_reason:"refusal"` | HTTP 200, `stop_details{type,category,explanation}`. It can arrive before any output or mid-stream. The recommended handling is to retry on a **different** model | DOCS |
| B2 | Refusal in stream-json | The CLI represents an unrecovered refusal as an assistant message with `message.stop_reason:"refusal"` and `stop_details`. The headless emitter forwards `message` verbatim. Not live-probed | BINARY (+ #814 transcript) · UNVERIFIED-PROBE |
| B3 | `system/informational` | **Emitted on stdout in stream-json**, shape `{type:"system",subtype:"informational",content,level,tool_use_id?,prevent_continuation?,uuid,session_id}`. The "safeguards stopped … continuing once" notice is one of these, with `level:"notice"` | DOCS + BINARY + PROBE |
| B4 | Hint URL | Stable: `https://code.claude.com/docs/en/model-config#automatic-model-fallback`. For CVP: `https://support.claude.com/en/articles/14604842-real-time-cyber-safeguards-on-claude` (also embedded in the CLI) | DOCS + BINARY |
| B5 | Other subtypes | `model_refusal_fallback`, `model_refusal_no_fallback`, `model_fallback`, `informational`, `hook_*`, `permission_denied`, `status`, `notification`, `turn_preempted`, … (list below) | BINARY |
| C1 | `TaskOutput` | **Removed in 2.1.277.** The model reads the task's `output_file` with `Read`. `BashOutput`, `AgentOutputTool` and friends are in the CLI's removed-tools set. `KillShell`/`KillBash` are aliases of `TaskStop` | DOCS + BINARY |
| C2 | Interrupted-turn result | `terminal_reason` is `"aborted_streaming"` or `"aborted_tools"`. The result is usually `subtype:"success"` and sometimes `error_during_execution` with an `[ede_diagnostic]` error. The turn's `command_lifecycle` ends `"cancelled"`. **Classify on `terminal_reason`**, not on subtype/is_error | DOCS (Py SDK) + BINARY |
| D | Config path env | `UNTETHER_CONFIG_PATH`, read at call time. `HOME_CONFIG_PATH` is computed at **import time** from `Path.home()`. `api/oauth/usage` is in the binary (14 hits) | local code + BINARY |

---

## A. #812: async hooks

### A1. `async` / `asyncRewake` semantics

- **VERIFIED-DOCS** (hooks, "Command hook fields" and "Run hooks in the background", retrieved
  2026-09-29): `async` "runs in the background without blocking". It is "only available on
  `type: "command"` hooks". Async hooks "can't block or control Claude's behavior". Their output
  (`additionalContext`, `systemMessage`) "is delivered on the next conversation turn. If the session
  is idle, the response waits until the next user interaction. Exception: an `asyncRewake` hook that
  exits with code 2 wakes Claude immediately even when the session is idle." `asyncRewake`: "runs in
  the background and wakes Claude on exit code 2. The hook's stderr, or stdout if stderr is empty, is
  shown to Claude as a system reminder".
- **VERIFIED-BINARY** (hook schema @198144573): `async:"If true, hook runs in background without
  blocking"`, `asyncRewake:"If true, hook runs in background and wakes the model on exit code 2
  (blocking error). Implies async."`, plus internal `rewakeMessage` / `rewakeSummary` (default
  summary `"Stop hook feedback"`).
- **Which events.** The fields are on the generic command-hook schema, not per event.
  **VERIFIED-PROBE** on `SessionStart` (P1/P2) and `UserPromptSubmit` (P3/P5). The #812 evidence
  shows them on `Stop`/`SubagentStop`/`PostToolUse` (security-guidance plugin).
  **VERIFIED-BINARY** (@207329002, @207334056): the branch is taken when
  `e.async || e.asyncRewake && (!Te() || rJe())`. `Te()` = `!launchOptions.isInteractive()` and
  `rJe()` = `launchOptions.hasStreamingInput()` (@197594062, @197595739). **So in `-p` mode
  `asyncRewake` only backgrounds with `--input-format stream-json`.** With a plain `-p "prompt"`
  it runs synchronously. Untether always uses stream-json input, so it gets the background
  behaviour.
- **Timeouts.**
  - **VERIFIED-DOCS**: the default `timeout` is 600 s for `command` hooks. "Once an async hook is
    running in the background, Claude Code doesn't enforce `timeout` on it. Claude Code still
    enforces `timeout` on a hook you run with `asyncRewake`."
  - **VERIFIED-PROBE** (P3 debug log): `Hooks: Registering async hook async_hook_<pid>
    (UserPromptSubmit) with timeout 600000ms`.
  - **VERIFIED-BINARY**: the registry falls back to 15 000 ms only when no timeout is passed
    (`r.asyncTimeout||15000`, @205736049).
- **stdin close / exit in `-p` stream-json mode.**
  - **VERIFIED-DOCS**: "with the `-p` flag, Claude Code kills any async hook still running at
    teardown and finalizes it with outcome `cancelled`". CHANGELOG 2.1.23: "Fixed pending async hooks
    not being cancelled when headless streaming sessions ended".
  - **VERIFIED-BINARY**: the teardown `Lmo()` kills every registry hook still running and calls
    `Vke(r,1,"cancelled")` (@205740433, called from teardown `Qm()` @221156917). **But `asyncRewake`
    hooks are not in that registry.** `$Ie()` returns early for them and adds the promise to a
    separate set (`TLt()`). After the final result the print path runs `await iEr()`, which waits for
    that set with `Promise.race([allSettled, 30000ms])` (`Rxo=30000`, @207316224; call site
    @221100296).
  - **VERIFIED-PROBE P3**: stdin closed at ~4 s. The plain async hook got
    `hook_response{outcome:"cancelled",exit_code:1}` at 4.01 s. The process then stayed up until
    **34.09 s** (≈ 4 s + the 30 s cap) and exited. The `asyncRewake` hook (`sleep 50`) was killed
    with no `hook_response` on stdout, and no `sleep 50` process was left behind.
  - **VERIFIED-PROBE P5 leg B**: stdin closed at 3 s. The `asyncRewake` hook exited **2** at ~9 s,
    inside the 30 s wait, and the CLI exited at 10.1 s **without running the rewake turn and without
    writing that hook's `hook_response` to stdout**. The debug log does record `Hook
    UserPromptSubmit … error: status code 2 … rewake-findings`. **The CLI waits for the rewake but
    drops what it produces.** This is the mechanism behind #812's lost findings. Untether's 15 s
    close grace is also shorter than the CLI's 30 s wait, so the grace SIGINT fires first.
  - Observation (**VERIFIED-PROBE P2**, explanation **UNVERIFIED**): with only `SessionStart` async
    hooks and no user message, the process stayed up until both 45 s hooks finished (46.6 s). The
    plain async hook was *not* cancelled. `SessionStart` has a special in-band settle path
    (`Settling … pending async ${e} hook(s)` @205740323). Don't generalise from SessionStart.
- **Plain `async` responses are withheld while idle. VERIFIED-PROBE (CLI 2.1.285, 2026-09-30,
  stdin open 30 s, user's `moshi-hook` `async: true` on UserPromptSubmit + Stop).** The hook
  processes exit within a second, but their `hook_response` (`outcome:"success"`) lands only when
  stdin closes (30.01 s), or at the next turn. The `asyncRewake` Stop hook (security-guidance) and
  the sync hooks report at once (the rewake 0.28 s after the result, while idle). So an unpaired
  `hook_started` after the result isn't proof of a running hook. Untether's hold (#812) releases
  once the CLI has no `<shell> -c` child left (every command hook runs as `/bin/sh -c <command>`),
  logging `claude.hook.hold_released reason=no_hook_process`.

### A2. `--include-hook-events`

- **VERIFIED-BINARY** (@214409016): `.option("--include-hook-events","Include all hook lifecycle
  events in the output stream (only works with --output-format=stream-json)")`. `claude --help`
  lists it.
- **VERIFIED-DOCS** (cli-reference.md): `SessionStart` and `Setup` hook events are always included.
  `Notification`, `SessionEnd`, `PreCompact` and `PostCompact` never produce `hook_started`. For
  those, `hook_response` comes "only when a hook that runs in the background finishes".
- **Shapes. VERIFIED-BINARY** (emitters @205732232) **and VERIFIED-PROBE** (P3/P5):
  ```
  {"type":"system","subtype":"hook_started","hook_id":<uuid>,"hook_name":"UserPromptSubmit"|"SessionStart:startup",
   "hook_event":"UserPromptSubmit","uuid","session_id"}
  {"type":"system","subtype":"hook_progress","hook_id","hook_name","hook_event","stdout","stderr","output","uuid","session_id"}
  {"type":"system","subtype":"hook_response","hook_id","hook_name","hook_event","output","stdout","stderr",
   "exit_code"?:int,"outcome":"success"|"error"|"cancelled","uuid","session_id"}
  ```
  - `hook_id` pairs `hook_started` with `hook_response`.
  - `hook_progress` polls every `intervalMs ?? 1000` and fires only when output changed (`V8`).
  - `exit_code` is omitted when undefined.
  - Exit 2 is `outcome:"error"`.
  - **There is no `async`/`background`/`rewake` field on any of the three.** An async hook shows
    up only as a `hook_started` whose `hook_response` arrives after the turn's result (P5 leg A:
    started at 1.24 s, response at 9.26 s, turn result at 1.27 s).
- **Gating (VERIFIED-BINARY):** `q8(ev)` is true for `SessionStart`/`Setup`, otherwise only when
  the flag is set and the event is in the known hook-event list (@205732232, list @198119156).

### A3. How a rewake appears in the stream

**VERIFIED-PROBE P5 leg A** (stdin kept open, rewake exits 2 at ~9 s while idle):
```
9.26 system/hook_response {hook_id:<rewake>, outcome:"error", exit_code:2, stderr:"rewake-findings\n"}
9.26 system/hook_started … (the new turn's UserPromptSubmit hooks)
9.28 system/init
9.28 system/informational … "Original prompt: <task-notification>\n<summary>Stop hook feedback</summary>\n</task-notification>\n<system-reminder>\nStop hook blocking error from command \"UserPromptSubmit\": rewake-findings\n\n</system-reminder>"
9.28 result {subtype:"success", origin:{"kind":"task-notification","producer":"session-task"}, …}
```
- **No `command_lifecycle` frames** accompany the rewake turn, unlike user injections (F7 in
  `2026-09-27-claude-live-session-probes.md`).
- The only markers are the result's `origin.kind == "task-notification"` and the preceding
  `hook_response{exit_code:2}` for a hook whose `hook_started` preceded the last result.
- (In this probe the wake turn's prompt was itself blocked by the probe's blocking hook. That is a
  probe artefact that kept it zero-token.)
- **VERIFIED-BINARY** (`GRt` @205740746): the rewake enqueues
  `{mode:"task-notification", priority:"next", stopHookActive:true}` with
  `turnAttribution:"inherit"`, and the value is `<task-notification><summary>…`.
- The default prefix is `Stop hook blocking error from command "<hook_name>":` (@207317519).
- **VERIFIED-DOCS** (SDK TS, `SDKResultMessage.origin` / `SDKMessageOrigin`): injected follow-ups
  carry `origin: { kind: "task-notification" }`.

### A4. Agent SDK

- **VERIFIED-DOCS** (SDK TS Options table): `includeHookEvents: boolean` (default `false`) adds
  `SDKHookStartedMessage` / `SDKHookProgressMessage` / `SDKHookResponseMessage`. Their typed shapes
  match A2, and `outcome` is `"success" | "error" | "cancelled"`.
- **VERIFIED-BINARY** (bundled SDK spawn code @222109504):
  `if(this.options.includeHookEvents)y.push("--include-hook-events")`.
- **VERIFIED-DOCS** (claude-agent-sdk-python `types.py` @37422c2): `ClaudeAgentOptions.include_hook_events`,
  and `HookEventMessage(SystemMessage)` for `hook_started`/`hook_response`. `message_parser.py`
  routes these before the generic system case.
- Neither SDK exposes an "async" flag on these messages.

### A5. Asking "are async hooks pending?"

- **VERIFIED-BINARY**: the 64 control_request subtypes in the schema block around
  `subtype:R("stop_task")` (@≈199.9M) have **no** pending-hook query. The full list is:
  `rename_session set_color mcp_status file_suggestions get_context_usage get_session_cost
  list_models get_usage get_status export_conversation get_memory_dialog get_skills_dialog
  get_chrome_dialog get_chrome_browsers select_chrome_browser get_sandbox_dialog
  get_binary_version mcp_call rewind_files cancel_async_message read_file get_workspace_diff
  get_plan seed_read_state hook_callback register_device_hooks upload_device_hook_template
  remote_tools_announce remote_tool_call remote_plumbing_call remote_tools_probe
  remote_tools_reannounce turn_handoff upgrade_relay_marker mcp_message mcp_set_servers
  reload_plugins reload_skills reload_output_styles register_repo_root set_cwd claim_session
  mcp_reconnect mcp_toggle mcp_read_resource set_mcp_permission_mode_override
  set_chrome_browser_hints set_prompt_suggestions_paused stop_task background_tasks
  apply_flag_settings get_settings update_settings get_hooks_listing list_permission_rules
  elicitation request_user_dialog submit_feedback remote_control_work_secret
  oauth_token_refresh host_auth_token_refresh message_rated` (+ `success`/`error` responses).
  `interrupt`, `initialize`, `set_permission_mode` and `set_model` are defined elsewhere.
- `stop_task` takes a `task_id` (background tasks, not hooks). `background_tasks` backgrounds
  foreground Bash/agents.
- `get_hooks_listing` returns the configured hooks per event/matcher/source. Each hook carries
  `runsInBackground:true` when `type==="command" && (async || asyncRewake)` (@220755259). **It
  does not distinguish `async` from `asyncRewake`.**
- **Consequence:** the only runtime signal is the unpaired `hook_started` from A2.

---

## B. #814: safeguard stops

### B1. API semantics (VERIFIED-DOCS, platform.claude.com, retrieved 2026-09-29)

- A refusal is HTTP 200 with `stop_reason:"refusal"` and
  `stop_details:{type:"refusal",category,explanation}`.
- `category` ∈ `cyber | bio | frontier_llm | reasoning_extraction | general_harms`, or `null`.
- "A refusal can arrive before any output, or mid-stream after partial output … treat any partial
  output as incomplete and discard it."
- Pitfalls: "Re-sending a refused request to the same model usually earns another refusal. Point
  the retry at the fallback model." Also "Instrument refusals as their own signal. A refusal is an
  HTTP 200 …".
- Streaming: "`stop_details` arrives on the `message_delta` event alongside `stop_reason`"
  (handle-streaming-refusals).

### B2. Does stream-json carry `message.stop_reason:"refusal"`?

- **VERIFIED-BINARY**: an unrecovered refusal is an assistant message with `isApiErrorMessage:true`
  and `message.stop_reason==="refusal"`. The CLI reads `message.stop_details.category/explanation`
  from it (`Sn()` @212949256, `Oo()` @212270698).
- **VERIFIED-BINARY**: the headless emitter (`FHe` @212310186) yields main-thread assistant
  messages as
  `{type:"assistant",message:g.message,parent_tool_use_id:null,session_id,uuid,timestamp,error:g.error,request_id?}`,
  with `message` passed through verbatim.
- **VERIFIED-DOCS**: `SDKAssistantMessage.message` is a `BetaMessage` "with … `stop_reason`". The
  Python SDK parses `stop_reason=data["message"].get("stop_reason")` (`message_parser.py`).
  `SDKAssistantMessageError` has **no** refusal value, so don't expect `error:"refusal"`.
- #814's transcript excerpt shows `{"type":"assistant","message":{"stop_reason":"refusal",…}}`.
- **UNVERIFIED-PROBE**: stdout emission of that exact line was not probed live. Reproducing it
  needs a real classifier refusal, which costs tokens.

### B3. `system/informational`: stream or transcript-only?

- **Stream. VERIFIED-DOCS** (SDK TS `SDKInformationalMessage`): shape
  `{type:"system",subtype:"informational",content:string,level:"info"|"notice"|"suggestion"|"warning",tool_use_id?,prevent_continuation?,uuid,session_id}`.
  "Render `content` as plaintext at the given `level`."
- **VERIFIED-PROBE** (P4/P5): `{"type":"system","subtype":"informational","content":"UserPromptSubmit
  operation blocked by hook: …","level":"warning","prevent_continuation":true,"uuid":…}` arrived on
  stdout **before** the result line.
- **VERIFIED-BINARY** — the safeguard notice:
  `` FT(`${vi(u)}'s safeguards stopped the response above \xB7 continuing once with that noted`,"notice") ``
  (@212950584), where the constructor builds `{type:"system",subtype:"informational",content,level}`.
  - It fires on the same-model "refusal retry", which is on unless `CLAUDE_CODE_DISABLE_REFUSAL_RETRY`
    is set (@212949256).
  - Headless mode yields informational messages collected during a turn **after** that turn's
    generator (`mr()`: `yield*vo(…); for(fn of _t) if informational yield ms(fn)`, @221003330).
    Ordering relative to the turn's `result` line is **UNVERIFIED**. Parse it in both positions.
    In live sessions it may land between turns.
- Grep counts: `safeguards stopped` 2, `continuing once` 2, `"informational"` 39.

### B4. Guidance URLs for a hint

- **VERIFIED-DOCS** `https://code.claude.com/docs/en/model-config#automatic-model-fallback`:
  - Fable 5.x/Opus 5.5 cyber → Opus 4.8, bio → Opus 5. Sonnet 5.5 cyber → Sonnet 5. Opus 5 cyber →
    Opus 4.8.
  - "In non-interactive mode and SDK integrations that can't show the prompt, a flagged request ends
    the turn with a refusal".
  - `switchModelsOnFlag` (default `true`) controls auto-switching (settings-reference.md).
- **VERIFIED-DOCS** CVP: `https://support.claude.com/en/articles/14604842-real-time-cyber-safeguards-on-claude`
  (redirects to `…-on-claude-opus-and-sonnet`). The article's current note says it "doesn't apply
  to Claude Opus 5.5 or Sonnet 5.5" yet, with CVP expansion "soon".
- **VERIFIED-BINARY**: the CLI embeds that URL and `support.claude.com/en/articles/{16049681,15363606,8106465}`
  (per-model "why Claude switched models" articles) (@203874530).
- Also usable: `https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback`.

### B5. System subtypes the binary knows (VERIFIED-BINARY)

`LC_ALL=C grep -aoE 'type:"system",subtype:"[a-z_]+"' <bin> | sort | uniq -c` finds these:

- **Documented SDK stream subtypes (not yet modelled by Untether):**
  - `informational`, `status`, `notification`, `compact_boundary`
  - `hook_started` / `hook_progress` / `hook_response`
  - `permission_denied`, `session_state_changed`, `worker_shutting_down`, `plugin_install`
  - `commands_changed`, `thinking_tokens`, `memory_recall`, `elicitation_complete`, `mirror_error`
- **Refusal family (undocumented in the SDK reference; stream shapes from the emitters @220984028 / @213113537):**
  - `model_refusal_fallback`:
    `{trigger:"refusal",direction:"retry",scope?:"session"|"local",original_model,fallback_model,request_id,api_refusal_category,api_refusal_explanation,saw_cyber_refusal?,retracted_message_uuids?,refused_user_message_uuid,content,session_id,uuid}`
  - `model_refusal_no_fallback`: `{original_model,request_id,api_refusal_category,api_refusal_explanation,refused_user_message_uuid,content}`
  - `model_fallback`: `{trigger,original_model,fallback_model,content}`
- **Remote/CCR-only or internal-looking:**
  - `turn_starting` and `session_state_changed` via the CCR tee (@220434879)
  - `turn_preempted` (opt-in `rapidFollowupPreempt`)
  - `turn_duration`, `stop_hook_summary`, `away_summary`, `post_turn_summary`
  - `bridge_*`, `cloud_session_*`, `vcs_state_changed`, `file_snapshot`, `scheduled_task_fire`
  - `task_summary`, `api_error`, `local_command`, `dev_intent`, `memory_saved`, `code_change_published`
  - `feedback_draft_queued`, `peer_message_hold`, `permission_retry`, `per_turn_effort_changed`
  - `control_request_progress`, `tool_host_result`, `session_metadata`, `agents_killed`
  - `model_consent_fallback`
- Untether's `StreamSystemMessage` (`src/untether/schemas/claude.py:143`) is a single struct with
  `forbid_unknown_fields=False`. Unknown subtypes decode, and the new keys (`content`, `level`,
  `hook_id`, `outcome`, `exit_code`, …) don't collide with existing field types (checked by
  reading the struct). `StreamResultMessage` has **no** `stop_reason`, `terminal_reason` or
  `origin` fields yet.
  - Risk (**UNVERIFIED**): the API's server-side-fallback `{"type":"fallback"}` content block is
    not in Untether's `StreamContentBlock` tagged union. The CLI appears to strip it (`bze`
    @207197946), but that is unconfirmed.

---

## C. #806 / #813: related SDK facts

### C1. `TaskOutput` and successors

- **VERIFIED-DOCS** (SDK TS, "TaskOutput"): "Removed in Claude Code v2.1.277 … Claude reads a
  background task's output file with `Read` instead."
- **VERIFIED-DOCS** (CHANGELOG 2.1.277): "Removed the deprecated TaskOutput tool …". Earlier
  history: 2.1.83 deprecated it, and 2.0.64 unshipped `AgentOutputTool`/`BashOutputTool` in favour
  of `TaskOutputTool`.
- **VERIFIED-BINARY** (@206094493): the removed-tools set is
  `["Frame","FrameRead","TeamCreate","TeamDelete","SuggestBackgroundPR","AutofixPr","TaskOutput","AgentOutputTool","BashOutputTool","AgentOutput","BashOutput"]`.
  Permission rules naming them log "names a removed tool".
- `TaskStop` has `aliases:["KillShell","KillBash"]` (@212614467).
- The P5 `system/init` tool list has no `TaskOutput`.
- **Implication for #813:** on 2.1.284 a wake turn's single tool call that collects a finished
  task's result is almost certainly **`Read`** on `task_notification.output_file`, which is
  read-only. `TaskOutput` cannot occur. The result is also delivered unprompted through
  `task_notification` plus the wake turn (SDK TS `SDKTaskNotificationMessage`).

### C2. Interrupt vs closing stdin; the interrupted-turn result

- **VERIFIED-DOCS** (claude-agent-sdk-python `ResultMessage.terminal_reason` docstring): "A value
  of `"aborted_streaming"` or `"aborted_tools"` indicates the turn was cancelled (via
  `ClaudeSDKClient.interrupt` or an `interrupt` control request)."
- **VERIFIED-DOCS** (SDK TS): `terminal_reason` enum includes both. An `SDKAssistantMessage`
  truncated by an interrupt carries `aborted: true` and **no `stop_reason`**. The interrupt receipt
  `{still_queued, cancelled?}` arrives before the interrupted turn's result
  (`interrupt_receipt_v1`; `cancel_queued` needs `interrupt_cancel_queued_v1`).
- **VERIFIED-PROBE** (P4): `system/init.capabilities` on 2.1.284 =
  `["interrupt_receipt_v1","interrupt_cancel_queued_v1","msg_lifecycle_v1","mcp_read_resource_v1","mcp_tool_ui_meta_v1"]`.
- **VERIFIED-BINARY**, engine result builder (@213118560):
  - For an aborted terminal (`H0(bt)` = `aborted_streaming|aborted_tools`) the result is
    `subtype:"error_during_execution", is_error:true, errors:["[ede_diagnostic] result_type=… stop_reason=…"]`
    **only when** the last message is not assistant text/thinking and not a tool_result user
    message.
  - Otherwise it is `subtype:"success"` with `is_error` false unless an API error occurred.
  - In every case `terminal_reason` is set.
  - `kgn()` (@199569942) classifies `aborted_*` as **non-error** terminals.
  - The turn's input uuid gets `command_lifecycle{state:"cancelled"}` (`tvn`: aborted or error →
    `"cancelled"`, @213087463; also @212980600).
- **Classification rule for #806:** cancelled ⇔ `result.terminal_reason in
  {"aborted_streaming","aborted_tools"}`. Do not use `subtype` or `is_error`. `command_lifecycle`
  `cancelled` is **not** sufficient on its own, because error terminals such as `model_error`
  also produce it.
- Closing stdin is not an interrupt: an idle close ends the process with no result for any turn
  (F3/F4 in the 2026-09-27 finding).
- **UNVERIFIED-PROBE**: a live interrupt was not probed, because it needs an in-flight model turn.

---

## D. #808: config/test isolation (local code)

- `src/untether/settings.py:977` `_resolve_config_path(path)` resolves in this order: explicit
  `path` → `os.environ["UNTETHER_CONFIG_PATH"]` (read **at call time**) → `HOME_CONFIG_PATH`.
- `load_settings_if_exists()` (:943) calls `migrate_config_file(cfg_path)` **whenever the file
  exists**. That is the write path.
- `src/untether/config.py:16`: `HOME_CONFIG_PATH = Path.home() / ".untether" / "untether.toml"`
  is evaluated **at import**, so monkeypatching `HOME` after import does not move it.
  `config.load_or_init_config` (:60) also honours `UNTETHER_CONFIG_PATH`.
- Other `Path.home()` import-time constants a guard should know about:
  `telegram/commands/usage.py:24` `_DEFAULT_CREDENTIALS_PATH = ~/.claude/.credentials.json` and
  `:25` `_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"`.
- ⇒ an autouse fixture should `monkeypatch.setenv("UNTETHER_CONFIG_PATH", tmp)`. Patching
  `HOME` alone is insufficient.
- **VERIFIED-BINARY**: `/api/oauth/usage` appears 14 times (`` `${s.url}/api/oauth/usage ``, plus
  `?at_wall=` / `?cedar_ember=` variants). The endpoint is used by the CLI itself, undocumented.
- The CLI also has an experimental `get_usage` control_request ("structured /usage data … plan
  rate-limit utilization … response shape may change", @199817618).

---

## Implications for Untether (for the rc14 plan to cite)

1. **#812**:
   - Pass `--include-hook-events`.
   - Track `hook_id`s whose `hook_started` has no `hook_response` when the result arrives. While
     any are pending, the idle close must **keep stdin open**. Closing it loses rewake findings
     even though the CLI waits for them (A1, P5-B).
   - Bound the hold by the hook `timeout` (default 600 s) and the existing 1800 s hold / 4 h cap.
   - A rewake then arrives as an ordinary wake turn with `origin.kind=="task-notification"` and no
     `command_lifecycle` (A3). Attribute it as "hook feedback", not a background task.
   - Optional: call `get_hooks_listing` once to learn whether any background hooks are configured.
     It can't tell `async` from `asyncRewake`.
   - Plain `async` hooks are cancelled at close, but their output only mattered on the next turn.
   - Expect more stream volume, because every configured hook on every tool call emits
     started/response.
2. **#814**:
   - Detect `assistant.message.stop_reason == "refusal"` (with `stop_details.category`), the
     `system/informational` notice, and the `model_refusal_fallback` / `model_refusal_no_fallback`
     / `model_fallback` subtypes.
   - Surface all of them.
   - Link `model-config#automatic-model-fallback`, plus the CVP support article for `cyber`.
3. **#806**: finalise an in-flight follow-up turn as cancelled when `terminal_reason ∈
   {aborted_streaming, aborted_tools}` or when the bridge requested the cancel. Add
   `terminal_reason`, `stop_reason` and `origin` to `StreamResultMessage`.
4. **#813**: treat a single `Read` of a task's `output_file` in a wake turn as non-substantive.
   `TaskOutput` no longer exists on ≥2.1.277.
5. **#808**: pin `UNTETHER_CONFIG_PATH` in an autouse fixture. `HOME` patching doesn't reach
   import-time constants.

## Appendix: zero-token probes (CLI 2.1.284, lba-1, 2026-09-29)

These are scratch files that were not committed. Common flags: `claude -p --input-format stream-json
--output-format stream-json --verbose --setting-sources local --strict-mcp-config
--no-session-persistence --settings <file>`. P3–P5 also used `ANTHROPIC_BASE_URL=http://127.0.0.1:9
CLAUDE_CODE_MAX_RETRIES=0 … --model haiku`. The stdin user line was
`{"type":"user","message":{"role":"user","content":"x"},"uuid":"11111111-1111-4111-8111-111111111111"}`.

| Probe | Settings (`hooks`) | stdin | Result |
|---|---|---|---|
| P1 | SessionStart: sync `echo`, async `sleep 6`, asyncRewake `sleep 10; exit 0` | closed at 2 s, no user msg | exit at 11.95 s. All three had a `hook_response`. Async had `hook_progress` first |
| P2 | SessionStart: async `sleep 45`, asyncRewake `sleep 45; exit 2` | closed at 2 s, no user msg | exit at 46.6 s. Both `hook_response` (rewake `outcome:"error",exit_code:2`). No turn, no cancel (SessionStart special case) |
| P3 | UserPromptSubmit: blocker `exit 2`, async `sleep 50`, asyncRewake `sleep 50; exit 0`, plus `--include-hook-events --debug-file` | user msg, closed at ~4 s | async `hook_response{outcome:"cancelled",exit_code:1}` at 4.01 s. Exit at **34.09 s** (30 s rewake wait). Rewake killed with no `hook_response` |
| P4 | UserPromptSubmit blocker only, no hook-events flag | user msg, closed | Raw shapes: `command_lifecycle` queued/started/completed, `system/init.capabilities`, `system/informational{level:"warning",prevent_continuation:true}`, result `total_cost_usd:0` |
| P5-A | UserPromptSubmit: blocker, asyncRewake `sleep 8; exit 2`, plus `--include-hook-events` | user msg, **open 25 s** | Rewake at 9.26 s leads to a self-started turn (`system/init`, result `origin:{kind:"task-notification",producer:"session-task"}`), with no `command_lifecycle` |
| P5-B | same as P5-A | user msg, **closed at 3 s** | CLI waited, rewake exited 2 at ~9 s, CLI exited at 10.1 s. **No wake turn and no `hook_response` on stdout.** Debug log: `Hook UserPromptSubmit … error: status code 2` |

Binary string commands (examples):
```
B=$(readlink -f $(which claude))
LC_ALL=C grep -acF 'asyncRewake' $B          # 16
LC_ALL=C grep -acF 'include-hook-events' $B  # 3
LC_ALL=C grep -aoE 'type:"system",subtype:"[a-z_]+"' $B | sort | uniq -c
python3 ctx.py 'function $Ie({processId:e' 0 3500      # rewake path + iEr() 30 s wait
python3 ctx.py 'function K8(e,n,r){if(!q8(r))return;' 0 1800   # hook_* emitters
python3 ctx.py 'rr=bt==="budget_exhausted"?' 0 1800            # engine result builder
```
