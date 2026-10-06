# Step 4 — Cross-reference systemic patterns

This is the canonical catalogue of known Untether systemic patterns. Walk this
list **before** scoring any signal or escalating any "error" to a bug.

For each pattern:
- **Sig** = grep signature in journalctl / chat
- **Class** = primary Step-1 class
- **Canonical issue** = the open or closed GH issue (if any) tracking the pattern
- **Posture** = `bug` (fix needed), `by-design` (do NOT escalate), `regression-watch` (was fixed, watch for relapse), `rate-limited` (real but expected at low frequency), `mitigation-exists` (Untether already mitigates this engine quirk)

## The pattern catalogue

### 1. Auto-continue death spiral on signal exits
- **Sig**: rapid `auto_continue` followed by `proc_returncode=143` or `137`, repeating
- **Class**: auto-continue
- **Canonical issue**: mitigation via `_is_signal_death()` in `runner_bridge.py`; configurable `[auto_continue].max_retries`
- **Posture**: regression-watch — signal-death suppression should kick in. If you see >1 retry on signal death, that's a bug.

### 2. PTY master_fd leak across runs
- **Sig**: subsequent Claude runs fail to spawn; `subprocess.create.failed` with FD-exhaustion-like errors; `lsof` shows growing FD count
- **Class**: control-channel
- **Canonical issue**: per `control-channel.md` §PTY lifecycle — `finally` must close `master_fd`
- **Posture**: regression-watch — every Claude runner exit path must close `master_fd`.

### 3. callback_data > 64 bytes silently dropped
- **Sig**: button click does nothing; no `callback_query` in journalctl; outbound message had a button with long `callback_data`
- **Class**: telegram-transport
- **Posture**: bug if surfaced — Telegram enforces 64 bytes silently. Always check `callback_data` length at construction.

### 4. Hot-reload race during active run
- **Sig**: `config.reload` event during an active session; subsequent events use stale config
- **Class**: config-hot-reload
- **Canonical**: `telegram-transport.md` §TelegramBridgeConfig hot-reload; `update_from()` must copy all fields atomically
- **Posture**: regression-watch — new fields added to `TelegramBridgeConfig` must be added to `update_from()`.

