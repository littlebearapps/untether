# Claude Code session crons: live firing, resume resurrection and host-side controls

**Date:** 2026-10-04 · **CLI:** Claude Code 2.1.289 (`~/.local/share/claude/versions/2.1.289`) · **Host:** lba-1 ·
**Method:** zero-token. Reads of the installed native binary (`strings` + targeted context dumps), the
public docs, the Python Agent SDK source, and the evidence the 2026-10-04 `/monitor untether-fleet` run
left on lba-1 (`untether-dev` journal and session transcript `8867caad`). No model turns were spent.

Grounds [#925](https://github.com/littlebearapps/untether/issues/925) and
[#926](https://github.com/littlebearapps/untether/issues/926) (rc20 plans `01-925-…`, `02-926-…`).
**Re-run the gated probes at the end before building on rows marked BINARY against a newer CLI.**

## Summary table

| # | Finding | Source |
|---|---|---|
| F1 | Print/stream-json mode has its own cron scheduler, created only when `isKairosCronEnabled()` (`!CLAUDE_CODE_DISABLE_CRON && flag tengu_kairos_cron`, default on). It runs both CronCreate jobs (`onFireTask`) and ScheduleWakeup wake-ups (`onFire(…,"schedule_wakeup")`). `isLoading: running \|\| inputClosed`, so a job fires only between turns and only while stdin is open. `isKilled` is re-checked on every 1 s tick. A fire enqueues `{mode:"prompt", isMeta:true, priority:"later", modelScheduledOrigin:true, wakeupSource}` | BINARY (`createCronScheduler({onFire,onFireTask,isLoading,getJitterConfig,isKilled})` in the headless setup) |
| F2 | Each fire is a `command_lifecycle` with a uuid Untether never wrote, so `_open_followup_turn` labels the turn `scheduled_wakeup` (`runners/claude.py:7071`). The transcript records `queue-operation enqueue "<prompt>"` and then `user {isMeta:true, promptSource:"system"}` | INCIDENT: journal 04:19–04:23Z, `claude.turn.started reason=scheduled_wakeup` ×5; transcript lines 60–92 |
| F3 | **`--resume` / `--continue` resurrects session-only CronCreate jobs.** On resume the CLI scans the transcript: CronCreate `tool_use` calls, plus the **non-error** `tool_result`s whose `toolUseResult` carries `{id, …}`. It skips `durable:true` jobs, ids in `deletedCronIds` (the `input.id` of **any** CronDelete `tool_use`; the result isn't checked), ids already loaded, recurring jobs older than `recurringMaxAgeMs` (default `604800000` = 7 d; remote config `tengu_kairos_cron_config` allows up to 30 d, and `0` means **no age limit**: `s.recurringMaxAgeMs!==0&&r-g.createdAt>=s.recurringMaxAgeMs`), and one-shots whose time has passed. Then it logs `resume: resurrected N session cron task(s)`. This only happens when `isKairosCronEnabled()` is true | BINARY (`function os({calls,results,deletedCronIds})` + the transcript scan `qi()`), DOCS |
| F4 | A resurrected recurring job has no `lastFiredAt`, so its next fire is computed from `createdAt`. That time is in the past, so the job **fires on the first idle tick after spawn**. This beats a stdin prompt written a second later | BINARY (`h1t(e.cron, e.lastFiredAt ?? e.createdAt, …)`); INCIDENT: transcript line 97, the tick was enqueued at 04:24:22 and Untether's prompt at 04:24:23 |
| F5 | F3 and F4 contradict #289 Probe 1 (CLI 2.1.129, 2026-05-06), which found "No scheduled jobs." after resume. Upstream changed the behaviour between 2.1.129 and 2.1.289, and the docs now state it | `docs/plans/2026-05-06-289-loop-and-cron-interception.md` §Probe 1 vs DOCS |
| F6 | `CLAUDE_CODE_DISABLE_CRON=1` (a boolean env var, read at spawn) turns off CronCreate/CronDelete/CronList (`isEnabled()`), the headless scheduler (so ScheduleWakeup **fires** stop too in that process) and resurrection (F3 returns early). The ScheduleWakeup tool has no `isEnabled` gate of its own, so it may stay callable but never fire (**UNVERIFIED**, G5). The docs say "The cron tools and `/loop` become unavailable, and any already-scheduled tasks stop firing" | BINARY, DOCS |
| F7 | CronCreate's and ScheduleWakeup's `checkPermissions` return `allow` (in `auto` mode, `passthrough` to the classifier). They therefore **never reach `can_use_tool`**, so Untether's existing approval path can't see or deny them | BINARY (CronCreate tool `create().checkPermissions`) |
| F8 | SDK hook callbacks. `initialize.hooks = {<Event>: [{matcher, hookCallbackIds:[…], timeout?}]}`. When a hook fires the CLI sends `control_request{subtype:"hook_callback", callback_id, input, tool_use_id}`. The response payload **is the hook output**. A PreToolUse deny is `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"…"}}`, and `{}` means no opinion. "If any hook returns deny, the operation is blocked regardless of other hooks"; deny beats defer, ask and allow | BINARY (initialize schema `hookCallbackIds`, `createHookCallback`), DOCS, Python SDK `_internal/query.py` |
| F9 | No control request lists or deletes session crons. `interrupt` cancels only dynamic `/loop` wake-ups (`kind:"loop"`, logged as "cancelled N pending loop wakeup(s) on user abort"), not CronCreate jobs. `stop_task` acts on background tasks, and `cancel_async_message` drops a queued message by uuid. `apply_flag_settings` exists, but nothing shows it affects crons at runtime (**UNVERIFIED**) | BINARY (control subtype schemas) |
| F10 | `durable:true` jobs persist to `<project>/.claude/scheduled_tasks.json`. They survive restarts, are excluded from F3, and fire in later sessions in that project | BINARY, DOCS |
| F11 | The Stop hook input carries `session_crons` ("Session-scoped cron tasks (CronCreate, ScheduleWakeup, /loop) that will wake this session later"). This is observability only and isn't needed for the fix | BINARY |

## Incident reconstruction (dev bot, chat -5284581592, 2026-10-04, rc19 code)

| UTC | Event |
|---|---|
| 04:18:36 | CronCreate `*/1` with prompt "[v935] reply with the word tick" → job `6a9af2cb`, "Session-only (not written to disk, dies when Claude exits)". `loop.scheduled` → `ut_loop_0fefb522` with caps 1 / 2 h / 2 d |
| 04:19:13–04:23:12 | The CLI fires 5× inside the live process (pid 839110). Untether logs `loop.fire_skipped_subprocess_alive` every 30 s |
| 04:24:01 | `/cancel` → `loop.cancelled reason=user_cancel`, the session id goes into `do_not_resume`, and the idle live session closes (rc=0) |
| 04:24:16 | The next prompt resumes `8867caad` (pid 883771). At 04:24:22 the CLI enqueues "tick" (resurrected job, F3/F4) **before** the user's prompt (04:24:23). The first result is "tick", and the real answer arrives as turn 2 (`reason=unknown`, "🔔 Claude continued") |
| 04:25:13, 04:26:13 | More "tick" fires from the resurrected `*/1` job |
| 04:27:00 | The new `*/3` loop `ut_loop_ed4c4201` expires at its first fire with `reason=do_not_resume` (the sentinel is session-wide) |

## Implications for Untether

1. Since live sessions (#776, rc11) keep the process open, the #289 premise ("the CLI's schedule dies with the
   subprocess") is false. Since F3, it is also false across processes. Untether's timer and the CLI's scheduler
   both run, the CLI copy is uncapped, and `/cancel` can't reach it.
2. The only deterministic host-side levers are: **deny the CronCreate before it runs** (SDK PreToolUse hook, F8);
   then the job never exists and resume can't resurrect it, because a denied call leaves an error `tool_result`.
   Or **spawn with `CLAUDE_CODE_DISABLE_CRON=1`** (F6), which is coarse: no cron tools, no wake-up fires and no
   resurrection in that process.
3. A transcript is cleaned for good only by a CronDelete `tool_use` for the id (F3). Only the model can produce one.

## Gated probes for the implementation session (haiku, a few cents each; not run during planning)

- **G1. Hook deny.** Initialize with `hooks.PreToolUse=[{matcher:"CronCreate",hookCallbackIds:["ut_loop_cron_create"],timeout:30}]`
  and ask for a `*/1` CronCreate. Answer the `hook_callback` with a deny. Check that the `tool_result` has
  `is_error:true` and contains the reason, that CronList says "No scheduled jobs.", and that there is no
  `scheduled_wakeup` turn in 130 s with stdin open. Then `--resume` with no hooks, wait 70 s: no fire.
- **G2. Hook frames.** With `--include-hook-events`, does a callback hook emit `hook_started`/`hook_response`
  (this affects #812 hook tracking)?
- **G3. `-p` resume of a session with a live CronCreate.** Does the resurrected job fire before the process exits?
- **G4. Ordering.** Can `hook_callback` arrive before the assistant line that carries the `tool_use` (streaming tool
  execution)? Untether registers idempotently either way, but record which order you see.
- **G5. `CLAUDE_CODE_DISABLE_CRON=1` on `--resume`.** Check the init `tools` list (are CronCreate and ScheduleWakeup
  absent?), whether a ScheduleWakeup call succeeds but never fires, and that there is no resurrection.

## Addendum: probe results (2026-10-04, CLI 2.1.289, haiku)

- **G1 PASS.** `initialize` with `hooks.PreToolUse=[{matcher:"CronCreate",hookCallbackIds:["ut_loop_cron_create"],timeout:30}]`
  returned `control_response` success. The model reached CronCreate through `ToolSearch select:CronCreate` (it is a
  deferred tool); the CLI then sent `control_request{subtype:"hook_callback", callback_id:"ut_loop_cron_create",
  input:{session_id, transcript_path, hook_event_name:"PreToolUse", tool_name:"CronCreate", tool_input:{cron, prompt}, …},
  tool_use_id}`. Answering `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny",
  "permissionDecisionReason":"<reason>"}}` gave a `tool_result` with `is_error:true` and content
  `PreToolUse:CronCreate hook error: <reason>` (note the prefix). CronList afterwards: "No scheduled jobs."; no fire in
  130 s idle with stdin open; `--resume` without hooks plus 70 s idle: no resurrection. Other tools behaved identically
  with the hook registered.
- **G2.** Callback hooks emit **no** `hook_started` / `hook_response` system frames (only settings-file hooks do), so
  they never enter the #812 `pending_hooks` tracker and can't arm #923's idle candidate.
- **G4.** The assistant line carrying the CronCreate `tool_use` arrived ~20 ms **before** the `hook_callback` in this run.
  Untether registers idempotently per `tool_use_id`, so either order is handled.
- **Fleet false-positive check for the native-fire detector (#925 §13b amendment 2): clean.** 7 days, 5 hosts: 8
  sessions logged `claude.turn.started reason=scheduled_wakeup`; every one had a ScheduleWakeup and/or CronCreate
  `tool_use` in its transcript (0/8 without a known cron), so the detector ships with suppression on.

## Sources (fetched 2026-10-04)

- https://code.claude.com/docs/en/scheduled-tasks: "When you resume with `--resume` or `--continue`, Claude Code restores tasks that haven't expired"; Disable scheduled tasks (`CLAUDE_CODE_DISABLE_CRON=1`); Limitations
- https://code.claude.com/docs/en/agent-sdk/hooks: PreToolUse `permissionDecision` deny, precedence, matchers
- https://raw.githubusercontent.com/anthropics/claude-agent-sdk-python/main/src/claude_agent_sdk/_internal/query.py: initialize `hooks` config (`matcher`, `hookCallbackIds`, `timeout`) and the `hook_callback` response (`response` = hook output)
- Installed CLI 2.1.289 binary: the strings and functions quoted above
