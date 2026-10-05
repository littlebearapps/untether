# Claude CLI surface for wake-turn attribution, hook rewake signals and background cost (rc17 #828 / #825 / #821)

Date: 2026-10-02 · CLI: Claude Code **2.1.287** (`~/.local/share/claude/versions/2.1.287`, lba-1) ·
Method: zero-token binary reads (`grep -aob` + `dd`), primary docs, read-only fleet logs. No paid CLI runs.

Labels as in `2026-09-29-claude-rc14-cli-surface.md`: VERIFIED-BINARY / VERIFIED-DOCS / VERIFIED-LOG / UNVERIFIED.

## 1. Hook frames still carry no async / subagent marker (#828)

- **VERIFIED-BINARY** (2.1.287 @≈206756429): the emitters are
  `mQ(e,n,r)` → `{type:"system",subtype:"hook_started",hook_id:e,hook_name:n,hook_event:r}` and
  `vc(e)` → `{…subtype:"hook_response",hook_id,hook_name,hook_event,output,stdout,stderr,exit_code?,outcome}`.
  No `agent_id`, no `async`/`asyncRewake`, no `parent_tool_use_id`. A background subagent's
  `PreToolUse` hook and the main thread's are indistinguishable on the frame (same as 2.1.285, §A2).
- **VERIFIED-DOCS** (https://code.claude.com/docs/en/hooks, fetched 2026-10-02):
  - exit 2 per event: `PreToolUse` "Blocks the tool call"; `Stop` "Prevents Claude from stopping";
    `UserPromptSubmit` "Blocks the prompt"; `PostToolUse` "Shows stderr to Claude; the tool already ran";
    `PermissionRequest` "Exit code 2 isn't honored".
  - `asyncRewake`: "runs in the background and wakes Claude on exit code 2"; `async`/`asyncRewake`
    "apply only to command hooks"; async hooks "can't block or control Claude's behavior". **Any
    command hook event can be async/asyncRewake** — the docs' canonical async example is a
    `PostToolUse` `Write|Edit` test runner, so excluding tool-scoped events by name would drop real
    rewakes.
  - Hook **input** (stdin to the hook) has `agent_id` / `agent_type` "Present only when the hook fires
    inside a subagent call" — but this never reaches the stream-json frame.
- **VERIFIED-LOG** (channelo, rc14, issue #828): five `claude.hook.rewake_signal` lines, all
  `hook_event=PreToolUse hook_name=PreToolUse:Bash held_s=0.0–0.1 turn_open=False` during background
  builder agents; zero real asyncRewake hooks.
- **Structural discriminator available today:** `PendingHook.turn` already records whether a hook
  started inside a turn (`state.turn`) or while idle (`state.turn + 1`, `claude.py:3329`). A rewake
  hook must have started in a turn that has since closed; a background subagent's sync hook (or the
  next turn's `UserPromptSubmit`) starts and ends while idle.
- `get_hooks_listing` control request (`runsInBackground`) still can't tell `async` from
  `asyncRewake` (§A5) — not a usable discriminator.

## 2. Foreground → background transitions are reported, Untether ignores them (#825 comment variant)

- **VERIFIED-BINARY** (2.1.287 @≈204107300): the `task_updated` patch differ emits
  `is_backgrounded` whenever it changes:
  `let i="isBackgrounded"in e?…,u="isBackgrounded"in n?…;if(u!==i&&u!==void 0)r.is_backgrounded=u`.
- **VERIFIED-BINARY**: the CLI moves running foreground work to the background on its own:
  `"… was moved to the background as task <id> so that a message that arrived while it was running can
  reach you; it was not interrupted and keeps running"` and `"… is still running after N s. It was
  moved to the background as task <id> and keeps running; you'll receive a notification with the
  result when it completes."` Foreground agents carry an `autoBackgroundMs`. Env
  `CLAUDE_AUTO_BACKGROUND_TASKS` exists (semantics UNVERIFIED).
- **VERIFIED-BINARY** (bundled consumer @≈210882022): the CLI's own stream consumer treats
  `task_updated.patch.is_backgrounded === true` and membership of a `background_tasks_changed`
  snapshot as the same "backgrounded" set.
- **VERIFIED-DOCS** (https://code.claude.com/docs/en/agent-sdk/typescript, fetched 2026-10-02):
  `SDKTaskUpdatedMessage.patch` includes `is_backgrounded`.
- **Untether at HEAD e83f372:** `_apply_task_event` reads only `patch.status`
  (`claude.py:5481-5488`); the snapshot branch never promotes a known foreground task
  (`:5394-5430`).
- **VERIFIED-LOG** (nsd, 0.35.5rc16, eliixa-web `-5157380693`, 2026-10-01):
  `09:44:09.720Z claude.turn.notification_ignored task_id=biu1nu74t is_backgrounded=False owned_by_subagent=False`
  then `09:44:09.994Z claude.turn.started reason=unknown turn=3` → `🔔 Claude continued`. The values
  come from the **known** task (`claude.py:6633-6641`; an unknown id would log `None` — the
  notification frame has no such fields, §3), i.e. a parent-owned foreground Bash that the CLI later
  backgrounded ("the copy attempt that timed out").
- `system/turn_starting {mode, task_id}` exists (@≈224223107) but is emitted only by the remote-io
  (CCR) transport's stdout tee — **not** in local `-p --output-format stream-json`. Not usable.

## 3. `task_notification` / `task_progress` carry tokens, not cost (#821)

- **VERIFIED-BINARY** (@≈201263696) `task_notification` emitter:
  `{task_id, tool_use_id, status, reason?, output_file, summary, usage, resource_links?, skip_transcript?, ambient?}`
  — no `is_backgrounded` / `owned_by_subagent`.
- **VERIFIED-DOCS** (SDK TS): `usage?: {total_tokens, tool_uses, duration_ms}` on
  `task_notification` (same shape on `task_progress`, `2026-09-27-claude-live-session-probes.md`). The
  hooks reference describes the Agent tool's `totalTokens` as "Token count from the subagent's final
  API request … This isn't a total across the whole run", so `total_tokens` is **not** a spend figure.
- **VERIFIED-DOCS** (https://code.claude.com/docs/en/agent-sdk/cost-tracking, fetched 2026-10-02):
  `total_cost_usd` "Counts subagent requests alongside the top-level loop"; `modelUsage` likewise,
  "broken down by model" (`ModelUsage.costUSD`). No per-subagent / per-task breakdown.
- Candidates for true attribution (v0.35.6, now v0.36.2; all UNVERIFIED for this purpose): `modelUsage` per-model
  `costUSD` deltas (only separates agents on a different model); the `get_session_cost` control
  request (§A5 list; response shape unknown); token × price estimates.