### 5. Restart-required config key edited silently
- **Sig**: `restart_required=true` log line emitted but service was not restarted; user-visible behaviour is stale
- **Class**: config-hot-reload
- **Canonical**: `RESTART_REQUIRED_FIELDS` on `TelegramTransportSettings` (`settings.py`); `_notify_restart_required` (`telegram/loop.py`) broadcasts to project chats + admin DMs (#318 follow-up)
- **Posture**: regression-watch — broadcast must reach the user.

### 6. Agent self-restart during active run (`feedback_agent_self_restart_pattern`)
- **Sig**: agent runs `systemctl --user restart untether` inside an active session; 120s graceful drain timeout; outbox message dropped silently
- **Class**: config-hot-reload (root cause: agent confusion about hot-reload)
- **Canonical**: #547 (closed v0.35.3) + MEMORY.md `feedback_agent_self_restart_pattern`
- **Posture**: by-design behavior on the *kernel/systemd* side — fix is to educate the agent (preamble + this debug-rule), not patch the daemon. If observed: flag, never blame the daemon.

### 7. Cron/webhook run denied an approval (`permission.unattended_deny`)
- **Sig**: `permission.unattended_deny` / a `🔒 unattended (cron:<id>) · denied <Tool> ×N` row on a run with `trigger=cron:<id>` or `webhook:<id>`; a `plan` or prompting-mode cron ends with a plan or a report instead of acting
- **Class**: control-channel
- **Canonical**: #835 (0.35.5rc17, shipping in v0.36.0) — unattended runs deny at once instead of waiting on a button nobody can tap. Before #835 the same runs sat waiting (long `peak_idle` + repeat `stall_warning`, #526; MEMORY.md `feedback_cron_plan_mode_stalls`)
- **Posture**: **by-design** — the fix is config, not code: give the cron an explicit autonomous `permission_mode` (`plan-auto`, `auto`, `dontAsk`). A cron/webhook run that is still *waiting* on an approval is a `bug` (an #835 regression).

### 8. CLI-style Telegram summary brevity drift
- **Sig**: final Telegram message > 5000 chars; plan body re-pasted in the final summary
- **Class**: control-channel (Claude agent prompt)
- **Canonical**: feedback memory `feedback_telegram_summary_brevity`; rc11 overshot at 42k chars; rc13 #515 walked it back
- **Posture**: regression-watch — finals should be 500–1500 chars / 3–7 bullets.

### 9. Stale callback buttons (ephemeral cleanup miss)
- **Sig**: old buttons still respond after a run completed; `_EPHEMERAL_MSGS` registry (`runner_bridge.py`) not drained in `finally`
- **Class**: telegram-transport
- **Canonical**: `register_ephemeral_message` + `ProgressEdits.delete_ephemeral()`
- **Posture**: regression-watch — every run handler must drain ephemerals in `finally`.

### 10. Session registry cleanup miss (PTY/outline/ask)
- **Sig**: `_SESSION_STDIN`, `_REQUEST_TO_SESSION`, `_OUTLINE_PENDING`, `_DISCUSS_APPROVED`, `_PLAN_EXIT_APPROVED`, `_PENDING_ASK_REQUESTS`, `_ASK_QUESTION_FLOWS` retain entries for terminated sessions (`_DISCUSS_COOLDOWN` was retired in #570)
- **Class**: session-resume-lock
- **Canonical**: `control-channel.md` §Session registries — clean up in `finally` of `run_impl`
- **Posture**: regression-watch.

### 11. MCP catalog staleness on Claude runs (#365)
- **Sig**: `event=catalog_staleness.detected` followed by Claude using a stale MCP catalog (missing servers, wrong status)
- **Class**: runner-subprocess (claude only)
- **Canonical**: #365 — opt-in `notify_catalog_refresh` (default off); detection-only by default
- **Posture**: mitigation-exists — if user reports MCP issues, suggest enabling the proactive refresh.

### 12. Stall watchdog threshold mismatch (MCP vs local tool)
- **Sig**: stall fired at 10-min mark while an MCP tool was active; expected 15-min threshold
- **Class**: stall-liveness-watchdog
- **Canonical**: `[watchdog]` config — `tool_timeout` (10m default) vs `mcp_tool_timeout` / `subagent_timeout` (15m default); detection via tool_name + MCP server name in stall context. The chosen threshold is logged as `progress_edits.stall_threshold_selected reason=…`
- **Posture**: regression-watch — MCP-aware threshold detection must use the right context field. For a Codex/OpenCode stall that only warned at 15 min with `reason=active_children`, see §25.

### 13. Outbox deny-glob false positive
- **Sig**: `file_transfer.denied` for a legitimate file the user expected to receive
- **Class**: outbox-delivery
- **Canonical**: `[transports.telegram.files]` config — `deny_globs` list (shared by `/file` transfers and outbox delivery)
- **Posture**: bug surface for user-config tuning; check the deny pattern matched, then consider config narrowing.

### 14. Auto-error-watcher noisy signature
- **Sig**: same `auto:error-report` issue refiled multiple times, or a new signature creates duplicate issues across hosts
- **Class**: auto-error-watcher
- **Canonical**: `~/.local/state/untether-issue-watcher/seen.json` per-host dedup; cross-host dedup is by signature
- **Posture**: bug — signature should match across hosts. If two hosts file separate issues for the same signature, the dedup logic missed.

### 15. Trigger pause/resume gating not honoured (#294)
- **Sig**: cron fired while `TriggerManager.is_paused()` should have been true; or webhook returned 200 instead of 503 while paused
- **Class**: trigger-cron-webhook
- **Canonical**: #294 — master pause toggle; in-memory only, restart auto-resumes
- **Posture**: regression-watch.

### 16. ExitPlanMode re-issued immediately after a denial
- **Sig**: `ExitPlanMode` rapid-fire retries after a Telegram deny, with no intervening assistant text turn
- **Class**: control-channel
- **Canonical**: `control-channel.md` §Outline gate. The gate is now purely **text-based** — a re-issue is auto-denied until ≥200 chars of visible outline exist (`_OUTLINE_PENDING`). The 30/60/90/120s progressive cooldown (`_DISCUSS_COOLDOWN`) that used to back this was **retired in #570**, after the upstream loop was verified fixed on CLI 2.1.215 (2026-07-20).
- **Posture**: regression-watch — on the *upstream* loop, not on the retired cooldown. Repro: deny an ExitPlanMode control_request via the Telegram buttons and watch for an immediate re-issue. If it returns, reopen #570's lineage (#126) rather than reinstating a timer.

### 17. `_clear_background_handle` racing watchdog read (#374, #333, #507 redux)
- **Sig**: background-handle scalar wiped before watchdog reads it; "dead wakeup" symptom
- **Class**: stall-liveness-watchdog
- **Canonical**: MEMORY.md `project_channelo_rc15_dead_wakeup_507_redux`; fixed by #374 (handle cleared on terminal signal) and #573 (lifecycle v2), both closed in the 0.35.5rc line (ships as v0.36.0)
- **Posture**: regression-watch — was a known defect in the v0.35.3 line.

### 18. Integration-test attestation gate bypass
- **Sig**: `fleet-rollout.sh` proceeded without `~/.untether-dev/integration-test-pass-${VERSION}.json` existing; `--skip-test-gate` used silently
- **Class**: fleet-rollout
- **Canonical**: `release-discipline.md` §Pre-rollout integration test attestation
- **Posture**: bug — gate exists explicitly to prevent this.

### 19. Help-FAQ silently regressed
- **Sig**: `docs/faq/faq.md` has fewer than 7 question-shaped H2s, or contains TODO/placeholder, or breaks the marketing-site `docs-sync.config.ts` mapping
- **Class**: help-faq-release-guard
- **Canonical**: #477, #483 in `.claude/rules/help-faq.md`
- **Posture**: bug — FAQ MUST stay current; `help-faq-protect.sh` blocks deletes but does not enforce content shape.

### 20. CI ty diagnostics pile-up
- **Sig**: ty job in CI has hundreds of pre-existing diagnostics; `continue-on-error: true`
- **Class**: ci-pipeline-release-guard
- **Canonical**: MEMORY.md pattern note — non-blocking since rc9
- **Posture**: by-design — informational. Don't escalate. Tackling ty is a planned enhancement, not a bug.

### 21. Claude's own schedule refused (`loop.cli_job_denied`)
- **Sig**: `loop.cli_job_denied loop_mode=off`; Claude says it can't schedule a recurring/timed task and points at Loop mode or `/at`; a self-paced wake chain closes with reason `wake_cap`; `ℹ️ Claude's scheduling is off in this session`
- **Class**: control-channel
- **Canonical**: #925 / #926 (0.35.5rc20, shipping in v0.36.0, `### breaking`) — Untether owns `CronCreate`/`CronDelete` via `PreToolUse` hooks; `.claude/rules/control-channel.md` §Scheduling hooks
- **Posture**: **by-design** with Loop mode off or past `[loop] max_iterations`. `loop_mode=on` that still denies without registering a loop, or a CronCreate hook left unanswered (tool blocks ~30 s), is a `bug`. Kill switch: `[loop] own_schedule = false`.

### 22. Prompting-mode approval prompts (`acceptEdits` / `default` / `manual`)
- **Sig**: users report "it keeps asking now"; `control_request.received` + Telegram approvals for `Bash`/`Edit`/`Write` in a chat set to `/planmode off`
- **Class**: control-channel
- **Canonical**: #749 (`### breaking`) — the prompting modes now prompt; `--allowedTools` is no longer sent there
- **Posture**: **by-design**. Point at an autonomous mode (`plan-auto`, `auto`, `dontAsk`) or an explicit `[engines.claude] allowed_tools`.

### 23. Live-session final delivered twice at close (#928)
- **Sig**: the same answer posted twice for one turn, the second when the live session closes; a second `runner.completed` with `turn_cost_usd=0.0`; `final.early_delivery_timeout` (silent before rc2); slow `deleteMessage` / ReadTimeout retries on the replaced progress message just before
- **Class**: telegram-transport (final delivery)
- **Canonical**: #928 (0.36.0rc2) — a final counts as sent once handed to the transport (`delivery["sent"]` set before `send_result_message`, reset only if it raises); the replace-delete is queued with `wait=False`. `.claude/rules/runner-development.md` §Final delivery
- **Posture**: regression-watch. Shared root to look for: any "was it sent?" flag set only after the awaited send returns — the outbox still delivers an op whose waiter was cancelled. Known gaps (rc2): when the bound fires, the rest of `_on_run_completed` (outbox files, budget stop) is skipped, and a timeout mid split-final can leave later chunks unsent.

### 24. Non-Claude session cleared (or kept) after a failed resume (#952)
- **Sig**: `session.auto_cleared engine=codex|opencode|pi` after a bad model / missing key / rate limit, and the next message starts a new session; or the inverse — a resume that keeps failing with "session not found" while `session.auto_clear_skipped reason=not_resume_failure` repeats
- **Class**: session-resume-lock
- **Canonical**: #952 (0.36.0rc2) — the `num_turns == 0` gate (#45) is **Claude/AMP-only** (`_TURN_COUNT_ENGINES`); Codex/OpenCode/Pi never report turns and clear only when the error matches `_RESUME_FAILURE_RE`. A cleared session adds `ℹ️ The saved session couldn't be resumed, so it was cleared` to the error card. Pre-spawn blocks (`session.auto_clear_skipped reason=prespawn_blocked`, #838) never clear
- **Posture**: `auto_clear_skipped reason=not_resume_failure` on a non-resume error is **by-design**. A non-resume error that clears, or a genuine "not found" that never clears (the engine's wording missing from `_RESUME_FAILURE_RE` — check after a CLI upgrade), is a `bug`.

### 25. Permanent child processes forcing the 15-min stall threshold (#953)
- **Sig**: a Codex or OpenCode stall warns only after ~15 min with `stall_threshold_selected reason=active_children` / `⏳ Child processes idle (N children, …)`; or a `kill -STOP`-ed engine reported as child-idle
- **Class**: stall-liveness-watchdog
- **Canonical**: #953 (0.36.0rc2) — children earn the subagent threshold unconditionally only on Claude (`_CHILD_WORK_ENGINES`); elsewhere (Codex npm shim, OpenCode MCP servers) only while the process tree uses CPU. A stopped/D-state engine now reads `⏳ Engine process is stopped (state T, N min)` / `blocked on I/O`
- **Posture**: regression-watch. "Engine process is stopped" is by-design reporting — look for who stopped the process (a debugger, `kill -STOP`, a suspended terminal), not at the watchdog.

### 26. OpenCode run refused before spawning (`opencode.version.unsupported`)
- **Sig**: `🛑 OpenCode 2.x isn't supported yet, so this run wasn't started`; `opencode.version.unsupported version=2.…`; no engine process
- **Class**: runner-subprocess (opencode only)
- **Canonical**: #970 (0.36.0rc2) — only the OpenCode 1.x CLI (npm `opencode-ai`) is supported; 2.x (`@opencode/cli`) runs prompts on a shared background service outside Untether's process control. The chat's session is kept
- **Posture**: **by-design** — the fix is on the host (`npm uninstall -g @opencode/cli && npm install -g opencode-ai@1`), not code. `opencode.version.unknown` / `probe_failed` fail open and are noise unless runs then hang.

### 27. Stale repaint over a final ("working" card with a dead cancel button)
- **Sig**: after `/cancel` of a live follow-up or wake turn, the card still reads `working · Ns` with a cancel button that does nothing
- **Class**: telegram-transport (progress)
- **Canonical**: #948 (0.36.0rc2) — a debounced repaint could be enqueued after the `cancelled` edit and coalesce over it; `ProgressEdits.stop_repaints()` now orders every final edit after any in-flight repaint
- **Posture**: regression-watch — a new final/cancel path that sets `_finalizing` directly instead of awaiting `stop_repaints()` reintroduces it.

## How to use this list

1. In sweep mode (Step S-3 in `../debug.md`): for each error signature in the
   aggregated log buffer, walk the table top to bottom. Mark each finding with
   the matched pattern (if any) and its posture. Findings with posture
   `by-design` are dropped from the triage report; `rate-limited` are noted but
   not ranked highly; `regression-watch`, `mitigation-exists`, `bug` are all
   ranked.

2. In targeted mode (Step 4 in `../debug.md`): pull the issue body, walk the
   table, and pick the matching pattern. If a match exists with a canonical
   issue, comment on the canonical issue rather than creating a new one. If
   the pattern is `by-design`, say so clearly and stop — no fix.

## Maintenance

- New systemic pattern observed during debugging → add a row here.
- Pattern resolved by a release → keep the row, change posture to
  `regression-watch`, add the fix commit SHA.
- Pattern proves to be a one-off (not systemic) → remove it.

Cross-reference this file against:
- `MEMORY.md` (project memories with `project_*`, `feedback_*` prefixes)
- `CHANGELOG.md` (recent fixes — candidates for `regression-watch` posture)
- `.claude/rules/` (canonical rule files often encode the prevention)
