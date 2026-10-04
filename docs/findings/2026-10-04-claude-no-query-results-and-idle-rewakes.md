# Claude CLI: 0-turn "no-query" results and asyncRewake hooks fired inside subagents (rc20 #923 / #928)

Date: 2026-10-04 · CLI: Claude Code **2.1.289** (`~/.local/share/claude/versions/2.1.289`, lba-1) ·
Method: primary docs (fetched 2026-10-04), zero-token binary reads (`grep -aob` + `dd`), read-only fleet
journals (lba-1 staging, channelo, sl, nsd). No paid CLI runs.

Labels as in `2026-10-02-claude-wake-attribution-surface.md`: VERIFIED-DOCS / VERIFIED-BINARY / VERIFIED-LOG / UNVERIFIED.

## 1. Empty `num_turns: 0` results are a documented, native event (#928)

- **VERIFIED-DOCS** (https://code.claude.com/docs/en/agent-sdk/typescript, `SDKResultMessage` → `origin`):
  "When several background-task completions are queued together, Claude Code can answer them in one turn
  rather than one turn each. Each completion still produces its own result with this origin. All but the
  last of the completions Claude Code answers together produce **empty results with `num_turns: 0`**, in
  order, and the last one's result carries the turn that answers them all." Same page: such results carry
  `origin: { kind: "task-notification" }`, and "Check `kind` to distinguish results that answer your prompt
  from injected follow-ups before routing or suppressing them … don't suppress on `kind` alone."
- **VERIFIED-DOCS** (same page): `local_command` is set "on the success result of a turn that a command
  completed without entering the agent loop, such as `/compact`"; absent on every turn that entered the
  agent loop. `result_index` numbers every result the process writes.
- **VERIFIED-DOCS** (same page, `SDKUserMessage`): `shouldQuery: false` "append[s] the message to the
  transcript without triggering an assistant turn".
- **VERIFIED-BINARY** (2.1.289), three CLI paths that dispatch a queued item **without a model call** in
  print / stream-json mode, each still emitting a `result`:
  - task-notification coalescing (@≈226324996): a dequeued `task-notification` with a follower queued is
    rewritten `{shouldQuery:!1, queryHeldForNextTurn:!0}`; `noteTurnEnded` treats a result with
    `num_turns>0` as the answering turn and otherwise holds the chars when `result===""`; telemetry
    `print_task_notification_coalesce` / `owed_call_dispatched` (@≈226327208).
  - agent handback pointer (@≈211487181, `Ptt`, gate `tengu_hushed_kestrel` default true): when an
    agent's result was already handed back to the parent, its task-notification is enqueued
    `{shouldQuery:!1, handbackPointer:!0}` (telemetry `agent_handback_pointer_notice`).
  - generic `shouldQuery===false` route (@≈226353385): `flushBeforeResult().then(()=>emit(result))`.
  The result builder forwards `origin:A.origin` (@≈215927714). No result field marks "no query"
  (no `query_held` / `handback_pointer` key on the wire; `no_query_first` is an unrelated turn-handoff flag).
- **VERIFIED-LOG** (fleet, 48 h to 2026-10-04 08:10Z): every `claude.turn.completed num_turns=0` is
  `reason=unknown` (lba-1 5, channelo 8, sl 6, nsd 3 = **22/22**), each 0.2–9 s after a `task_finished`
  turn with `num_turns=1`. `runner.completed` logs them with `answer_len=0 duration_api_ms=0
  elapsed_s≈0.01 turn_cost_usd=0.0`. Real live turns report a cumulative `duration_api_ms` (e.g.
  385136), so `duration_api_ms == 0` separates them. rc17 dev-bot runs saw the same tail after **single**
  background-agent wakes (B-LIVE-2, R17-828, `docs/plans/v0.35.5-rc17/_LIVE-RESULTS-devbot.md:130`),
  which fits the handback-pointer path as well as coalescing.
- **VERIFIED-LOG — side effect:** an empty `unknown` turn arms `unattributed_turn_completed_at`, so a
  top-level task ending ≤ 30 s later is paired with the invisible turn (`claude.turn.task_end_paired`) and
  its real wake turn opens `push=False` / `already_announced`. 7 days: **12 of 17** `task_end_paired`
  lines fleet-wide paired with a 0-turn tail (e.g. channelo 2026-10-04T05:02:50Z → turn 6
  `live_turn.started push=False`, rescued only by `live_turn.push_promoted`).
- **UNVERIFIED (gated probe P1 in plan 06):** whether a `system/init` frame precedes the empty result, and
  that `origin.kind == "task-notification"` is present on the observed tails (Untether doesn't log
  `origin`). Docs say yes for the latter.

## 2. A subagent's asyncRewake hook wakes the parent while it is idle (#923)

- **VERIFIED-DOCS** (https://code.claude.com/docs/en/hooks, fetched 2026-10-04): "Hooks from settings
  files, managed policy settings, and plugins also run inside subagents. When a subagent calls a tool,
  tool events such as `PreToolUse` and `PostToolUse` fire the same configured hooks as in the main
  conversation" (input carries `agent_id`). Async limitations: "Exception: an `asyncRewake` hook that
  exits with code 2 wakes Claude immediately even when the session is idle."
- **VERIFIED-BINARY** (2.1.289 @≈209777731, `F0e`): on an asyncRewake exit 2 the CLI emits the
  `hook_response` frame (`Vc({hookId,hookName,hookEvent,…,exitCode,outcome})`), then enqueues
  `aDt({summary, body, priority:"next", stopHookActive:!0, turnAttribution:"inherit"})` →
  `{mode:"task-notification", agentId:Ve(), …}` (@≈208165496). The frame still carries no agent / async
  marker (unchanged since 2.1.285; see the 2026-10-02 note §1).
- **VERIFIED-BINARY**: the enabled `security-guidance` plugin
  (`~/.claude/plugins/cache/claude-plugins-official/security-guidance/2.0.9/hooks/hooks.json`) registers
  `PostToolUse` matcher `Bash` hooks with `"asyncRewake": true` and `"if": "Bash(git commit:*)"` (and
  `git push`, `gt create|modify|submit`), plus `Stop` and `SubagentStop` asyncRewake reviews. The project
  hook `PostToolUse:Bash` in outlook-assistant (`context-drift-check.sh`) never exits 2, so the exit-2
  `PostToolUse:Bash` responses are security-guidance commit reviews.
- **VERIFIED-LOG** (lba-1 staging, outlook-assistant, session 80188636, 2026-10-03): the parent closed
  turn 4 at 07:51:43Z while five background agents worked; a `PostToolUse` hook started at ≈07:53:45Z
  (`pending_hold hook_names=['PostToolUse']` 07:53:46Z, parent idle → an agent's `git commit`), answered
  exit 2 at 07:55:26.75Z (`blocking_exit held_s=101.0 turn_open=False started_turn=5`), and the parent
  opened turn 5 **0.58 s** later (`reason=unknown`, 7 model turns). 7-day fleet scan: six exit-2 responses
  followed by a parent turn within 5 s, all `PostToolUse:Bash`, held 52.9–112.9 s, gap 0.47–0.68 s;
  five started inside a parent turn (labelled `hook_rewake` by #828's outlived-turn rule), one started
  while idle (#923, labelled `unknown`). The only idle-started exit-2 responses elsewhere (channelo, 2×
  `PreToolUse:Bash`, held < 0.1 s — #828's population) were followed by no turn.
- **UNVERIFIED (gated probe P2 in plan 05):** a reproducible dev-bot trace of a subagent-fired asyncRewake
  waking the parent (the fleet evidence is two-for-two on timing, not a controlled run).
